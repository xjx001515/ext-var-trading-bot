"""Quantity-confirmed M10 reopen and durable pending lifecycle, with no live trading."""
import ast
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock

import test_m10_guard as m10_tests
from ast_support import ROOT, load_definitions, parsed
from bot_core.execution_confirmation import (
    verify_position_change, verify_pending_settlement, pending_blocks, apply_confirmation_fields,
)


def row(qty, side='LONG', price=1):
    return {'position_state': 'FOUND' if qty else 'CONFIRMED_EMPTY', 'has_position': bool(qty),
            'side': side if qty else 'NONE', 'qty_abs': qty, 'notional_u': qty * price, 'mark_px': price}


class Step3ConfirmationTests(unittest.TestCase):
    def replay(self, **kwargs):
        return m10_tests.RollbackIntegrationTests().replay(**kwargs)

    def test_submitted_without_delta_is_pending_and_never_retried_or_cleared(self):
        env, calls, *_ = self.replay(ok2=True, position_rows=[row(0), row(0)], outcomes=(True, True))
        self.assertEqual(len(calls), 1)
        self.assertFalse(env['ok3'])
        self.assertEqual(env['step3_detail']['attempt1']['state'], 'pending')
        self.assertIn('confirm', env['step3_detail']['attempt1'])
        env['_m10_reopen_clear'].assert_not_called()
        env['_exec_goto_coin_var'].assert_not_called()
        env['_legacy_deleted_after_failure'].assert_not_called()
        pending = env['_m10_reopen_mark_pending'].call_args.kwargs
        self.assertEqual((pending['state'], pending['pending_kind'], pending['blocking'], pending['ttl_sec']),
                         ('step3_unconfirmed', 'step3_unconfirmed', True, 180))
        env['_m10_step3_track_unconfirmed'].assert_called_once()
        self.assertTrue(env['reason'].startswith('tp_koujing_fail:step1='))
        self.assertIn(' step3=unconfirmed', env['reason'])

    def test_full_quantity_delta_confirms_and_clears(self):
        env, calls, *_ = self.replay(ok2=True, position_rows=[row(12), row(222)])
        self.assertEqual(len(calls), 1)
        self.assertTrue(env['ok3'])
        self.assertEqual(env['step3_detail']['attempt1']['state'], 'confirmed')
        env['_m10_reopen_clear'].assert_called_once()
        self.assertTrue(env['_m10_reopen_clear'].call_args.kwargs['healed'])
        env['_m10_reopen_mark_pending'].assert_not_called()

    def test_residual_position_cannot_confirm_rollback(self):
        env, calls, *_ = self.replay(fill=0.9, position_rows=[row(12), row(12)])
        self.assertEqual(calls[0]['usd'], 110)
        self.assertEqual(len(calls), 1)
        self.assertEqual(env['step3_detail']['attempt1']['confirm']['actual_delta_qty'], 0)
        self.assertIn(' step3=unconfirmed', env['reason'])
        self.assertTrue(env['reason'].endswith(' rollback=unconfirmed'))
        env['_m10_reopen_clear'].assert_not_called()

    def test_price_only_change_and_large_residual_do_not_confirm(self):
        env, calls, *_ = self.replay(position_rows=[row(60), row(60, price=4)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(env['_m10_step3_state'], 'pending')

    def test_clear_failure_then_confirmed_second_attempt(self):
        env, calls, *_ = self.replay(ok2=True, outcomes=(False, True), position_rows=[row(12), row(12), row(222)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(env['step3_detail']['attempt1']['state'], 'failed')
        self.assertEqual(env['step3_detail']['attempt2']['state'], 'confirmed')
        self.assertTrue(env['ok3'])
        env['_m10_reopen_clear'].assert_called_once()

    def test_false_return_with_pending_journal_does_not_retry(self):
        env, calls, *_ = self.replay(outcomes=(False,), position_rows=[row(0), row(0)],
                                    pending_scope={'state': 'PENDING_CONFIRM'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(env['_m10_step3_state'], 'pending')

    def test_exception_after_dispatch_is_pending(self):
        env, calls, *_ = self.replay(submit_error=TimeoutError('receipt missing'), position_rows=[row(0), row(0)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(env['_m10_step3_state'], 'pending')

    def test_partial_delta_is_pending_even_with_false_submit_return(self):
        env, calls, *_ = self.replay(outcomes=(False,), position_rows=[row(12), row(20)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(env['step3_detail']['attempt1']['confirm']['reason'], 'partial_quantity_change_pending')
        self.assertEqual(env['_m10_step3_state'], 'pending')

    def test_definitive_rejection_is_failed_not_confirmed(self):
        env, calls, *_ = self.replay(outcomes=(True, True), definitive_reject=True, position_rows=[row(0), row(0), row(0)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(env['_m10_step3_state'], 'failed')
        env['_m10_reopen_clear'].assert_not_called()

    def test_second_attempt_unconfirmed_has_no_third_attempt(self):
        env, calls, *_ = self.replay(outcomes=(False, True), position_rows=[row(0), row(0), row(0)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(env['_m10_step3_state'], 'pending')
        self.assertIn('rollback=unconfirmed', env['reason'])

    def test_unknown_pre_and_wrong_post_side_remain_unconfirmed(self):
        for rows in ([{}, row(120)], [row(0), row(120, side='SHORT')]):
            env, calls, *_ = self.replay(position_rows=rows)
            self.assertEqual(len(calls), 1)
            self.assertEqual(env['_m10_step3_state'], 'pending')


class PendingLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clock = [2000.0]
        env = {'OUT_DIR': temp.name, 'os': os, 'copy': copy, 'time': types.SimpleNamespace(time=lambda: self.clock[0]),
               '_SYNC_PW_WORKER_LOCK': threading.RLock(), '_M10_REOPEN_PENDING': {}, '_M10_REOPEN_CLEAR_TOMBSTONE': {},
               '_safety_pending_blocks': pending_blocks, '_safety_verify_pending_settlement': verify_pending_settlement,
               '_selftest_context_active': lambda: False, '_pin': Mock(), 'safe_log': Mock(), 'now_ts': lambda: self.clock[0],
               '_log_exc': Mock(), '_record_swallowed_exception': Mock(),
               '_req_is_safe_reduce': lambda r: r.get('action_type') in ('close', 'reduce'),
               '_get_snapshot_coin_leg': lambda s, c, p: s.get('positions', {}).get(p, {}).get(c, {}),
               '_SNAPSHOT_COMMIT_LOCK': threading.RLock(), '_pw_runtime_gen_value': lambda: 3,
               '_snapshot_positions_verified': lambda s: s.get('data_quality') == 'ok',
               '_m2_make_fail_snapshot': lambda r, **k: {'data_quality':'fail', 'reason':r},
               '_audit_observe_snapshot': Mock(), '__file__':'not_imported',
               '_m10_reopen_force_submit_from_pending': Mock(side_effect=AssertionError('must not resubmit'))}
        def write(path, obj, **kwargs):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(obj), encoding='utf-8')
            return True
        env['safe_write_json'] = write
        names = {'_safety_submission_records', '_safety_submission_save', '_safety_pending_scope',
                 '_safety_reserve_submission', '_safety_resolve_submission', '_m10_step3_track_unconfirmed',
                 '_m10_step3_restore_unconfirmed', '_m10_step3_reconcile_unconfirmed',
                 '_m10_reopen_mark_pending', '_m10_reopen_clear', '_m10_reopen_pending_is_expired',
                 '_m10_reopen_pending_should_block', '_m10_recover_reconcile_no_request'}
        names.update({'_m2_accept_snapshot', '_m10_step3_reconcile_scan'})
        self.env = load_definitions(names, env)

    def track(self):
        self.env['_m10_step3_track_unconfirmed']('VAR', 'Q', 'LONG', 120, row(12))
        self.env['_m10_step3_restore_unconfirmed']('Q')

    def snap(self, qty, **changes):
        return dict({'ts': self.clock[0] + 1, 'data_quality': 'ok', 'positions': {'VAR': {'Q': row(qty)}}}, **changes)

    def test_shared_journal_survives_restart_without_overwriting_baseline(self):
        self.track()
        self.env['_m10_step3_track_unconfirmed']('VAR', 'Q', 'LONG', 120, row(60))
        original = self.env['_safety_submission_records']()['VAR|Q']
        self.assertEqual(original['pre_position']['qty_abs'], 12)
        self.assertEqual(original['m10_step3_confirmation']['pre_position']['qty_abs'], 12)
        self.env.pop('_SAFETY_SUBMISSION_RECORDS')
        self.env['_M10_REOPEN_PENDING'].clear()
        self.env['_m10_step3_restore_unconfirmed']('Q')
        self.assertEqual(self.env['_M10_REOPEN_PENDING']['Q']['pending_kind'], 'step3_unconfirmed')
        self.assertEqual(self.env['_M10_REOPEN_PENDING']['Q']['fail_count'], 0)

    def test_next_round_requires_quantity_delta_and_never_auto_reopens(self):
        self.track()
        handled, why, _ = self.env['_m10_recover_reconcile_no_request']('Q', snapshot=self.snap(60))
        self.assertFalse(handled)  # 60U absolute holding exceeds old 35% rule, delta still too small
        self.assertIn('partial_quantity_change_pending', why)
        self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])
        self.env['_m10_reopen_force_submit_from_pending'].assert_not_called()
        handled, why, _ = self.env['_m10_recover_reconcile_no_request']('Q', snapshot=self.snap(132))
        self.assertEqual((handled, why), (True, 'step3_confirmed'))
        self.assertNotIn('Q', self.env['_M10_REOPEN_PENDING'])
        self.assertEqual(self.env['_safety_submission_records']()['VAR|Q']['state'], 'CONFIRMED_SUCCESS')
        self.env['_m10_step3_restore_unconfirmed']('Q')
        self.assertNotIn('Q', self.env['_M10_REOPEN_PENDING'])

    def test_ttl_does_not_resolve_or_allow_duplicate_open(self):
        self.track(); self.clock[0] += 1000
        rec = self.env['_M10_REOPEN_PENDING']['Q']
        self.assertEqual(rec['ttl_sec'], 180)
        self.assertFalse(self.env['_m10_reopen_pending_is_expired'](rec))
        blocked, _, _ = self.env['_m10_reopen_pending_should_block']('Q', mod='M10', action_type='koujing_a_3step', snapshot=self.snap(12))
        self.assertTrue(blocked)
        self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])

    def test_stale_or_failed_snapshot_cannot_heal(self):
        self.track()
        for snap in (self.snap(132, ts=1000), self.snap(132, data_quality='fail')):
            handled, _, _ = self.env['_m10_recover_reconcile_no_request']('Q', snapshot=snap)
            self.assertFalse(handled)
            self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])

    def test_recent_clear_tombstone_cannot_hide_new_unconfirmed_order(self):
        self.env['_m10_reopen_mark_pending']('Q', platform='VAR', side='LONG', amount_u=120)
        self.env['_m10_reopen_clear']('Q', reason='previous_order_done')
        self.track()
        self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])
        self.assertEqual(self.env['_M10_REOPEN_PENDING']['Q']['last_fail_reason'], '')

    def test_other_pending_kind_keeps_existing_expiry_behavior(self):
        self.assertTrue(self.env['_m10_reopen_pending_is_expired']({'pending_kind':'step3_reopen', 'ts':1000, 'ttl_sec':180}))

    def test_accepted_scan_confirms_even_without_any_new_request(self):
        self.track()
        self.env['_m2_accept_snapshot'](self.snap(12), 3)
        self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])
        self.env['_m2_accept_snapshot'](self.snap(132), 3)
        self.assertNotIn('Q', self.env['_M10_REOPEN_PENDING'])
        self.env['_m10_reopen_force_submit_from_pending'].assert_not_called()

    def test_rejected_worker_generation_does_not_confirm(self):
        self.track()
        result = self.env['_m2_accept_snapshot'](self.snap(132), 2)
        self.assertEqual(result['data_quality'], 'fail')
        self.assertIn('Q', self.env['_M10_REOPEN_PENDING'])


class ResultRecordTests(unittest.TestCase):
    def test_parent_pending_is_not_failed_and_reason_prefix_is_kept(self):
        tree = parsed(str(ROOT / 'BOT5-2-414 .py'))
        candidates = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.body
                      and isinstance(n.body[0], ast.If) and "'M10'" in ast.unparse(n.body[0].test)
                      and 'm10_step_rows' in ast.unparse(n.body[0].test)]
        self.assertEqual(len(candidates), 1)
        reason = 'tp_koujing_fail:step1=True step2=True step3=False step3=unconfirmed'
        env = {'mod':'M10', 'act':'koujing_a_3step', 'reason':reason,
               '_rr_exec':{'ok':False, 'req':{'meta':{}}, 'submit_failed':True},
               'm10_step_rows':[{'ok':False, 'pending_confirm':True, 'step_no':3,
                                'req':{'meta':{'m10_step_no':3}}, 'detail':{'state':'pending'}}],
               '_safety_apply_confirmation_fields':apply_confirmation_fields, '_record_swallowed_exception':Mock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[candidates[0]],type_ignores=[])), '<M10 result aggregation>', 'exec'), env)
        result = env['_rr_exec']
        self.assertTrue(result['pending_confirm'])
        self.assertFalse(result['submit_failed'])
        self.assertFalse(result['confirmed_fail'])
        self.assertEqual(result['reason'], reason)
        self.assertNotEqual(result.get('fail_step'), '3')
        env['_record_swallowed_exception'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
