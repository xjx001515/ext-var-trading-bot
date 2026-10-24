"""Pure execution evidence rules. Unknown reads never mean an empty account."""
import math
from .position_truth import normalize_position_read

FOUND = 'FOUND'
CONFIRMED_EMPTY = 'CONFIRMED_EMPTY'
UNAVAILABLE = 'UNAVAILABLE'


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def quantity(row):
    if not isinstance(row, dict):
        return None
    for key in ('qty_abs', 'qty', 'quantity', 'size', 'position_qty', 'size_qty'):
        if key in row and row[key] is not None:
            value = _number(row[key])
            if value is not None:
                return abs(value)
    return None


def position_read(value):
    """Accept the position_truth {state,row} protocol and legacy positive rows."""
    read = normalize_position_read(value)
    state, row = read['state'], read.get('row')
    if state == CONFIRMED_EMPTY:
        return state, {'side': 'NONE', 'qty_abs': 0.0, 'notional_u': 0.0}
    if isinstance(row, dict) and row.get('available') is False:
        return UNAVAILABLE, None
    return state, row


def verify_position_change(*, kind, side, usd, pre, post, tolerance_usd=10.0):
    pre_state, pre_row = position_read(pre)
    post_state, post_row = position_read(post)
    ev = {'pre_state': pre_state, 'post_state': post_state,
          'confirm_basis': 'position_quantity_delta', 'effective': False}
    def pending(reason):
        ev['reason'] = reason
        return False, ev
    if UNAVAILABLE in (pre_state, post_state):
        return pending('position_truth_unavailable')
    pre_q, post_q = quantity(pre_row), quantity(post_row)
    ev.update(pre_qty=pre_q, post_qty=post_q)
    if pre_q is None or post_q is None:
        return pending('position_quantity_unavailable')
    if (pre_state == FOUND and pre_q <= 0) or (post_state == FOUND and post_q <= 0):
        return pending('position_quantity_unavailable')
    action = str(kind or '').lower()
    side = str(side or '').upper()
    before_side = str(pre_row.get('side') or '').upper()
    after_side = str(post_row.get('side') or '').upper()
    if action.startswith('close_all'):
        ok = pre_state == FOUND and pre_q > 0 and post_state == CONFIRMED_EMPTY
        ev.update(effective=ok, reason='confirmed_empty_after_close' if ok else 'close_not_confirmed_empty')
        return ok, ev
    opening = action.startswith('open')
    if opening:
        if after_side != side or (pre_state == FOUND and before_side != side):
            return pending('position_side_mismatch')
        delta = post_q - pre_q
        price_row = post_row
    else:
        # side means HELD position side here (the UI callers already translate
        # reduce-only buy/sell into pre_row.side). Never ignore a mismatch.
        if before_side != side or (post_state == FOUND and after_side != side):
            return pending('position_side_mismatch')
        delta = pre_q - post_q
        price_row = pre_row
    ev['actual_delta_qty'] = delta
    if delta <= max(1e-12, max(pre_q, post_q) * 1e-10):
        return pending('position_quantity_no_effect')
    requested = abs(_number(usd) or 0.0)
    # Prices only value an independently observed quantity change; a price move
    # without a quantity change can never pass this gate.
    price = next((_number(price_row.get(k)) for k in ('mark_px', 'mark_price', 'price', 'current_price')
                  if (_number(price_row.get(k)) or 0) > 0), None)
    if price is None:
        q = quantity(price_row)
        n = _number(price_row.get('notional_u'))
        price = n / q if q and n and n > 0 else None
    if not opening and post_state == CONFIRMED_EMPTY:
        ev.update(effective=True, reason='confirmed_empty_after_close')
        return True, ev
    if requested <= 0:
        return pending('request_amount_unavailable')
    if requested > 0:
        if price is None:
            return pending('quantity_seen_request_coverage_unknown')
        executed = delta * price
        required = max(5.0, requested - max(3.0, _number(tolerance_usd) or 10.0))
        ev.update(quantity_delta_value_u=executed, required_delta_u=required)
        if executed + 1e-9 < required:
            return pending('partial_quantity_change_pending')
    ev.update(effective=True, reason='open_quantity_delta_ok' if opening else 'close_quantity_delta_ok')
    return True, ev


