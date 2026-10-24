import copy
import types
import unittest
import threading
import re

from ast_support import load_definitions
from bot_core.position_truth import snapshot_has_positions_truth, normalize_position_read, coin_confirmed_flat, position_quantity, annotate_snapshot, choose_round_snapshot
import math


class Locator:
    def __init__(self, count=0, visible=False):
        self.n = count
        self.visible = visible
    def count(self):
        return self.n
    def nth(self, index):
        return self
    def is_visible(self):
        return self.visible


class EmptyPositionsPage:
    """Match the actual selector text instead of accepting every text selector."""
    def __init__(self, text, rows=0, visible=True):
        self.text, self.rows, self.visible = text, rows, visible

    def locator(self, selector):
        if selector == 'rows':
            return Locator(self.rows, True)
        if selector.startswith('text=/') and selector.endswith('/i'):
            matches = re.search(selector[6:-2], self.text, flags=re.IGNORECASE) is not None
            return Locator(int(matches), self.visible)
        raise AssertionError('Unexpected selector: ' + selector)


class ScanIntegrationTests(unittest.TestCase):
    def env(self):
        return load_definitions({'_UiDeadError', '_scan_detect_positions_empty_state',
                                 '_coin_pool_refresh_runtime_state', '_scan_reconcile_grid_and_missing_leg'}, {
            '_snapshot_positions_verified': snapshot_has_positions_truth,
            '_position_read_truth': normalize_position_read, '_coin_confirmed_flat': coin_confirmed_flat,
            '_pw_require_page': lambda *a: None, '_ext_prepare_positions_view': lambda p: None,
            '_platform_access_probe': lambda *a: {'blocked': False, 'reason': 'no_access_warning'},
            '_var_prepare_positions_view': lambda p: None,
            '_pw_call': lambda where, fn, *a, **k: fn(*a, **k),
            '_is_target_closed_exc': lambda e: False, 'copy': copy,
        })

    def test_zero_rows_without_visible_hint_stays_unknown(self):
        env = self.env()
        for hidden_hint in (False, True):
            page = types.SimpleNamespace(locator=lambda sel: Locator(0 if sel == 'rows' else int(hidden_hint), False))
            empty, _, detail = env['_scan_detect_positions_empty_state'](page, {'name': 'EXT', 'rows': 'rows'})
            self.assertFalse(empty)
            self.assertFalse(detail['confirmed_empty'])

    def test_explicit_visible_hint_requires_zero_rows(self):
        env = self.env()
        for rows_count, expected in ((0, True), (1, False)):
            page = types.SimpleNamespace(locator=lambda sel: Locator(rows_count if sel == 'rows' else 1, True))
            empty, _, detail = env['_scan_detect_positions_empty_state'](page, {'name': 'EXT', 'rows': 'rows'})
            self.assertEqual(empty, expected)

    def test_live_ext_empty_sentence_is_recognized_by_main_scan(self):
        env = self.env()
        empty, reason, detail = env['_scan_detect_positions_empty_state'](
            EmptyPositionsPage('You have no positions.'), {'name': 'EXT', 'rows': 'rows'})
        self.assertTrue(empty)
        self.assertTrue(detail['confirmed_empty'])
        self.assertEqual(detail['rows_count'], 0)

    def test_empty_sentence_cannot_override_present_rows_or_hidden_hint(self):
        env = self.env()
        for rows, visible in ((1, True), (0, False)):
            with self.subTest(rows=rows, visible=visible):
                empty, _, detail = env['_scan_detect_positions_empty_state'](
                    EmptyPositionsPage('You have no positions.', rows, visible),
                    {'name': 'EXT', 'rows': 'rows'})
                self.assertFalse(empty)
                self.assertFalse(detail['confirmed_empty'])

    def test_empty_message_must_not_be_a_partial_or_loading_message(self):
        env = self.env()
        for text in ('You have no positions in this market.', 'No positions available while loading.', ''):
            with self.subTest(text=text):
                empty, _, detail = env['_scan_detect_positions_empty_state'](
                    EmptyPositionsPage(text), {'name': 'EXT', 'rows': 'rows'})
                self.assertFalse(empty)
                self.assertFalse(detail['confirmed_empty'])

    def test_main_empty_account_evidence_survives_dq_and_round_selection(self):
        env = self.env()
        coins = ['IRYS', 'MAGMA']
        snapshot = {'positions': {'EXT': {}, 'VAR': {}},
                    'balances': {'EXT': {'usd': 67.02}, 'VAR': {'usd': 48.69}},
                    'raw': {'scan_effective_coins': coins, 'platform_empty_detail_map': {}}}
        for platform, message in (('EXT', 'You have no positions.'), ('VAR', 'No positions')):
            empty, _, detail = env['_scan_detect_positions_empty_state'](
                EmptyPositionsPage(message), {'name': platform, 'rows': 'rows'})
            snapshot['raw']['platform_empty_detail_map'][platform] = detail
            for coin in coins:
                snapshot['positions'][platform][coin] = (
                    {'coin': coin, 'side': 'NONE', 'notional_u': 0.0, 'upnl_u': 0.0,
                     'reason': 'empty_positions_view', 'position_state': 'CONFIRMED_EMPTY', 'confirmed_empty': True}
                    if empty else {'coin': coin, 'side': 'NONE', 'notional_u': 0.0, 'reason': 'row_not_found'})
        checked = annotate_snapshot(snapshot, coins)
        self.assertTrue(snapshot_has_positions_truth(checked), checked['data_quality_reason'])
        selected, meta = choose_round_snapshot(checked, last_good_snapshot=None, last_good_ts=0,
                                               max_age_sec=900, now=1000)
        self.assertFalse(meta['wait_only'])
        self.assertFalse(meta['fallback_used'])
        self.assertTrue(snapshot_has_positions_truth(selected))

    def test_prepare_failure_cannot_be_empty(self):
        env = self.env()
        env['_ext_prepare_positions_view'] = lambda p: (_ for _ in ()).throw(RuntimeError('detached'))
        empty, reason, _ = env['_scan_detect_positions_empty_state'](None, {'name': 'EXT', 'rows': 'rows'})
        self.assertFalse(empty)
        self.assertEqual(reason, 'positions_view_unavailable')

    def test_failed_snapshot_cannot_release_or_rebuild_coin_pool(self):
        env = self.env()
        state = {'active_holding_coins': ['Q', 'W'], 'waiting_queue': ['BTC']}
        before = copy.deepcopy(state)
        env['_coin_pool_state_load'] = lambda **k: state
        env['_coin_pool_dedupe'] = lambda x: list(dict.fromkeys(x))
        env['_coin_pool_collect_active_holding_coins'] = lambda *a, **k: self.fail('must not mutate pool on failed scan')
        result = env['_coin_pool_refresh_runtime_state']({'data_quality': 'fail'}, force=True)
        self.assertEqual(state, before)
        self.assertEqual(result, before)
        self.assertIsNot(result, state)

    def test_failed_snapshot_cannot_clear_grid(self):
        env = self.env()
        env['_grid_force_clear_leg_history'] = lambda *a, **k: self.fail('must not clear ledger')
        result = env['_scan_reconcile_grid_and_missing_leg']({'data_quality': 'fail'}, 'Q')
        self.assertEqual(result['grid_clears'], 0)
        self.assertEqual(result['skip'], 'position_truth_unavailable')

    def test_retired_scan_result_cannot_overwrite_last_good(self):
        original = {'ts': 10, 'sentinel': 'newer_scan'}
        env = load_definitions({'_m2_accept_snapshot'}, {
            '_SNAPSHOT_COMMIT_LOCK': threading.RLock(), '_pw_runtime_gen_value': lambda: 3,
            '_snapshot_positions_verified': lambda snap: True, 'copy': copy,
            'LAST_GOOD_SNAPSHOT': original, 'LAST_GOOD_TS': 10, 'LAST_GOOD_REASON': 'existing',
            '_m2_make_fail_snapshot': lambda reason, **k: {'data_quality': 'fail', 'reason': reason},
        })
        result = env['_m2_accept_snapshot']({'ts': 11, 'positions': {}}, 2)
        self.assertEqual(result['data_quality'], 'fail')
        self.assertIs(env['LAST_GOOD_SNAPSHOT'], original)
        self.assertEqual(env['LAST_GOOD_TS'], 10)

    def test_current_generation_commits_a_copy(self):
        env = load_definitions({'_m2_accept_snapshot'}, {
            '_SNAPSHOT_COMMIT_LOCK': threading.RLock(), '_pw_runtime_gen_value': lambda: 3,
            '_snapshot_positions_verified': lambda snap: True, 'copy': copy,
            'LAST_GOOD_SNAPSHOT': {}, 'LAST_GOOD_TS': 0, 'LAST_GOOD_REASON': '',
        })
        value = {'ts': 11, 'positions': {'Q': {'qty': 1}}}
        self.assertIs(env['_m2_accept_snapshot'](value, 3), value)
        value['positions']['Q']['qty'] = 2
        self.assertEqual(env['LAST_GOOD_SNAPSHOT']['positions']['Q']['qty'], 1)

    def test_strategy_adapter_preserves_real_quantity_and_empty_evidence(self):
        for row in (
            {'side': 'SHORT', 'notional_u': 100, 'qty_abs': 20},
            {'side': 'NONE', 'notional_u': 0, 'position_state': 'CONFIRMED_EMPTY', 'confirmed_empty': True},
            {'side': 'LONG', 'notional_u': 100},
        ):
            env = load_definitions({'_snapshot_leg_truth'}, {
                '_get_position': lambda *a: row, '_position_read_truth': normalize_position_read,
                '_position_quantity': position_quantity, 'math': math,
            })
            out = env['_snapshot_leg_truth']({}, 'Q', 'EXT')
            self.assertEqual(out['position_state'], normalize_position_read(row)['state'])
            if out['position_state'] == 'CONFIRMED_EMPTY':
                self.assertTrue(out['confirmed_empty'])
            else:
                self.assertEqual(out['qty'], position_quantity(row))
                self.assertEqual(out['side'], row['side'])

    def test_adapter_does_not_guess_platform_side(self):
        env = load_definitions({'_snapshot_leg_truth'}, {
            '_get_position': lambda *a: {'notional_u': 100, 'qty_abs': 20},
            '_position_read_truth': normalize_position_read, '_position_quantity': position_quantity,
        })
        out = env['_snapshot_leg_truth']({}, 'Q', 'EXT')
        self.assertEqual(out['position_state'], 'UNAVAILABLE')
        self.assertEqual(out['side'], '')
