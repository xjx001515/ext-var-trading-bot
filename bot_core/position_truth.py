"""Pure position evidence and snapshot quality decisions.

Missing UI data is unknown, never proof that a position was closed. Presence
can be established by a valid row; execution confirmation needs stronger
quantity/fill evidence and lives in execution_confirmation.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

FOUND = 'FOUND'
CONFIRMED_EMPTY = 'CONFIRMED_EMPTY'
UNAVAILABLE = 'UNAVAILABLE'


def finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def position_quantity(row):
    if not isinstance(row, dict):
        return None
    for key in ('qty_abs', 'quantity', 'qty', 'size', 'position_qty', 'size_qty',
                'position_amt', 'positionAmt', 'contracts', 'position_size'):
        if key in row and row[key] is not None:
            value = finite_number(row[key])
            if value is not None:
                return abs(value)
    return None


def normalize_position_read(row, *, confirmed_empty=False):
    if isinstance(row, dict) and row.get('state') in (FOUND, CONFIRMED_EMPTY, UNAVAILABLE) and 'row' in row:
        if row['state'] == UNAVAILABLE:
            return {'state': UNAVAILABLE, 'row': row.get('row'), 'reason': row.get('reason', 'read_unavailable')}
        return normalize_position_read(row.get('row'), confirmed_empty=row['state'] == CONFIRMED_EMPTY)
    if not isinstance(row, dict):
        return {'state': CONFIRMED_EMPTY if confirmed_empty else UNAVAILABLE,
                'row': None, 'reason': 'explicit_empty' if confirmed_empty else 'row_unavailable'}
    reason = str(row.get('reason') or '').lower()
    state = str(row.get('position_state') or '').upper()
    if state == UNAVAILABLE or row.get('recovered_from_last_good') or row.get('stale') or any(
            text in reason for text in ('row_not_found', 'read_fail', 'read_error', 'timeout', 'deferred', 'human_verify', 'last_good_hold')):
        return {'state': UNAVAILABLE, 'row': row, 'reason': reason or 'read_unavailable'}
    side = str(row.get('side') or '').upper()
    quantity = next((abs(n) for key in ('qty_abs', 'quantity', 'qty', 'size')
                     if (n := finite_number(row.get(key))) is not None and n != 0), 0.0)
    notional = next((abs(n) for key in ('notional_u', 'notional_usd', 'value_usd')
                     if (n := finite_number(row.get(key))) is not None and n != 0), 0.0)
    present = side in ('LONG', 'SHORT') and (quantity > 0 or notional > 0)
    empty = confirmed_empty or state == CONFIRMED_EMPTY or row.get('confirmed_empty') is True
    if present and empty:
        return {'state': UNAVAILABLE, 'row': row, 'reason': 'conflicting_empty_and_position'}
    if present:
        return {'state': FOUND, 'row': row, 'reason': 'position_row'}
    if empty and side in ('', 'NONE', 'FLAT') and not quantity and not notional:
        return {'state': CONFIRMED_EMPTY, 'row': row, 'reason': 'explicit_empty'}
    return {'state': UNAVAILABLE, 'row': row, 'reason': 'zero_or_invalid_row_without_empty_evidence'}


def _position_maps(snapshot):
    positions = snapshot.get('positions') if isinstance(snapshot, dict) else None
    if isinstance(positions, dict):
        return {p: positions.get(p) if isinstance(positions.get(p), dict) else {} for p in ('EXT', 'VAR')}
    return {'EXT': {}, 'VAR': {}}


def assess_snapshot(snapshot, coins=None, *, require_balances=True):
    if not isinstance(snapshot, dict):
        return {'ok': False, 'reason': 'snapshot_invalid', 'states': {}, 'coins': []}
    maps = _position_maps(snapshot)
    raw = snapshot.get('raw') or {}
    explicit_empty = raw.get('platform_empty_detail_map') or {}
    requested = list(coins) if coins is not None else list(raw.get('scan_effective_coins') or [])
    if coins is None:
        requested += list(maps['EXT']) + list(maps['VAR'])
    requested = sorted({str(c).strip().upper() for c in requested if str(c).strip()})
    errors = []
    states = {p: {} for p in ('EXT', 'VAR')}
    for platform in ('EXT', 'VAR'):
        details = explicit_empty.get(platform) or {}
        platform_empty = details.get('confirmed_empty') is True
        if not requested and not platform_empty:
            errors.append(f'{platform}:no_positions_evidence')
        for coin in requested:
            observation = normalize_position_read(maps[platform].get(coin), confirmed_empty=platform_empty)
            states[platform][coin] = observation['state']
            if observation['state'] == UNAVAILABLE:
                errors.append(f'{platform}:{coin}:{observation["reason"]}')
            elif observation['state'] == FOUND:
                # Presence protects an existing position, but risk calculations
                # also require a usable valuation before this scan can trade.
                value = finite_number((observation['row'] or {}).get('notional_u'))
                if value is None or value <= 0:
                    errors.append(f'{platform}:{coin}:position_valuation_unavailable')
        if require_balances:
            balance = ((snapshot.get('balances') or {}).get(platform) or {}).get('usd')
            number = finite_number(balance)
            if number is None or number < 0:
                errors.append(f'{platform}:balance_unavailable')
    return {'ok': not errors, 'reason': 'positions_verified' if not errors else '|'.join(errors),
            'states': states, 'coins': requested}


def annotate_snapshot(snapshot, coins=None):
    """Return a copy; callers may replace their in-progress scan snapshot."""
    output = deepcopy(snapshot)
    quality = assess_snapshot(output, coins)
    output['data_quality'] = 'ok' if quality['ok'] else 'fail'
    output['data_quality_reason'] = quality['reason']
    output['dq_reason'] = quality['reason']
    output.setdefault('meta', {})['position_truth_verified'] = quality['ok']
    output['meta']['position_truth_errors'] = [] if quality['ok'] else quality['reason'].split('|')
    for platform, rows in quality['states'].items():
        for coin, state in rows.items():
            row = output.setdefault('positions', {}).setdefault(platform, {}).setdefault(coin, {})
            row['position_state'] = state
    return output


def snapshot_has_positions_truth(snapshot):
    return bool(isinstance(snapshot, dict) and snapshot.get('data_quality') == 'ok'
                and not snapshot.get('dq_fallback') and assess_snapshot(snapshot)['ok'])


def coin_confirmed_flat(snapshot, coin):
    if not snapshot_has_positions_truth(snapshot):
        return False
    quality = assess_snapshot(snapshot, [str(coin).upper()], require_balances=False)
    return quality['ok'] and all(quality['states'][p].get(str(coin).upper()) == CONFIRMED_EMPTY for p in ('EXT', 'VAR'))


def choose_round_snapshot(snapshot, *, last_good_snapshot, last_good_ts, max_age_sec, now):
    meta = {'wait_only': False, 'fallback_used': False, 'reason_code': 'ok', 'new_risk_blocked': False}
    from .platform_access import snapshot_access_block
    access_reason = snapshot_access_block(snapshot)
    if access_reason:
        meta.update(wait_only=True, reason_code=access_reason, new_risk_blocked=True,
                    user_action_required=True, last_good_age_sec=None)
        return None, meta
    if snapshot_has_positions_truth(snapshot):
        result = deepcopy(snapshot)
        result.update(dq_policy='live_ok', dq_new_risk_blocked=False)
        return result, meta
    age = now - (finite_number(last_good_ts) or 0.0)
    if age < 0 or age > max_age_sec or not snapshot_has_positions_truth(last_good_snapshot):
        meta.update(wait_only=True, reason_code='scan_dq_fail', last_good_age_sec=age)
        return None, meta
    result = deepcopy(last_good_snapshot)
    result.update(dq_fallback=True, dq_origin=(snapshot or {}).get('data_quality') if isinstance(snapshot, dict) else 'invalid',
                  last_good_age_sec=age, last_good_age_max_sec=max_age_sec,
                  dq_policy='protective_only_last_good', dq_new_risk_blocked=True)
    meta.update(fallback_used=True, reason_code='scan_last_good_fallback', new_risk_blocked=True, last_good_age_sec=age)
    return result, meta
