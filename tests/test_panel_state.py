"""Offline tests: pure helpers and AST-selected panel methods; all process I/O mocked."""
from __future__ import annotations

import ast
import copy
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

from bot_core import panel_state as state


ROOT = Path(__file__).resolve().parents[1]
EDIT_PATH = ROOT / "repair_backups/safety_refactor_20261022/panel_edits.py"
spec = importlib.util.spec_from_file_location("panel_edits_test", EDIT_PATH)
edits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edits)
ORIGINAL = (ROOT / "repair_backups/safety_refactor_20261022/面板4-11.py").read_text(encoding="utf-8-sig")
PATCHED = edits.apply(ORIGINAL)
TREE = ast.parse(PATCHED)


def identity(pid=501, created="637000000000000000"):
    return {"exists": True, "pid": pid, "created": created,
            "executable": r"C:\Python\python.exe", "script": r"C:\bot folder\BOT.py",
            "command_line": r'"C:\Python\python.exe" "C:\bot folder\BOT.py" --cdp fake'}


def make_bot(*, command_rc=0):
    calls = []
    runner = Mock(return_value=types.SimpleNamespace(returncode=command_rc, stdout=""))
    fake_subprocess = types.SimpleNamespace(Popen=object, run=runner, DEVNULL=-3, CREATE_NO_WINDOW=0)
    env = {"_panel_state": state, "subprocess": fake_subprocess, "Optional": __import__('typing').Optional,
           "Path": Path, "os": types.SimpleNamespace(name="nt"), "json": json,
           "time": types.SimpleNamespace(sleep=lambda delay: calls.append(delay)),
           "sys": types.SimpleNamespace(executable=r"C:\Python\python.exe")}
    selected = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "BotProc")
    exec(compile(ast.Module(body=[selected], type_ignores=[]), "<BotProc AST only>", "exec"), env)
    bot = env["BotProc"]()
    bot.identity = identity()
    bot.p = Mock(pid=501)
    bot.p.poll.return_value = None
    return bot, runner, calls


class SnapshotTests(unittest.TestCase):
    def eq(self, **extra):
        return dict(ts=1000, balance_ts=1000, data_quality="ok", balance_fresh=True,
                    balances={"EXT": {"usd": 60}, "VAR": {"usd": 40}, "TOTAL": {"usd": 100}}, **extra)

    def values(self, obj, now=1001):
        return state.equity_values(obj, now=now, e_keys=["e", "EXT"], v_keys=["v", "VAR"], total_keys=["total"])

    def test_valid_equity(self):
        self.assertEqual(self.values(self.eq()), (60, 40, 100, True, "ok"))

    def test_elapsed_time_overrides_old_fresh_flag(self):
        out = self.values(self.eq(), now=2000)
        self.assertEqual(out[:3], (60, 40, 100))
        self.assertFalse(out[3])
        self.assertIn("stale", out[4])

    def test_explicit_stale_flag(self):
        obj = self.eq(); obj["balance_fresh"] = False
        self.assertFalse(self.values(obj)[3])

    def test_quality_failure(self):
        obj = self.eq(); obj["data_quality"] = "fail"
        self.assertFalse(self.values(obj)[3])

    def test_missing_timestamp(self):
        obj = self.eq(); del obj["balance_ts"]; del obj["ts"]
        self.assertFalse(self.values(obj)[3])

    def test_future_timestamp(self):
        self.assertFalse(self.values(self.eq(), now=100)[3])

    def test_missing_platform_is_not_zero(self):
        obj = self.eq(); del obj["balances"]["VAR"]
        self.assertFalse(self.values(obj)[3])

    def test_invalid_and_nonfinite_amounts(self):
        for value in ("oops", float("nan"), float("inf"), True):
            obj = self.eq(); obj["balances"]["EXT"]["usd"] = value
            self.assertFalse(self.values(obj)[3])

    def test_total_mismatch(self):
        obj = self.eq(); obj["balances"]["TOTAL"]["usd"] = 999
        self.assertFalse(self.values(obj)[3])

    def test_zero_equity_is_valid(self):
        obj = self.eq(); obj["balances"] = {"EXT": 0, "VAR": 0, "TOTAL": 0}
        self.assertEqual(self.values(obj), (0, 0, 0, True, "ok"))

    def test_positions_quality_age_and_missing_platform(self):
        obj = {"ts": 1000, "data_quality": "ok", "EXT": [], "VAR": []}
        self.assertTrue(state.snapshot_status(obj, now=1001, kind="positions")[0])
        self.assertFalse(state.snapshot_status(obj, now=2000, kind="positions")[0])
        del obj["EXT"]
        self.assertFalse(state.snapshot_status(obj, now=1001, kind="positions")[0])

    def test_row_not_found_cannot_confirm_empty(self):
        obj = {"ts": 1000, "data_quality": "ok", "EXT": [{"reason": "row_not_found", "notional_u": 0}], "VAR": []}
        self.assertFalse(state.snapshot_status(obj, now=1001, kind="positions")[0])


