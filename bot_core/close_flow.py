"""Bounded full-close ownership and submission evidence. No browser or file IO."""
from contextlib import contextmanager
import threading
import time


WORK_SECONDS = 120.0
OUTER_SECONDS = 125.0
FINAL_RESERVE_SECONDS = 5.0
_local = threading.local()


class CloseDeadlineError(TimeoutError):
    pass


def is_full_close_batch(requests):
    rows = list(requests or [])
    def eligible(req):
        get = req.get if isinstance(req, dict) else lambda k, d='': getattr(req, k, d)
        return (str(get('module')).upper() in ('ANCHOR', 'S1')
                and str(get('coin')).upper() == 'ALL'
                and str(get('action_type')).lower() in ('close_pair', 'force_close_all', 'close_all'))
    return bool(rows) and all(eligible(req) for req in rows)


def current():
    return getattr(_local, 'flow', None)


def pretrade_evidence(request, snapshot, *, now):
    """Permit the full-close reader to run on fresh structured evidence.

    This is not a fill or flat confirmation. The executor must read both live
    position tables again before committing an anchor update.
    """
    from .execution_confirmation import position_read, quantity
    from .position_truth import finite_number
    s = snapshot if isinstance(snapshot, dict) else {}
    if not is_full_close_batch([request]):
        return False, {'reason': 'not_anchor_s1_full_close'}
    meta = s.get('meta') or {}
    ts = finite_number(s.get('ts'))
    if (s.get('data_quality') != 'ok' or ts is None or not 0 <= now-ts <= 20
            or s.get('stale') or s.get('recovered_from_last_good') or s.get('dq_fallback')
            or meta.get('cache_hit') or str(s.get('scan_scope') or meta.get('scan_scope')
                or meta.get('scope') or '').lower() in ('local', 'partial')):
        return False, {'reason': 'full_close_snapshot_not_fresh'}
    positions = s.get('raw_positions') or s.get('positions') or {}
    counts = {}
    for platform in ('EXT', 'VAR'):
        rows = positions.get(platform) if isinstance(positions, dict) else None
        if not isinstance(rows, dict) or not rows:
            return False, {'reason': 'full_close_position_evidence_missing', 'platform': platform}
        found = empty = 0
        for row in rows.values():
            if not isinstance(row, dict) or row.get('stale') or row.get('recovered_from_last_good'):
                return False, {'reason': 'full_close_position_unavailable', 'platform': platform}
            row_ts = finite_number(row.get('ts'))
            if row_ts is not None and not 0 <= now-row_ts <= 20:
                return False, {'reason': 'full_close_row_stale', 'platform': platform}
            state, normalized = position_read(row)
            if state == 'FOUND' and (quantity(normalized) or 0) > 0:
                found += 1
            elif state == 'CONFIRMED_EMPTY':
                empty += 1
            else:
                return False, {'reason': 'full_close_position_unavailable', 'platform': platform}
        counts[platform] = {'found': found, 'explicit_empty_rows': empty}
    return True, {'reason': 'fresh_structured_positions', 'counts': counts,
                  'snapshot_ts': ts, 'executor_live_confirmation_required': True}


def submission_summary(reports):
    attempts, uncertain = [], False
    for report in reports.values():
        rows = [a for r in report.get('close_attempts', []) for a in r.get('attempts', [])
                if a.get('state') in ('CLICK_STARTED', 'CLICKED', 'CLICK_UNKNOWN')]
        attempts.extend(rows)
        if rows and not report.get('flat_confirmed'):
            uncertain = True
    return {'submit_attempted': bool(attempts), 'no_submit': not bool(attempts),
            'pending': uncertain, 'pending_confirm': uncertain,
            'submission_state': ('NOT_SUBMITTED' if not attempts else
                'CLICK_UNKNOWN' if any(a.get('state') != 'CLICKED' for a in attempts) else 'CLICKED')}


@contextmanager
def scope(flow):
    previous = current()
    _local.flow = flow
    try:
        yield flow
    finally:
        _local.flow = previous


def invoke(fn, requests, deadline, *, clock=time.monotonic, assert_live=lambda: None):
    if not is_full_close_batch(requests) or current() is not None:
        return fn()
    with scope(CloseFlow(deadline=deadline, clock=clock, assert_live=assert_live)):
        return fn()