def has_fill_evidence(result):
    if not isinstance(result, dict):
        return False
    # A price, order id or requested qty alone is never execution evidence.
    if any((_number(result.get(k)) or 0) > 0 for k in
           ('filled_qty', 'fill_qty', 'filled_notional_u', 'fill_notional_u', 'filled_notional')):
        return True
    return result.get('fill_confirmed') is True and str(result.get('fill_reconcile_state') or '').lower() == 'filled'


def confirmation_state(result):
    r = result if isinstance(result, dict) else {}
    submitted = bool(r.get('submit_ok') or r.get('ok') or r.get('pending') or r.get('pending_confirm'))
    if r.get('confirmed_fail') is True or r.get('definitive_reject') is True:
        return 'confirmed_fail'
    # Fresh postcheck evidence always outranks old nested evidence and flags.
    if r.get('postcheck_ok') is False:
        if has_fill_evidence(r):
            return 'fill_seen_postcheck_fail_recovering'
        return 'submit_seen_postcheck_fail_recovering' if submitted else 'confirmed_fail'
    if r.get('postcheck_ok') is True:
        return 'confirmed_success'
    req = r.get('req') if isinstance(r.get('req'), dict) else {}
    meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}
    if meta.get('_submit_verified_ok') is True:
        return 'confirmed_success'
    if r.get('pending_timed_out'):
        # Uncertain submission remains blocked; timeout is not permission to retry.
        return 'submitted_pending_confirm'
    if has_fill_evidence(r):
        return 'fill_seen_pending_confirm'
    if submitted:
        return 'submitted_pending_confirm'
    return 'submit_fail' if r.get('ok') is False else 'unknown'


def apply_submission_evidence(result, attempts=()):
    """Only this request's click attempts may promote a failed UI return to pending.

    A same-symbol journal entry from a different request is deliberately not an
    input. A timeout *inside* click is ambiguous, unlike a pre-click rejection.
    """
    r = dict(result or {})
    rows = [a for a in attempts if isinstance(a, dict)]
    dispatched = [a for a in rows if a.get('state') in ('CLICK_STARTED', 'CLICKED', 'CLICK_UNKNOWN')]
    no_submit_codes = ('submit_click_fail', 'submit_btn_disabled', 'submit_btn_not_found',
        'submit_btn_not_visible', 'submit_btn_wait_enabled_fail', 'submit_btn_still_disabled',
        'blocked_existing_unconfirmed_no_submit', 'submission_journal_unavailable_no_submit', 'insufficient_available_margin')
    if rows and not dispatched or (not rows and str(r.get('reason') or '') in no_submit_codes):
        reason = str((rows[-1].get('reason') if rows else '') or r.get('reason') or 'no_submit')
        r.update(ok=False, submit_ok=False, submit_attempted=False, no_submit=True,
                 pending=False, pending_confirm=False, confirmed_fail=True, postcheck_ok=False,
                 confirmed_success=False, trade_effective=False, reason=reason,
                 submission_state='NOT_SUBMITTED')
        if reason in ('blocked_existing_unconfirmed_no_submit', 'submission_journal_unavailable_no_submit'):
            r['skipped'] = True
            if rows:
                r['submission_blocker'] = rows[-1].get('blocker') or {}
                r['message'] = rows[-1].get('explanation') or reason
    elif dispatched:
        state = 'CLICK_UNKNOWN' if any(a.get('state') in ('CLICK_STARTED', 'CLICK_UNKNOWN') for a in dispatched) else 'CLICKED'
        r.update(submit_attempted=True, submission_state=state, no_submit=False)
        if r.get('postcheck_ok') is not True and not r.get('confirmed_fail'):
            r.update(ok=False, submit_ok=state == 'CLICKED', pending=True, pending_confirm=True,
                     confirmed_success=False, trade_effective=False,
                     reason='submit_click_outcome_unknown' if state == 'CLICK_UNKNOWN' else 'submitted_pending_confirm')
    return r