class DayAccountingTests(unittest.TestCase):
    def advance(self, data, total, today="2026-10-22", valid=True):
        return state.advance_equity_days(data, total=total, today=today,
                                        observed_local=today + " 05:14:00", valid=valid)

    def test_first_observation_baseline(self):
        store, today, hist, changed = self.advance({"days": {}}, 100)
        self.assertEqual((today, hist, changed), (0, 0, True))
        self.assertEqual(store["days"]["2026-10-22"]["baseline_source"], "first_observation")

    def test_restart_carries_previous_close_and_keeps_history(self):
        before = {"days": {"2026-10-21": {"equity_0am": 200.97, "last_total": 200.69,
                  "close_total": 200.69, "day_profit": -0.28, "settled": True}}}
        saved = copy.deepcopy(before)
        after, today, hist, changed = self.advance(before, 195.14)
        self.assertEqual((today, hist, changed), (-5.55, -0.28, True))
        self.assertEqual(before, saved)
        self.assertEqual(after["days"]["2026-10-21"], saved["days"]["2026-10-21"])
        self.assertEqual(after["days"]["2026-10-22"]["equity_0am"], 200.69)

    def test_existing_historical_gap_not_backfilled(self):
        before = {"days": {"2026-10-20": {"day_profit": -0.28, "equity_0am": 200.97, "last_total": 200.69},
                  "2026-10-21": {"day_profit": 17.9, "equity_0am": 195.14, "last_total": 213.04},
                  "2026-10-22": {"day_profit": -97.33, "equity_0am": 213.04, "last_total": 115.71}}}
        after, today, hist, _ = self.advance(before, 115)
        self.assertEqual((today, hist), (-98.04, 17.62))
        self.assertEqual(after["days"]["2026-10-21"], before["days"]["2026-10-21"])

    def test_zero_balance_updates_and_zero_baseline_stays_zero(self):
        store = {"days": {"2026-10-22": {"equity_0am": 100, "last_total": 50, "day_profit": -50}}}
        after, today, _, _ = self.advance(store, 0)
        self.assertEqual(today, -100)
        self.assertEqual(after["days"]["2026-10-22"]["last_total"], 0)
        store = {"days": {"2026-10-22": {"equity_0am": 0, "last_total": 0, "day_profit": 0}}}
        after, today, _, _ = self.advance(store, 10)
        self.assertEqual(today, 10)
        self.assertEqual(after["days"]["2026-10-22"]["equity_0am"], 0)

    def test_zero_previous_close_is_not_discarded(self):
        store = {"days": {"2026-10-21": {"equity_0am": 100, "last_total": 0, "close_total": 0, "day_profit": -100}}}
        after, today, hist, _ = self.advance(store, 20)
        self.assertEqual((today, hist), (20, -100))
        self.assertEqual(after["days"]["2026-10-22"]["equity_0am"], 0)

    def test_stale_and_nonfinite_observation_do_not_write_or_settle(self):
        store = {"days": {"2026-10-21": {"last_total": 100, "day_profit": 5, "settled": False}}}
        for value, valid in ((80, False), (float("nan"), True)):
            after, _, _, changed = self.advance(store, value, valid=valid)
            self.assertFalse(changed)
            self.assertEqual(after, store)


