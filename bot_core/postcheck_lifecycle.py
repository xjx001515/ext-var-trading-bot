"""Bounded in-round verification and evidence-only reconciliation across rounds.

No browser, files, timers or orders are used here. Repeated empty scans are an
explicit policy inference, not an exchange rejection or proof of trade history.
"""
from copy import deepcopy

from .execution_confirmation import (
    has_fill_evidence, pending_blocks, position_read, verify_pending_settlement,
)
from .position_truth import finite_number

VERIFY_MAX_ATTEMPTS = 8
VERIFY_TIMEOUT_SEC = 120.0
EMPTY_SCAN_COUNT = 3
EMPTY_SCAN_INTERVAL_SEC = 30.0
SCAN_MAX_AGE_SEC = 90.0


def verification_budget(payload, *, now, monotonic=None):
    """The original start/deadline never moves when a check is rescheduled."""
    p = payload if isinstance(payload, dict) else {}
    retries = max(0, min(VERIFY_MAX_ATTEMPTS, int(finite_number(p.get('verify_retry_left')) or 0)))
    created = finite_number(p.get('created_ts'))
    deadline = finite_number(p.get('deadline_ts'))
    left = 0.0 if created is None else created + VERIFY_TIMEOUT_SEC - now
    if deadline is not None:
        left = min(left, deadline - now)
    mono_start = finite_number(p.get('created_monotonic'))
    if monotonic is not None and mono_start is not None:
        left = min(left, mono_start + VERIFY_TIMEOUT_SEC - monotonic)
    reason = 'attempts_exhausted' if retries == 0 else ('deadline_exhausted' if left <= 0 else '')
    return {'exhausted': bool(reason), 'reason': reason,
            'retry_left': retries, 'seconds_left': max(0.0, left)}


def _scan_position(snapshot, platform, coin):
    positions = snapshot.get('positions') or {}
    rows = positions.get(platform) if isinstance(positions, dict) else None
    row = rows.get(coin) if isinstance(rows, dict) else None
    if row is not None:
        return row
    raw = snapshot.get('raw') or {}
    details = (raw.get('platform_empty_detail_map') or {}).get(platform) or {}
    if details.get('confirmed_empty') is True:
        return {'position_state': 'CONFIRMED_EMPTY', 'reason': 'explicit_platform_empty'}
    return {'position_state': 'UNAVAILABLE'}


def observe_pending(record, snapshot, *, scan_id, now):
    """Advance one record once per independent main-loop scan, without trading.

Only ordinary B1/legacy open records can become NOT_FILLED by empty consensus.
M9/M10 and close/reduce records retain their existing settlement policy.
"""
    r = deepcopy(record)
    if not pending_blocks(r):
        return r, 'terminal'
    monitor = r.get('deferred_postcheck') or {}
    module = str(r.get('module') or monitor.get('module') or '').upper()
    if r.get('m10_step3_confirmation') or module in ('M9', 'M10') or module.startswith('M9'):
        return r, 'separate_confirmation_policy'
    if str(r.get('kind') or '').lower() != 'open' or module not in ('', 'B1'):
        return r, 'separate_confirmation_policy'
    s = snapshot if isinstance(snapshot, dict) else {}
    progress = dict(r.get('empty_scan_evidence') or {})
    key = str(scan_id or '')
    if not key or key == progress.get('last_scan_id'):
        return r, 'duplicate_scan'
    ts = finite_number(s.get('ts'))
    submitted = finite_number(r.get('ts'))
    last_ts = finite_number(progress.get('last_observed_ts'))
    meta = s.get('meta') or {}
    row = _scan_position(s, str(r.get('platform') or ''), str(r.get('coin') or ''))
    row_ts = finite_number(row.get('ts')) if isinstance(row, dict) else None
    fresh = (s.get('data_quality') == 'ok' and not s.get('dq_fallback')
             and not s.get('stale') and not s.get('recovered_from_last_good')
             and not meta.get('cache_hit') and not meta.get('postcheck_direct_read')
             and str(s.get('scan_scope') or meta.get('scan_scope') or meta.get('scope') or '').lower() not in ('local', 'partial')
             and ts is not None and submitted is not None and submitted < ts <= now
             and now - ts <= SCAN_MAX_AGE_SEC
             and (last_ts is None or ts > last_ts)
             and (row_ts is None or (submitted < row_ts <= now and now - row_ts <= SCAN_MAX_AGE_SEC)))
    progress['last_scan_id'] = key
    if not fresh:
        progress.update(count=0, first_empty_ts=None, last_empty_ts=None, reason='scan_not_fresh_or_complete')
        r['empty_scan_evidence'] = progress
        return r, 'scan_not_fresh_or_complete'
    progress['last_observed_ts'] = ts
    state, _ = position_read(row)
    confirmed, evidence = verify_pending_settlement(r, row)
    if confirmed:
        r.update(state='CONFIRMED_SUCCESS', resolved_ts=now, evidence=evidence,
                 empty_scan_evidence=progress)
        return r, 'confirmed'
    if state != 'CONFIRMED_EMPTY':
        if state == 'FOUND':
            # A partial fill must never later be relabelled as "never filled".
            progress['position_seen'] = True
        progress.update(count=0, first_empty_ts=None, last_empty_ts=None, reason=state.lower())
        r['empty_scan_evidence'] = progress
        return r, 'position_seen' if state == 'FOUND' else 'position_unavailable'
    if progress.get('position_seen') or has_fill_evidence(r) or r.get('open_order_active') is True:
        progress.update(count=0, first_empty_ts=None, last_empty_ts=None, reason='fill_or_working_order_evidence')
        r['empty_scan_evidence'] = progress
        return r, 'fill_or_working_order_evidence'
    previous_empty = finite_number(progress.get('last_empty_ts'))
    if previous_empty is not None and ts - previous_empty < EMPTY_SCAN_INTERVAL_SEC:
        r['empty_scan_evidence'] = progress
        return r, 'empty_scan_too_close'
    count = min(EMPTY_SCAN_COUNT, int(progress.get('count') or 0) + 1)
    progress.update(count=count, last_empty_ts=ts, reason='fresh_explicit_empty')
    if not progress.get('first_empty_ts'):
        progress['first_empty_ts'] = ts
    r['empty_scan_evidence'] = progress
    if count >= EMPTY_SCAN_COUNT and ts - submitted >= VERIFY_TIMEOUT_SEC:
        evidence = dict(progress, basis='consecutive_fresh_empty_scans', inference=True,
                        exchange_rejection_confirmed=False, minimum_age_sec=VERIFY_TIMEOUT_SEC)
        r.update(state='CONFIRMED_NOT_FILLED', resolved_ts=now, evidence=evidence,
                 failure_reason='not_filled_after_fresh_empty_scans')
        return r, 'not_filled'
    return r, 'empty_scan_progress'
