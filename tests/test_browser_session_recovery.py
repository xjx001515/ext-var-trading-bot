"""Session and EXT recovery regressions. Every browser, driver and order is fake."""
import ast
from contextlib import contextmanager
import copy
import threading
import types
import unittest
from unittest.mock import Mock

from ast_support import ROOT, load_definitions, parsed
from bot_core.browser_runtime import SessionLiveness, ExtRecovery, supported_kwargs, safe_url
from bot_core import close_flow
import test_ext_recovery as ext_tests


def noop(*args, **kwargs):
    return None


class Browser:
    def __init__(self, contexts=()):
        self.contexts = list(contexts)
        self.connected = True
        self.created_targets = 0
        for ctx in self.contexts:
            ctx.browser = self

    def is_connected(self):
        return self.connected

    def new_browser_cdp_session(self):
        def send(method, params=None):
            if method == 'Target.getTargets':
                return {'targetInfos': [{'targetId': str(id(p))} for c in self.contexts for p in c.pages if not p.closed]}
            if method == 'Target.createTarget':
                self.created_targets += 1
                ctx = self.contexts[0]
                page = ext_tests.Page(ctx.clock, params['url'], healthy=True)
                page.context = ctx; ctx.pages.append(page); ctx.last_created = page
                return {'targetId': str(id(page))}
            raise AssertionError(method)
        return types.SimpleNamespace(send=send, detach=noop)


def context(clock, pages=(), fail_new=False):
    ctx = ext_tests.Context(clock, pages, fail_new)
    @contextmanager
    def expect_page(**kwargs):
        waiting = types.SimpleNamespace(value=None)
        yield waiting
        waiting.value = ctx.last_created
    ctx.expect_page = expect_page
    return ctx


def call(label, fn, *args, **kwargs):
    kwargs.pop('_operation_timeout_ms', None)
    return fn(*args, **kwargs)


def runtime_env(browser, healthy=True):
    sessions = SessionLiveness()
    ctx = browser.contexts[0]
    rt = {'pw': types.SimpleNamespace(stop=Mock()), 'browser': browser, 'context': ctx,
          'cdp': 'fake', 'owner_thread_ident': threading.get_ident(),
          'ext_page': ctx.pages[0], 'var_page': ctx.pages[1], 'ext_exe': object(), 'var_exe': object()}
    env = {'threading': threading, 'time': types.SimpleNamespace(time=lambda: 2000),
           '_UiDeadError': type('FakeUiDead', (Exception,), {}), '_UiLinkError': type('FakeUiLink', (Exception,), {}),
           '_PW_SESSIONS': sessions, '_SessionLiveness': SessionLiveness, 'PW_ALIVE': False,
           '_WORKER_STATE': types.SimpleNamespace(assert_live=noop, is_owner=lambda:True), '_worker_runtime_update_allowed': lambda: True,
           '_pw_runtime_is_recovering': lambda *a: False, '_pin': noop, '_record_swallowed_exception': noop,
           '_EXEC_RUNTIME_LOCK': threading.RLock(), '_EXEC_RUNTIME_CACHES': {'exec': rt},
           '_exec_runtime_worker_key': lambda **k: 'exec', '_exec_worker_stage_touch': noop,
           '_has_running_asyncio_loop': lambda: True, '_pw_runtime_mark_ready': Mock(),
           '_ext_effective_open_url': lambda **k: k['default_url'],
           'EXT': {'open_url':'https://app.extended.exchange/trade'}, 'EXT_NORMAL_OPEN_URL':'https://app.extended.exchange/trade',
           '_ExtExecutor': lambda page, **k: types.SimpleNamespace(page=page),
           '_VarExecutor': lambda page, **k: types.SimpleNamespace(page=page),
           '_page_health_check': lambda page, **k: {'ok':bool(page and page.healthy and not page.closed), 'reason':'unhealthy'},
           'ensure_page': Mock(side_effect=TimeoutError('recovery failed')), '_log_exc': noop,
           '_exec_ts': lambda: 2000, 'safe_log': noop,
           '_exec_perf_now': lambda: 2000, '_exec_perf_ms': lambda *a: 0, '_exec_perf_log_safe': noop, '_active_req_touch': noop,
           '_exec_batch_watch_update': noop}
    names = {'_pw_set_alive', '_pw_guard', '_exec_runtime_connection_alive', '_exec_runtime_page_alive',
             '_exec_runtime_cache_get', '_exec_runtime_get', '_rebuild_single_page_in_context',
             '_ensure_executor_pages_healthy', '_ensure_m2_pages_healthy', '_ExecRuntimeSession'}
    load_definitions(names, env)
    return env, rt