def apply_confirmation_fields(result):
    r = dict(result or {})
    state = confirmation_state(r)
    pending = state in ('submitted_pending_confirm', 'fill_seen_pending_confirm',
                        'fill_seen_postcheck_fail_recovering', 'submit_seen_postcheck_fail_recovering')
    r.update(confirmation_state=state, pending=pending, pending_confirm=pending,
             confirmed_success=state == 'confirmed_success', confirmed_fail=state == 'confirmed_fail',
             submit_failed=state == 'submit_fail', trade_effective=state == 'confirmed_success',
             pending_timeout_fail=False,
             pending_timed_out=bool(r.get('pending_timed_out')) if pending else False,
             status_label='confirmed' if state == 'confirmed_success' else ('pending_confirm' if pending else state))
    evidence = dict(r.get('execution_evidence') or {})
    evidence.update(final_state=state, final_confirmation_code=state,
                    postcheck_ok=r.get('postcheck_ok'), effective=state == 'confirmed_success',
                    trade_effective_count=int(state == 'confirmed_success'))
    r['execution_evidence'] = evidence
    if pending:
        if r.get('reason') != 'submitted_pending_confirm':
            r.setdefault('raw_execution_reason', r.get('reason'))
        r['reason'] = 'submit_click_outcome_unknown' if r.get('submission_state') == 'CLICK_UNKNOWN' else 'submitted_pending_confirm'
    elif not r.get('no_submit') and ('pending' in str(r.get('reason') or '').lower() or 'unconfirmed' in str(r.get('reason') or '').lower()):
        r['reason'] = str(r.get('postcheck_reason') or state)
    return r


def scoped_position(state, platform):
    prefix = str(platform).lower() + '_'
    state = state if isinstance(state, dict) else {}
    reason = str(state.get(prefix + 'reason') or '').lower()
    explicit_empty = state.get(prefix + 'position_state') == CONFIRMED_EMPTY
    if state.get(prefix + 'recovered_from_last_good') or any(k in reason for k in ('row_not_found', 'timeout', 'unavailable', 'last_good', 'partial')) or ('absent' in reason and not explicit_empty):
        return {'position_state': UNAVAILABLE}
    return {'position_state': state.get(prefix + 'position_state', ''),
            'side': state.get(prefix + 'side'), 'qty_abs': state.get(prefix + 'qty'),
            'notional_u': state.get(prefix + 'notional_u')}


def verify_scoped_change(*, action, platform, side, usd, pre, post, tolerance_usd=10.0):
    action = str(action or '').lower()
    platform = str(platform or '').upper()
    if action in ('close_pair', 'force_close_all'):
        legs = [verify_position_change(kind='close_all', side='', usd=0,
                    pre=scoped_position(pre, p), post=scoped_position(post, p)) for p in ('EXT', 'VAR')]
        # An already explicitly empty leg needs no trade but is safe for pair flatten.
        ok = all(ok or (ev['pre_state'] == CONFIRMED_EMPTY and ev['post_state'] == CONFIRMED_EMPTY) for ok, ev in legs)
        return ok, {'reason': 'pair_confirmed_empty' if ok else 'pair_truth_pending', 'legs': [v for _, v in legs]}
    if platform not in ('EXT', 'VAR'):
        return False, {'reason': 'unsupported_scope_pending'}
    opening = action in ('open', 'open_leg', 'open_more', 'add', 'increase', 'add_leg', 'reopen_after_tp')
    full_close = action in ('close_all', 'close', 'close_leg', 'close_one_leg', 'close_full', 'tp_full')
    reducing = action in ('reduce', 'decrease', 'trim', 'reduce_pair', 'reduce_loss_leg')
    if not (opening or full_close or reducing):
        return False, {'reason': 'unsupported_action_pending'}
    before, after = scoped_position(pre, platform), scoped_position(post, platform)
    direction = str(side or (after if opening else before).get('side') or '').upper()
    direction = {'BUY': 'LONG', 'SELL': 'SHORT'}.get(direction, direction)
    return verify_position_change(kind='open' if opening else ('close_all' if full_close else 'close'),
                side=direction, usd=usd, pre=before, post=after, tolerance_usd=tolerance_usd)


def pending_blocks(record):
    """Time, attempts and raw return booleans never resolve a submission."""
    if not isinstance(record, dict) or not record:
        return False
    return str(record.get('state') or '').upper() not in (
        'CONFIRMED_SUCCESS', 'CONFIRMED_REJECTED', 'CONFIRMED_CANCELLED', 'CONFIRMED_NOT_FILLED', 'RESOLVED_FLAT')


def verify_pending_settlement(record, post):
    if not isinstance(record, dict):
        return False, {'reason': 'pending_record_missing'}
    return verify_position_change(kind=str(record.get('kind') or 'close'),
        side=str(record.get('side') or ''), usd=record.get('intent_usd', 0),
        pre=record.get('pre_position'), post=post,
        tolerance_usd=record.get('tolerance_usd', 10.0))