class IdentityTests(unittest.TestCase):
    def test_exact_identity(self):
        self.assertTrue(state.process_identity_matches(identity(), identity()))

    def test_pid_reuse_and_same_filename_elsewhere_rejected(self):
        self.assertFalse(state.process_identity_matches(identity(), identity(created="new")))
        observed = identity(); observed["command_line"] = r'python "C:\elsewhere\BOT.py"'
        self.assertFalse(state.process_identity_matches(identity(), observed))

    def test_script_as_non_script_argument_rejected(self):
        observed = identity(); observed["command_line"] = r'python -c "C:\bot folder\BOT.py"'
        self.assertFalse(state.process_identity_matches(identity(), observed))

    def test_legacy_pid_and_unknown_process_rejected(self):
        self.assertFalse(state.process_identity_matches(501, identity()))
        self.assertFalse(state.process_identity_matches(identity(), None))

    def test_successful_command_is_insufficient(self):
        self.assertFalse(state.stop_confirmed(identity(), identity(), command_returncode=0))
        self.assertFalse(state.stop_confirmed(identity(), None, command_returncode=0))
        self.assertFalse(state.stop_confirmed(identity(), {"exists": False}, command_returncode=1))
        self.assertTrue(state.stop_confirmed(identity(), {"exists": False}, command_returncode=0))


