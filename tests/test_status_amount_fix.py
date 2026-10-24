"""Offline replays of the observed stale status and 190 -> 161.18 -> 190 bugs."""
import ast
import copy
import math
import threading
import time
import types
import unittest
from unittest.mock import Mock

from ast_support import ROOT, load_definitions, parsed
from bot_core.panel_state import merge_status_snapshot


BEFORE = ROOT / 'repair_backups/status_amount_fix_20261023/BOT5-2-414 .py'
AMOUNT_NAMES = {
    '_coerce_final_amount_u', '_apply_final_amount_contract',
    '_is_b1_new_coin_first_open_req', '_b1_first_open_expected_amount_u',
    '_lock_b1_first_open_amount_contract', '_b1_first_open_amount_contract_sync',
    '_b1_fill_required_fields', '_b1_scale_first_open_amount', '_b1_first_open_min_effective_u',
    '_req_amount_u_read', '_req_amount_u_write', '_hardcap_precheck_and_autoclamp',
    '_coin_platform_cap_u_from_snapshot', '_platform_equity_u_from_snapshot',
    '_yingding_ratio_for_coin', '_normalize_request_amount_truth', '_m9_open_locked_amount_truth',
    'yingding_jiancha',
}


def amount_env(path=None, dynamic=True, multiplier=1):
    pick = Mock(return_value=(190.0, {}))
    env = {
        'math': math, 'copy': copy, 'FINAL_AMOUNT_FIELDS':
        ('final_amount_u', 'amount_u', 'amount_usd', 'usd', 'u', 'amount', 'notional_u'),
        '_safe_float': lambda x, d=0: d if x is None else float(x),
        '_canon_action_type': lambda a: str(a or '').lower(),
        '_coin_pool_is_dynamic_new_coin': lambda c: dynamic,
        '_b1_pick_u_in_bucket_no_repeat': pick, '_snap_get_rsi': lambda *a: 65.406,
        '_dynamic_new_coin_round_clamp_u': lambda u, c: min(u, 1000),
        'cfg_getf_runtime': lambda k, d: multiplier if k == 'B1_FIRST_OPEN_AMOUNT_MULTIPLIER' else d,
        '_pin': Mock(), '_rsi_amount_pin': Mock(), '_log_exc': Mock(),
        '_record_swallowed_exception': Mock(), '_record_new_coin_cap_event': Mock(),
        '_req_is_safe_reduce_only': lambda r: r.get('reduce_only') is True,
        '_new_coin_single_open_cap_u': lambda: 1000, '_new_coin_total_hardcap_u': lambda: 1000,
        '_dynamic_new_coin_total_notional_u': lambda s, c: sum(abs(v.get(c, {}).get('notional_u', 0)) for v in s.get('positions', {}).values()),
        '_pos_root': lambda s: s.get('positions', {}), 'LAST_GOOD_SNAPSHOT': {},
        '_a1_extract_leg': lambda s, c, p: (s.get('positions', {}).get(p, {}).get(c, {}).get('side', 'NONE'), s.get('positions', {}).get(p, {}).get(c, {}).get('notional_u', 0)),
        'B1_NO_LEG_EPS_U': 0.01, 'PEIZHI': {}, 'ZIJIN_MAODIAN_U': 2000,
        '_total_u_from_snapshot_or_anchor': lambda s, a: sum(v['usd'] for v in s['balances'].values()),
        '_quanju_sum_notional_u': lambda s: 0, '_is_reduce_action': lambda a: a in ('close', 'reduce'),
    }
    load_definitions(AMOUNT_NAMES, env, path=path)
    tree = parsed(str(path or ROOT / 'BOT5-2-414 .py'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'GOMenHu')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_std_amount')
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), '<isolated std_amount>', 'exec'), env)
    return env, pick


def snapshot(equity=80.59, current=0):
    return {'balances': {'EXT': {'usd': 81.16}, 'VAR': {'usd': equity}},
            'positions': {'EXT': {'MAGMA': {'side': 'LONG', 'notional_u': 156.72, 'qty_abs': 310}},
                          'VAR': {'IRYS': {'side': 'NONE' if not current else 'LONG', 'notional_u': current, 'reason': 'ok'}}}}


def request(**changes):
    return dict({'module': 'B1', 'coin': 'IRYS', 'platform': 'VAR', 'side': 'LONG',
                 'action_type': 'open_leg', 'params': {}, 'meta': {}}, **changes)


