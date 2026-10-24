"""Offline regression tests. Only AST-selected definitions are executed.

Never import the bot module: its startup code can connect to trading accounts.
All pages, navigation, clocks, futures, log writers and runtime resets are fakes.
"""
from __future__ import annotations

import ast
import concurrent.futures
import os
from pathlib import Path
import threading
import types
import unittest
from bot_core.browser_registry import PageRegistry
from bot_core.worker_state import WorkerState, timeout_context
from bot_core.browser_runtime import ExtRecovery

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'BOT5-2-414 .py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
NAMES = {
    '_UiDeadError', '_UiLinkError', '_UiRecoverTimeoutError',
    '_ext_navigation_url', '_ext_url_is_perp', '_ext_page_recovery_gate',
    '_ensure_page_platform_key', '_ensure_page_same_platform',
    '_ensure_page_platform_pages', '_ensure_page_registry_get',
    '_ensure_page_registry_set', 'pick_page', 'ensure_page', '_ensure_page_impl',
    '_worker_heartbeat_stale_sec', '_worker_heartbeat_timeout_reason',
    '_run_call_in_worker_thread', 'm2_scan',
}
NODES = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in NAMES]
assert {n.name for n in NODES} == NAMES
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *NODES], type_ignores=[])), str(SOURCE), 'exec')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class Page:
    def __init__(self, clock, url='about:blank', healthy=False, fail_nav=False):
        self.clock = clock
        self.url = url
        self.healthy = healthy
        self.fail_nav = fail_nav
        self.closed = False
        self.goto_calls = []
        self.close_calls = 0
        self.context = None

    def is_closed(self):
        return self.closed

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.fail_nav:
            self.clock.sleep(kwargs.get('timeout', 26000) / 1000)
            raise TimeoutError('navigation remained about:blank')
        self.url = url
        self.healthy = True

    def evaluate(self, script, **kwargs):
        if self.healthy:
            return {'text_len': 800, 'child_cnt': 10, 'html_len': 2000}
        return {'text_len': 0, 'child_cnt': 0, 'html_len': 0}

    def title(self):
        return 'Extended'

    def wait_for_timeout(self, ms):
        self.clock.sleep(ms / 1000)

    def wait_for_load_state(self, state, timeout=None):
        if not self.healthy:
            raise TimeoutError('document not ready')

    def wait_for_function(self, *args, **kwargs):
        if not self.healthy:
            raise TimeoutError('body still blank')

    def bring_to_front(self):
        pass

    def close(self):
        self.closed = True
        self.close_calls += 1


class Context:
    def __init__(self, clock, pages=(), fail_new=False):
        self.clock = clock
        self.pages = list(pages)
        self.new_count = 0
        self.fail_new = fail_new
        for page in self.pages:
            page.context = self

    def new_page(self):
        self.new_count += 1
        page = Page(self.clock, fail_nav=self.fail_new)
        page.context = self
        self.pages.append(page)
        return page

    def new_cdp_session(self, page):
        def send(method, params=None):
            if method == 'Target.getTargetInfo':
                return {'targetInfo': {'targetId': str(id(page))}}
            if method == 'Page.stopLoading':
                return {}
            if method == 'Page.navigate':
                if page.fail_nav:
                    raise TimeoutError('CDP navigation also failed')
                page.url = params['url']; page.healthy = True
                return {'frameId': 'main'}
            raise AssertionError(method)
        return types.SimpleNamespace(send=send, detach=lambda: None)


def noop(*args, **kwargs):
    return None


