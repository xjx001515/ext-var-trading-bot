"""Offline worker regressions; never import or execute the bot module."""
from __future__ import annotations

import ast
import concurrent.futures
import copy
import importlib.util
from pathlib import Path
import sys
import threading
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_core.worker_state import WorkerState, WorkerRetiredError, timeout_context
from bot_core.browser_runtime import SessionLiveness

spec = importlib.util.spec_from_file_location('worker_edits', ROOT / 'repair_backups/safety_refactor_20261022/worker_edits.py')
edits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edits)
SOURCE = (ROOT / 'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
PATCHED = SOURCE if 'from bot_core.worker_state import WorkerState as _WorkerState' in SOURCE else edits.apply(SOURCE)
if 'def _worker_runtime_update_allowed(' not in PATCHED:
    followup_spec = importlib.util.spec_from_file_location('worker_followup_edits', ROOT / 'repair_backups/safety_refactor_20261022/worker_followup_edits.py')
    followup = importlib.util.module_from_spec(followup_spec)
    followup_spec.loader.exec_module(followup)
    PATCHED = followup.apply(PATCHED)
TREE = ast.parse(PATCHED)


def extract(names, *, truncate_dispatch=False):
    nodes = copy.deepcopy([n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names])
    if truncate_dispatch:
        for node in nodes:
            if node.name in ('executor_run', 'executor_run_quanbu'):
                index = next(i for i, child in enumerate(node.body) if isinstance(child, ast.If)
                             and isinstance(child.test, ast.Call)
                             and getattr(child.test.func, 'id', '') == '_executor_worker_handoff_needed')
                # Retain actual dispatcher but replace every trading-body statement.
                node.body = node.body[:index + 1] + [ast.Return(ast.Constant('read_only_body_sentinel'))]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    return compile(ast.fix_missing_locations(module), '<offline-selected-worker-functions>', 'exec')


def noop(*args, **kwargs):
    return None


class WorkerStateTests(unittest.TestCase):
    def setUp(self):
        self.state = WorkerState()
        self.ids, self.names, self.hb, self.inflight = {}, {}, {}, {}

    def begin(self, key='W'):
        return self.state.begin(key, self.state.generation(key), self.ids, self.names)

    def invalidate(self, key='W'):
        return self.state.invalidate(key, self.hb, self.inflight, self.ids, self.names)

    def test_empty_owner_dictionary_is_populated(self):
        token = self.begin()
        self.assertEqual(self.ids['W'], threading.get_ident())
        self.assertTrue(self.names['W'])
        self.assertTrue(self.state.is_owner('W'))
        self.assertFalse(self.state.is_owner('OTHER'))
        self.assertGreater(token, 0)

    def test_retired_queued_generation_cannot_start(self):
        generation = self.state.generation('W')
        self.invalidate()
        with self.assertRaises(WorkerRetiredError):
            self.state.begin('W', generation, self.ids, self.names)
        self.assertEqual(self.ids, {})

    def test_old_thread_cannot_overwrite_or_clear_new_run(self):
        ready, release = threading.Event(), threading.Event()
        old_results, errors, callbacks = [], [], []

        def old_worker():
            try:
                token = self.begin()
                self.state.publish('W', self.hb, {'stage': 'old'})
                self.state.publish('W', self.inflight, {'stage': 'old'})
                ready.set()
                if not release.wait(3):
                    raise AssertionError('test rendezvous timed out')
                old_results.append(self.state.publish('W', self.hb, {'stage': 'late-old'}))
                old_results.append(self.state.clear('W', self.inflight))
                old_results.append(self.state.finish('W', token, self.hb, self.inflight,
                                                    self.ids, self.names,
                                                    on_finish=lambda: callbacks.append('old')))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=old_worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            self.invalidate()
            new_token = self.begin()
            self.state.publish('W', self.hb, {'stage': 'new'})
            self.state.publish('W', self.inflight, {'stage': 'new'})
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(old_results, [{}, False, False])
            self.assertEqual(callbacks, [])
            self.assertEqual(self.hb['W']['run_token'], new_token)
            self.assertEqual(self.hb['W']['stage'], 'new')
            self.assertEqual(self.inflight['W']['stage'], 'new')
            self.assertEqual(self.ids['W'], threading.get_ident())
        finally:
            release.set()
            thread.join(3)

    def test_finish_clears_only_own_records(self):
        token = self.begin()
        self.state.publish('W', self.hb, {'stage': 'run'})
        self.state.publish('W', self.inflight, {'stage': 'run'})
        self.assertTrue(self.state.finish('W', token, self.hb, self.inflight, self.ids, self.names))
        self.assertEqual((self.hb, self.inflight, self.ids, self.names), ({}, {}, {}, {}))
        self.assertFalse(self.state.is_owner())
        self.assertFalse(self.state.retired_current())

    def test_retired_context_cannot_dispatch_again(self):
        self.begin()
        self.invalidate()
        with self.assertRaises(WorkerRetiredError):
            self.state.assert_live()

    def test_compare_clear_never_clears_newer_token(self):
        old = self.begin()
        self.state.publish('W', self.inflight, {'stage': 'old'})
        self.invalidate()
        self.begin()
        self.state.publish('W', self.inflight, {'stage': 'new'})
        self.assertFalse(self.state.clear('W', self.inflight, expected_token=old))
        self.assertEqual(self.inflight['W']['stage'], 'new')

    def test_timeout_context_prefers_original_heartbeat(self):
        reason, stage, where = timeout_context('worker_stuck',
            {'stage': 'PW_CALL_BEGIN|submit', 'where': 'ext.submit'},
            {'last_stage': 'EXEC_RUNTIME_REBUILD_BEGIN', 'last_where': 'reset'})
        self.assertEqual(stage, 'PW_CALL_BEGIN|submit')
        self.assertEqual(where, 'ext.submit')
        self.assertIn('|timeout_stage=PW_CALL_BEGIN|submit', reason)
        self.assertNotIn('REBUILD', reason)


class SourceIntegrationTests(unittest.TestCase):
    def make_env(self):
        state = WorkerState()
        env = dict(threading=threading, time=time, _WORKER_STATE=state,
                   _EXEC_WORKER_LOCAL=state.local, _SYNC_PW_WORKER_THREAD_IDS={},
                   _SYNC_PW_WORKER_THREAD_NAMES={}, _SYNC_PW_WORKER_LOCK=threading.Lock(),
                   _SYNC_PW_WORKERS={}, _EXEC_WORKER_HEARTBEATS={}, _SYNC_PW_WORKER_INFLIGHT={},
                   _record_swallowed_exception=noop, _active_req_snapshot=lambda: {},
                   _active_req_touch=noop, _active_req_clear=noop, _pin=noop,
                   _worker_timeout_context=timeout_context, _module_timeouts_disabled=lambda mod: False,
                   _exec_runtime_stop=noop, _sync_pw_worker_rebuild=noop,
                   _worker_heartbeat_snapshot=lambda key: {}, _executor_timeout_mark=noop,
                   _worker_heartbeat_timeout_reason=lambda **kwargs: ('', {}))
        return env

    def test_noisy_probe_still_updates_heartbeat(self):
        env = self.make_env()
        calls = []
        env.update(_exec_stage_label=lambda stage, where: stage + '|' + where,
                   _should_skip_scan_probe_log=lambda stage, where: True,
                   _active_req_touch=lambda *a, **k: calls.append(a), _set_current_trace_id=noop)
        exec(extract({'_exec_stage_probe', '_worker_heartbeat_touch'}), env)
        env['_WORKER_STATE'].begin('W', 0, env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
        env['_exec_stage_probe']('PW_CALL_BEGIN', 'ensure_page.ext_health.probe', timeout_ms=26000)
        hb = env['_EXEC_WORKER_HEARTBEATS']['W']
        self.assertEqual(hb['timeout_ms'], 26000)
        self.assertEqual(hb['where'], 'ensure_page.ext_health.probe')
        self.assertEqual(calls, [])

    def test_same_worker_call_is_inline_without_submission(self):
        env = self.make_env()
        env['_WORKER_STATE'].begin('W', 0, env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
        exec(extract({'_run_call_in_worker_thread'}), env)
        self.assertEqual(env['_run_call_in_worker_thread'](lambda: 42, thread_name='W'), 42)
        self.assertEqual(env['_SYNC_PW_WORKERS'], {})

    def test_retired_task_cannot_start_next_ui_operation(self):
        env = self.make_env()
        env.update(_pw_guard=lambda **kwargs: True, _pw_call_timeout_ms=lambda **kwargs: None,
                   _exec_stage_probe=noop)
        exec(extract({'_pw_call'}), env)
        calls = []
        ui_call = lambda: calls.append('called') or 42
        # Main-thread callers without a worker lease remain valid.
        self.assertEqual(env['_pw_call']('offline.ui', ui_call), 42)
        state = env['_WORKER_STATE']
        state.begin('W', 0, env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
        self.assertEqual(env['_pw_call']('offline.ui', ui_call), 42)
        state.invalidate('W', env['_EXEC_WORKER_HEARTBEATS'], env['_SYNC_PW_WORKER_INFLIGHT'],
                         env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
        with self.assertRaises(WorkerRetiredError):
            env['_pw_call']('offline.ui', ui_call)
        self.assertEqual(calls, ['called', 'called'])

    def test_real_worker_nested_call_finishes_without_self_wait(self):
        env = self.make_env()
        env['_set_current_trace_id'] = noop
        exec(extract({'_run_call_in_worker_thread', '_worker_heartbeat_touch', '_sync_pw_worker_mark_inflight'}), env)
        run = env['_run_call_in_worker_thread']
        def outer():
            self.assertEqual(env['_SYNC_PW_WORKER_THREAD_IDS']['W'], threading.get_ident())
            return run(lambda: 'nested_finished', thread_name='W', timeout_sec=2)
        try:
            self.assertEqual(run(outer, thread_name='W', timeout_sec=3), 'nested_finished')
            self.assertEqual(env['_SYNC_PW_WORKER_THREAD_IDS'], {})
            self.assertEqual(env['_EXEC_WORKER_HEARTBEATS'], {})
            self.assertEqual(env['_SYNC_PW_WORKER_INFLIGHT'], {})
        finally:
            for pool in env['_SYNC_PW_WORKERS'].values():
                pool.shutdown(wait=True, cancel_futures=True)

    def test_owned_live_loop_uses_warm_cache_without_starting_runtime(self):
        env = self.make_env()
        env['_WORKER_STATE'].begin('W', 0, env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
        rt = dict(pw=object(), cdp='offline', owner_thread_ident=threading.get_ident(),
                  browser=types.SimpleNamespace(is_connected=lambda:True), page_health_ok=True,
                  ext_exe=object(), var_exe=object(), ext_page=object(), var_page=object())
        starts = []
        env.update(_has_running_asyncio_loop=lambda: True, _exec_worker_stage_touch=noop,
                   _exec_runtime_worker_key=lambda **k: 'W', _EXEC_RUNTIME_LOCK=threading.RLock(),
                   _EXEC_RUNTIME_CACHES={'W': rt}, _ensure_executor_pages_healthy=lambda *a, **k: (rt, False, ''),
                   _pw_runtime_mark_ready=noop, _PW_SESSIONS=SessionLiveness(), sync_playwright=lambda: starts.append('start'))
        exec(extract({'_exec_runtime_get', '_exec_runtime_connection_alive'}), env)
        result, created, error = env['_exec_runtime_get']('offline', 'unused')
        self.assertIs(result['pw'], rt['pw'])
        self.assertFalse(created)
        self.assertEqual(error, '')
        self.assertEqual(starts, [])
        env['_EXEC_RUNTIME_CACHES'] = {}
        result, created, error = env['_exec_runtime_get']('offline', 'unused')
        self.assertIsNone(result)
        self.assertEqual(error, 'runtime_rebuild_requires_idle_worker')
        self.assertEqual(starts, [])

    def test_quanbu_live_loop_handoffs_once_then_enters_original_body(self):
        env = self.make_env()
        key = 'ExecutorQuanbuSyncPlaywrightWorker_BTC'
        env.update(_has_running_asyncio_loop=lambda: True,
                   _executor_coin_worker_profile=lambda *a, **k: {'worker_name': key, 'timeout_sec': 90},
                   _set_current_trace_id=noop)
        dispatches = []
        def dispatch(fn, **kwargs):
            dispatches.append(kwargs['thread_name'])
            self.assertEqual(len(dispatches), 1, 'recursive handoff')
            env['_WORKER_STATE'].begin(key, 0, env['_SYNC_PW_WORKER_THREAD_IDS'], env['_SYNC_PW_WORKER_THREAD_NAMES'])
            return fn()
        env['_run_call_in_worker_thread'] = dispatch
        exec(extract({'_executor_worker_handoff_needed', 'executor_run_quanbu'}, truncate_dispatch=True), env)
        result = env['executor_run_quanbu']('offline', 'unused', {}, [], coins=[])
        self.assertEqual(result, 'read_only_body_sentinel')
        self.assertEqual(dispatches, [key])

    def test_completed_executor_timeout_is_original_task_exception(self):
        env = self.make_env()
        future = concurrent.futures.Future()
        actual = TimeoutError('specific operation timeout')
        future.set_exception(actual)
        env['_SYNC_PW_WORKERS']['W'] = types.SimpleNamespace(submit=lambda *a: future)
        resets = []
        env['_sync_pw_worker_rebuild'] = lambda *a, **k: resets.append('rebuild')
        exec(extract({'_run_call_in_worker_thread'}), env)
        with self.assertRaises(TimeoutError) as caught:
            env['_run_call_in_worker_thread'](noop, thread_name='W', timeout_sec=1, mod='EXECUTOR', on_timeout_rebuild=True)
        self.assertIs(caught.exception, actual)
        self.assertEqual(resets, [])

    def test_executor_stuck_is_not_recaught_as_full_budget_timeout(self):
        env = self.make_env()
        class Pending:
            def result(self, **kwargs):
                raise concurrent.futures.TimeoutError()
            def done(self):
                return False
        env['_SYNC_PW_WORKERS']['W'] = types.SimpleNamespace(submit=lambda *a: Pending())
        env['_worker_heartbeat_timeout_reason'] = lambda **k: ('EXECUTOR_worker_stuck:20s', {'stage': 'PW_CALL_BEGIN|submit', 'where': 'ext.submit'})
        active = {'last_stage': 'original'}
        env['_active_req_snapshot'] = lambda: active
        resets, marked = [], []
        env['_sync_pw_worker_rebuild'] = lambda *a, **k: (resets.append(a), active.update(last_stage='REBUILD'))
        env['_executor_timeout_mark'] = lambda *a, **k: marked.append((a, k))
        exec(extract({'_run_call_in_worker_thread'}), env)
        with self.assertRaisesRegex(TimeoutError, 'worker_stuck:20s') as caught:
            env['_run_call_in_worker_thread'](noop, thread_name='W', timeout_sec=90, mod='EXECUTOR', on_timeout_rebuild=True)
        self.assertEqual(len(resets), 1)
        self.assertEqual(len(marked), 1)
        self.assertIn('timeout_stage=PW_CALL_BEGIN|submit', str(caught.exception))
        self.assertNotIn('REBUILD', str(caught.exception))

    def test_applied_source_is_syntactically_valid(self):
        ast.parse(PATCHED)


class LateWorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.state = WorkerState()
        self.ids, self.names, self.hb, self.inflight = {}, {}, {}, {}
        self.effects = []
        self.runtime = {'pw': types.SimpleNamespace(stop=lambda: self.effects.append('stop'))}
        self.env = dict(_WORKER_STATE=self.state, time=time, threading=threading,
            _PW_SESSIONS=SessionLiveness(),
            _PW_RUNTIME_GEN=7, PW_ALIVE=True, _PW_RUNTIME_RECOVERING=False,
            _PW_RUNTIME_RECOVERING_SCOPE={'ALL': False, 'EXT': False, 'VAR': False},
            _RUNTIME_STATE={'state': 'new'}, _M2_SCAN_WATCH={'stage': 'new', 'pct': 40},
            _M2_RUNTIME_CRITICAL_STATE={'active': True, 'depth': 2, 'stage': 'new'},
            _M2_RUNTIME_CRITICAL_PENDING_RESET={}, _M2_SCAN_CACHE={'ts': 100, 'snap': {'new': 1}},
            _EXEC_RUNTIME_CACHES={'W': self.runtime}, _EXEC_RUNTIME_LOCK=threading.RLock(),
            _SNAPSHOT_COMMIT_LOCK=threading.RLock(), _MAIN_ROUND_WATCH={},
            _record_swallowed_exception=noop, _pin=noop, safe_log=noop,
            _pw_runtime_scope_from_reason=lambda *a: 'ALL', _pw_runtime_scope_normalize=lambda *a: 'ALL',
            RUNTIME_RECOVERING_TIMEOUT_SEC=20, _active_req_snapshot=lambda: {'worker_key': 'new'},
            _exec_dispatch_claim_release_scope=noop, panel_set_mainloop_state=noop,
            panel_update_heartbeat=lambda *a, **k: self.effects.append('heartbeat'),
            _runtime_state_normalize=lambda **kwargs: {}, _exec_runtime_worker_key=lambda: 'W')
        self.env['_m2_scan_watch_snapshot'] = lambda: dict(self.env['_M2_SCAN_WATCH'])

    def begin(self):
        return self.state.begin('W', self.state.generation('W'), self.ids, self.names)

    def retire(self):
        self.state.invalidate('W', self.hb, self.inflight, self.ids, self.names)

    def test_retired_runtime_and_observability_writes_are_noops(self):
        calls = {
            '_pw_runtime_begin_recover': lambda f: f('late'),
            '_pw_runtime_mark_ready': lambda f: f('late'),
            '_pw_runtime_hard_reset': lambda f: f('late', stop_exec_runtime=True),
            '_pw_runtime_auto_release_if_stale': lambda f: f(),
            '_pw_set_alive': lambda f: f(False),
            '_m2_runtime_critical_enter': lambda f: f('late'),
            '_m2_runtime_critical_exit': lambda f: f('late'),
            '_exec_runtime_stop': lambda f: f('late', stop_all=True),
            '_exec_runtime_cache_set': lambda f: f({'old': 1}, 'W'),
            '_exec_runtime_cache_pop': lambda f: f('W'),
            '_m2_scan_watch_update': lambda f: f('late', 99, state='DONE', touch_panel=True),
            '_m2_scan_watch_finish': lambda f: f('DONE', touch_panel=True),
            '_runtime_state_set': lambda f: f(state='late'),
            '_runtime_state_drop_keys': lambda f: f('state'),
            '_round_phase_touch': lambda f: f('late'),
            '_round_watch_touch': lambda f: f('late'),
            '_readiness_set': lambda f: f('pos', 'FAIL', 'late'),
            'panel_set_mainloop_state': lambda f: f('late'),
            'panel_update_heartbeat': lambda f: f({'late': 1}),
            '_active_req_bind': lambda f: f({'late': 1}),
        }
        exec(extract(set(calls) | {'_worker_runtime_update_allowed'}), self.env)
        self.begin()
        self.retire()
        before = {key: copy.deepcopy(self.env[key]) for key in (
            '_PW_RUNTIME_GEN', 'PW_ALIVE', '_PW_RUNTIME_RECOVERING_SCOPE', '_RUNTIME_STATE',
            '_M2_SCAN_WATCH', '_M2_RUNTIME_CRITICAL_STATE', '_M2_RUNTIME_CRITICAL_PENDING_RESET', '_M2_SCAN_CACHE')}
        for name, call in calls.items():
            with self.subTest(function=name):
                call(self.env[name])
        for key, value in before.items():
            self.assertEqual(self.env[key], value, key)
        self.assertIs(self.env['_EXEC_RUNTIME_CACHES']['W'], self.runtime)
        self.assertEqual(self.effects, [])

    def test_main_thread_and_live_owner_still_update_runtime(self):
        exec(extract({'_worker_runtime_update_allowed', '_pw_set_alive', '_pw_runtime_begin_recover',
                      '_pw_runtime_mark_ready', '_m2_scan_watch_update'}), self.env)
        self.env['_pw_set_alive'](False)
        self.assertFalse(self.env['PW_ALIVE'])
        self.env['_pw_runtime_begin_recover']('main')
        self.assertEqual(self.env['_PW_RUNTIME_GEN'], 8)
        self.begin()
        self.env['_PW_SESSIONS'].begin()  # A live owner has entered its driver context.
        self.env['_pw_runtime_mark_ready']('live owner')
        self.assertTrue(self.env['PW_ALIVE'])
        self.env['_m2_scan_watch_update']('fresh-stage', 70)
        self.assertEqual(self.env['_M2_SCAN_WATCH']['stage'], 'fresh-stage')
        self.assertEqual(self.effects, ['heartbeat'])

    def test_late_targetclosed_cannot_reset_successor_runtime(self):
        self.env.update(_pw_guard=lambda **k: True, _pw_call_timeout_ms=lambda **k: None,
                        _exec_stage_probe=noop,
                        _pw_runtime_hard_reset=lambda *a, **k: self.effects.append('reset'))
        exec(extract({'_pw_call'}), self.env)
        self.begin()
        def late_failure():
            self.retire()
            raise RuntimeError('Target page, context or browser has been closed')
        with self.assertRaises(WorkerRetiredError):
            self.env['_pw_call']('late.goto', late_failure)
        self.assertEqual(self.effects, [])
        self.assertTrue(self.env['PW_ALIVE'])
        self.assertEqual(self.env['_PW_RUNTIME_GEN'], 7)

    def test_late_success_is_not_delivered_as_current_ui_result(self):
        self.env.update(_pw_guard=lambda **k: True, _pw_call_timeout_ms=lambda **k: None,
                        _exec_stage_probe=noop)
        exec(extract({'_pw_call'}), self.env)
        self.begin()
        def late_success():
            self.retire()
            return 'obsolete result'
        with self.assertRaises(WorkerRetiredError):
            self.env['_pw_call']('late.read', late_success)

    def test_new_critical_section_replaces_retired_generation(self):
        exec(extract({'_worker_runtime_update_allowed', '_m2_runtime_critical_enter', '_m2_runtime_critical_exit'}), self.env)
        self.env['_M2_RUNTIME_CRITICAL_STATE'] = {}
        old_token = self.begin()
        self.env['_m2_runtime_critical_enter']('old')
        self.retire()
        new_token = self.begin()
        self.env['_m2_runtime_critical_enter']('new')
        new = dict(self.env['_M2_RUNTIME_CRITICAL_STATE'])
        self.assertEqual(new['depth'], 1)
        self.assertEqual(new['worker_generation'], 1)
        # Emulate the old thread's TLS after its pending call finally returns.
        self.state.local.run_token = old_token
        self.env['_m2_runtime_critical_exit']('old late exit')
        self.assertEqual(self.env['_M2_RUNTIME_CRITICAL_STATE'], new)
        self.state.local.run_token = new_token
        self.env['_m2_runtime_critical_exit']('new exit')
        self.assertEqual(self.env['_M2_RUNTIME_CRITICAL_STATE']['depth'], 0)

    def test_retired_critical_marker_does_not_defer_main_recovery(self):
        exec(extract({'_worker_runtime_update_allowed', '_m2_runtime_critical_enter',
                      '_pw_runtime_hard_reset', '_pw_runtime_begin_recover', '_pw_set_alive'}), self.env)
        self.env['_M2_RUNTIME_CRITICAL_STATE'] = {}
        self.begin()
        self.env['_m2_runtime_critical_enter']('old')
        self.retire()
        self.state.local.worker_key = ''
        self.state.local.run_token = None
        self.env['_runtime_state_set'] = noop
        self.env['_pw_runtime_hard_reset']('main recovery', source='SYS', stop_exec_runtime=False)
        self.assertEqual(self.env['_PW_RUNTIME_GEN'], 8)
        self.assertEqual(self.env['_M2_RUNTIME_CRITICAL_PENDING_RESET'], {})


if __name__ == '__main__':
    unittest.main()
