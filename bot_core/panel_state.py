"""Pure panel validation and accounting; no file, process, UI or network access."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
import ntpath
import shlex


SNAPSHOT_MAX_AGE_SEC = 900.0
EQUITY_CHANGE_BASIS = "权益变动（含浮盈亏与出入金；跨日承接上次记录）"


def merge_status_snapshot(base, patch):
    """Merge partial panel updates, replacing complete runtime observations.

    Missing keys in a runtime observation mean they were cleared. Unrelated
    trend/selection sections may be absent from heartbeats and must survive.
    """
    current = deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(patch, dict):
        return current
    observations = {
        "runtime_state", "scan_watch", "scan_plan", "round_watch",
        "diag_round_watch", "runtime_state_diag",
    }
    old_pid, new_pid = current.get("pid"), patch.get("pid")
    if old_pid and new_pid and str(old_pid) != str(new_pid):
        for key in observations | {"runtime_source", "err"}:
            current.pop(key, None)

    def merge(left, right):
        result = deepcopy(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    for key, value in patch.items():
        if key not in observations and isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = merge(current[key], value)
        else:
            current[key] = deepcopy(value)
    runtime = patch.get("runtime_state")
    if isinstance(runtime, dict):
        # These top-level mirrors cannot retain the previous process's source/error.
        current["runtime_source"] = str(runtime.get("runtime_source") or "runtime_state")
        current["err"] = str(runtime.get("err") or "")
    return current


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def snapshot_status(snapshot, *, now, kind, max_age=SNAPSHOT_MAX_AGE_SEC):
    """A cached value remains displayable, but never becomes fresh by rereading it."""
    if not isinstance(snapshot, dict) or not snapshot:
        return False, "snapshot_missing"
    dq = str(snapshot.get("data_quality") or snapshot.get("dq") or "").lower()
    if dq != "ok":
        return False, "data_quality:" + (dq or "missing")
    stamp = snapshot.get("balance_ts") if kind == "equity" else snapshot.get("snapshot_ts")
    if stamp is None:
        stamp = snapshot.get("ts")
    stamp = finite_number(stamp)
    now = finite_number(now)
    if stamp is None or stamp <= 0 or now is None:
        return False, "timestamp_missing"
    age = now - stamp
    if age < -30:
        return False, "timestamp_future"
    if age > max_age:
        return False, "snapshot_stale:%.0fs" % age
    if kind == "equity":
        if snapshot.get("balance_fresh") is False or snapshot.get("balance_fallback_used") is True:
            return False, "balance_stale_or_fallback"
    else:
        for plat in ("EXT", "VAR"):
            rows = snapshot.get(plat, snapshot.get(plat.lower()))
            if not isinstance(rows, (dict, list)):
                return False, "positions_missing:" + plat
            for row in rows.values() if isinstance(rows, dict) else rows:
                if not isinstance(row, dict):
                    return False, "position_row_invalid:" + plat
                reason = str(row.get("reason") or "").lower()
                if reason in ("row_not_found", "read_fail", "timeout", "bulk_partial_deferred"):
                    return False, "positions_uncertain:" + plat + ":" + reason
                for key in ("notional_u", "upnl_u"):
                    if key in row and finite_number(row[key]) is None:
                        return False, "position_number_invalid:" + plat + ":" + key
    return True, "ok"


def equity_values(snapshot, *, now, e_keys, v_keys, total_keys):
    """Return last observed amounts plus validity; never synthesize missing legs as zero."""
    root = snapshot if isinstance(snapshot, dict) else {}
    if isinstance(root.get("balances"), dict):
        root = root["balances"]
    lowered = {str(key).lower(): value for key, value in root.items()}

    def pick(keys):
        for key in keys:
            if key.lower() in lowered:
                value = lowered[key.lower()]
                if isinstance(value, dict):
                    value = value.get("usd")
                return finite_number(value)
        return None

    ext, var, total = pick(e_keys), pick(v_keys), pick(total_keys)
    if total is None and ext is not None and var is not None:
        total = ext + var
    ok, reason = snapshot_status(snapshot, now=now, kind="equity")
    if ext is None or var is None or total is None:
        ok, reason = False, "equity_amount_missing_or_invalid"
    elif abs(total - ext - var) > max(0.05, abs(total) * 0.000001):
        ok, reason = False, "equity_total_mismatch"
    return ext if ext is not None else 0.0, var if var is not None else 0.0, total if total is not None else 0.0, ok, reason


def _day_totals(days, today):
    hist = sum(finite_number(rec.get("day_profit")) or 0.0
               for day, rec in days.items() if day < today and isinstance(rec, dict))
    rec = days.get(today)
    current = (finite_number(rec.get("day_profit")) or 0.0) if isinstance(rec, dict) else 0.0
    return round(current, 6), round(hist, 6)


def advance_equity_days(store, *, today, observed_local, total, valid):
    """Update only a new valid observation; preserve existing historical baselines/PnL.

    Across missing midnight observations the next day carries the last observed
    balance. The change belongs to that observation interval, not an invented
    midnight mark. No backfill of previously missed intervals is attempted.
    """
    date.fromisoformat(today)
    original = store if isinstance(store, dict) else {}
    out = deepcopy(original)
    days = out.get("days") if isinstance(out.get("days"), dict) else {}
    amount = finite_number(total)
    if not valid or amount is None:
        current, hist = _day_totals(days, today)
        return out, current, hist, False
    prior_days = sorted(day for day, rec in days.items()
                        if isinstance(day, str) and day < today and isinstance(rec, dict))
    rec = days.get(today)
    if not isinstance(rec, dict):
        baseline, baseline_day = amount, today
        if prior_days:
            baseline_day = prior_days[-1]
            prev = days[baseline_day]
            baseline = finite_number(prev.get("close_total"))
            if baseline is None:
                baseline = finite_number(prev.get("last_total"))
            if baseline is None:
                current, hist = _day_totals(days, today)
                return out, current, hist, False
            # Close a formerly current day without recalculating historical PnL.
            if not prev.get("settled"):
                prev["settled"] = True
                prev["close_total"] = baseline
        rec = {"equity_0am": round(baseline, 6), "day_profit": 0.0,
               "last_total": round(baseline, 6), "settled": False,
               "created_local": observed_local, "close_total": None,
               "baseline_source": "previous_observation" if prior_days else "first_observation",
               "baseline_day": baseline_day}
        days[today] = rec
    baseline = finite_number(rec.get("equity_0am"))
    if baseline is None:
        current, hist = _day_totals(days, today)
        return deepcopy(original), current, hist, False
    new_profit = round(amount - baseline, 6)
    if (finite_number(rec.get("last_total")) != amount or
            finite_number(rec.get("day_profit")) != new_profit or "updated_local" not in rec):
        rec.update(day_profit=new_profit, last_total=round(amount, 6), updated_local=observed_local)
    out["days"] = days
    out["mode"] = "observed_equity_change_carry_forward"
    current, hist = _day_totals(days, today)
    return out, current, hist, out != original


def _windows_path(value):
    return ntpath.normcase(ntpath.normpath(str(value or "").strip().strip('"')))


def process_identity_matches(expected, observed):
    """Require PID, creation time, executable and exact script argument identity."""
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return False
    try:
        if int(expected.get("pid", 0)) <= 0 or int(expected["pid"]) != int(observed.get("pid", 0)):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    if not expected.get("created") or str(expected["created"]) != str(observed.get("created") or ""):
        return False
    executable, script = expected.get("executable"), expected.get("script")
    if not executable or not script or not ntpath.isabs(str(script)):
        return False
    if _windows_path(executable) != _windows_path(observed.get("executable")):
        return False
    try:
        args = shlex.split(str(observed.get("command_line") or ""), posix=False)
    except ValueError:
        return False
    # First argument is the interpreter; matching later arbitrary arguments is
    # unsafe. Our launcher supplies the absolute script as argument number two.
    return len(args) >= 2 and _windows_path(args[1]) == _windows_path(script)


def stop_confirmed(expected, after, *, command_returncode):
    """Unknown observation is not stopped. A reused PID means the old process exited."""
    if command_returncode != 0 or not isinstance(after, dict):
        return False
    if after.get("exists") is False:
        return True
    return bool(expected.get("created") and after.get("created") and
                str(expected["created"]) != str(after["created"]))