class SessionTests(unittest.TestCase):
    def test_m2_exit_releases_own_driver_and_keeps_executor_alive(self):
        sessions = SessionLiveness(); executor_browser = Browser()
        sessions.begin(executor_browser)
        stopped, entered, results = [], [], []
        @contextmanager
        def driver():
            entered.append(1)
            try:
                yield object()
            finally:
                stopped.append(1)
        env = load_definitions({'_owned_playwright_session'}, {
            'contextmanager':contextmanager, 'sync_playwright':driver, '_PW_SESSIONS':sessions})
        def scanner():
            with env['_owned_playwright_session']():
                sessions.begin(Browser())
                results.append(sessions.current_alive())
            results.append(sessions.current_alive())
        thread = threading.Thread(target=scanner); thread.start(); thread.join()
        self.assertEqual(results, [True, False])
        self.assertEqual((len(entered), len(stopped)), (1, 1))
        self.assertTrue(sessions.current_alive())
        self.assertTrue(env['PW_ALIVE'])

    def test_m2_exception_and_early_return_still_release_driver(self):
        for mode in ('raise', 'return'):
            sessions = SessionLiveness(); stopped = []
            @contextmanager
            def driver():
                try:
                    yield object()
                finally:
                    stopped.append(1)
            env = load_definitions({'_owned_playwright_session'}, {'contextmanager':contextmanager,
                'sync_playwright':driver, '_PW_SESSIONS':sessions})
            def run():
                with env['_owned_playwright_session']():
                    if mode == 'raise':
                        raise RuntimeError('scan read failed')
                    return 'early'
            if mode == 'raise':
                with self.assertRaises(RuntimeError): run()
            else:
                self.assertEqual(run(), 'early')
            self.assertEqual(stopped, [1]); self.assertFalse(sessions.current_alive())

    def test_global_true_does_not_authorize_unknown_or_disconnected_owner(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock, healthy=True), ext_tests.Page(clock, healthy=True)])
        browser = Browser([ctx]); env, rt = runtime_env(browser)
        env['PW_ALIVE'] = True
        self.assertFalse(env['_pw_guard']())
        env['_PW_SESSIONS'].begin(browser); self.assertTrue(env['_pw_guard']())
        browser.connected = False; self.assertFalse(env['_pw_guard']())
        browser.connected = True; env['_worker_runtime_update_allowed'] = lambda: False
        self.assertFalse(env['_pw_guard']())

    def test_cached_executor_revalidates_its_connection_before_health_check(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock, healthy=True), ext_tests.Page(clock, healthy=True)])
        browser = Browser([ctx]); env, rt = runtime_env(browser)
        self.assertFalse(env['PW_ALIVE'])  # Reproduce the old scanner's last-written flag.
        result, created, error = env['_exec_runtime_get']('fake', None)
        self.assertIsNotNone(result); self.assertFalse(created); self.assertEqual(error, '')
        self.assertTrue(env['_PW_SESSIONS'].current_alive())
        self.assertEqual(sum(p.close_calls for p in ctx.pages), 0)

    def test_foreign_thread_cannot_adopt_cached_runtime(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock)])
        env, rt = runtime_env(Browser([ctx])); rt['owner_thread_ident'] = -1
        self.assertFalse(env['_exec_runtime_connection_alive'](rt))

    def test_repaired_runtime_is_not_returned_as_a_preparation_error(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock)])
        env, rt = runtime_env(Browser([ctx]))
        env['_ensure_executor_pages_healthy'] = lambda *a, **k: (dict(rt, page_health_ok=True), True, 'ext_page:page_closed')
        result, _, error = env['_exec_runtime_get']('fake', None)
        self.assertIsNotNone(result); self.assertEqual(error, '')
        with env['_ExecRuntimeSession']('fake', None) as prepared:
            self.assertTrue(prepared['page_health_ok'])

    def test_m2_failed_recovery_does_not_return_old_page_as_usable(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock, healthy=True)])
        browser = Browser([ctx]); env, rt = runtime_env(browser)
        ext, var, repaired, reason = env['_ensure_m2_pages_healthy'](browser, rt['ext_page'], rt['var_page'], platforms=('EXT',))
        self.assertIsNone(ext); self.assertIs(var, rt['var_page'])
        self.assertIn('legacy_deleted_fail', reason)
        self.assertFalse(ctx.pages[0].closed)

    def test_existing_runtime_isolation_selfcheck_remains_valid(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock)])
        env, _ = runtime_env(Browser([ctx]))
        env['_PW_RUNTIME_RECOVERING'] = False
        env['_pw_runtime_is_recovering'] = lambda *a: env['_PW_RUNTIME_RECOVERING']
        env['_pw_runtime_begin_recover'] = lambda *a: env.update(_PW_RUNTIME_RECOVERING=True)
        load_definitions({'_exec_runtime_stop','_run_exec_runtime_isolation_regression_suite'}, env)
        report = env['_run_exec_runtime_isolation_regression_suite']()
        self.assertTrue(report['all_pass'], report)

    def test_disconnected_owned_driver_is_disposed_before_rebuild(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock)])
        browser = Browser([ctx]); env, rt = runtime_env(browser)
        load_definitions({'_exec_runtime_stop'}, env)
        browser.connected = False
        result, _, error = env['_exec_runtime_get']('fake', None)
        self.assertIsNone(result)
        self.assertEqual(error, 'runtime_rebuild_requires_idle_worker')
        rt['pw'].stop.assert_called_once()
        self.assertEqual(env['_EXEC_RUNTIME_CACHES'], {})

    def test_failed_repair_returns_no_executor_and_preserves_original_pages(self):
        clock = ext_tests.Clock(); ctx = context(clock, [ext_tests.Page(clock), ext_tests.Page(clock, healthy=True)])
        env, rt = runtime_env(Browser([ctx]))
        result, created, error = env['_exec_runtime_get']('fake', None)
        self.assertIsNone(result); self.assertFalse(created); self.assertIn('page_recovery_failed', error)
        self.assertEqual(sum(p.close_calls for p in ctx.pages), 0)
        with self.assertRaisesRegex(RuntimeError, 'page_recovery_failed'):
            with env['_ExecRuntimeSession']('fake', None):
                self.fail('An unhealthy runtime reached order execution')


