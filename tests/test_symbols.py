import unittest
from bot_core.symbols import canonical_symbol, same_symbol

class SymbolTests(unittest.TestCase):
    def test_substrings_rejected(self):
        for target, market in [('Q', 'RFQ-USD'), ('W', 'WLD'), ('BTC', 'WBTC-USD')]:
            self.assertFalse(same_symbol(target, market))
    def test_market_and_url(self):
        self.assertTrue(same_symbol('Q', 'Q-USD LONG 10'))
        self.assertTrue(same_symbol('Q', '/perpetual/Q'))
        self.assertFalse(same_symbol('Q', '/perpetual/RFQ'))
        self.assertTrue(same_symbol('BTC', 'BTC/USD'))
    def test_only_explicit_aliases(self):
        self.assertFalse(same_symbol('1000PEPE', 'KPEPE-USD'))
        self.assertTrue(same_symbol('1000PEPE', 'KPEPE-USD', aliases=['KPEPE']))
        self.assertEqual(canonical_symbol(''), '')

if __name__ == '__main__':
    unittest.main()
