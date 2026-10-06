import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading.exit_rules import candle_exit


class ExitTests(unittest.TestCase):
    def test_ambiguous_bar_stop_before_target_and_timeout(self):
        bar = SimpleNamespace(open=100, high=104, low=98, close=102)
        self.assertEqual(candle_exit(1,99,103,bar,6,6),(99,'SL'))

    def test_adverse_short_gap(self):
        bar = SimpleNamespace(open=105, high=106, low=100, close=101)
        self.assertEqual(candle_exit(-1,103,97,bar,1,6),(105,'SL'))

    def test_timeout_only_after_full_holding_period(self):
        bar = SimpleNamespace(open=100, high=101, low=99.5, close=100.5)
        self.assertIsNone(candle_exit(1,99,103,bar,5,6))
        self.assertEqual(candle_exit(1,99,103,bar,6,6),(100.5,'TIMEOUT'))
        self.assertIsNone(candle_exit(1,99,103,bar,100,0))

    def test_target_before_timeout(self):
        bar = SimpleNamespace(open=100, high=104, low=99.5, close=102)
        self.assertEqual(candle_exit(1,99,103,bar,6,6),(103,'TP'))

    def test_replay_timeout_uses_next_open_and_deducts_costs(self):
        from core.settings import cfg
        from trading.backtest import run_backtest
        settings = cfg.model_copy(deep=True)
        settings.ema_period = 30
        settings.fee_bps, settings.slippage_bps = 5, 2
        settings.rr_min, settings.min_net_rr = 1, .8
        df = pd.DataFrame([dict(timestamp=i*300000,open=100.,high=100.5,low=99.5,close=100.2,volume=100.) for i in range(105)])
        signal = dict(signal='LONG',entry=100.2,sl=99.,tp=103.)
        zone = dict(candle_time=1,type='BULLISH')
        with patch('trading.backtest.detect_fvgs',return_value=[zone]), patch('trading.backtest.validate_signal',return_value=signal):
            result = run_backtest(df,settings=settings,max_hold_bars=2)
        trade = result['trade_log'][0]
        self.assertEqual(trade['entry_timestamp'],101*300000)
        self.assertEqual(trade['timestamp'],102*300000)
        self.assertEqual(trade['held_bars'],2)
        self.assertEqual(trade['exit_reason'],'TIMEOUT')
        self.assertAlmostEqual(trade['net_r'],.06)


if __name__ == '__main__':
    unittest.main()
