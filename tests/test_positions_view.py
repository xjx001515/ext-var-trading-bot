import unittest
import copy
import types
from bot_core.positions_view import coverage_details_verified, pagination_detected, table_coverage_verified
from bot_core.position_truth import annotate_snapshot, snapshot_has_positions_truth


class CoverageTests(unittest.TestCase):
    def valid(self):
        return dict(table_ready=True, virtualized=False, pagination_present=False,
                    all_rows_visible=True, scope_all=True, declared_count_matches=True, row_count=3)

    def test_complete_static_table(self):
        self.assertTrue(coverage_details_verified(self.valid(), 3))

    def test_last_page_is_not_the_whole_table(self):
        details = self.valid()
        details.update(pagination_present=True, next_disabled=True, previous_disabled=False)
        self.assertFalse(coverage_details_verified(details, 3))

    def test_virtual_or_partially_parsed_tables_are_not_complete(self):
        for field, value in [('table_ready', False), ('virtualized', True),
                             ('all_rows_visible', False), ('scope_all', False), ('declared_count_matches', False)]:
            details = self.valid()
            details[field] = value
            self.assertFalse(coverage_details_verified(details, 3))
        self.assertFalse(coverage_details_verified(self.valid(), 2))
        self.assertFalse(coverage_details_verified({}, 3))
        self.assertFalse(coverage_details_verified(self.valid(), 0))

    def test_site_navigation_current_page_is_not_position_pagination(self):
        observed = {'tag': 'A', 'aria_current': 'page', 'text': 'Trade', 'visible': True,
                    'label': None, 'testid': None, 'class_name': '', 'in_pagination_container': False}
        self.assertFalse(pagination_detected([observed]))

    def test_numbered_or_explicit_pagination_still_blocks_absence(self):
        candidates = [
            {'tag':'A','aria_current':'page','text':'2','visible':True},
            {'tag':'BUTTON','label':'Next','disabled':True,'visible':True},
            {'tag':'DIV','class_name':'positions-pagination','visible':True},
            {'tag':'A','text':'Last','in_pagination_container':True,'visible':True},
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertTrue(pagination_detected([candidate]))
        self.assertTrue(pagination_detected(None))
        self.assertTrue(pagination_detected([{}]))

    def test_hidden_pagination_does_not_describe_current_visible_table(self):
        self.assertFalse(pagination_detected([{'tag':'BUTTON','label':'Next','visible':False}]))

    def test_live_magma_table_can_prove_irys_absent_without_losing_magma(self):
        detail = self.valid()
        detail.update(row_count=1, pagination_candidates=[{'tag':'A','aria_current':'page',
            'text':'Trade','visible':True,'in_pagination_container':False}])
        locator = types.SimpleNamespace(evaluate_all=lambda expression, platform: copy.deepcopy(detail))
        page = types.SimpleNamespace(locator=lambda selector: locator)
        complete = table_coverage_verified(page, {'name':'EXT','rows':'rows'}, 1)
        self.assertTrue(complete)
        snapshot = {'balances':{'EXT':{'usd':79.32},'VAR':{'usd':80.59}},
            'raw':{'scan_effective_coins':['MAGMA','IRYS'],
                   'platform_empty_detail_map':{'VAR':{'confirmed_empty':True}}},
            'positions':{'EXT':{
                'MAGMA':{'side':'LONG','qty_abs':310,'notional_u':154.85,'reason':'ok'},
                'IRYS':{'side':'NONE','notional_u':0,'reason':'trusted_bulk_absent',
                        'position_state':'CONFIRMED_EMPTY','confirmed_empty':complete}},'VAR':{}}}
        checked = annotate_snapshot(snapshot)
        self.assertTrue(snapshot_has_positions_truth(checked), checked['data_quality_reason'])
        self.assertEqual(checked['positions']['EXT']['MAGMA']['qty_abs'],310)
        self.assertEqual(checked['positions']['EXT']['MAGMA']['position_state'],'FOUND')
        self.assertEqual(checked['positions']['EXT']['IRYS']['position_state'],'CONFIRMED_EMPTY')