class AmountReplayTests(unittest.TestCase):
    def assert_amount(self, req, expected):
        for obj in (req, req['params'], req['meta']):
            for key in ('final_amount_u', 'amount_u', 'amount_usd', 'intent_amount', 'amount_usd_intent'):
                self.assertAlmostEqual(obj[key], expected, msg=key)
        self.assertEqual(req['meta']['amount_locked_final_u'], expected)
        self.assertEqual(req['meta']['b1_amount_expected_u'], expected)
        self.assertEqual(req['params']['B1__amount_usd'], expected)
        self.assertEqual(req['meta']['amt_det']['final_u'], expected)
        self.assertEqual(req['meta']['extras']['amount_usd_intent'], expected)

    def test_original_reproduces_190_after_161_18_cap(self):
        env, _ = amount_env(BEFORE)
        req, snap = request(), snapshot()
        env['_b1_fill_required_fields'](req, snap)
        self.assertEqual(req['amount_usd'], 190)
        ok, _, _ = env['_hardcap_precheck_and_autoclamp'](snap, req)
        self.assertTrue(ok)
        self.assertAlmostEqual(req['amount_usd'], 161.18)
        env['_b1_fill_required_fields'](req, snap)
        self.assertEqual(req['amount_usd'], 190)

    def test_full_chain_preserves_capped_amount_and_magma_snapshot(self):
        env, pick = amount_env()
        req, snap = request(), snapshot()
        original_snapshot = copy.deepcopy(snap)
        env['_b1_fill_required_fields'](req, snap)
        ok, reason, _ = env['_hardcap_precheck_and_autoclamp'](snap, req)
        self.assertEqual((ok, reason), (True, 'hardcap_preclamp'))
        for _ in range(3):
            env['_b1_fill_required_fields'](req, snap)
            env['_normalize_request_amount_truth'](req)
            req = env['_std_amount'](types.SimpleNamespace(snapshot=snap), req)
            self.assertTrue(env['_b1_first_open_amount_contract_sync'](req)[0])
            self.assertTrue(env['_hardcap_precheck_and_autoclamp'](snap, req)[0])
            self.assert_amount(req, 161.18)
        self.assertEqual(pick.call_count, 1)
        self.assertTrue(env['yingding_jiancha'](snap, 'IRYS', 'VAR', req['amount_usd'], 'B1', 'open')[0])
        self.assertEqual(snap, original_snapshot)

    def test_minimum_160_and_platform_ratio_are_preserved(self):
        env, _ = amount_env()
        self.assertEqual(env['_b1_first_open_min_effective_u'](), 160)
        self.assertEqual(env['_yingding_ratio_for_coin']('IRYS'), 2)
        self.assertEqual(env['_yingding_ratio_for_coin']('BTC'), 2.5)
        self.assertEqual(env['_yingding_ratio_for_coin']('ETH'), 2.5)
        for equity, allowed in ((67.02, False), (79.999, False), (80, True), (80.59, True)):
            with self.subTest(equity=equity):
                req, snap = request(), snapshot(equity)
                env['_b1_fill_required_fields'](req, snap)
                ok, reason, _ = env['_hardcap_precheck_and_autoclamp'](snap, req)
                self.assertEqual(ok, allowed)
                if not allowed:
                    self.assertEqual(reason, 'b1_cap_below_min_amount')

    def test_current_exposure_reduces_room_and_zero_room_rejects(self):
        env, _ = amount_env()
        for current, expected in ((10, 'b1_cap_below_min_amount'), (161.18, 'hardcap_precheck_no_room')):
            req, snap = request(), snapshot(current=current)
            env['_b1_fill_required_fields'](req, snap)
            ok, reason, _ = env['_hardcap_precheck_and_autoclamp'](snap, req)
            self.assertFalse(ok)
            self.assertEqual(reason, expected)

    def test_smaller_new_coin_total_cap_still_rejects_below_minimum(self):
        env, _ = amount_env()
        env['_new_coin_total_hardcap_u'] = lambda: 150
        req, snap = request(), snapshot()
        env['_b1_fill_required_fields'](req, snap)
        self.assertEqual(env['_hardcap_precheck_and_autoclamp'](snap, req)[:2], (False, 'b1_cap_below_min_amount'))

    def test_non_dynamic_amount_not_scaled_twice_or_rounded_after_cap(self):
        env, pick = amount_env(dynamic=False, multiplier=2)
        req, snap = request(amount_usd=100), snapshot()
        env['_b1_fill_required_fields'](req, snap)
        self.assertEqual(req['amount_usd'], 200)
        env['_hardcap_precheck_and_autoclamp'](snap, req)
        env['_b1_fill_required_fields'](req, snap)
        self.assert_amount(req, 161.18)
        pick.assert_not_called()

    def test_missing_leg_mirror_rule_remains_independent_of_first_open_floor(self):
        env, _ = amount_env()
        req = request(coin='MAGMA', side='SHORT', meta={'missing_leg_mode': True})
        snap = snapshot(); snap['positions']['EXT']['MAGMA']['notional_u'] = 100
        env['_b1_fill_required_fields'](req, snap)
        self.assertEqual(req['amount_usd'], 100)
        self.assertTrue(env['_hardcap_precheck_and_autoclamp'](snap, req)[0])

    def test_missing_params_meta_still_retains_contract(self):
        env, pick = amount_env()
        req = request(); del req['params']; del req['meta']
        env['_b1_fill_required_fields'](req, snapshot())
        env['_b1_fill_required_fields'](req, snapshot())
        self.assertEqual(pick.call_count, 1)
        self.assert_amount(req, 190)

    def test_close_request_is_not_resized(self):
        env, _ = amount_env()
        req = request(module='M9', action_type='close', amount_usd=123, reduce_only=True)
        before = copy.deepcopy(req)
        self.assertEqual(env['_hardcap_precheck_and_autoclamp'](snapshot(), req)[:2], (True, 'skip:risk_reduce'))
        self.assertEqual(req, before)


