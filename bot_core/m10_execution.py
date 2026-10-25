"""M10 execution evidence. Missing rows and boolean UI returns are not fills."""
from .execution_confirmation import position_read, quantity
from .position_truth import finite_number


SENT = frozenset(('CLICK_STARTED', 'CLICKED', 'CLICK_UNKNOWN'))


def outcome(attempts, *, confirmed=False, delta_qty=0.0, reported=False):
    rows = [a for a in attempts if isinstance(a, dict)]
    sent = any(a.get('state') in SENT for a in rows)
    if confirmed:
        return 'confirmed'
    if sent or (finite_number(delta_qty) or 0) > 0 or (reported and not rows):
        return 'pending'
    return 'failed'


def close_progress(pre, post, partial_ratio=.90):
    before, a = position_read(pre)
    after, b = position_read(post)
    qa, qb = quantity(a), quantity(b)
    detail = {'pre_state': before, 'post_state': after, 'pre_qty': qa, 'post_qty': qb,
              'mode': 'unconfirmed', 'filled_ratio': 0.0, 'filled_ratio_effective': 0.0,
              'closed_qty': 0.0, 'full_success': False, 'partial_success': False,
              'has_position': after == 'FOUND', 'side': (b or {}).get('side', ''),
              'notional_u': (b or {}).get('notional_u'), 'basis': 'position_quantity_delta'}
    if before != 'FOUND' or not qa or after not in ('FOUND', 'CONFIRMED_EMPTY') or qb is None:
        return False, dict(detail, reason='position_quantity_unavailable')
    if after == 'FOUND' and (qb <= 0 or a.get('side') != b.get('side')):
        return False, dict(detail, reason='position_side_mismatch')
    closed = min(qa, max(0.0, qa - qb))
    ratio = closed / qa
    full = after == 'CONFIRMED_EMPTY'
    partial = ratio >= min(.99, max(.50, float(partial_ratio)))
    detail.update(closed_qty=closed, filled_ratio=ratio, filled_ratio_effective=ratio,
                  full_success=full, partial_success=partial,
                  mode='full_success' if full else 'partial_success' if partial else 'unconfirmed',
                  reason='quantity_close_confirmed' if full or partial else 'close_quantity_unconfirmed')
    return full or partial, detail


def rollback_limit(pre, current, closed_qty):
    """Cap restoration by proven closed quantity AND the still-missing quantity.

    Dollar rounding remains in m10_guard; this only reduces a restoration limit.
    """
    a_state, a = position_read(pre)
    b_state, b = position_read(current)
    qa, qb = quantity(a), quantity(b)
    if a_state != 'FOUND' or not qa or b_state not in ('FOUND', 'CONFIRMED_EMPTY') or qb is None:
        return {'ok': False, 'reason': 'rollback_position_unavailable'}
    if b_state == 'FOUND' and a.get('side') != b.get('side'):
        return {'ok': False, 'reason': 'rollback_position_side_changed'}
    qty = max(0.0, min(finite_number(closed_qty) or 0.0, qa - qb))
    prices = []
    for row in (a, b):
        q = quantity(row)
        n = finite_number(row.get('notional_u'))
        if q and n and n > 0:
            prices.append(n / q)
        for name in ('mark_px', 'px', 'price'):
            px = finite_number(row.get(name))
            if px and px > 0:
                prices.append(px)
    if not prices or qty <= 0:
        return {'ok': False, 'reason': 'rollback_no_verified_missing_quantity'}
    return {'ok': True, 'max_qty': qty, 'max_u': qty * min(prices),
            'pre_qty': qa, 'current_qty': qb, 'reason': 'verified_closed_quantity_cap'}
