"""Structural contracts: safety work must not activate or retune strategies."""
import ast
from pathlib import Path
import unittest
from ast_support import ROOT, parsed

BEFORE = ROOT / 'repair_backups/safety_refactor_20261022/BOT5-2-414 .py'
AFTER = ROOT / 'BOT5-2-414 .py'


def main_entry(tree):
    # Legacy disabled entry blocks use `and False`; they do not start the app.
    return next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and ast.unparse(n.test) in ("__name__ == '__main__'", '"__main__" == __name__'))


def active_functions(tree):
    entry = main_entry(tree)
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.lineno < entry.lineno}


class StrategyPreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = parsed(str(BEFORE))
        cls.after = parsed(str(AFTER))

    def test_strategy_decision_bodies_unchanged(self):
        before, after = active_functions(self.before), active_functions(self.after)
        names = ('_m9_pick_grid_mode', '_trend_effective_side', '_m9_trend_open_policy',
                 '_trend_state_inline', '_m9_payload_trend_view', '_a1_check_before_execute',
                 '_trend_alpha_guard_for_b2_m10')
        for name in names:
            self.assertTrue(name in before, 'Missing active strategy function: ' + name)
            self.assertEqual(ast.dump(before[name], include_attributes=False),
                             ast.dump(after[name], include_attributes=False), name)

    def test_missing_helper_is_hoisted_without_activating_late_overrides(self):
        old = next(n for n in self.before.body if isinstance(n, ast.FunctionDef) and n.name == '_single_leg_live_side')
        current = active_functions(self.after)['_single_leg_live_side']
        self.assertEqual(ast.dump(old, include_attributes=False), ast.dump(current, include_attributes=False))
        self.assertEqual(ast.dump(main_entry(self.before), include_attributes=False),
                         ast.dump(main_entry(self.after), include_attributes=False))

    def test_strategy_threshold_assignments_unchanged(self):
        prefixes = ('M1_', 'M9_', 'M10_', 'B1_', 'B2_', 'S1_', 'ANCHOR_', 'SAFE_GAP',
                    'LEG_GAP_', 'LEG_ONE_SHOT_', 'GRID_NEW_LAYER_', 'TP_', 'BUDGET_')
        def assignments(tree):
            result = {}
            entry = main_entry(tree)
            for top in tree.body:
                if top.lineno >= entry.lineno or isinstance(top, (ast.FunctionDef, ast.ClassDef)):
                    continue
                for node in ast.walk(top):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id.startswith(prefixes):
                            result.setdefault(target.id, []).append(ast.dump(node.value, include_attributes=False))
            return result
        self.assertEqual(assignments(self.before), assignments(self.after))

    def test_worker_does_not_commit_last_good_snapshot(self):
        node = active_functions(self.after)['_m2_scan_impl']
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
        self.assertFalse(names & {'LAST_GOOD_SNAPSHOT', 'LAST_GOOD_TS', 'LAST_GOOD_REASON'})