def make_env():
    clock = Clock()
    logs = []
    resets = []
    worker_state = WorkerState()
    env = dict(
        time=clock, threading=threading, os=os, _futures=concurrent.futures,
        UI_SINGLE_CHAIN_ENABLE=True, EXT_NORMAL_OPEN_URL='https://app.extended.exchange/trade',
        _EXT_ENSURE_LOCK=threading.Lock(),
        _EXT_PAGE_RETRY_STATE={'failures': 0, 'retry_at': 0.0, 'attempted': False},
        _EXT_RECOVERY=ExtRecovery(clock.time), _ext_recovery_log=lambda event, **d: logs.append(event),
        _VAR_RECOVERY=ExtRecovery(clock.time, platform='VAR'), _VAR_ENSURE_LOCK=threading.Lock(),
        _VAR_PAGE_RETRY_STATE={'failures': 0, 'retry_at': 0.0, 'attempted': False},
        _var_need_click=lambda page, coin: False,
        _ENSURE_PAGE_SINGLETON_REGISTRY={}, _ENSURE_PAGE_SINGLETON_META={},
        _ENSURE_PAGE_HEALTH={}, _ENSURE_PAGE_RECOVER_GUARD={},
        _OWNED_PAGE_REGISTRY=PageRegistry(),
        _WORKER_STATE=worker_state, _EXEC_WORKER_LOCAL=worker_state.local,
        _worker_timeout_context=timeout_context,
        _worker_runtime_update_allowed=lambda: True,
        _env_int=lambda name, default: default, _env_float=lambda name, default: default,
        _env_flag=lambda name, default: default,
        _ensure_page_wait_platform_recovery=lambda *a, **k: (True, 'ok'),
        _ext_effective_open_url=lambda **k: k.get('default_url'),
        _ext_url_is_join=lambda url: '/join/' in url,
        _pw_require_page=lambda p, where: None if not p.closed else (_ for _ in ()).throw(RuntimeError('closed')),
        _pw_call=lambda where, fn, *a, **k: fn(*a, **{key:value for key,value in k.items() if key != '_operation_timeout_ms'}),
        _pw_goto=lambda p, url, where, **k: p.goto(url, **k),
        _pw_front=lambda p, where: p.bring_to_front(),
        _ext_temp_unavailable_detect=lambda *a, **k: {'active': False},
        _ext_crash_page_detect=lambda *a, **k: {'active': False},
        _ext_crash_page_self_heal=noop, _ext_temp_unavailable_self_heal=noop,
        _active_req_touch=noop, _record_swallowed_exception=noop, _log_exc_level=noop,
        _ensure_page_health_key=lambda p, pk: str(id(p)),
        _ensure_page_health_reset_for_page=noop, _ensure_page_health_touch=noop,
        _ensure_page_health_forget=noop,
        _ensure_page_should_recycle=lambda *a, **k: (False, '', {}),
        safe_log=lambda fn: logs.append(fn()), now_ts=clock.time,
        _worker_heartbeat_snapshot=lambda key: {},
        _SYNC_PW_WORKER_LOCK=threading.Lock(), _SYNC_PW_WORKERS={},
        _SYNC_PW_WORKER_THREAD_IDS={}, _SYNC_PW_WORKER_THREAD_NAMES={},
        _module_timeouts_disabled=lambda mod: False,
        _active_req_snapshot=lambda: {}, _pin=noop,
        _exec_runtime_stop=lambda *a, **k: resets.append(('stop', a, k)),
        _sync_pw_worker_rebuild=lambda *a, **k: resets.append(('rebuild', a, k)),
        _m2_scan_timeout_sec=lambda default: default,
        _m2_scan_runtime_gen_capture=lambda: 1,
        _m2_scan_watch_update=noop, _m2_scan_watch_finish=noop,
        _m2_scan_watch_snapshot=lambda: {}, write_jsonl=noop,
        _m2_make_fail_snapshot=lambda reason, **k: {'data_quality': 'fail', 'reason': reason, **k},
        _pw_runtime_hard_reset=lambda *a, **k: resets.append(('hard_reset', a, k)),
    )
    exec(CODE, env)
    return env, clock, logs, resets


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env, self.clock, self.logs, self.resets = make_env()

    def run_ensure(self, *contexts):
        browser = types.SimpleNamespace(contexts=list(contexts))
        return self.env['ensure_page'](browser, 'app.extended.exchange', 'https://app.extended.exchange/perp')

    def test_healthy_trade_page_reused_without_navigation_or_close(self):
        first = Page(self.clock, 'https://app.extended.exchange/trade/ETH-USD', True)
        second = Page(self.clock, 'https://app.extended.exchange/trade/BTC-USD', True)
        ctx = Context(self.clock, [first, second])
        self.assertIs(self.run_ensure(ctx), first)
        self.assertEqual(ctx.new_count, 0)
        self.assertEqual(first.goto_calls + second.goto_calls, [])
        self.assertEqual(first.close_calls + second.close_calls, 0)

    def test_healthy_page_in_second_context_reused(self):
        healthy = Page(self.clock, 'https://app.extended.exchange/trade', True)
        empty = Context(self.clock)
        second = Context(self.clock, [healthy])
        self.assertIs(self.run_ensure(empty, second), healthy)
        self.assertEqual(empty.new_count + second.new_count, 0)

    def test_blank_dom_with_valid_url_is_repaired_in_same_tab(self):
        blank = Page(self.clock, 'https://app.extended.exchange/trade', False)
        ctx = Context(self.clock, [blank])
        self.assertIs(self.run_ensure(ctx), blank)
        self.assertEqual(ctx.new_count, 0)
        self.assertEqual(len(blank.goto_calls), 1)
        self.assertEqual(blank.goto_calls[0][1]['wait_until'], 'commit')
        self.assertEqual(blank.close_calls, 0)

    def test_large_skeleton_without_visible_text_is_not_accepted(self):
        class SkeletonPage(Page):
            def evaluate(self, script, **kwargs):
                if self.healthy:
                    return super().evaluate(script, **kwargs)
                return {'text_len': 0, 'child_cnt': 90, 'html_len': 50000}
        blank = SkeletonPage(self.clock, 'https://app.extended.exchange/trade')
        ctx = Context(self.clock, [blank])
        self.assertIs(self.run_ensure(ctx), blank)
        self.assertEqual(len(blank.goto_calls), 1)
        self.assertEqual(ctx.new_count, 0)

    def test_healthy_alternative_preferred_to_repairing_bad_tab(self):
        bad = Page(self.clock, 'https://app.extended.exchange/trade', False, True)
        healthy = Page(self.clock, 'https://app.extended.exchange/trade/ETH-USD', True)
        ctx = Context(self.clock, [bad, healthy])
        self.assertIs(self.run_ensure(ctx), healthy)
        self.assertEqual(bad.goto_calls, [])
        self.assertEqual(bad.close_calls + healthy.close_calls, 0)

    def test_failed_same_tab_is_kept_and_cooldown_blocks_navigation(self):
        blank = Page(self.clock, 'https://app.extended.exchange/trade', False, True)
        ctx = Context(self.clock, [blank])
        for _ in range(20):
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_deferred'):
                self.run_ensure(ctx)
        self.assertEqual(len(blank.goto_calls), 1)
        self.assertFalse(blank.closed)
        self.assertEqual(ctx.new_count, 0)

    def test_new_tab_failure_backoff_and_bounded_creation(self):
        ctx = Context(self.clock, fail_new=True)
        for expected_delay in [60, 120, 240, 300, 300]:
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_deferred'):
                self.run_ensure(ctx)
            state = self.env['_EXT_PAGE_RETRY_STATE']
            self.assertAlmostEqual(state['retry_at'] - self.clock.now, expected_delay)
            count = ctx.new_count
            for _ in range(10):
                with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_deferred'):
                    self.run_ensure(ctx)
            self.assertEqual(ctx.new_count, count)
            self.clock.sleep(expected_delay)
        self.assertEqual(ctx.new_count, 1)
        self.assertTrue(all(not p.closed for p in ctx.pages))

    def test_user_opened_healthy_tab_bypasses_cooldown(self):
        ctx = Context(self.clock, fail_new=True)
        with self.assertRaises(self.env['_UiLinkError']):
            self.run_ensure(ctx)
        healthy = Page(self.clock, 'https://app.extended.exchange/trade', True)
        healthy.context = ctx
        ctx.pages.append(healthy)
        self.assertIs(self.run_ensure(ctx), healthy)
        self.assertEqual(ctx.new_count, 1)
        self.assertEqual(self.env['_EXT_PAGE_RETRY_STATE']['failures'], 0)

    def test_new_tab_uses_trade_and_commit_then_document_readiness(self):
        ctx = Context(self.clock)
        page = self.run_ensure(ctx)
        self.assertEqual(page.goto_calls[0][0], 'https://app.extended.exchange/trade')
        self.assertEqual(page.goto_calls[0][1]['wait_until'], 'commit')
        self.assertTrue(page.healthy)
        self.assertEqual(ctx.new_count, 1)

    def test_competing_recovery_fails_fast(self):
        ctx = Context(self.clock)
        lock = self.env['_EXT_ENSURE_LOCK']
        lock.acquire()
        try:
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_in_progress'):
                self.run_ensure(ctx)
        finally:
            lock.release()
        self.assertEqual(ctx.new_count, 0)

    def test_var_guard_uses_its_own_lock_while_ext_is_busy(self):
        sentinel = object()
        self.env['_ensure_page_impl'] = lambda *a, **k: sentinel
        self.env['_EXT_ENSURE_LOCK'].acquire()
        try:
            self.assertIs(self.env['ensure_page'](types.SimpleNamespace(contexts=[object()]), 'variational.io', 'https://omni.variational.io/perpetual/BTC'), sentinel)
        finally:
            self.env['_EXT_ENSURE_LOCK'].release()

    def test_legacy_path_does_not_accept_blank_dom(self):
        self.env['UI_SINGLE_CHAIN_ENABLE'] = False
        blank = Page(self.clock, 'https://app.extended.exchange/trade', False)
        ctx = Context(self.clock, [blank])
        self.assertIs(self.run_ensure(ctx), blank)
        self.assertEqual(len(blank.goto_calls), 1)
        self.assertEqual(ctx.new_count, 0)

    def test_legacy_path_self_heal_respects_backoff(self):
        self.env['UI_SINGLE_CHAIN_ENABLE'] = False
        heal_calls = []
        self.env['_ext_crash_page_self_heal'] = lambda *a, **k: heal_calls.append('crash')
        self.env['_ext_temp_unavailable_self_heal'] = lambda *a, **k: heal_calls.append('temp')
        blank = Page(self.clock, 'https://app.extended.exchange/trade', False, True)
        ctx = Context(self.clock, [blank])
        with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_deferred'):
            self.run_ensure(ctx)
        calls_after_failure = list(heal_calls)
        for _ in range(5):
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'ext_recovery_deferred'):
                self.run_ensure(ctx)
        self.assertEqual(heal_calls, calls_after_failure)
        self.assertEqual(len(blank.goto_calls), 1)
        self.assertEqual(ctx.new_count, 0)

    def test_route_mapping_preserves_market_query_and_fragment(self):
        route = self.env['_ext_navigation_url']
        self.assertEqual(route('https://app.extended.exchange/perp/Q-USD?a=1#positions'), 'https://app.extended.exchange/trade/Q-USD?a=1#positions')
        foreign = 'https://example.com/perp/Q-USD'
        self.assertEqual(route(foreign), foreign)

    def test_cooldown_is_a_failed_scan_without_runtime_rebuild(self):
        calls = []
        def fail(*a, **k):
            calls.append(k)
            raise self.env['_UiLinkError']('ensure_page.ext', 'ext_recovery_deferred retry_after_sec=60')
        self.env['_run_call_in_worker_thread'] = fail
        result = self.env['m2_scan']('fake', ['BTC'], 'fake')
        self.assertEqual(result['data_quality'], 'fail')
        self.assertEqual(result['note'], 'ext_recovery_wait_fail_closed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.resets, [])


class Future:
    def __init__(self, clock, mode='pending', ready_after=None):
        self.clock = clock
        self.started = clock.now
        self.mode = mode
        self.ready_after = ready_after

    def done(self):
        return self.mode == 'task_timeout'

    def result(self, timeout=None):
        if self.mode == 'task_timeout':
            raise TimeoutError('actual navigation timeout')
        self.clock.sleep(timeout or 0)
        if self.ready_after is not None and self.clock.now - self.started >= self.ready_after:
            return {'ok': True}
        raise concurrent.futures.TimeoutError()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.env, self.clock, self.logs, self.resets = make_env()
        self.hb = {'ts': self.clock.now, 'stage': 'PW_CALL_BEGIN', 'where': 'ensure_page.goto', 'timeout_ms': 26000}
        self.env['_worker_heartbeat_snapshot'] = lambda key: dict(self.hb)

    def run_worker(self, future, budget=90):
        self.env['_SYNC_PW_WORKERS']['M2SyncPlaywrightWorker'] = types.SimpleNamespace(submit=lambda *a: future)
        return self.env['_run_call_in_worker_thread'](noop, thread_name='M2SyncPlaywrightWorker', timeout_sec=budget, mod='M2', on_timeout_rebuild=True)

    def test_navigation_not_dead_at_eighteen_seconds(self):
        self.clock.sleep(18.3)
        reason, detail = self.env['_worker_heartbeat_timeout_reason'](worker_key='M2', timeout_sec=90, mod='M2')
        self.assertEqual(reason, '')
        self.assertEqual(detail['stale_sec'], 28)
        self.clock.sleep(10)
        reason, _ = self.env['_worker_heartbeat_timeout_reason'](worker_key='M2', timeout_sec=90, mod='M2')
        self.assertIn('M2_worker_stuck', reason)

    def test_navigation_succeeding_at_25_seconds_is_not_killed(self):
        self.assertEqual(self.run_worker(Future(self.clock, ready_after=25)), {'ok': True})
        self.assertEqual(self.resets, [])

    def test_stuck_worker_rebuilt_once_with_original_reason(self):
        with self.assertRaisesRegex(TimeoutError, 'M2_worker_stuck'):
            self.run_worker(Future(self.clock))
        self.assertEqual([r[0] for r in self.resets], ['stop', 'rebuild'])

    def test_completed_task_timeout_is_not_relabelled(self):
        with self.assertRaisesRegex(TimeoutError, '^actual navigation timeout$'):
            self.run_worker(Future(self.clock, mode='task_timeout'))
        self.assertEqual(self.resets, [])

    def test_success_racing_with_wait_timeout_is_returned(self):
        class RacingFuture:
            def done(self):
                return True

            def result(self, timeout=None):
                if timeout is not None:
                    raise concurrent.futures.TimeoutError()
                return {'ok': True, 'race': True}
        self.assertEqual(self.run_worker(RacingFuture()), {'ok': True, 'race': True})
        self.assertEqual(self.resets, [])

    def test_overall_ninety_second_deadline_remains_enforced(self):
        self.hb['timeout_ms'] = 120000
        with self.assertRaisesRegex(TimeoutError, 'M2_worker_timeout:90.0s'):
            self.run_worker(Future(self.clock))
        self.assertAlmostEqual(self.clock.now, 1090)
        self.assertEqual([r[0] for r in self.resets], ['stop', 'rebuild'])

    def test_executor_heartbeat_policy_unchanged(self):
        self.clock.sleep(40)
        reason, detail = self.env['_worker_heartbeat_timeout_reason'](worker_key='EXECUTOR', timeout_sec=54, mod='EXECUTOR')
        self.assertIn('EXECUTOR_worker_stuck', reason)
        self.assertEqual(detail['stale_sec'], 35)


if __name__ == '__main__':
    unittest.main(verbosity=2)