class PanelStatusTests(unittest.TestCase):
    def stale(self):
        return {'pid': 10, 'runtime_source': 'stopped_by_user', 'err': 'old',
                'runtime_state': {'round_state': 'ERROR', 'pw_recovering': True, 'err': 'old'},
                'scan_watch': {'state': 'ERROR', 'extra': {'retry_attempt': 2, 'detail': 'runtime_recovering'}},
                'trend_observe': {'coins': ['MAGMA']}, 'selection_observe': {'head': 'IRYS'}}

    def test_success_removes_stale_fields_but_keeps_other_sections(self):
        old = self.stale(); saved = copy.deepcopy(old)
        patch = {'pid': 10, 'runtime_state': {'round_state': 'SLEEPING', 'runtime_source': 'round_done'},
                 'scan_watch': {'state': 'DONE', 'reason': 'scan_ok', 'extra': {'dq': 'ok'}}}
        out = merge_status_snapshot(old, patch)
        self.assertEqual(out['runtime_state'], patch['runtime_state'])
        self.assertEqual(out['scan_watch'], patch['scan_watch'])
        self.assertEqual(out['runtime_source'], 'round_done')
        self.assertEqual(out['err'], '')
        self.assertEqual(out['trend_observe'], old['trend_observe'])
        self.assertEqual(out['selection_observe'], old['selection_observe'])
        self.assertEqual(old, saved)

    def test_actual_new_error_is_never_hidden(self):
        patch = {'runtime_state': {'err': 'new_timeout', 'pw_recovering': True},
                 'scan_watch': {'state': 'ERROR', 'reason': 'new_timeout'}}
        out = merge_status_snapshot(self.stale(), patch)
        self.assertEqual(out['err'], 'new_timeout')
        self.assertTrue(out['runtime_state']['pw_recovering'])

    def test_new_pid_drops_prior_runtime_but_partial_portal_update_preserves_current(self):
        out = merge_status_snapshot(self.stale(), {'pid': 11, 'ts': 2000})
        self.assertNotIn('runtime_state', out)
        self.assertNotIn('scan_watch', out)
        base = self.stale()
        out = merge_status_snapshot(base, {'grid_book_observe': {'url': 'local'}})
        self.assertEqual(out['runtime_state'], base['runtime_state'])
        self.assertEqual(out['scan_watch'], base['scan_watch'])

    def test_empty_observation_clears_old_keys_and_nested_partial_sections_merge(self):
        out = merge_status_snapshot({'scan_watch': {'old': 1}, 'trend': {'a': 1}}, {'scan_watch': {}, 'trend': {'b': 2}})
        self.assertEqual(out['scan_watch'], {})
        self.assertEqual(out['trend'], {'a': 1, 'b': 2})

    def test_real_bridge_uses_replacement_on_both_output_files(self):
        files = {'primary': self.stale(), 'fallback': self.stale()}
        env = load_definitions({'_panel_bridge_write_json'}, {
            '_PANEL_BRIDGE_WRITE_LOCK': threading.RLock(), '_merge_panel_status_snapshot': merge_status_snapshot,
            'safe_read_json': lambda path, default: copy.deepcopy(files.get(path, default)),
            'safe_write_json': lambda path, value: files.update({path: copy.deepcopy(value)}),
            '_log_exc': Mock(), '_record_swallowed_exception': Mock(),
        })
        env['_panel_bridge_write_json']('primary', 'fallback', {'runtime_state': {'round_state': 'SLEEPING'}})
        for out in files.values():
            self.assertNotIn('pw_recovering', out['runtime_state'])
            self.assertEqual(out['err'], '')
        env['_log_exc'].assert_not_called()

    def test_runtime_explicit_clear_and_omitted_error_have_distinct_meanings(self):
        env = load_definitions({'_runtime_state_set', '_runtime_state_normalize'}, {
            '_RUNTIME_STATE': {'err': 'old'}, '_worker_runtime_update_allowed': lambda: True,
            'time': types.SimpleNamespace(time=lambda: 2000),
        })
        self.assertEqual(env['_runtime_state_set'](state='ERROR')['err'], 'old')
        self.assertEqual(env['_runtime_state_set'](state='RUNNING', err='')['err'], '')
        self.assertEqual(env['_runtime_state_set'](state='ERROR', err='new')['err'], 'new')

    def test_new_scan_resets_id_start_time_and_old_retry_details(self):
        env = load_definitions({'_m2_scan_watch_update'}, {
            '_worker_runtime_update_allowed': lambda: True, 'threading': threading,
            'time': types.SimpleNamespace(time=lambda: 2000),
            '_M2_SCAN_WATCH': {'started_ts': 1000, 'round_id': 'old', 'extra': {'retry_attempt': 2}},
            'panel_update_heartbeat': Mock(), '_record_swallowed_exception': Mock(),
        })
        out = env['_m2_scan_watch_update']('dispatch', 0, extra={'timeout_sec': 90})
        self.assertEqual(out['started_ts'], 2000)
        self.assertNotEqual(out['round_id'], 'old')
        self.assertEqual(out['extra'], {'timeout_sec': 90})
        self.assertEqual(env['_m2_scan_watch_update']('snapshot', 92, extra={})['extra'], {})

    def test_scan_success_label_is_not_a_block_label(self):
        registry = Mock(return_value={'human': '扫描/数据质量阶段阻断'})
        env = load_definitions({'_reason_human_text'}, {
            '_normalize_reason_code': lambda r: r, '_reason_registry_get': registry,
        })
        self.assertEqual(env['_reason_human_text']('scan_ok'), '扫描完成，数据校验通过')
        registry.assert_not_called()
        self.assertEqual(env['_reason_human_text']('scan_dq_fail'), '扫描/数据质量阶段阻断')

    def test_successful_round_clears_old_error_and_finishes_same_phase(self):
        env = load_definitions({'_round_watch_finish', '_runtime_state_set', '_runtime_state_normalize'}, {
            '_MAIN_ROUND_WATCH': {'stage': 'SCAN_M2', 'err': 'old_error', 'active': True},
            '_RUNTIME_STATE': {'err': 'old_error'}, '_worker_runtime_update_allowed': lambda: True,
            '_round_watchdog_clear_trip': Mock(), '_round_watchdog_round_key_from_watch': lambda: 'round',
            '_normalize_reason_code': lambda r, **k: r, '_record_swallowed_exception': Mock(),
            'time': types.SimpleNamespace(time=lambda: 2000),
        })
        env['_round_watch_finish']('SLEEPING', reason='round_done')
        self.assertEqual(env['_MAIN_ROUND_WATCH']['stage'], 'ROUND_SUMMARY')
        self.assertEqual(env['_MAIN_ROUND_WATCH']['err'], '')
        self.assertEqual(env['_RUNTIME_STATE']['err'], '')
        env['_round_watch_finish']('ERROR', err='current_error')
        self.assertEqual(env['_RUNTIME_STATE']['err'], 'current_error')

    def test_concurrent_partial_bridge_writers_do_not_drop_sections(self):
        files = {'primary': {}}
        def read(path, default):
            value = copy.deepcopy(files.get(path, default))
            time.sleep(0.002)
            return value
        env = load_definitions({'_panel_bridge_write_json'}, {
            '_PANEL_BRIDGE_WRITE_LOCK': threading.RLock(), '_merge_panel_status_snapshot': merge_status_snapshot,
            'safe_read_json': read, 'safe_write_json': lambda path, value: files.update({path: copy.deepcopy(value)}),
            '_log_exc': Mock(), '_record_swallowed_exception': Mock(),
        })
        threads = [threading.Thread(target=env['_panel_bridge_write_json'], args=('primary', '', {str(i): i})) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(files['primary'], {str(i): i for i in range(8)})

    def test_heartbeat_uses_current_watch_without_old_file_read(self):
        tree = parsed(str(ROOT / 'BOT5-2-414 .py'))
        heartbeat = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'panel_update_heartbeat')
        stmt = next(n for n in heartbeat.body[2].body if isinstance(n, ast.Try)
                    and any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_status_seed' for t in x.targets) for x in n.body))
        env = {'obj': {}, '_round_watch_snapshot': lambda: {'stage': 'ROUND_SUMMARY', 'err': ''},
               '_runtime_state_snapshot': lambda: {'round_state': 'SLEEPING'},
               '_runtime_state_diag_payload': lambda o, s: {'state_chain_consistent': True},
               '_record_swallowed_exception': Mock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[stmt], type_ignores=[])), '<heartbeat observation>', 'exec'), env)
        self.assertEqual(env['obj']['round_watch'], {'stage': 'ROUND_SUMMARY', 'err': ''})
        env['_record_swallowed_exception'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
