import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ast_support import ROOT, parsed
from bot_core import read_only_validation as ro


class ReadOnlyTests(unittest.TestCase):
    def test_explicit_flag_and_persistent_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'bot.py'
            self.assertFalse(ro.requested(script, []))
            self.assertTrue(ro.requested(script, ['--read-only']))
            latch = Path(directory) / ro.LATCH
            for value in ('{broken', '{}', '{"enabled":true}', '[]'):
                latch.write_text(value)
                self.assertTrue(ro.requested(script, []))
            latch.write_text('{"enabled":false}')
            self.assertFalse(ro.requested(script, []))
            self.assertTrue(ro.requested(script, ['--read-only']))

    def test_ready_accounts_still_cannot_enable_trading(self):
        report = ro.combine({p: {'scan_ok': True, 'equity': 50} for p in ('EXT', 'VAR')}, 1, timestamp=100)
        self.assertTrue(report['scan_ok'])
        self.assertEqual(report['total_equity'], 100)
        self.assertFalse(report['trade_enabled'])
        self.assertFalse(report['automatic_trade_restore'])

    def test_unknown_balance_never_counted_as_zero(self):
        report = ro.combine({'EXT': {'equity': None}, 'VAR': {'equity': 48.69}}, 1)
        self.assertIsNone(report['total_equity'])

    def test_logged_out_ext_placeholders_are_unknown(self):
        result = ro.parse_capture('EXT', {'text': 'Start Trading\nConnect Wallet\nLog in to view the tables.\nEquity\n0.00 USD', 'wallet_visible': False})
        self.assertIsNone(result['equity'])
        self.assertFalse(result['login_verified'])
        self.assertFalse(result['confirmed_empty'])

    def test_var_loading_wallet_header_is_not_login(self):
        result = ro.parse_capture('VAR', {'text': 'Connect your wallet to see your positions\nPortfolio Value\n$0.00', 'wallet_visible': True})
        self.assertEqual(result['access']['reason'], 'var_auth_required')
        self.assertFalse(result['login_verified'])

    def test_var_balance_readable_but_warning_blocks_scan_ready(self):
        capture = {'wallet_visible': True, 'text': 'Portfolio Value\n$48.69\nAvailable to Trade\n$48.69\nNo positions\nService access is restricted in your region.',
                   'tables': [{'rows': [{'cells': ['Instrument', 'Quantity', 'Value']}]}]}
        result = ro.parse_capture('VAR', capture)
        self.assertEqual(result['equity'], 48.69)
        self.assertEqual(result['available_to_trade'], 48.69)
        self.assertTrue(result['confirmed_empty'])
        self.assertFalse(result['scan_ok'])

    def test_authenticated_zero_balance_and_explicit_empty_are_valid(self):
        capture = {'wallet_visible': True, 'all_positions_selected': True,
                   'text': 'Equity\n0.00 USD\nAvailable To Trade\n0.00 USD\nNo open positions',
                   'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE']}]}]}
        result = ro.parse_capture('EXT', capture)
        self.assertTrue(result['scan_ok'])
        self.assertEqual(result['equity'], 0)

    def test_ext_observed_empty_sentence_with_period_is_recognized(self):
        capture = {'wallet_visible': True, 'all_positions_selected': True,
                   'text': 'Equity\n67.02 USD\nAvailable To Trade\n67.02 USD\nYou have no positions.',
                   'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE']}]}]}
        result = ro.parse_capture('EXT', capture)
        self.assertTrue(result['confirmed_empty'])
        self.assertTrue(result['positions_readable'])
        self.assertTrue(result['scan_ok'])

    def test_empty_sentence_never_matches_a_qualified_or_failed_scan(self):
        for message in ('You have no positions in this market.',
                        'No positions available while loading.',
                        'Could not load positions.'):
            with self.subTest(message=message):
                capture = {'wallet_visible': True, 'all_positions_selected': True,
                           'text': 'Equity\n67.02 USD\nAvailable To Trade\n67.02 USD\n' + message,
                           'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE']}]}]}
                result = ro.parse_capture('EXT', capture)
                self.assertFalse(result['confirmed_empty'])
                self.assertFalse(result['scan_ok'])

    def test_filtered_or_partial_table_is_not_empty_account(self):
        capture = {'wallet_visible': True, 'all_positions_selected': False,
                   'text': 'Equity\n100 USD\nAvailable To Trade\n20 USD\nNo positions',
                   'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE']}]}]}
        self.assertFalse(ro.parse_capture('EXT', capture)['confirmed_empty'])
        capture['all_positions_selected'] = True
        capture['tables'][0]['virtualized'] = True
        self.assertFalse(ro.parse_capture('EXT', capture)['scan_ok'])

    def test_visible_positions_are_parsed_without_actions(self):
        capture = {'wallet_visible': True, 'all_positions_selected': True,
                   'text': 'Equity\n100.50 USD\nAvailable To Trade\n40.25 USD',
                   'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE', 'U. PNL']},
                                        {'cells': ['Q-USD', 'LONG', '120 USD\n10 Q', '-2 USD']}]}]}
        result = ro.parse_capture('EXT', capture)
        self.assertTrue(result['scan_ok'])
        self.assertEqual(result['positions'][0]['quantity'], 10)
        self.assertEqual(result['positions'][0]['coin'], 'Q')

    def test_missing_quantity_keeps_partial(self):
        capture = {'wallet_visible': True, 'all_positions_selected': True,
                   'text': 'Equity\n100 USD\nAvailable To Trade\n10 USD',
                   'tables': [{'rows': [{'cells': ['MARKET', 'SIDE', 'VALUE / SIZE']},
                                        {'cells': ['Q-USD', 'LONG', '120 USD']}]}]}
        self.assertFalse(ro.parse_capture('EXT', capture)['scan_ok'])

    def test_publish_never_writes_financial_ledgers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = ['out/positions.json', 'out/equity.json', 'out/state/anchor_state.json',
                         'out/state/m9_wangge_zhangben.json', 'out/executor_results.jsonl', 'out/trades.jsonl']
            for name in protected:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text('original')
            report = ro.combine({p: {'scan_ok': True, 'equity': 50} for p in ('EXT', 'VAR')}, 2)
            ro.publish(root, report)
            self.assertTrue(all((root / name).read_text() == 'original' for name in protected))
            self.assertFalse(json.loads((root / 'out/status.json').read_text(encoding='utf-8'))['trade_enabled'])

    def test_no_browser_inputs_or_navigation_in_observer(self):
        tree = ast.parse(Path(ro.__file__).read_text(encoding='utf-8'))
        forbidden = {'click', 'dblclick', 'press', 'fill', 'type', 'goto', 'reload', 'new_page', 'new_context', 'bring_to_front'}
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & forbidden)

    def test_read_only_entry_exits_before_legacy_initialization(self):
        tree = parsed(str(ROOT / 'BOT5-2-414 .py'))
        entry_index = next(i for i, n in enumerate(tree.body)
                           if isinstance(n, ast.If) and '_readonly_requested' in ast.unparse(n.test))
        prefix = tree.body[:entry_index + 1]
        self.assertFalse(any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) for n in prefix))
        sentinel = ast.Expr(ast.Call(ast.Name('legacy_started', ast.Load()), [], []))
        module = ast.fix_missing_locations(ast.Module(body=prefix + [sentinel], type_ignores=[]))
        with patch.object(ro, 'requested', return_value=True), patch.object(ro, 'run', return_value=0) as run:
            with self.assertRaises(SystemExit):
                exec(compile(module, '<readonly-prefix>', 'exec'), {'__name__': '__main__', '__file__': str(ROOT / 'BOT5-2-414 .py'), 'legacy_started': lambda: self.fail('Legacy runtime started')})
            run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