class CloseFlow:
    def __init__(self, *, deadline=None, clock=time.monotonic, assert_live=lambda: None):
        self.clock = clock
        self.deadline = clock() + WORK_SECONDS if deadline is None else deadline
        self.assert_owner_live = assert_live
        self.owner_thread = threading.get_ident()
        self.records = {}
        self.recovered = set()
        self.coins = {'EXT': [], 'VAR': []}
        self.final_verification = False

    def check(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError('close_flow_owner_thread_mismatch')
        self.assert_owner_live()
        if self.remaining() <= 0:
            raise CloseDeadlineError('full_close_deadline_exhausted')

    def remaining(self):
        reserve = 0.0 if self.final_verification else FINAL_RESERVE_SECONDS
        return max(0.0, self.deadline - reserve - self.clock())

    def timeout_ms(self, requested=3000):
        self.check()
        return max(1, min(int(requested or 3000), int(self.remaining() * 1000)))

    def begin(self, platform, coin):
        self.check()
        key = (str(platform).upper(), str(coin).upper())
        return self.records.setdefault(key, {'platform': key[0], 'coin': key[1],
            'state': 'NO_SUBMIT', 'submitted': False, 'reason': 'pre_submit', 'attempts': []})

    def record_attempt(self, attempt):
        self.check()
        record = self.begin(attempt.get('platform'), attempt.get('coin'))
        if not any(a.get('id') == attempt.get('id') for a in record['attempts']):
            record['attempts'].append(attempt)
        state = attempt.get('state')
        if state in ('CLICK_STARTED', 'CLICKED', 'CLICK_UNKNOWN'):
            record['submitted'] = True
            record['state'] = 'UNCONFIRMED'
        elif not record['submitted']:
            record['state'] = 'NO_SUBMIT'
        record['reason'] = str(attempt.get('reason') or record['reason'])

    def mark_unknown(self, platform, coin, reason):
        record = self.begin(platform, coin)
        record.update(state='UNCONFIRMED', submitted=True, reason=str(reason))

    def mark_result(self, platform, coin, ok, reason=''):
        self.assert_owner_live()
        record = self.records.get((str(platform).upper(), str(coin).upper()))
        if record is None:
            record = self.begin(platform, coin)
        record['reason'] = str(reason or record['reason'])
        if ok:
            record['state'] = 'CONFIRMED_EMPTY'
        return record

    def can_resubmit(self, platform, coin):
        self.check()
        p, c = str(platform).upper(), str(coin).upper()
        record = self.records.get((p, c))
        if not record or record['state'] != 'NO_SUBMIT' or record['submitted']:
            return False
        # ALL could include any already dispatched coin. Never replay that scope.
        return not any(r['submitted'] and r['platform'] == p and (c == 'ALL' or r['coin'] == 'ALL')
                       for r in self.records.values())

    def recover_once(self, platform, coin, recover):
        self.check()
        p = str(platform).upper()
        if p in self.recovered or not self.can_resubmit(p, coin) or self.remaining() < 3:
            return False
        self.recovered.add(p)
        ok = bool(recover())
        self.check()
        return ok

    def submitted(self, platform, coin):
        p, c = str(platform).upper(), str(coin).upper()
        return any(r['submitted'] and r['platform'] == p and (r['coin'] in (c, 'ALL') or c == 'ALL')
                   for r in self.records.values())

    def details(self, platform):
        p = str(platform).upper()
        return [dict(r, attempts=[dict(a) for a in r['attempts']])
                for r in self.records.values() if r['platform'] == p]


def wait_ready(locator, call, *, timeout_ms, clock=time.monotonic, sleep=time.sleep,
               assert_live=lambda: None, sample=None):
    """Observe enabled state until a fixed deadline; this function never clicks."""
    deadline = clock() + max(0.0, timeout_ms / 1000.0)
    count = 0
    while True:
        assert_live()
        count += 1
        if call('close.ready.enabled', locator.is_enabled, timeout=max(1, int((deadline-clock())*1000))):
            return True, {'reason': 'close_button_ready', 'samples': count}
        if clock() >= deadline:
            detail = {'reason': 'close_button_disabled_timeout', 'samples': count}
            if sample is not None:
                try:
                    detail['ui'] = sample()
                except Exception as exc:
                    detail['sample_error'] = type(exc).__name__
            return False, detail
        sleep(min(.1, max(0.0, deadline - clock())))


CLOSE_DIALOG_SAMPLE_JS = r"""button => {
  const d=button.closest('dialog,[role="dialog"]');
  return {disabled:button.disabled, aria_disabled:button.getAttribute('aria-disabled'),
    busy:button.getAttribute('aria-busy'), button_text:(button.innerText||'').slice(0,120),
    dialog_present:!!d, dialog_text:d ? (d.innerText||'').slice(0,1800) : '',
    inputs:d ? [...d.querySelectorAll('input')].map(x=>({type:x.type,value:x.value,
      placeholder:x.placeholder,min:x.min,max:x.max,disabled:x.disabled})).slice(0,8) : []};
}"""
