import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading.entry_rules import entry_rejection


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(rr_min=1, min_net_rr=.8, fee_bps=5, slippage_bps=2)
        self.signal = dict(signal='LONG',entry=100,sl=99,tp=102)

    def test_valid_price(self):
        self.assertIsNone(entry_rejection(self.signal,100,self.settings))

    def test_nonfinite_price(self):
        self.assertEqual(entry_rejection(self.signal,float('nan'),self.settings),'invalid_execution_price')

    def test_drift(self):
        self.assertEqual(entry_rejection(self.signal,100.3,self.settings),'execution_price_moved')

    def test_tiny_target_erased_by_costs(self):
        signal = dict(signal='LONG',entry=100,sl=99.9,tp=100.125)
        self.assertEqual(entry_rejection(signal,100,self.settings),'insufficient_rr_after_costs')

    def test_short_already_at_target(self):
        signal = dict(signal='SHORT',entry=100,sl=101,tp=98)
        self.assertEqual(entry_rejection(signal,98,self.settings),'price_outside_trade_levels')


if __name__ == '__main__':
    unittest.main()
