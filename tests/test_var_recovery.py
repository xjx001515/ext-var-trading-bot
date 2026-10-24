"""VAR recovery and 03:02 heap-recycle replay using fake browser targets only."""
import copy
import json
import sys
import textwrap
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ast_support import ROOT, load_definitions
from bot_core.browser_runtime import ExtRecovery, PageIdentity
import test_ext_recovery as ext
from test_browser_session_recovery import Browser, context, call


class VarRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env, self.clock, self.logs, self.resets = ext.make_env()

    def ensure(self, browser):
        return self.env['ensure_page'](browser, 'omni.variational.io', 'https://omni.variational.io/perpetual/BTC')

    def test_0302_readable_page_survives_heap_recycle_hint(self):
        p = ext.Page(self.clock, 'https://omni.variational.io/perpetual/BTC', healthy=True)
        ctx = ext.Context(self.clock, [p])
        self.env['_ensure_page_should_recycle'] = lambda *a, **k: (True, 'var_heap_grow_10m:255.9mb', {'heap_mb': 562.094, 'grow_mb_10m': 255.857})
        self.assertIs(self.ensure(SimpleNamespace(contexts=[ctx])), p)
        self.assertEqual(ctx.new_count, 0)
        self.assertEqual(p.close_calls, 0)
        self.assertEqual(p.goto_calls, [])

    def test_unhealthy_original_recovers_in_place_without_new_target(self):
        p = ext.Page(self.clock, 'https://omni.variational.io/perpetual/BTC', healthy=False)
        ctx = ext.Context(self.clock, [p])
        self.assertIs(self.ensure(SimpleNamespace(contexts=[ctx])), p)
        self.assertEqual(len(p.goto_calls), 1)
        self.assertEqual(p.goto_calls[0][1]['wait_until'], 'commit')
        self.assertEqual((p.close_calls, ctx.new_count), (0, 0))

    def test_var_discovery_keeps_original_account_context(self):
        primary = ext.Context(self.clock)
        other_page = ext.Page(self.clock,'https://omni.variational.io/perpetual/BTC',healthy=True)
        other = ext.Context(self.clock,[other_page])
        recovered = self.ensure(Browser([primary,other]))
        self.assertIs(recovered.context,primary)
        self.assertEqual(other_page.goto_calls,[])
        self.assertFalse(other_page.closed)

    def test_var_rebuild_failure_keeps_old_page_and_names_correct_platform(self):
        p = ext.Page(self.clock,'https://omni.variational.io/perpetual/BTC',fail_nav=True)
        ctx = ext.Context(self.clock,[p]); Browser([ctx])
        load_definitions({'_rebuild_single_page_in_context'},self.env)
        page, reason = self.env['_rebuild_single_page_in_context'](ctx,target_url=p.url,platform='VAR')
        self.assertIsNone(page)
        self.assertTrue(reason.startswith('var_preserved_recovery_failed'))
        self.assertFalse(p.closed)
        self.assertEqual(ctx.new_count,0)

    def test_failure_keeps_original_and_backs_off_without_global_reset(self):
        p = ext.Page(self.clock, 'https://omni.variational.io/perpetual/BTC', fail_nav=True)
        ctx = ext.Context(self.clock, [p])
        browser = SimpleNamespace(contexts=[ctx])
        for _ in range(2):
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'var_recovery_deferred'):
                self.ensure(browser)
        self.assertFalse(p.closed)
        self.assertEqual(ctx.new_count, 0)
        self.assertEqual(len(p.goto_calls), 1)
        self.assertFalse(self.resets)

    def test_failed_blank_candidate_is_reused_not_closed_or_recreated(self):
        ctx = ext.Context(self.clock, fail_new=True)
        browser = SimpleNamespace(contexts=[ctx])
        for n in range(3):
            if n:
                self.clock.sleep(400)
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'var_recovery_deferred'):
                self.ensure(browser)
        self.assertEqual(ctx.new_count, 1)
        self.assertEqual(ctx.pages[0].close_calls, 0)
        self.assertEqual(len(ctx.pages[0].goto_calls), 3)

    def test_var_registry_registration_cannot_close_original_tab(self):
        old = ext.Page(self.clock, 'https://omni.variational.io/perpetual/ETH', healthy=True)
        new = ext.Page(self.clock, 'https://omni.variational.io/perpetual/BTC', healthy=True)
        ctx = ext.Context(self.clock, [old, new]); browser = SimpleNamespace(contexts=[ctx])
        # SimpleNamespace is not weak-referenceable, so use the real fake Browser.
        browser = Browser([ctx])
        self.env['_OWNED_PAGE_REGISTRY'].set(browser, 'VAR', old)
        self.env['_ensure_page_registry_set'](browser, 'VAR', new, close_old=True)
        self.assertEqual(old.close_calls, 0)
        self.assertEqual(len(ctx.pages), 2)

    def test_reconnect_rediscovers_same_var_target(self):
        clock = self.clock; recovery = ExtRecovery(clock.time, platform='VAR')
        first = context(clock, fail_new=True)
        with self.assertRaises(Exception):
            recovery.recover(Browser([first]), 'https://omni.variational.io/perpetual/BTC', call=call, log=lambda *a, **k: None)
        target = recovery.candidate_id
        fresh_ctx = context(clock)
        p = fresh_ctx.new_page()
        original_session = fresh_ctx.new_cdp_session
        def same_target_session(page):
            session = original_session(page)
            old_send = session.send
            session.send = lambda method, params=None: ({'targetInfo': {'targetId': target}}
                if method == 'Target.getTargetInfo' else old_send(method, params))
            return session
        fresh_ctx.new_cdp_session = same_target_session
        p.healthy = True
        p.url = 'https://omni.variational.io/perpetual/BTC'
        found = recovery.recover(Browser([fresh_ctx]), p.url, call=call, log=lambda *a, **k: None)
        self.assertIs(found, p)
        self.assertEqual(fresh_ctx.new_count, 1)

    def test_platform_recovery_locks_and_candidates_are_independent(self):
        self.env['_VAR_ENSURE_LOCK'].acquire()
        try:
            with self.assertRaisesRegex(self.env['_UiLinkError'], 'var_recovery_in_progress'):
                self.ensure(SimpleNamespace(contexts=[]))
            p = ext.Page(self.clock, 'https://app.extended.exchange/trade', healthy=True)
            ctx = ext.Context(self.clock, [p])
            self.assertIs(self.env['ensure_page'](SimpleNamespace(contexts=[ctx]), 'extended.exchange', p.url), p)
        finally:
            self.env['_VAR_ENSURE_LOCK'].release()

    def test_stalled_lock_starts_alarm_clock(self):
        self.env['_VAR_ENSURE_LOCK'].acquire()
        try:
            with self.assertRaises(self.env['_UiLinkError']):
                self.ensure(SimpleNamespace(contexts=[]))
            self.clock.sleep(601)
            self.assertTrue(self.env['_VAR_RECOVERY'].status()['alarm'])
        finally:
            self.env['_VAR_ENSURE_LOCK'].release()

    def test_recovery_cannot_navigate_a_target_repurposed_for_other_platform(self):
        recovery = ExtRecovery(self.clock.time, platform='VAR')
        p = ext.Page(self.clock, 'https://app.extended.exchange/trade', healthy=True)
        ctx = ext.Context(self.clock, [p])
        with self.assertRaisesRegex(RuntimeError, 'other_platform_no_navigation'):
            recovery.recover(SimpleNamespace(contexts=[ctx]), 'https://omni.variational.io/perpetual/BTC',
                             page=p, call=call, log=lambda *a,**k:None)
        self.assertEqual(p.goto_calls,[]); self.assertFalse(p.closed)

    def test_var_deferred_recovery_returns_failed_scan_without_global_hard_reset(self):
        self.env['_run_call_in_worker_thread'] = lambda fn, **kwargs: fn()
        def fail(*a, **k):
            raise self.env['_UiLinkError']('ensure_page.var','var_recovery_deferred retry_after_sec=60')
        self.env['_m2_scan_impl'] = fail
        result = self.env['m2_scan']('fake', ['TIA'], 'unused', only_positions=True)
        self.assertEqual(result['data_quality'],'fail')
        self.assertEqual(result['note'],'var_recovery_wait_fail_closed')
        self.assertFalse(self.resets)

    def test_both_alerts_survive_independent_recovery(self):
        source = (ROOT/'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
        a = source.index('def _status_attach_single_source_truth(')
        b = source.index('    try:\n        out = _runtime_state_apply_to_status', a)
        prefix = source[a:b] + '    return out\n'
        self.env['dict'] = dict
        exec(compile('from __future__ import annotations\n'+prefix, '<status prefix>', 'exec'), self.env)
        for name in ('_EXT_RECOVERY', '_VAR_RECOVERY'):
            self.env[name].fail('navigation timeout')
        self.clock.sleep(601)
        status = self.env['_status_attach_single_source_truth']({})
        self.assertIn('EXT', status['browser_recovery_alert'])
        self.assertIn('VAR', status['browser_recovery_alert'])
        self.env['_EXT_RECOVERY'].success()
        status = self.env['_status_attach_single_source_truth']({})
        self.assertNotIn('EXT', status['browser_recovery_alert'])
        self.assertIn('VAR', status['browser_recovery_alert'])


class HealthIdentityTests(unittest.TestCase):
    def env(self):
        clock = ext.Clock(); clock.now = 10000
        env = {'time': clock, '_PAGE_IDENTITIES': PageIdentity(), '_ENSURE_PAGE_HEALTH': {},
               '_pw_call': call, '_env_float': lambda k,d:d, '_env_int': lambda k,d:d}
        names = {'_ensure_page_health_platform', '_ensure_page_health_key',
                 '_ensure_page_health_env_float', '_ensure_page_health_env_int',
                 '_ensure_page_health_read_heap_mb', '_ensure_page_health_reset_for_page',
                 '_ensure_page_health_touch', '_ensure_page_should_recycle'}
        load_definitions(names, env)
        return env, clock

    def page(self, target='var-real-tab'):
        p = ext.Page(ext.Clock(), 'https://omni.variational.io/perpetual/BTC', healthy=True)
        p.context = SimpleNamespace(new_cdp_session=lambda page: SimpleNamespace(
            send=lambda method: {'targetInfo': {'targetId': target}}, detach=lambda:None))
        p.evaluate = lambda js:562.094
        return p

    def test_same_target_different_wrappers_share_health_identity(self):
        env, _ = self.env()
        self.assertEqual(env['_ensure_page_health_key'](self.page(), 'VAR'), env['_ensure_page_health_key'](self.page(), 'VAR'))

    def test_platform_identity_never_uses_another_platforms_cache_key(self):
        env, _ = self.env(); p = self.page()
        self.assertEqual(env['_ensure_page_health_key'](p,'EXT'),'EXT:var-real-tab')
        self.assertEqual(env['_ensure_page_health_key'](p,'VAR'),'VAR:var-real-tab')

    def test_different_targets_never_inherit_each_others_heap_baseline(self):
        env, _ = self.env()
        a, b = self.page('a'), self.page('b')
        env['_ensure_page_health_touch'](a, 'VAR', force_probe=True)
        self.assertNotEqual(env['_ensure_page_health_key'](a,'VAR'), env['_ensure_page_health_key'](b,'VAR'))
        row = env['_ensure_page_health_touch'](b, 'VAR', force_probe=True)
        self.assertEqual(row['grow_mb_10m'], 0)

    def test_expired_two_hour_baseline_does_not_trigger_ten_minute_growth(self):
        env, clock = self.env(); p = self.page()
        row = env['_ensure_page_health_reset_for_page'](p, 'VAR')
        row.update(born_ts=2394.969, heap_base_ts=2394.969, heap_base_mb=306.237)
        recycle, reason, detail = env['_ensure_page_should_recycle'](p, 'VAR')
        self.assertFalse(recycle, reason)
        self.assertEqual(detail['grow_mb_10m'], 0)
        self.assertEqual(detail['heap_base_ts'], clock.time())

    def test_real_growth_in_valid_window_is_retained_as_diagnostic(self):
        env, clock = self.env(); p = self.page()
        row = env['_ensure_page_health_reset_for_page'](p, 'VAR')
        row.update(heap_base_ts=clock.time()-300, heap_base_mb=306.237)
        recycle, reason, detail = env['_ensure_page_should_recycle'](p, 'VAR')
        self.assertTrue(recycle)
        self.assertAlmostEqual(detail['grow_mb_10m'],255.857)

    def test_close_audit_logs_target_caller_reason_and_blocks_managed_tab(self):
        env, clock = self.env(); p = self.page(); log = Mock()
        env.update(_WORKER_STATE=SimpleNamespace(assert_live=lambda:None), sys=sys,
                   threading=threading, _browser_safe_url=lambda s:s, _ext_recovery_log=log,
                   _ensure_page_health_forget=Mock())
        load_definitions({'_browser_close_page'}, env)
        self.assertFalse(env['_browser_close_page'](p, platform='VAR', reason='replay old recycle'))
        self.assertFalse(p.closed)
        self.assertEqual(log.call_args_list[0].args[0], 'PAGE_CLOSE_REQUEST')
        evidence = log.call_args_list[0].kwargs
        self.assertEqual(evidence['target_id'], 'var-real-tab')
        self.assertIn('caller', evidence)
        self.assertIn('line', evidence)
        self.assertEqual(log.call_args_list[-1].args[0], 'PAGE_CLOSE_BLOCKED')


if __name__ == '__main__':
    unittest.main()
