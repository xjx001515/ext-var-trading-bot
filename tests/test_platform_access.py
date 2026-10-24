"""Offline regression tests for the observed live access-state failures."""
import copy
import types
import unittest

from ast_support import load_definitions
from bot_core.platform_access import inspect_platform_access, snapshot_access_block
from bot_core.position_truth import choose_round_snapshot


class AccessClassificationTests(unittest.TestCase):
    def test_logged_out_ext_zero_equity_is_not_a_balance(self):
        result = inspect_platform_access('EXT', 'Start Trading\nConnect Wallet\nLog in to view the tables.\nEquity\n0.00 USD')
        self.assertTrue(result['blocked'])
        self.assertEqual(result['reason'], 'ext_auth_required')

    def test_visible_var_restriction_overrides_visible_balance(self):
        result = inspect_platform_access('VAR', 'Portfolio $48.69\nService access is restricted in your region.\nNo positions')
        self.assertEqual(result['reason'], 'var_access_restricted')

    def test_restriction_can_clear_without_sticky_country_classification(self):
        self.assertTrue(inspect_platform_access('VAR', 'Service access is restricted in your region.')['blocked'])
        self.assertFalse(inspect_platform_access('VAR', 'Portfolio $48.69\nAvailable to Trade $48.69')['blocked'])

    def test_no_warning_is_not_claimed_to_prove_account_readiness(self):
        result = inspect_platform_access('EXT', 'Trade Portfolio Equity 100 USD')
        self.assertEqual(result['reason'], 'no_access_warning')
        self.assertNotIn('authenticated', result)

    def test_blank_page_is_unknown(self):
        self.assertEqual(inspect_platform_access('EXT', '')['reason'], 'ext_page_text_unavailable')

    def test_warning_match_is_case_and_whitespace_tolerant(self):
        self.assertEqual(inspect_platform_access('VAR', 'SERVICE ACCESS IS\nRESTRICTED IN YOUR REGION.')['reason'], 'var_access_restricted')

    def test_warning_does_not_use_wallet_or_personal_data(self):
        result = inspect_platform_access('VAR', '0xABCDEF Portfolio $48.69')
        self.assertNotIn('0xABCDEF', str(result))


class AccessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code_names = {'_platform_access_probe', '_m2_ext_should_reload_after_zero_rows',
                          '_ext_url_is_perp', '_scan_detect_positions_empty_state', 'read_ext_equity_balance', '_UiDeadError'}

    def env(self):
        return load_definitions(self.code_names, {
            '_inspect_platform_access': inspect_platform_access,
            '_pw_call': lambda where, fn, *a, **k: fn(*a, **k),
            '_EXT_ZERO_ROWS_RELOAD_STATE': {'streak': 0.0, 'last_reload_ts': 0.0},
            'time': types.SimpleNamespace(time=lambda: 1000.0),
            'EXT': {'name': 'EXT'}, 'EXT_ZERO_ROWS_RELOAD_STREAK_NEED': 2,
            'EXT_ZERO_ROWS_RELOAD_COOLDOWN_SEC': 90,
        })

    def test_root_trade_and_perp_are_not_bad_routes(self):
        for url in ('https://app.extended.exchange/trade', 'https://app.extended.exchange/perp',
                    'https://app.extended.exchange/trade/NEAR-USD'):
            env = self.env()
            env['_platform_access_probe'] = lambda *a: {'blocked': False}
            env['_scan_detect_positions_empty_state'] = lambda *a, **k: (False, '', {})
            env['_ext_temp_unavailable_detect'] = lambda *a: {}
            reload, reason, _ = env['_m2_ext_should_reload_after_zero_rows'](types.SimpleNamespace(url=url))
            self.assertFalse(reload)
            self.assertEqual(reason, 'single_round_zero_rows')

    def test_auth_required_does_not_reload_or_scan_positions(self):
        env = self.env()
        env['_platform_access_probe'] = lambda *a: inspect_platform_access('EXT', 'Log in to view the tables.')
        env['_scan_detect_positions_empty_state'] = lambda *a, **k: self.fail('must not scan an unauthenticated page')
        reload, reason, detail = env['_m2_ext_should_reload_after_zero_rows'](None)
        self.assertFalse(reload)
        self.assertEqual(reason, 'ext_auth_required')

    def test_auth_required_never_confirms_empty(self):
        env = self.env()
        env['_platform_access_probe'] = lambda *a: inspect_platform_access('EXT', 'Log in to view the tables.')
        empty, reason, detail = env['_scan_detect_positions_empty_state'](None, {'name': 'EXT'})
        self.assertFalse(empty)
        self.assertFalse(detail['confirmed_empty'])
        self.assertEqual(reason, 'ext_auth_required')

    def test_auth_required_never_returns_zero_equity(self):
        env = self.env()
        env['_platform_access_probe'] = lambda *a: inspect_platform_access('EXT', 'Log in to view the tables.')
        value, reason = env['read_ext_equity_balance'](None)
        self.assertIsNone(value)
        self.assertEqual(reason, 'ext_auth_required')

    def test_probe_failure_is_unavailable(self):
        env = self.env()
        page = types.SimpleNamespace(locator=lambda *a: (_ for _ in ()).throw(RuntimeError('read failed')))
        self.assertEqual(env['_platform_access_probe'](page, 'VAR')['reason'], 'var_page_text_unavailable')

    def test_actual_visible_probe_uses_bounded_read(self):
        env = self.env()
        called = []
        body = types.SimpleNamespace(inner_text=lambda **k: called.append(k) or 'Log in to view the tables.')
        page = types.SimpleNamespace(locator=lambda s: body)
        self.assertEqual(env['_platform_access_probe'](page, 'EXT')['reason'], 'ext_auth_required')
        self.assertEqual(called, [{'timeout': 2500}])

    def test_both_platform_access_failures_are_reported(self):
        snapshot = {'raw': {'platform_access': {
            'EXT': inspect_platform_access('EXT', 'Log in to view the tables.'),
            'VAR': inspect_platform_access('VAR', 'Service access is restricted in your region.')}}}
        self.assertEqual(snapshot_access_block(snapshot), 'ext_auth_required|var_access_restricted')

    def test_access_block_does_not_fall_back_to_tradable_old_snapshot(self):
        last = {'data_quality': 'ok', 'balances': {'EXT': {'usd': 50}, 'VAR': {'usd': 50}},
                'positions': {p: {'Q': {'side': 'LONG', 'qty_abs': 1, 'notional_u': 10}} for p in ('EXT', 'VAR')}}
        before = copy.deepcopy(last)
        snap = {'data_quality': 'fail', 'raw': {'platform_access': {'EXT': inspect_platform_access('EXT', 'Log in to view the tables.')}}}
        result, meta = choose_round_snapshot(snap, last_good_snapshot=last, last_good_ts=100, max_age_sec=900, now=101)
        self.assertIsNone(result)
        self.assertTrue(meta['wait_only'])
        self.assertTrue(meta['new_risk_blocked'])
        self.assertEqual(meta['reason_code'], 'ext_auth_required')
        self.assertEqual(last, before)
