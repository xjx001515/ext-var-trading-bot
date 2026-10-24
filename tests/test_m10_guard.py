"""M10 minimum-size rules and isolated executor rollback replay; no live actions."""
import ast
import copy
from pathlib import Path
import tempfile
import textwrap
import types
import unittest
from unittest.mock import Mock

from bot_core import m10_guard as guard
from bot_core.execution_confirmation import verify_position_change


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
BEFORE = (ROOT / 'repair_backups/m10_rollback_20261023/BOT5-2-414 .py').read_text(encoding='utf-8-sig')


def source_function(name, next_name):
    section = SOURCE[SOURCE.index('def ' + name + '('):SOURCE.index('def ' + next_name + '(')]
    namespace = {'Any': object}
    exec(compile(ast.parse(section), '<isolated pure M10 function>', 'exec'), namespace)
    return namespace[name]


CALC_STEP2 = source_function('_m10_calc_step2_close_u10', '_m10_calc_step3_reopen_u10')
_start = SOURCE.index("                                    step3_detail = {'reopen_u':")
_end = SOURCE.index('\n                    elif (req.module in', _start)
REOPEN_CODE = compile(ast.parse(textwrap.dedent(SOURCE[_start:_end])), '<M10 rollback and existing step3 only>', 'exec')


class GuardTests(unittest.TestCase):
    def test_recorded_q_minimum_prevents_step1(self):
        plan = guard.plan_step2_u(3.17, -31.26, 187.31, 0.90, CALC_STEP2)
        self.assertEqual(plan, 10)
        ok, reason, detail = guard.precheck_step2(plan, 187.31, min_qty=1000, price=0.1441)
        self.assertFalse(ok)
        self.assertEqual(reason, 'step2_below_min_trade_size')
        self.assertAlmostEqual(detail['min_u'], 144.1)

    def test_same_q_plan_without_known_minimum_is_allowed(self):
        plan = guard.plan_step2_u(3.17, -31.26, 187.31, 0.90, CALC_STEP2)
        self.assertEqual(guard.precheck_step2(plan, 187.31)[:2], (True, 'ok'))

    def test_under_10_is_rejected_without_rounding_up(self):
        for amount in (0, 9, 9.99):
            self.assertEqual(guard.precheck_step2(amount, 200)[:2], (False, 'step2_floor10_lt_10'))

    def test_small_remainder_is_rejected(self):
        self.assertEqual(guard.precheck_step2(150, 200, min_qty=100, price=0.8)[:2],
                         (False, 'step2_remainder_below_min_trade_size'))

    def test_parse_minimum_quantity_and_exact_symbol(self):
        self.assertEqual(guard.parse_min_trade_size('Your order size is below the minimum trade size of 1,000 Q.'), (1000, 'Q'))
        self.assertEqual(guard.parse_min_trade_size('Minimum Trade Size of 1,234.5 rfq'), (1234.5, 'RFQ'))
        self.assertEqual(guard.parse_min_trade_size('unrelated message'), (0, ''))

    def test_rollback_amount_contract(self):
        for pre, ratio, expected in ((123.17, 1, 120), (123.17, 0.9, 110), (20, 1, 40), (0, 1, 0)):
            self.assertEqual(guard.rollback_restore_u(pre, ratio), expected)

    def test_failed_amount_persistence_and_four_hour_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'memo.json')
            store = guard.MinSizeStore(path)
            store.note_failed('EXT', 'Q', 20, now=1000)
            saved = guard.MinSizeStore(path)
            self.assertEqual(saved.failed_u('EXT', 'Q', now=1001), 20)
            for amount in (10, 20):
                self.assertEqual(guard.precheck_step2(amount, 500, failed_u=saved.failed_u('EXT', 'Q', now=1001))[:2],
                                 (False, 'step2_le_recent_failed_amount'))
            self.assertTrue(guard.precheck_step2(30, 500, failed_u=20)[0])
            self.assertEqual(saved.failed_u('EXT', 'Q', now=1000 + 4 * 3600), 20)
            self.assertEqual(saved.failed_u('EXT', 'Q', now=1001 + 4 * 3600), 0)
            saved.clear_failed('EXT', 'Q')
            self.assertEqual(guard.MinSizeStore(path).failed_u('EXT', 'Q', now=1001), 0)

    def test_known_override_and_learned_minimum_use_only_temp_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'memo.json')
            store = guard.MinSizeStore(path)
            self.assertEqual(store.min_qty('EXT', 'Q'), 1000)
            self.assertEqual(store.min_qty('EXT', 'Q', 'EXT:Q=1200'), 1200)
            store.learn_min_qty('VAR', 'ABC', 5, source='ui_hint')
            self.assertEqual(guard.MinSizeStore(path).min_qty('VAR', 'ABC'), 5)
            self.assertEqual(store.min_qty('EXT', 'RFQ'), 0)


