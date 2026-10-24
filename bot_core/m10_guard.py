"""M10 koujing_a_3step safety rules: never strip a hedge leg when step2 cannot execute.

Step1 closes the winning leg before step2 reduces the losing leg. If step2 can never
fill (exchange minimum trade size, e.g. EXT Q = 1,000 Q ~ 145U vs a 10U reduce), the
hedge is gone and nothing restores it. These helpers decide, before step1, whether
step2 is feasible, and size the rollback that restores the winning leg when step2
fails anyway. Pure functions plus a small JSON store; no network, browser or process.
"""
import json
import math
import os
import re
import time

# Minimum trade sizes (in coin units) observed on the exchange UI. Used until learned.
KNOWN_MIN_QTY = {
    ('EXT', 'Q'): 1000.0,  # 2026-10-21: "Your order size is below the minimum trade size of 1,000 Q."
}
MIN_SIZE_MARGIN = 1.05      # USD value moves with price; keep a buffer above qty * price
FAIL_MEMO_TTL_SEC = 4 * 3600.0
_MIN_TRADE_RE = re.compile(r'minimum\s+trade\s+size\s+of\s+([\d,]+(?:\.\d+)?)\s*([A-Za-z0-9]+)', re.I)


def _num(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if math.isfinite(n) else default


def _key(platform, coin):
    return f"{str(platform or '').upper().strip()}|{str(coin or '').upper().strip()}"


def parse_min_trade_size(text):
    """'...below the minimum trade size of 1,000 Q.' -> (1000.0, 'Q'); otherwise (0.0, '')."""
    m = _MIN_TRADE_RE.search(str(text or ''))
    if not m:
        return 0.0, ''
    return _num(m.group(1).replace(',', '')), m.group(2).upper()


def plan_step2_u(win_upnl_u, loss_upnl_u, loss_notional_u, budget_ratio, calc_fn):
    """Same amount rule the executor uses after step1, evaluated before step1."""
    target = max(0.0, _num(win_upnl_u)) * max(0.0, _num(budget_ratio))
    ratio = min(max(target / max(1e-9, abs(_num(loss_upnl_u))), 0.0), 1.0)
    loss_n = max(0.0, _num(loss_notional_u))
    return _num((calc_fn(loss_n * ratio, loss_n) or {}).get('final_u'))


def precheck_step2(plan_u, loss_notional_u, *, min_qty=0.0, price=0.0, failed_u=0.0, margin=MIN_SIZE_MARGIN):
    """Return (ok, reason, detail). ok=False means step1 must not run."""
    plan_u = _num(plan_u)
    loss_n = max(0.0, _num(loss_notional_u))
    min_u = _num(min_qty) * _num(price) if _num(min_qty) > 0 and _num(price) > 0 else 0.0
    detail = {'plan_u': plan_u, 'loss_notional_u': loss_n, 'min_qty': _num(min_qty),
              'price': _num(price), 'min_u': min_u, 'failed_u': _num(failed_u)}
    if plan_u < 10.0:
        return False, 'step2_floor10_lt_10', detail
    need = min_u * max(1.0, _num(margin, MIN_SIZE_MARGIN))
    if min_u > 0 and plan_u < need:
        return False, 'step2_below_min_trade_size', detail
    remain = loss_n - plan_u
    if min_u > 0 and 10.0 < remain < need:
        detail['remain_u'] = remain
        return False, 'step2_remainder_below_min_trade_size', detail
    if _num(failed_u) > 0 and plan_u <= _num(failed_u) + 1e-9:
        return False, 'step2_le_recent_failed_amount', detail
    return True, 'ok', detail


def rollback_restore_u(pre_win_notional_u, fill_ratio):
    """Notional to reopen on the winning leg after step2 failed: what step1 closed, floored to 10U.
    Never larger than what was closed except the exchange's 40U order floor for tiny legs."""
    closed = max(0.0, _num(pre_win_notional_u)) * max(0.0, min(_num(fill_ratio, 1.0), 1.0))
    u = float(int(closed // 10) * 10)
    if 0.0 < closed and u < 40.0:
        u = 40.0
    return u


class MinSizeStore:
    """Learned minimum trade sizes and recently failed step2 amounts, persisted as JSON."""

    def __init__(self, path):
        self.path = path
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding='utf-8') as fh:
                    data = json.load(fh)
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
            self._data.setdefault('min_qty', {})
            self._data.setdefault('failed', {})
        return self._data

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass  # learning is best effort; the rollback still protects the hedge

    def min_qty(self, platform, coin, env_value=''):
        k = _key(platform, coin)
        for part in str(env_value or '').split(','):  # M10_MIN_QTY_OVERRIDES="EXT:Q=1000,VAR:FOO=5"
            name, _, qty = part.strip().partition('=')
            if name.strip().upper().replace(':', '|') == k and _num(qty) > 0:
                return _num(qty)
        learned = _num((self._load()['min_qty'].get(k) or {}).get('qty'))
        if learned > 0:
            return learned
        p, c = k.split('|', 1)
        return _num(KNOWN_MIN_QTY.get((p, c)))

    def learn_min_qty(self, platform, coin, qty, source=''):
        if _num(qty) <= 0:
            return
        self._load()['min_qty'][_key(platform, coin)] = {'qty': _num(qty), 'ts': time.time(), 'source': str(source or '')}
        self._save()

    def failed_u(self, platform, coin, ttl_sec=FAIL_MEMO_TTL_SEC, now=None):
        rec = self._load()['failed'].get(_key(platform, coin)) or {}
        age = (time.time() if now is None else now) - _num(rec.get('ts'))
        return _num(rec.get('amount_u')) if rec and age <= ttl_sec else 0.0

    def note_failed(self, platform, coin, amount_u, reason='', now=None):
        if _num(amount_u) < 10.0:  # nothing was submitted
            return
        self._load()['failed'][_key(platform, coin)] = {'amount_u': _num(amount_u), 'ts': time.time() if now is None else now, 'reason': str(reason or '')}
        self._save()

    def clear_failed(self, platform, coin):
        if self._load()['failed'].pop(_key(platform, coin), None) is not None:
            self._save()