class RecoveryTests(unittest.TestCase):
    def test_same_target_cdp_fallback_without_duplicate_or_close(self):
        clock = ext_tests.Clock(); ctx = context(clock, fail_new=True); browser = Browser([ctx])
        original = ctx.new_cdp_session
        def session(page):
            result = original(page); old_send = result.send
            def send(method, params=None):
                if method == 'Page.navigate':
                    page.url = params['url']; page.healthy = True
                    return {'frameId':'main'}
                return old_send(method, params)
            result.send = send
            return result
        ctx.new_cdp_session = session
        recovery = ExtRecovery(clock.time); events = []
        p = recovery.recover(browser, 'https://app.extended.exchange/trade', call=call,
                             log=lambda event, **k: events.append(event))
        self.assertTrue(p.healthy); self.assertEqual(ctx.new_count, 1); self.assertEqual(p.close_calls, 0)
        self.assertIn('CDP_NAV_ACCEPTED', events)

    def test_closed_owned_target_can_use_create_target_with_url(self):
        clock = ext_tests.Clock(); ctx = context(clock, fail_new=True); browser = Browser([ctx])
        recovery = ExtRecovery(clock.time)
        with self.assertRaises(TimeoutError):
            recovery.recover(browser, 'https://app.extended.exchange/trade', call=call, log=noop)
        recovery.fail('timeout'); ctx.pages[0].closed = True  # Simulate user/browser closure.
        p = recovery.recover(browser, 'https://app.extended.exchange/trade', call=call, log=noop)
        self.assertTrue(p.healthy); self.assertEqual(browser.created_targets, 1)
        self.assertEqual(ctx.new_count, 1); self.assertEqual(p.goto_calls, [])

    def test_fresh_wrapper_reuses_same_target_id(self):
        clock = ext_tests.Clock(); old_ctx = context(clock, fail_new=True); recovery = ExtRecovery(clock.time)
        with self.assertRaises(TimeoutError):
            recovery.recover(Browser([old_ctx]), 'https://app.extended.exchange/trade', call=call, log=noop)
        target = recovery.candidate_id
        page = ext_tests.Page(clock); ctx = context(clock, [page]); original = ctx.new_cdp_session
        def session(p):
            s = original(p); send = s.send
            s.send = lambda method, params=None: {'targetInfo':{'targetId':target}} if method == 'Target.getTargetInfo' else send(method, params)
            return s
        ctx.new_cdp_session = session
        self.assertIs(recovery.recover(Browser([ctx]), 'https://app.extended.exchange/trade', call=call, log=noop), page)
        self.assertEqual(ctx.new_count, 0)

    def test_unknown_creation_outcome_blocks_another_creation(self):
        clock = ext_tests.Clock(); ctx = context(clock); browser = Browser([ctx]); recovery = ExtRecovery(clock.time)
        ctx.new_page = Mock(side_effect=TimeoutError('reply lost'))
        for _ in range(2):
            with self.assertRaises((TimeoutError, RuntimeError)):
                recovery.recover(browser, 'https://app.extended.exchange/trade', call=call, log=noop)
        ctx.new_page.assert_called_once()

    def test_alarm_clock_starts_before_a_recovery_call_can_hang(self):
        clock = ext_tests.Clock(); ctx = context(clock); recovery = ExtRecovery(clock.time)
        def stuck_creation():
            clock.sleep(601)
            self.assertTrue(recovery.status()['alarm'])
            raise TimeoutError('creation reply missing')
        ctx.new_page = stuck_creation
        with self.assertRaises(TimeoutError):
            recovery.recover(Browser([ctx]), 'https://app.extended.exchange/trade', call=call, log=noop)

    def test_alarm_uses_first_failure_not_last_attempt_and_clears_on_recovery(self):
        clock = ext_tests.Clock(); recovery = ExtRecovery(clock.time)
        recovery.fail('initial')
        for _ in range(6):
            clock.sleep(100); recovery.fail('retry')
        status = recovery.status()
        self.assertEqual(status['elapsed_sec'], 600); self.assertTrue(status['alarm'])
        self.assertIn('app.extended.exchange/trade', status['message'])
        self.assertTrue(recovery.alarm_due()); self.assertFalse(recovery.alarm_due())
        clock.sleep(300); self.assertTrue(recovery.alarm_due())
        recovery.success(); self.assertFalse(recovery.status()['alarm']); self.assertEqual(recovery.status()['message'], '')

    def test_diagnostic_url_does_not_expose_query_or_fragment(self):
        self.assertEqual(safe_url('https://app.extended.exchange/trade?secret=x#token'), 'https://app.extended.exchange/trade')