class HintPage:
    def __init__(self, text='minimum trade size of 1,000 Q', error=None):
        self.text, self.error, self.calls = text, error, []

    def locator(self, selector):
        self.calls.append(selector)
        if selector != 'text=/minimum trade size/i':
            raise AssertionError(selector)
        return self

    @property
    def first(self):
        return self

    def inner_text(self, *, timeout):
        self.calls.append(timeout)
        if self.error:
            raise self.error
        return self.text


class RollbackIntegrationTests(unittest.TestCase):
    def replay(self, *, ok1=True, ok2=False, close_u=10, pre=123.17, fill=1,
               win_plat='VAR', hint='minimum trade size of 1,000 Q',
               hint_error=None, outcomes=(True,), store_fail=False, position_rows=None,
               pending_scope=None, submit_error=None, definitive_reject=False):
        store = Mock(spec=guard.MinSizeStore)
        if store_fail:
            store.note_failed.side_effect = OSError('memo unavailable')
        ext_page, var_page = HintPage(hint, hint_error), HintPage(hint, hint_error)
        calls, logs, sequence = [], [], iter(outcomes)
        side = 'LONG' if win_plat == 'VAR' else 'SHORT'
        remaining = max(0.0, pre * (1 - fill))
        current = {'position_state': 'FOUND' if remaining else 'CONFIRMED_EMPTY',
                   'side': side if remaining else 'NONE', 'qty_abs': remaining,
                   'notional_u': remaining, 'mark_px': 1.0, 'has_position': bool(remaining)}
        rows = iter(position_rows) if position_rows is not None else None
        last_row = copy.deepcopy(current)
        def probe(platform):
            nonlocal last_row
            if rows is not None:
                last_row = next(rows, last_row)
                return copy.deepcopy(last_row)
            return copy.deepcopy(current)
        def submit(exe, order, **kwargs):
            calls.append(copy.deepcopy(vars(order)))
            if submit_error:
                raise submit_error
            result = next(sequence, False)
            if definitive_reject:
                order.meta = {'definitive_reject': True}
            if result and rows is None and not definitive_reject:
                current.update(position_state='FOUND', side=side, has_position=True,
                               qty_abs=current['qty_abs'] + order.usd, notional_u=current['notional_u'] + order.usd)
            return result
        env = {
            'ok1': ok1, 'ok2': ok2, 'ok3': False, 'close_u': close_u,
            '_pre_win_notional': pre, '_step1_fill_ratio': fill,
            'win_plat': win_plat, 'loss_plat': 'EXT' if win_plat == 'VAR' else 'VAR',
            'pre_ext': {'side': 'SHORT'}, 'pre_var': {'side': 'LONG'}, 'coin0': 'Q',
            'step3_u': 210.0, 'step3_side': 'LONG', 'step3_loss_remain_u_real': 187.31,
            'step3_loss_remain_source': 'post_step2_probe', 'step3_loss_probe': {},
            '_M10_GUARD_STORE': store, '_m10_guard': guard,
            'copy': copy, '_safety_verify_position_change': verify_position_change,
            '_safety_pending_scope': Mock(return_value=pending_scope or {}),
            '_safety_resolve_submission': Mock(return_value=True),
            '_m10_step3_track_unconfirmed': Mock(return_value=win_plat + '|Q'),
            '_a1_norm_side': lambda s: str(s).upper(),
            'ext_page': ext_page, 'var_page': var_page, 'ext_exe': object(), 'var_exe': object(),
            'time': types.SimpleNamespace(time=lambda: 2000),
            '_ExecOrder': lambda **kwargs: types.SimpleNamespace(**kwargs),
            '_exec_attach_order_ctx': Mock(), '_exe_open_position_safe': submit,
            '_side_map_long_short_to_buy_sell': lambda s: 'buy' if s == 'LONG' else 'sell',
            '_probe_read_leg': probe, 'req': {}, 'steps': [], '_m10_snapshot_ctx': {},
            'safe_log': lambda callback: logs.append(callback()), '_log_exc': Mock(), '_tp_log': Mock(),
            '_record_swallowed_exception': Mock(), '_m10_emit_step_row': Mock(),
            '_m10_reopen_clear': Mock(), '_m10_reopen_mark_pending': Mock(),
            '_legacy_deleted_after_failure': Mock(), '_m10_finish_profile': Mock(),
            '_exec_goto_coin_ext': Mock(return_value=True), '_exec_goto_coin_var': Mock(return_value=True),
            '_pw_wait_ms': Mock(),
        }
        exec(REOPEN_CODE, env)
        return env, calls, logs, store, ext_page, var_page

    def test_failure_restores_original_side_and_120_not_210(self):
        env, calls, logs, store, ext, var = self.replay()
        self.assertEqual([(r['platform'], r['side'], r['usd']) for r in calls], [('var', 'buy', 120)])
        self.assertFalse(env['ok'])  # rollback does not turn the failed take-profit into success
        self.assertTrue(env['reason'].startswith('tp_koujing_fail:step1='))
        self.assertTrue(env['reason'].endswith('rollback=restored'))
        self.assertEqual(env['step3_detail']['planned_step3_u'], 210)
        self.assertEqual(env['step3_detail']['rule'], 'm10_step2_fail_rollback_restore_win_leg')
        self.assertTrue(any('[M10][STEP2_FAIL_ROLLBACK]' in line for line in logs))
        store.note_failed.assert_called_once_with('EXT', 'Q', 10)
        store.learn_min_qty.assert_called_once_with('EXT', 'Q', 1000, source='ui_hint')
        self.assertEqual(ext.calls, ['text=/minimum trade size/i', 800])
        self.assertEqual(var.calls, [])
        env['_legacy_deleted_after_failure'].assert_not_called()

    def test_partial_fill_restores_only_closed_portion(self):
        env, calls, *_ = self.replay(fill=0.9)
        self.assertEqual(calls[0]['usd'], 110)
        self.assertLessEqual(calls[0]['usd'], 123.17 * 0.9)

    def test_ext_winner_restores_short_and_reads_var_hint(self):
        env, calls, _, store, ext, var = self.replay(win_plat='EXT')
        self.assertEqual((calls[0]['platform'], calls[0]['side']), ('ext', 'sell'))
        store.learn_min_qty.assert_called_once_with('VAR', 'Q', 1000, source='ui_hint')
        self.assertEqual(ext.calls, [])
        self.assertEqual(var.calls[-1], 800)

    def test_floor_wait_still_rolls_back_but_does_not_memo_unsubmitted_amount(self):
        env, calls, _, store, *_ = self.replay(close_u=0)
        self.assertTrue(env['_m10_rb_mode'])
        self.assertEqual(calls[0]['usd'], 120)
        store.note_failed.assert_not_called()

    def test_hint_and_store_failures_do_not_block_rollback(self):
        env, calls, _, store, *_ = self.replay(hint_error=TimeoutError('no hint'), store_fail=True)
        self.assertEqual(calls[0]['usd'], 120)
        self.assertTrue(env['reason'].endswith('rollback=restored'))
        store.learn_min_qty.assert_not_called()

    def test_wrong_symbol_is_not_learned(self):
        _, calls, _, store, *_ = self.replay(hint='minimum trade size of 1,000 RFQ')
        self.assertEqual(calls[0]['usd'], 120)
        store.learn_min_qty.assert_not_called()

    def test_existing_second_attempt_is_reused(self):
        env, calls, *_ = self.replay(outcomes=(False, True))
        self.assertEqual([r['usd'] for r in calls], [120, 120])
        self.assertIn('attempt2', env['step3_detail'])
        self.assertTrue(env['reason'].endswith('rollback=restored'))

    def test_both_restore_attempts_fail_and_prefix_is_preserved(self):
        env, calls, *_ = self.replay(outcomes=(False, False))
        self.assertEqual(len(calls), 2)
        self.assertFalse(env['ok'])
        self.assertTrue(env['reason'].startswith('tp_koujing_fail:step1='))
        self.assertTrue(env['reason'].endswith('rollback=restore_fail'))

    def test_successful_step2_keeps_normal_step3_and_clears_memo(self):
        env, calls, _, store, *_ = self.replay(ok2=True)
        self.assertFalse(env['_m10_rb_mode'])
        self.assertEqual(calls[0]['usd'], 210)
        self.assertEqual(env['reason'], 'ok')
        store.clear_failed.assert_called_once_with('EXT', 'Q')
        store.note_failed.assert_not_called()

    def test_failed_step1_never_activates_rollback(self):
        env, calls, _, store, *_ = self.replay(ok1=False)
        self.assertFalse(env['_m10_rb_mode'])
        self.assertEqual(calls, [])
        store.note_failed.assert_not_called()

    def test_tiny_or_zero_position_never_reopens_more_than_closed(self):
        for pre in (0, 20, 39.99):
            with self.subTest(pre=pre):
                env, calls, *_ = self.replay(pre=pre)
                self.assertEqual(calls, [])
                self.assertEqual(env['step3_u'], 0)
                self.assertTrue(env['reason'].endswith('rollback=restore_fail'))

    def test_step1_failure_record_has_initialized_clear_report(self):
        start = SOURCE.index('                            _step1_probe_detail = {}')
        end = SOURCE.index('                            if not ok1 and page_win', start)
        namespace = {}
        exec(compile(textwrap.dedent(SOURCE[start:end]), '<step1 initialization>', 'exec'), namespace)
        self.assertEqual(namespace['_step1_clear_rep'], {})
        start = SOURCE.index('                            if not ok1:\n                                try:\n                                    _step1_id', end)
        end = SOURCE.index('                            else:\n                                _step1_mode', start)
        emit = Mock(); swallowed = Mock()
        namespace.update(ok1=False, steps=[], win_plat='VAR', pre_ext={'side':'SHORT'}, pre_var={'side':'LONG'},
                         pre_var_n=123.17, pre_ext_n=187.31, _ok1_path='failed', _step1_t0=1000,
                         _m10_emit_step_row=emit, _record_swallowed_exception=swallowed, _m10_finish_profile=Mock())
        exec(compile(textwrap.dedent(SOURCE[start:end]), '<step1 failure record>', 'exec'), namespace)
        emit.assert_called_once()
        self.assertFalse(emit.call_args.kwargs['detail']['did_clear_grid'])
        swallowed.assert_not_called()

    def test_two_existing_skip_branches_are_unchanged(self):
        def skipped_section(source):
            start = source.index('                                if loss_notional_now <= 0.0:')
            end = source.index('                                else:\n                                    _step1_fill_ratio', start)
            return source[start:end]
        self.assertEqual(skipped_section(SOURCE), skipped_section(BEFORE))


if __name__ == '__main__':
    unittest.main()
