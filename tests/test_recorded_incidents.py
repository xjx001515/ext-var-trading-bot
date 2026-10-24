"""Read-only replays of the reported incidents; never load the trading module."""
import json
from pathlib import Path
import unittest
from bot_core.position_truth import assess_snapshot
from bot_core.execution_confirmation import verify_position_change, apply_confirmation_fields
from bot_core.symbols import same_symbol

ROOT = Path(__file__).resolve().parents[1]


class RecordedIncidentTests(unittest.TestCase):
    def test_oct22_zero_snapshot_is_rejected_as_unknown(self):
        positions = json.loads((ROOT / 'out/positions.json').read_text(encoding='utf-8-sig'))
        equity = json.loads((ROOT / 'out/equity.json').read_text(encoding='utf-8-sig'))
        snap = {'positions': positions['raw_positions'], 'balances': equity['balances'],
                'data_quality': positions['data_quality']}
        result = assess_snapshot(snap)
        self.assertFalse(result['ok'])
        self.assertIn('row_not_found', result['reason'])

    def test_reported_price_only_reduce_cannot_count_as_fill(self):
        ok, evidence = verify_position_change(kind='close', side='LONG', usd=120,
            pre={'side': 'LONG', 'notional_u': 1000, 'qty_abs': 10},
            post={'side': 'LONG', 'notional_u': 999.99, 'qty_abs': 10})
        self.assertFalse(ok)
        self.assertEqual(evidence['reason'], 'position_quantity_no_effect')

    def test_reported_receipt_timeout_never_becomes_trade_effective(self):
        result = apply_confirmation_fields({'ok': True, 'reason': 'receipt_timeout_pending_unconfirmed',
                                            'pending': True, 'fill_confirmed': False, 'postcheck_ok': False})
        self.assertFalse(result['trade_effective'])
        self.assertTrue(result['pending_confirm'])

    def test_reported_symbol_collisions_are_rejected(self):
        for target, actual in [('Q', 'RFQ-USD'), ('W', 'WLD'), ('ETH', 'WETH')]:
            self.assertFalse(same_symbol(target, actual))
