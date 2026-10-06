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

    def test_half_spread_at_designed_floor_is_accepted(self):
        settings = SimpleNamespace(rr_min=1.5, min_net_rr=.8, fee_bps=5, slippage_bps=2)
        signal = dict(signal='LONG', entry=100, sl=99.5, tp=100.765)  # rr_min from a 2 bps fill estimate
        self.assertIsNone(entry_rejection(signal, 100.01, settings))  # ask a hair above the close
        short = dict(signal='SHORT', entry=100, sl=100.5, tp=99.235)
        self.assertIsNone(entry_rejection(short, 99.99, settings))

    def test_material_adverse_drift_still_rejected(self):
        settings = SimpleNamespace(rr_min=1.5, min_net_rr=.8, fee_bps=5, slippage_bps=2)
        signal = dict(signal='LONG', entry=100, sl=99.5, tp=100.765)
        self.assertEqual(entry_rejection(signal, 100.1, settings), 'insufficient_execution_rr')


if __name__ == '__main__':
    unittest.main()