class PanelIntegrationTests(unittest.TestCase):
    def model_method(self, name, **extras):
        model = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "PanelModel")
        node = next(node for node in model.body if isinstance(node, ast.FunctionDef) and node.name == name)
        env = {"_panel_state": state, "Optional": __import__('typing').Optional,
               "Tuple": __import__('typing').Tuple, "Dict": dict, "Any": object,
               "date": date, "datetime": datetime,
               "time": types.SimpleNamespace(time=lambda: 2000),
               "_safe_float": lambda value, default: default if value is None else float(value),
               "E_KEY_CANDIDATES": ["EXT"], "V_KEY_CANDIDATES": ["VAR"], "T_KEY_CANDIDATES": ["TOTAL"]}
        env.update(extras)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<PanelModel method AST only>", "exec"), env)
        return env[name]

    def test_actual_read_equity_preserves_stale_invalid(self):
        method = self.model_method("read_equity", _read_json=lambda _: {
            "ts": 1, "data_quality": "ok", "EXT": 60, "VAR": 40, "TOTAL": 100})
        result = method(types.SimpleNamespace(fp_equity="unused"))
        self.assertEqual(result[:3], (60, 40, 100))
        self.assertFalse(result[3])

    def test_actual_runtime_accepts_zero_without_live_file_io(self):
        method = self.model_method("_tick_runtime")
        model = types.SimpleNamespace(runtime_state={"today": date.today().isoformat(), "init_total": 100,
            "last_total": 30, "last_tick_ts": 1999, "today_run_sec": 10, "total_run_sec": 10}, _save_runtime=Mock())
        method(model, True, {"run_state": "running"}, 0)
        self.assertEqual(model.runtime_state["last_total"], 0)
        self.assertEqual(model.runtime_state["today_run_sec"], 11)

    def test_actual_runtime_does_not_count_stale_as_running(self):
        method = self.model_method("_tick_runtime")
        model = types.SimpleNamespace(runtime_state={"today": date.today().isoformat(), "init_total": 100,
            "last_total": 30, "last_tick_ts": 1999, "today_run_sec": 10, "total_run_sec": 10}, _save_runtime=Mock())
        method(model, False, {"run_state": "running"}, 0)
        self.assertEqual(model.runtime_state["last_total"], 30)
        self.assertEqual(model.runtime_state["today_run_sec"], 10)

    def test_actual_profit_method_does_not_save_stale_observation(self):
        method = self.model_method("_compute_equity_change_today_hist_local")
        store = {"days": {date.today().isoformat(): {"equity_0am": 100, "last_total": 30, "day_profit": -70}}}
        model = types.SimpleNamespace(_profit_daily_cache=store, _save_profit_daily_store=Mock())
        self.assertEqual(method(model, 0, False), (-70, 0))
        model._save_profit_daily_store.assert_not_called()

    def test_apply_compiles_full_panel_without_import(self):
        compile(PATCHED, "<panel compile only>", "exec")
        self.assertNotIn("_compute_realized_profit_today_hist_local", PATCHED)
        self.assertNotIn('eq_reason = "stdout:"', PATCHED)
        self.assertNotIn('pos_reason = "stdout:"', PATCHED)
        self.assertIn("今日权益变动：", PATCHED)

    def test_apply_rejects_drift_or_double_application(self):
        with self.assertRaises(ValueError):
            edits.apply(PATCHED)

    def test_nonzero_taskkill_keeps_handle_and_pid_file(self):
        bot, runner, _ = make_bot(command_rc=5)
        bot._query_identity = Mock(return_value=identity())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "out").mkdir()
            pid_file = root / "out/panel_bot_pid.txt"; pid_file.write_text(json.dumps(identity()))
            self.assertFalse(bot.stop(root))
            self.assertIsNotNone(bot.p)
            self.assertTrue(pid_file.exists())
            self.assertEqual(bot.last_stop_reason, "taskkill_failed:5")
            runner.assert_called_once()

    def test_zero_taskkill_but_process_still_alive_keeps_state(self):
        bot, runner, sleeps = make_bot()
        bot._query_identity = Mock(return_value=identity())
        self.assertFalse(bot.stop())
        self.assertIsNotNone(bot.p)
        self.assertEqual(len(sleeps), 3)

    def test_success_waits_for_absence_and_clears_only_matching_pid(self):
        bot, runner, _ = make_bot()
        bot._query_identity = Mock(side_effect=[identity(), identity(), {"exists": False}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "out").mkdir()
            pid_file = root / "out/panel_bot_pid.txt"; pid_file.write_text(json.dumps(identity()))
            self.assertTrue(bot.stop(root))
            self.assertIsNone(bot.p)
            self.assertFalse(pid_file.exists())

    def test_reused_pid_never_killed(self):
        bot, runner, _ = make_bot()
        bot._query_identity = Mock(return_value=identity(created="new"))
        self.assertFalse(bot.stop())
        runner.assert_not_called()

    def test_legacy_pid_file_never_killed_or_deleted(self):
        bot, runner, _ = make_bot(); bot.p = None; bot.identity = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "out").mkdir()
            pid_file = root / "out/panel_bot_pid.txt"; pid_file.write_text("501")
            self.assertFalse(bot.stop(root))
            runner.assert_not_called()
            self.assertEqual(pid_file.read_text(), "501")

    def test_process_query_failure_cannot_mean_exited(self):
        bot, runner, _ = make_bot()
        bot._query_identity = Mock(return_value=None)
        self.assertFalse(bot.stop())
        runner.assert_not_called()

    def test_query_uses_returncode_and_explicit_absence(self):
        bot, runner, _ = make_bot(command_rc=5)
        self.assertIsNone(bot._query_identity(501))
        runner.return_value = types.SimpleNamespace(returncode=0, stdout='{"exists": false}')
        self.assertEqual(bot._query_identity(501), {"exists": False})
        self.assertIn("ProcessId = 501", runner.call_args.args[0][-1])
        self.assertNotIn("taskkill", runner.call_args.args[0])

    def test_launch_records_identity_and_absolute_script_with_mock_process(self):
        bot, _, _ = make_bot(); bot.p = None; bot.identity = None
        env = bot.start.__func__.__globals__
        proc = Mock(pid=501); proc.poll.return_value = None
        env["subprocess"].Popen = Mock(return_value=proc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); script = str((root / "BOT.py").resolve())
            observed = identity(); observed["command_line"] = '"C:\\Python\\python.exe" "' + script + '" --cdp fake'
            bot._query_identity = Mock(return_value=observed)
            self.assertTrue(bot.start("BOT.py", "fake", root))
            stored = json.loads((root / "out/panel_bot_pid.txt").read_text(encoding="utf-8"))
            self.assertEqual(stored["script"], script)
            self.assertEqual(stored["created"], identity()["created"])
            self.assertEqual(env["subprocess"].Popen.call_args.args[0][1], script)

    def test_changed_pid_file_not_removed(self):
        bot, runner, _ = make_bot()
        bot._query_identity = Mock(side_effect=[identity(), {"exists": False}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "out").mkdir()
            pid_file = root / "out/panel_bot_pid.txt"; pid_file.write_text(json.dumps(identity(pid=888)))
            self.assertTrue(bot.stop(root))
            self.assertTrue(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
