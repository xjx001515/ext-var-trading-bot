"""Replay the TIA verification loop with fake IO; never import the application."""
import copy
import json
import os
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ast_support import ROOT, load_definitions
from bot_core import postcheck_lifecycle as lifecycle
from bot_core.execution_confirmation import (
    pending_blocks, scoped_position, verify_pending_settlement, verify_scoped_change,
)


def record(**changes):
    return dict(state='PENDING_CONFIRM', platform='EXT', coin='TIA', side='SHORT',
                kind='open', ts=1000.0, intent_usd=260.0,
                pre_position={'position_state': 'CONFIRMED_EMPTY'}, **changes)


def scan(ts, *, row=None, **changes):
    return dict(ts=ts, data_quality='ok', scan_scope='full',
                positions={'EXT': {'TIA': row if row is not None else {
                    'position_state': 'CONFIRMED_EMPTY', 'side': 'NONE', 'qty_abs': 0,
                    'reason': 'empty_positions_view'}}}, **changes)


class FakeTime:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 500.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.wall += seconds
        self.mono += seconds


def request(**changes):
    values = dict(module='B1', coin='TIA', platform='EXT', side='SHORT',
                  action_type='open', amount_usd=260, params={}, meta={'req_id': 'new-request'})
    values.update(changes)
    return SimpleNamespace(**values)


FUNCTIONS = {
    '_postcheck_pending_verify_payload', '_postcheck_pending_verify_active',
    '_postcheck_pending_verify_due', '_postcheck_pending_verify_mark',
    '_postcheck_pending_verify_bump', '_postcheck_pending_verify_clear',
    '_postcheck_verify_budget', '_postcheck_defer_to_scan',
    '_postcheck_reconcile_unconfirmed_scan', '_safety_submission_records',
    '_safety_submission_save', '_safety_pending_scope', '_safety_reserve_submission',
    '_safety_resolve_submission', '_exec_clear_pending', '_postcheck_validate_effect_v2',
}


def namespace(directory):
    clock = FakeTime()
    def write(path, payload, **kwargs):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload), encoding='utf-8')
        return True
    env = {'time': clock, 'copy': copy, 'os': os, 'OUT_DIR': directory,
           '_postcheck_lifecycle': lifecycle, '_SYNC_PW_WORKER_LOCK': threading.RLock(),
           '_safety_pending_blocks': pending_blocks, '_safety_scoped_position': scoped_position,
           '_safety_verify_pending_settlement': verify_pending_settlement,
           '_safety_verify_scoped_change': verify_scoped_change,
           '_req_get_dict': lambda r: r if isinstance(r, dict) else vars(r),
           '_postcheck_req_action_lower': lambda r: (r if isinstance(r, dict) else vars(r))['action_type'],
           '_postcheck_req_amount_u': lambda r: (r if isinstance(r, dict) else vars(r)).get('amount_usd', 0),
           '_a1_norm_side': lambda s: {'SELL': 'SHORT', 'BUY': 'LONG'}.get(str(s).upper(), str(s).upper()),
           'safe_write_json': write, '_record_swallowed_exception': Mock(),
           '_req_exec_once_mark': Mock(), 'safe_log': lambda fn: env['logs'].append(fn()),
           'now_ts': clock.time, 'logs': [], '_EXEC_PENDING': {}, '_EXEC_PENDING_INFO': {}}
    load_definitions(FUNCTIONS, env)
    return env, clock


