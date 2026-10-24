"""Preview legacy pending settlements from explicit position evidence.

Pure functions only. This module does not write a live journal, create fills,
change grid layers, move an anchor, or submit orders.
"""
from copy import deepcopy
import hashlib
import json

from .execution_confirmation import position_read, quantity, verify_pending_settlement, has_fill_evidence
from .position_truth import finite_number
from .symbols import same_symbol


TERMINAL = {'CONFIRMED_SUCCESS', 'CONFIRMED_NOT_FILLED', 'RESOLVED_FLAT',
            'CONFIRMED_REJECTED', 'CONFIRMED_CANCELLED'}
MIN_POSITION_RATIO = .5
MAX_SCAN_AGE = 90.0
EMPTY_COUNT = 3
EMPTY_INTERVAL = 30.0
MIN_ORDER_AGE = 120.0


def fingerprint(record):
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def position_for(snapshot, platform, coin):
    """Absent from a filtered export is UNAVAILABLE, never inferred empty."""
    for bucket in ('raw_positions', 'positions'):
        positions = snapshot.get(bucket)
        rows = positions.get(platform) if isinstance(positions, dict) else positions
        if isinstance(rows, dict):
            for name, row in rows.items():
                if same_symbol(coin, name) and isinstance(row, dict):
                    return row
        elif isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and str(row.get('platform') or '').upper() == platform and same_symbol(coin, row.get('coin')):
                    return row
    for row in snapshot.get(platform, []) if isinstance(snapshot.get(platform), list) else []:
        if isinstance(row, dict) and same_symbol(coin, row.get('coin')):
            return row
    raw = snapshot.get('raw') or {}
    details = (raw.get('platform_empty_detail_map') or {}).get(platform) or {}
    if details.get('confirmed_empty') is True:
        return {'position_state': 'CONFIRMED_EMPTY', 'reason': 'explicit_platform_empty'}
    return {'position_state': 'UNAVAILABLE', 'reason': 'symbol_not_covered_by_snapshot'}


def _fresh(snapshot, record, row, now):
    ts = finite_number(snapshot.get('ts'))
    submitted = finite_number(record.get('ts'))
    meta = snapshot.get('meta') or {}
    if (snapshot.get('data_quality') != 'ok' or snapshot.get('dq_fallback') or snapshot.get('stale')
            or snapshot.get('recovered_from_last_good') or meta.get('cache_hit')
            or str(snapshot.get('scan_scope') or meta.get('scan_scope') or meta.get('scope') or '').lower() in ('local', 'partial')
            or ts is None or submitted is None or not submitted < ts <= now or now - ts > MAX_SCAN_AGE):
        return False
    row_ts = finite_number(row.get('ts'))
    return not row.get('stale') and not row.get('recovered_from_last_good') and (
        row_ts is None or submitted < row_ts <= now and now - row_ts <= MAX_SCAN_AGE)


def confirmed_platform_closes(records):
    result = {}
    for record in records.values():
        if (record.get('state') == 'CONFIRMED_SUCCESS' and record.get('coin') == 'ALL'
                and str(record.get('kind') or '').startswith('close')):
            ts = finite_number(record.get('resolved_ts'))
            if ts is not None:
                p = str(record.get('platform') or '')
                result[p] = max(result.get(p, 0), ts)
    return result


