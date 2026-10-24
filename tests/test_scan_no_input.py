"""Regression: a read-only scan must not send Escape into a wallet dialog."""
import ast
from pathlib import Path
import unittest
from ast_support import ROOT, parsed


def escape_calls(tree):
    scan = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_m2_scan_impl')
    return [n for n in ast.walk(scan) if isinstance(n, ast.Call)
            and any(isinstance(a, ast.Constant) and a.value == 'Escape' for a in n.args)]


class ScanNoInputTests(unittest.TestCase):
    def test_before_fix_reproduces_two_escape_calls(self):
        before = ROOT / 'repair_backups/login_interruption_20261023/BOT5-2-414 .py'
        self.assertEqual(len(escape_calls(parsed(str(before)))), 2)

    def test_scan_no_longer_dismisses_login_or_wallet_dialog(self):
        current = ROOT / 'BOT5-2-414 .py'
        self.assertEqual(escape_calls(parsed(str(current))), [])