def install_actual_verification_loop(env, clock):
    source = (ROOT / 'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
    begin = source.index('                        try:\n                            if _postcheck_pending_verify_active(req0):')
    end = source.index('                        _snap_pretrade = None', begin)
    block = textwrap.dedent(source[begin:end])
    driver = ('def drive(pending, snap):\n'
              '    passes = 0\n'
              '    while pending:\n'
              '        passes += 1\n'
              '        if passes > 100: raise AssertionError("round failed to finish")\n'
              '        req0 = pending.pop(0)\n' + textwrap.indent(block, '        ') +
              '        executions.append(req0)\n'
              '    return passes\n')
    def read(*args, **kwargs):
        clock.sleep(3)
        return scan(clock.time())
    def scoped(s, c):
        row = s['positions']['EXT'][c]
        return {'ext_position_state': row['position_state'], 'ext_side': row.get('side', 'NONE'),
                'ext_qty': row.get('qty_abs', 0), 'ext_notional_u': row.get('notional_u', 0),
                'ext_reason': row.get('reason', 'ok')}
    env.update(cdp='fake://no-network', fp_yunxing='unused', fp_baocuo='unused',
               ADAPTER=SimpleNamespace(on_snapshot=Mock()), pending=[], executions=[], exec_res=[],
               _postcheck_direct_read_target_coin_snapshot=Mock(side_effect=read),
               _postcheck_extract_coin_state=scoped, _ORIG_TIME_SLEEP=clock.sleep,
               POSTCHECK_VERIFY_RETRY_DELAY_SEC=6.0, POSTCHECK_VERIFY_FINAL_DELAY_SEC=8.0,
               _round_req_terminal=Mock(), _state_commit_confirmed_success=Mock(return_value={}),
               _state_commit_postcheck_fail=Mock(return_value={}), _log_exc=Mock(), _append_txt=Mock())
    exec(compile(driver, '<actual verification loop only>', 'exec'), env)


class BudgetTests(unittest.TestCase):
    def test_zero_count_exhausts_even_if_result_is_uncertain(self):
        self.assertTrue(lifecycle.verification_budget(
            {'created_ts': 1000, 'verify_retry_left': 0}, now=1001)['exhausted'])

    def test_deadline_exhausts_with_attempts_remaining(self):
        value = lifecycle.verification_budget({'created_ts': 1000, 'verify_retry_left': 8}, now=1120)
        self.assertEqual(value['reason'], 'deadline_exhausted')

    def test_wall_clock_rollback_cannot_extend_monotonic_budget(self):
        value = lifecycle.verification_budget({'created_ts': 1000, 'created_monotonic': 100,
            'verify_retry_left': 8}, now=900, monotonic=221)
        self.assertTrue(value['exhausted'])

    def test_missing_budget_fails_closed_without_loop(self):
        self.assertTrue(lifecycle.verification_budget({}, now=1000)['exhausted'])

    def test_repeated_mark_preserves_original_start_and_count(self):
        with tempfile.TemporaryDirectory() as directory:
            env, clock = namespace(directory)
            req = request()
            first = env['_postcheck_pending_verify_mark'](req, verify_retry_left=8)
            env['_postcheck_pending_verify_bump'](req)
            clock.sleep(100)
            last = env['_postcheck_pending_verify_mark'](req, verify_retry_left=999)
            self.assertEqual(last['created_ts'], first['created_ts'])
            self.assertEqual(last['created_monotonic'], first['created_monotonic'])
            self.assertEqual(last['verify_retry_left'], 7)
            self.assertEqual(env['_postcheck_verify_budget'](req)['seconds_left'], 20)


class EmptyEvidenceTests(unittest.TestCase):
    def observe(self, r, ts, **kwargs):
        return lifecycle.observe_pending(r, scan(ts, **kwargs), scan_id=str(ts), now=ts)

    def test_three_distinct_fresh_empty_scans_release_legacy_tia(self):
        r = record()
        r['pre_position'] = {'position_state': 'UNAVAILABLE'}
        for ts in (1120, 1150):
            r, outcome = self.observe(r, ts)
            self.assertTrue(pending_blocks(r))
        r, outcome = self.observe(r, 1180)
        self.assertEqual(outcome, 'not_filled')
        self.assertEqual(r['state'], 'CONFIRMED_NOT_FILLED')
        self.assertFalse(pending_blocks(r))
        self.assertTrue(r['evidence']['inference'])
        self.assertFalse(r['evidence']['exchange_rejection_confirmed'])

    def test_repeated_scan_id_is_only_one_observation(self):
        r = record()
        for ts in (1120, 1150, 1180):
            r, _ = lifecycle.observe_pending(r, scan(ts), scan_id='same-round', now=ts)
        self.assertEqual(r['empty_scan_evidence']['count'], 1)
        self.assertTrue(pending_blocks(r))

    def test_stale_timestamp_and_future_snapshot_do_not_count(self):
        for ts, now in ((999, 1120), (1001, 1300), (1200, 1150)):
            r, _ = lifecycle.observe_pending(record(), scan(ts), scan_id=str(now), now=now)
            self.assertEqual(r['empty_scan_evidence']['count'], 0)

    def test_fallback_local_cached_and_failed_scans_reset_streak(self):
        for extra in ({'data_quality': 'fail'}, {'dq_fallback': True},
                      {'scan_scope': 'local'}, {'meta': {'cache_hit': True}},
                      {'meta': {'postcheck_direct_read': True}}):
            r, _ = self.observe(record(), 1120)
            s = scan(1150)
            s.update(extra)
            r, _ = lifecycle.observe_pending(r, s, scan_id='bad', now=1150)
            self.assertEqual(r['empty_scan_evidence']['count'], 0)
            r, _ = self.observe(r, 1180)
            self.assertTrue(pending_blocks(r))

    def test_missing_row_without_explicit_empty_is_unknown(self):
        r = record()
        for ts in (1120, 1150, 1180):
            s = scan(ts)
            s['positions']['EXT'] = {}
            r, _ = lifecycle.observe_pending(r, s, scan_id=str(ts), now=ts)
        self.assertTrue(pending_blocks(r))
        self.assertEqual(r['empty_scan_evidence']['count'], 0)

    def test_explicit_empty_platform_can_prove_absent_coin(self):
        r = record()
        for ts in (1120, 1150, 1180):
            s = scan(ts)
            s['positions']['EXT'] = {}
            s['raw'] = {'platform_empty_detail_map': {'EXT': {'confirmed_empty': True}}}
            r, _ = lifecycle.observe_pending(r, s, scan_id=str(ts), now=ts)
        self.assertFalse(pending_blocks(r))

    def test_short_interval_and_before_two_minutes_cannot_release(self):
        r = record()
        for ts in (1001, 1031, 1061):
            r, _ = self.observe(r, ts)
        self.assertTrue(pending_blocks(r))
        r, _ = self.observe(r, 1062)
        self.assertTrue(pending_blocks(r))

    def test_partial_or_wrong_side_position_prevents_not_filled_inference(self):
        for side in ('SHORT', 'LONG'):
            r, _ = self.observe(record(), 1120, row={'side': side, 'qty_abs': 5, 'notional_u': 5})
            for ts in (1150, 1180, 1210):
                r, _ = self.observe(r, ts)
            self.assertTrue(pending_blocks(r))

    def test_filled_order_or_working_order_is_not_failed_by_empty_scans(self):
        for fields in ({'filled_qty': 1}, {'open_order_active': True}):
            r = dict(record(), **fields)
            for ts in (1120, 1150, 1180):
                r, _ = self.observe(r, ts)
            self.assertTrue(pending_blocks(r))

    def test_expected_quantity_confirms_success(self):
        r, result = self.observe(record(), 1120,
            row={'side': 'SHORT', 'qty_abs': 260, 'notional_u': 260})
        self.assertEqual(result, 'confirmed')
        self.assertFalse(pending_blocks(r))

    def test_m10_m9_and_reductions_keep_their_settlement_rules(self):
        for extra in ({'module': 'M9'}, {'module': 'M10'},
                      {'m10_step3_confirmation': {'kind': 'open'}}, {'kind': 'close'}):
            r = dict(record(), **extra)
            initial = copy.deepcopy(r)
            for ts in (1120, 1150, 1180):
                r, _ = self.observe(r, ts)
            self.assertEqual(r, initial)


class ActualLoopTests(unittest.TestCase):
    def run_case(self, retries=8, age=0, fault=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env, clock = namespace(temp.name)
        install_actual_verification_loop(env, clock)
        req = request()
        pre = {'ext_position_state': 'CONFIRMED_EMPTY', 'ext_side': 'NONE', 'ext_qty': 0,
               'ext_notional_u': 0, 'ext_reason': 'empty_positions_view'}
        env['_postcheck_pending_verify_mark'](req, pre_state=pre, verify_retry_left=retries)
        clock.sleep(age)
        if fault:
            env['_postcheck_direct_read_target_coin_snapshot'].side_effect = fault
        return env, clock, req

    def test_zero_retries_does_not_read_or_submit_and_round_finishes(self):
        env, clock, req = self.run_case(retries=0)
        queue = [req]
        self.assertEqual(env['drive'](queue, scan(clock.time())), 1)
        self.assertEqual(queue, [])
        env['_postcheck_direct_read_target_coin_snapshot'].assert_not_called()
        env['_state_commit_postcheck_fail'].assert_not_called()
        self.assertEqual(env['executions'], [])
        self.assertTrue(env['_safety_pending_scope']('EXT', 'TIA'))
        self.assertFalse(env['_postcheck_pending_verify_active'](req))

    def test_eight_checks_exhaust_and_do_not_starve_other_work(self):
        env, clock, req = self.run_case()
        other = request(module='ANCHOR', coin='ALL', platform='PAIR', action_type='close_pair')
        queue = [req, other]
        env['drive'](queue, scan(clock.time()))
        self.assertEqual(env['_postcheck_direct_read_target_coin_snapshot'].call_count, 8)
        self.assertEqual(env['executions'], [other])
        self.assertLessEqual(clock.time() - 1000, 120)
        self.assertTrue(any('POSTCHECK_DEFER_UNCONFIRMED' in s for s in env['logs']))
        self.assertFalse(any('POSTCHECK_VERIFY_RETRY' in s and 'retry_left=0' in s for s in env['logs']))

    def test_deadline_exhaustion_detaches_with_retries_remaining(self):
        env, clock, req = self.run_case(retries=8, age=120)
        env['drive']([req], scan(clock.time()))
        env['_postcheck_direct_read_target_coin_snapshot'].assert_not_called()
        self.assertEqual(env['executions'], [])

    def test_quantity_success_commits_without_defer_or_replay(self):
        env, clock, req = self.run_case()
        def read(*args, **kwargs):
            clock.sleep(3)
            return scan(clock.time(), row={'side': 'SHORT', 'qty_abs': 260,
                'notional_u': 260, 'position_state': 'FOUND'})
        env['_postcheck_direct_read_target_coin_snapshot'].side_effect = read
        env['drive']([req], scan(clock.time()))
        env['_state_commit_confirmed_success'].assert_called_once()
        self.assertEqual(env['executions'], [])
        self.assertFalse(env['_postcheck_pending_verify_active'](req))

    def test_slow_check_uses_remaining_deadline_and_is_not_rescheduled(self):
        env, clock, req = self.run_case(age=117)
        def read(*args, **kwargs):
            self.assertLessEqual(kwargs['deadline_monotonic'], 620)
            clock.sleep(3)
            return scan(clock.time())
        env['_postcheck_direct_read_target_coin_snapshot'].side_effect = read
        env['drive']([req], scan(clock.time()))
        self.assertEqual(env['_postcheck_direct_read_target_coin_snapshot'].call_count, 1)
        self.assertEqual(env['executions'], [])
        self.assertEqual(clock.mono, 620)

    def test_read_errors_do_not_fall_through_to_order_execution(self):
        env, clock, req = self.run_case(retries=1, fault=RuntimeError('read unavailable'))
        env['drive']([req], scan(clock.time()))
        self.assertEqual(env['executions'], [])
        self.assertTrue(env['_safety_pending_scope']('EXT', 'TIA'))

    def test_validator_exception_is_detached_not_reexecuted(self):
        env, clock, req = self.run_case()
        env['_postcheck_validate_effect_v2'] = Mock(side_effect=RuntimeError('validation bug'))
        env['drive']([req], scan(clock.time()))
        self.assertEqual(env['executions'], [])
        self.assertFalse(env['_postcheck_pending_verify_active'](req))

    def test_journal_failure_still_ends_round_and_keeps_submission_blocked(self):
        env, clock, req = self.run_case(retries=0)
        env['safe_write_json'] = lambda *a, **k: False
        env['drive']([req], scan(clock.time()))
        self.assertEqual(env['executions'], [])
        self.assertTrue(env['_safety_pending_scope']('EXT', 'TIA'))
        self.assertTrue(env['_SAFETY_SUBMISSION_FAULT'])

    def test_old_170_claim_does_not_become_new_260_submission(self):
        env, clock, req = self.run_case(retries=0)
        env['_postcheck_pending_verify_clear'](req)
        old = record()
        old.update(ts=900, intent_usd=170, pre_position={'position_state': 'UNAVAILABLE'})
        env['_safety_submission_records']()['EXT|TIA'] = old
        env['drive']([req], scan(clock.time()))
        self.assertEqual(env['executions'], [])
        self.assertFalse(env['exec_res'][0]['submit_ok'])
        self.assertEqual(env['exec_res'][0]['reason'], 'blocked_existing_unconfirmed_no_submit')
        persisted = env['_safety_pending_scope']('EXT', 'TIA')
        self.assertEqual(persisted['ts'], 900)
        self.assertEqual(persisted['intent_usd'], 170)

    def test_three_fresh_rounds_persist_failure_and_clear_legacy_maps_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            env, clock = namespace(directory)
            env['_safety_submission_records']()['EXT|TIA'] = record()
            self.assertTrue(env['_safety_submission_save']())
            for ts in (1120, 1150, 1180):
                env, clock = namespace(directory)
                clock.wall = ts
                env['_EXEC_PENDING'][('EXT', 'TIA', 'SHORT')] = 1000
                env['_EXEC_PENDING_INFO'][('EXT', 'TIA', 'SHORT')] = {'kind': 'open'}
                env['_postcheck_reconcile_unconfirmed_scan'](scan(ts), scan_id=str(ts))
            self.assertFalse(env['_safety_pending_scope']('EXT', 'TIA'))
            self.assertNotIn(('EXT', 'TIA', 'SHORT'), env['_EXEC_PENDING'])
            after, _ = namespace(directory)
            self.assertFalse(after['_safety_pending_scope']('EXT', 'TIA'))
            data = json.loads((Path(directory) / 'state/execution_unconfirmed.json').read_text())
            self.assertEqual(data['records']['EXT|TIA']['state'], 'CONFIRMED_NOT_FILLED')
            self.assertTrue(any('POSTCHECK_SCAN_NOT_FILLED' in x for x in env['logs']))

    def test_failed_resolution_write_cannot_unlock_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            env, clock = namespace(directory)
            env['_safety_submission_records']()['EXT|TIA'] = record()
            for ts in (1120, 1150):
                clock.wall = ts
                env['_postcheck_reconcile_unconfirmed_scan'](scan(ts), scan_id=str(ts))
            clock.wall = 1180
            env['safe_write_json'] = lambda *a, **k: False
            self.assertEqual(env['_postcheck_reconcile_unconfirmed_scan'](scan(1180), scan_id='1180'), [])
            self.assertTrue(env['_safety_pending_scope']('EXT', 'TIA'))

    def test_already_settled_journal_also_clears_legacy_memory_block(self):
        with tempfile.TemporaryDirectory() as directory:
            env, clock = namespace(directory)
            r = record()
            r['state'] = 'CONFIRMED_SUCCESS'
            env['_safety_submission_records']()['EXT|TIA'] = r
            env['_EXEC_PENDING'][('EXT', 'TIA', 'SHORT')] = 1000
            clock.wall = 1120
            env['_postcheck_reconcile_unconfirmed_scan'](scan(1120), scan_id='round-new')
            self.assertNotIn(('EXT', 'TIA', 'SHORT'), env['_EXEC_PENDING'])

    def test_bounded_direct_read_has_no_second_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            env, clock = namespace(directory)
            load_definitions({'_postcheck_direct_read_target_coin_snapshot'}, env)
            env.update(_ORIG_TIME_SLEEP=clock.sleep, m2_scan=Mock(return_value=scan(1005)),
                       _snapshot_refresh_fullscan=Mock(side_effect=AssertionError('unbounded fallback')),
                       _m2_scan_cached=Mock(side_effect=AssertionError('second scan')))
            env['_postcheck_direct_read_target_coin_snapshot']('fake', {}, 'TIA', '',
                wait_sec=5, deadline_monotonic=620)
            call = env['m2_scan'].call_args
            self.assertEqual(call.kwargs['_postcheck_deadline_monotonic'], 620)
            self.assertEqual(call.kwargs['scan_scope'], 'full')
            self.assertTrue(call.kwargs['only_positions'])
            env['_m2_scan_cached'].assert_not_called()
            clock.mono = 621
            self.assertIsNone(env['_postcheck_direct_read_target_coin_snapshot']('fake', {}, 'TIA', '', deadline_monotonic=620))
            self.assertEqual(env['m2_scan'].call_count, 1)

    def test_actual_worker_wait_honors_deadline_even_if_module_timeouts_disabled(self):
        import concurrent.futures
        from bot_core.worker_state import timeout_context
        source = (ROOT / 'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
        begin = source.index('    _future = _executor.submit(_worker_bootstrap, fn, _key, _worker_generation)')
        end = source.index('\ndef _executor_coin_worker_profile', begin)
        tail = textwrap.dedent(source[begin:end])
        code = 'def wait():\n' + textwrap.indent(tail, '    ')
        clock = FakeTime()
        waits = []
        class Future:
            def done(self):
                return False
            def result(self, timeout=None):
                if timeout is None:
                    raise AssertionError('unbounded wait')
                waits.append(timeout)
                clock.sleep(timeout)
                raise concurrent.futures.TimeoutError()
        env = {'_executor': SimpleNamespace(submit=lambda *a: Future()), '_worker_bootstrap': None,
               'fn': None, '_key': 'M2', '_worker_generation': 0, 'time': clock,
               '_futures': concurrent.futures, '_module_timeouts_disabled': lambda *a: True,
               'deadline_monotonic': 502, 'timeout_sec': 90, 'mod': 'M2',
               '_worker_heartbeat_timeout_reason': lambda **k: ('', {}),
               '_worker_timeout_context': timeout_context, '_worker_heartbeat_snapshot': lambda *a: {},
               '_active_req_snapshot': lambda: {}, '_exec_runtime_stop': Mock(),
               'on_timeout_rebuild': True, '_sync_pw_worker_rebuild': Mock(),
               '_record_swallowed_exception': Mock(), 'logfp': None, 'thread_name': 'M2'}
        # The extracted function assigns timeout_sec, so supply it as a local argument.
        code = code.replace('def wait():', 'def wait(timeout_sec=90):', 1)
        exec(compile(code, '<actual worker wait only>', 'exec'), env)
        with self.assertRaises(TimeoutError):
            env['wait']()
        self.assertLessEqual(clock.mono, 502)
        self.assertTrue(waits)
        env['_sync_pw_worker_rebuild'].assert_called_once()


if __name__ == '__main__':
    unittest.main()