class SDKSignatureTests(unittest.TestCase):
    def test_real_guard_preserves_original_page_and_locator_signatures(self):
        class Page:
            def evaluate(self, expression, *, arg=None): return 'page'
        class Locator:
            def evaluate(self, expression, *, arg=None, timeout=None): return timeout
        env = {'_playwright_runtime_guard_targets':lambda:[(Page,'evaluate','PAGE'),(Locator,'evaluate','LOCATOR')],
               '_pw_guard':lambda **k:True, '_WORKER_STATE':types.SimpleNamespace(assert_live=noop),
               '_browser_supported_kwargs':supported_kwargs, '_record_swallowed_exception':noop,
               '_is_target_closed_exc':lambda e:False, '_is_link_error_exc':lambda e:False,
               '_exec_stage_probe':noop, '_log_jsonl':noop}
        load_definitions({'_pw_call', '_pw_call_timeout_ms', '_install_playwright_runtime_guards'}, env)
        env['_install_playwright_runtime_guards']()
        self.assertEqual(env['_pw_call']('post_goto.health', Page().evaluate, 'script', timeout=4500), 'page')
        self.assertEqual(env['_pw_call']('post_goto.health', Locator().evaluate, 'script', timeout=4500), 4500)


class PanelAlarmTests(unittest.TestCase):
    def test_actual_panel_render_sets_and_clears_recovery_warning(self):
        tree = ast.parse((ROOT/'面板4-11.py').read_text(encoding='utf-8-sig'))
        statements = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)
                      and 'self.var_browser_recovery_alert.set(' in ast.unparse(node)]
        self.assertEqual(len(statements), 1)
        setter = Mock()
        env = {'self':types.SimpleNamespace(var_browser_recovery_alert=types.SimpleNamespace(set=setter))}
        code = compile(ast.fix_missing_locations(ast.Module(body=statements,type_ignores=[])), '<panel alert render>', 'exec')
        for message in ('EXT 页面恢复失败，请手动打开 app.extended.exchange/trade', ''):
            env['data'] = {'browser_recovery_alert':message}; exec(code,env)
            setter.assert_called_with(message)


