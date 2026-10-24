import copy
import unittest

from bot_core.position_truth import (
    normalize_position_read, assess_snapshot, annotate_snapshot,
    snapshot_has_positions_truth, coin_confirmed_flat, choose_round_snapshot,
)


def flat():
    return {'side': 'NONE', 'notional_u': 0, 'position_state': 'CONFIRMED_EMPTY', 'confirmed_empty': True}


def snapshot():
    return {'ts': 100, 'data_quality': 'ok',
            'balances': {'EXT': {'usd': 67}, 'VAR': {'usd': 48}},
            'positions': {'EXT': {'Q': {'side': 'LONG', 'notional_u': 150, 'qty_abs': 100}},
                          'VAR': {'Q': flat()}}}


class PositionTruthTests(unittest.TestCase):
    def test_none_and_zero_are_not_empty_evidence(self):
        for row in (None, {}, {'side': 'NONE', 'notional_u': 0, 'reason': 'ok'}, {'reason': 'row_not_found'}):
            self.assertEqual(normalize_position_read(row)['state'], 'UNAVAILABLE')

    def test_explicit_empty(self):
        self.assertEqual(normalize_position_read(None, confirmed_empty=True)['state'], 'CONFIRMED_EMPTY')
        self.assertEqual(normalize_position_read(flat())['state'], 'CONFIRMED_EMPTY')

    def test_presence_does_not_require_confirmation_quantity(self):
        self.assertEqual(normalize_position_read({'side': 'LONG', 'notional_u': 60})['state'], 'FOUND')

    def test_stale_and_failed_reads_are_unknown_even_with_old_values(self):
        for flag in ({'recovered_from_last_good': True}, {'stale': True}, {'reason': 'read_fail'}, {'position_state': 'UNAVAILABLE'}):
            self.assertEqual(normalize_position_read({'side': 'LONG', 'notional_u': 60, **flag})['state'], 'UNAVAILABLE')

    def test_nonfinite_values_and_conflicting_empty_are_rejected(self):
        for value in (float('nan'), float('inf'), True):
            self.assertEqual(normalize_position_read({'side': 'LONG', 'notional_u': value})['state'], 'UNAVAILABLE')
        self.assertEqual(normalize_position_read({'side': 'LONG', 'qty': 1}, confirmed_empty=True)['state'], 'UNAVAILABLE')

    def test_one_valid_platform_does_not_hide_other_platform_failure(self):
        obj = snapshot()
        obj['positions']['VAR']['Q'] = {'side': 'NONE', 'notional_u': 0, 'reason': 'row_not_found'}
        self.assertFalse(assess_snapshot(obj)['ok'])
        self.assertFalse(snapshot_has_positions_truth(obj))

    def test_all_missing_is_not_a_good_snapshot(self):
        obj = snapshot()
        obj['positions'] = {p: {'Q': {'reason': 'row_not_found', 'notional_u': 0}} for p in ('EXT', 'VAR')}
        self.assertFalse(assess_snapshot(obj)['ok'])

    def test_missing_balance_is_not_total_equity(self):
        obj = snapshot()
        obj['balances']['VAR']['usd'] = None
        self.assertFalse(assess_snapshot(obj)['ok'])

    def test_zero_balances_are_valid(self):
        obj = snapshot()
        obj['balances'] = {p: {'usd': 0} for p in ('EXT', 'VAR')}
        self.assertTrue(assess_snapshot(obj)['ok'])

    def test_existing_quantity_with_missing_valuation_cannot_be_cleared_or_traded(self):
        obj = snapshot()
        obj['positions']['EXT']['Q']['notional_u'] = 0
        self.assertEqual(normalize_position_read(obj['positions']['EXT']['Q'])['state'], 'FOUND')
        self.assertFalse(assess_snapshot(obj)['ok'])
        self.assertFalse(coin_confirmed_flat(obj, 'Q'))

    def test_explicit_empty_platforms_accept_no_positions(self):
        obj = snapshot()
        obj['positions'] = {'EXT': {}, 'VAR': {}}
        obj['raw'] = {'platform_empty_detail_map': {p: {'confirmed_empty': True} for p in ('EXT', 'VAR')}}
        self.assertTrue(assess_snapshot(obj)['ok'])
        self.assertTrue(coin_confirmed_flat(obj, 'ANY'))

    def test_human_verify_or_legacy_empty_flags_are_not_evidence(self):
        obj = snapshot()
        obj['positions'] = {'EXT': {}, 'VAR': {}}
        obj['raw'] = {'platform_empty_map': {'EXT': True, 'VAR': True}, 'platform_empty_reason_map': {'VAR': 'human_verify_blocked'}}
        self.assertFalse(assess_snapshot(obj)['ok'])

    def test_coin_outside_scanned_coverage_is_not_flat(self):
        self.assertFalse(coin_confirmed_flat(snapshot(), 'RFQ'))

    def test_two_explicit_empty_legs_are_flat(self):
        obj = snapshot()
        obj['positions']['EXT']['Q'] = flat()
        self.assertTrue(coin_confirmed_flat(obj, 'Q'))

    def test_annotating_failure_cannot_mutate_last_good(self):
        original = snapshot()
        before = copy.deepcopy(original)
        result = annotate_snapshot(original, ['Q', 'RFQ'])
        self.assertEqual(result['data_quality'], 'fail')
        self.assertEqual(original, before)

    def test_failed_scan_uses_only_verified_recent_fallback(self):
        result, meta = choose_round_snapshot({'data_quality': 'fail'}, last_good_snapshot=snapshot(), last_good_ts=100, max_age_sec=20, now=110)
        self.assertTrue(meta['new_risk_blocked'])
        self.assertTrue(result['dq_fallback'])
        self.assertFalse(snapshot_has_positions_truth(result))

    def test_invalid_or_expired_fallback_never_releases_gate(self):
        for last, ts in ((snapshot(), 50), ({'data_quality': 'ok'}, 100), (snapshot(), 200)):
            result, meta = choose_round_snapshot({'data_quality': 'fail'}, last_good_snapshot=last, last_good_ts=ts, max_age_sec=20, now=110)
            self.assertIsNone(result)
            self.assertTrue(meta['wait_only'])


if __name__ == '__main__':
    unittest.main()