def reconcile(record, snapshot, *, now, scan_id, platform_closes=None):
    r = deepcopy(record)
    p, c = str(r.get('platform') or '').upper(), str(r.get('coin') or '').upper()
    row = position_for(snapshot, p, c)
    state, normalized = position_read(row)
    normalized = normalized or {}
    intent = finite_number(r.get('intent_usd'))
    current_u = finite_number(normalized.get('notional_u'))
    q = quantity(normalized)
    basis = 'snapshot_notional_u'
    if current_u is None and q is not None:
        entry = finite_number(normalized.get('entry_px'))
        if entry is not None and entry > 0:
            current_u, basis = q * entry, 'quantity_x_entry_price'
    detail = {'original_state': r.get('state'), 'platform': p, 'coin': c,
              'side': r.get('side'), 'intent_usd': intent, 'current_side': normalized.get('side'),
              'current_notional_u': current_u, 'current_qty': q, 'valuation_basis': basis,
              'snapshot_ts': snapshot.get('ts'), 'record_fingerprint': fingerprint(record),
              'settlement_only': True, 'creates_fill_or_profit_or_grid_layer': False}
    def finish(reason, target=None, **evidence):
        detail.update(reason=reason, proposed_state=target or r.get('state'), **evidence)
        if target is not None:
            r.update(state=target, resolved_ts=now, evidence={'reason':reason,
                'basis':'legacy_position_reconciliation', 'inference':True,
                'snapshot_ts':snapshot.get('ts'), 'settlement_only':True, **evidence})
        return r, detail
    def reset_empty(reason, *, seen=False):
        progress = dict(r.get('empty_scan_evidence') or {})
        progress.update(count=0,first_empty_ts=None,last_empty_ts=None,reason=reason)
        if seen:
            progress['position_seen'] = True
        r['empty_scan_evidence'] = progress
    if r.get('state') in TERMINAL:
        return finish('already_terminal')
    if r.get('m10_step3_confirmation') or str(r.get('module') or '').upper() in ('M9', 'M10'):
        return finish('separate_m9_m10_confirmation_policy')
    if str(r.get('kind') or '').lower() != 'open' or str(r.get('module') or '').upper() not in ('', 'B1'):
        return finish('not_legacy_open_scope')
    if not _fresh(snapshot, r, row, now):
        reset_empty('snapshot_not_fresh_or_complete')
        return finish('snapshot_not_fresh_or_complete')
    if state == 'UNAVAILABLE':
        reset_empty('position_unavailable_or_not_covered')
        return finish('position_unavailable_or_not_covered')
    before_state, _ = position_read(r.get('pre_position'))
    if state == 'FOUND':
        reset_empty('position_observed', seen=q is not None and q > 0)
        side = str(normalized.get('side') or '').upper()
        if side != str(r.get('side') or '').upper():
            return finish('position_side_mismatch')
        if q is None or q <= 0:
            return finish('position_quantity_unavailable')
        if before_state != 'UNAVAILABLE':
            ok, ev = verify_pending_settlement(r, row)
            return finish('known_baseline_quantity_delta' if ok else ev.get('reason','quantity_delta_unconfirmed'),
                          'CONFIRMED_SUCCESS' if ok else None, quantity_evidence=ev)
        if intent is None or intent <= 0 or current_u is None:
            return finish('intent_or_notional_unavailable')
        if current_u + 1e-9 < intent * MIN_POSITION_RATIO:
            return finish('position_below_half_intent', minimum_required_u=intent*MIN_POSITION_RATIO)
        # The observer module is not the original submitting module.
        return finish('legacy_same_side_at_least_half_intent', 'CONFIRMED_SUCCESS',
                      minimum_required_u=intent*MIN_POSITION_RATIO, current_notional_u=current_u)
    submitted = finite_number(r.get('ts')) or 0
    ts = float(snapshot['ts'])
    progress = dict(r.get('empty_scan_evidence') or {})
    seen = bool(progress.get('position_seen') or has_fill_evidence(r) or before_state == 'FOUND')
    closed_after = (platform_closes or {}).get(p, 0)
    if r.get('open_order_active') is True:
        reset_empty('working_order_not_resolved')
        return finish('working_order_not_resolved')
    if seen and submitted < closed_after <= ts:
        return finish('later_confirmed_platform_close_and_fresh_empty', 'RESOLVED_FLAT',
                      platform_close_ts=closed_after)
    if not scan_id or str(scan_id) == progress.get('last_scan_id') or ts <= float(progress.get('last_observed_ts') or 0):
        return finish('duplicate_empty_snapshot')
    previous = finite_number(progress.get('last_empty_ts'))
    if previous is not None and ts - previous < EMPTY_INTERVAL:
        return finish('empty_scans_too_close')
    count = min(EMPTY_COUNT, int(progress.get('count') or 0)+1)
    progress.update(count=count,last_scan_id=str(scan_id),last_observed_ts=ts,last_empty_ts=ts,
                    position_seen=seen,reason='fresh_explicit_empty')
    r['empty_scan_evidence'] = progress
    if count < EMPTY_COUNT or ts - submitted < MIN_ORDER_AGE:
        return finish('needs_three_fresh_empty_scans', empty_scan_count=count)
    target = 'RESOLVED_FLAT' if seen else 'CONFIRMED_NOT_FILLED'
    return finish('observed_position_now_flat' if seen else 'no_fill_after_three_fresh_empty_scans',
                  target, empty_scan_count=count, exchange_rejection_confirmed=False)


def replay(records, snapshot, *, now, scan_id):
    closes = confirmed_platform_closes(records)
    updated, decisions = {}, []
    for key, record in records.items():
        updated[key], detail = reconcile(record, snapshot, now=now, scan_id=scan_id, platform_closes=closes)
        decisions.append(dict(record_key=key, **detail))
    return updated, decisions


def manual_resolution(record, *, expected_fingerprint, state, reason, now):
    """Compare-and-set plan for a future sole journal writer; never writes itself."""
    if fingerprint(record) != expected_fingerprint:
        raise ValueError('record_changed_since_preview')
    if state not in TERMINAL or not str(reason).strip():
        raise ValueError('explicit_terminal_state_and_reason_required')
    updated = deepcopy(record)
    updated.update(state=state,resolved_ts=now,evidence={'basis':'manual_review','reason':str(reason),
        'settlement_only':True,'creates_fill_or_profit_or_grid_layer':False})
    return updated, {'before_fingerprint':expected_fingerprint,'before_state':record.get('state'),
        'after_state':state,'reason':str(reason),'ts':now}