class AnchorRecoveryTests(unittest.TestCase):
    def test_failed_close_keeps_anchor_then_next_round_can_enqueue_again(self):
        tree = parsed(str(ROOT / 'BOT5-2-414 .py'))
        class Request:
            def __init__(self, **kwargs): self.__dict__.update(kwargs); self.meta = {}
            def dedupe_key(self): return 'ANCHOR|ALL|PAIR|close_pair'
            def to_dict(self): return dict(self.__dict__)
        anchor = [162.78]; updates = []
        def update(value, **kwargs): anchor[0] = value; updates.append(value); return value
        env = {'time':types.SimpleNamespace(time=lambda:2000, sleep=noop), 'Request':Request,
               '_pin':noop, '_log_exc':noop, '_record_swallowed_exception':noop, 'write_jsonl':noop, 'now_ts':lambda:2000,
               '_anchor_is_enabled':lambda:True, 'ANCHOR_TAKEPROFIT_PCT':0.015,
               '_anchor_update':update, '_anchor_bump_epoch':Mock(), '_wait_enter_no_exit':Mock(),
               '_request_obj_b1_first_open_amount_guard':lambda *a,**k:(True,'skip',{}),
               'validate_request':lambda r:(True,'ok',{}), '_noise_guard_check':lambda r:(True,'ok',{}),
               '_round_ui_budget_consume':lambda *a,**k:(True,'ok'), 'safe_float':lambda x,default=0:default if x is None else float(x),
               '_request_ensure_enqueue_round_meta':noop, '_apply_final_amount_contract_to_request':noop,
               '_exec_dispatch_claim_peek':lambda *a,**k:{}, '_request_pending_timeout_sec':lambda *a,**k:90,
               'print':noop, '_append_txt':noop, '_rizhi_path':lambda s:s,
               'fp_yunxing':'unused','fp_baocuo':'unused','coins':['MAGMA'], '_anchor_obs':{}, 'total_u':165.75}
        env.update(EXT={'name':'EXT'}, VAR={'name':'VAR'}, safe_log=lambda f:f(),
                   _exec_positions_confirmed_empty=lambda page,cfg:bool(page.flat),
                   _exec_read_portfolio_snapshot=lambda *a,**k:{'available':False},
                   _safety_submission_records=lambda:{})
        env.update(_close_flow=close_flow,_WORKER_STATE=types.SimpleNamespace(assert_live=noop),
                   _active_req_bind=noop,_full_close_recover_page=Mock(return_value=False))
        load_definitions({'_exec_global_close_report','_run_full_close_transaction'}, env)
        load_definitions({'_anchor_should_trigger','_anchor_next_trigger_u','RequestQueue'}, env)
        env['q'] = env['RequestQueue']('unused')
        trigger = next(n for n in ast.walk(tree) if isinstance(n,ast.If) and isinstance(n.test,ast.Call)
                       and isinstance(n.test.func,ast.Name) and n.test.func.id=='_anchor_should_trigger')
        trigger_code = compile(ast.fix_missing_locations(ast.Module(body=[trigger],type_ignores=[])), '<actual anchor trigger>', 'exec')
        source = (ROOT/'BOT5-2-414 .py').read_text(encoding='utf-8-sig')
        start = source.index('                        close_platforms = _run_full_close_transaction(')
        end = source.index('                    elif act == "koujing_a_3step":', start)
        import textwrap
        block = ast.parse(textwrap.dedent(source[start:end]))
        block.body = [n for n in block.body if not isinstance(n, ast.Continue)]
        close_code = compile(ast.fix_missing_locations(block), '<actual anchor result/commit>', 'exec')
        env.update(anchor_u=anchor[0]); exec(trigger_code, env)
        self.assertEqual(len(env['q'].items), 1)
        request = env['q'].items.pop(0)  # Main loop consumes the queued request; retain old dedupe key.
        env.update(req=request, params=request.params, coins_list=['MAGMA'], results=[],
                   ext_exe=types.SimpleNamespace(page=types.SimpleNamespace(flat=False), close_all_global=Mock(side_effect=TimeoutError('page recovery failed'))),
                   var_exe=types.SimpleNamespace(page=types.SimpleNamespace(flat=False), close_all_global=Mock(return_value=False)))
        exec(close_code, env)
        self.assertFalse(env['results'][-1]['ok']); self.assertFalse(env['results'][-1]['trade_effective'])
        self.assertEqual(anchor[0], 162.78); self.assertEqual(updates, [])
        env.update(anchor_u=anchor[0]); exec(trigger_code, env)
        self.assertEqual(len(env['q'].items), 1, 'Failure must not leave a stale dedupe key blocking the next round')
        request = env['q'].items.pop(0); env.update(req=request, params=request.params)
        env['ext_exe'].close_all_global = Mock(return_value=True); env['var_exe'].close_all_global = Mock(return_value=True)
        env['ext_exe'].page.flat = True
        # Submission booleans are insufficient while VAR still has a position.
        exec(close_code, env)
        self.assertFalse(env['results'][-1]['ok']); self.assertEqual(updates, [])
        self.assertEqual(env['results'][-1]['close_platforms']['VAR']['unverified_coins'], ['MAGMA'])
        env['var_exe'].page.flat = True
        exec(close_code, env)
        self.assertTrue(env['results'][-1]['ok']); self.assertEqual(updates, [165.75])


if __name__ == '__main__':
    unittest.main()
