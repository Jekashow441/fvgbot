import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from core import database
from trading.data_quality import decision_candles, validate_ohlcv, observed_price
from trading.position_risk import position_notional, realized_day_return
from trading.signal_delivery import original_signal
from trading.strategy import detect_fvgs
from trading.backtest import run_backtest


def bars(n=250,step=300000):
    return pd.DataFrame([dict(timestamp=i*step,open=100.,high=101.,low=99.,close=100.,volume=100.) for i in range(n)])


class AuditTests(unittest.TestCase):
    def test_snapshot_cannot_become_closed_after_fetch(self):
        df=bars()
        df.attrs['observed_at_ms']=249*300000-1
        with self.assertRaisesRegex(ValueError,'stale'):
            decision_candles(df,'5',249*300000,minimum=200)

    def test_bybit_cache_expires_on_candle_boundary(self):
        from core import bybit
        key='TESTUSDT_5_250'
        with patch.dict(bybit._CACHE,{key:{'time':299.9,'df':bars()}}),patch.object(bybit.time,'time',return_value=300.1):
            self.assertIsNone(bybit._cache_get(key))

    def test_current_candle_never_used_as_closed(self):
        df=bars()
        got=decision_candles(df,'5',249*300000,minimum=200)
        self.assertEqual(got.timestamp.iloc[-1],248*300000)
        df.loc[249,'close']=100.5
        pd.testing.assert_frame_equal(got,decision_candles(df,'5',249*300000,minimum=200))

    def test_stale_htf_rejected(self):
        with self.assertRaisesRegex(ValueError,'stale'):
            decision_candles(bars(step=3600000),'60',251*3600000,minimum=200)

    def test_missing_and_duplicate_candles_rejected(self):
        df=bars()
        for broken in (df.drop(index=100),pd.concat([df,df.tail(1)])):
            with self.assertRaisesRegex(ValueError,'noncontiguous'):
                validate_ohlcv(broken,'5')

    def test_bad_geometry_rejected(self):
        df=bars();df.loc[10,'close']=110
        with self.assertRaisesRegex(ValueError,'geometry'):
            validate_ohlcv(df,'5')

    def test_nan_rejected(self):
        df=bars();df.loc[10,'volume']=float('nan')
        with self.assertRaisesRegex(ValueError,'nonfinite'):
            validate_ohlcv(df,'5')

    def test_stale_and_future_observations_not_prices(self):
        df=bars(1)
        self.assertEqual(observed_price(df,'5',1000),100)
        self.assertIsNone(observed_price(df,'5',1000000))
        self.assertIsNone(observed_price(df,'5',-1))

    def test_risk_sizing_and_capital(self):
        s=cfg.model_copy(deep=True);s.paper_risk_per_trade_pct=.5;s.paper_trade_size_pct=100
        narrow=position_notional(100,99,10000,[],s)
        wide=position_notional(100,95,10000,[],s)
        self.assertLess(wide,narrow)
        self.assertAlmostEqual(wide*(.05+2*(s.fee_bps+s.slippage_bps)/10000),50)
        active=[dict(ml_features=json.dumps({'position_size_usdt':9900}))]
        self.assertEqual(position_notional(100,99,10000,active,s),100)

    def test_unknown_allocation_not_free_capital(self):
        self.assertEqual(position_notional(100,99,10000,[{}],cfg),0)

    def test_daily_limit_uses_account_return(self):
        rows=[dict(pnl_pct=-1,ml_features=json.dumps({'position_size_usdt':1000}))]
        self.assertAlmostEqual(realized_day_return(rows,9990),-.1)
        self.assertIsNone(realized_day_return([dict(pnl_pct=-1)],9990))

    def test_snapshot_never_changes_price_or_creation_time(self):
        with tempfile.TemporaryDirectory() as d,patch.object(database,'DB_PATH',str(Path(d)/'test.db')):
            database.init_db()
            with patch('trading.signal_delivery.time.time',return_value=1000):
                first=original_signal('x',{'entry':100})
            with patch('trading.signal_delivery.time.time',return_value=2000):
                second=original_signal('x',{'entry':105})
            self.assertEqual(first,second)

    def test_gap_maximum_and_partial_fill(self):
        df=bars(45)
        df.loc[39,['open','high','low','close','volume']]=[100,106,99.5,105.5,300]
        df.loc[40:,['open','high','low','close']]=[104,107,103,105]
        df.loc[43,'low']=102.5
        s=cfg.model_copy(deep=True);s.fvg_confirmations=1
        zone=detect_fvgs(df,s)[0]
        self.assertAlmostEqual(zone['fill_fraction'],.25)
        self.assertEqual(zone['touches'],1)
        s.fvg_max_size_atr=.01
        self.assertFalse(detect_fvgs(df,s))

    def test_replay_rejects_htf_gaps(self):
        with self.assertRaisesRegex(ValueError,'noncontiguous'):
            run_backtest(bars(),bars(step=3600000).drop(index=100),settings=cfg.model_copy(update={'timeframe':'5'}))

    def test_replay_cannot_use_future_htf(self):
        s=cfg.model_copy(update={'timeframe':'5','ema_period':20})
        df=bars(110);higher=bars(30,step=3600000)
        with patch('trading.backtest.detect_fvgs',return_value=[{'candle_time':1,'type':'BULLISH'}]),patch('trading.backtest.validate_signal') as validate:
            result=run_backtest(df,higher,settings=s)
            validate.assert_not_called() # Less than 20 HTF bars closed at decision time.
            self.assertEqual(result['all']['trades'],0)


if __name__ == '__main__':
    unittest.main()
