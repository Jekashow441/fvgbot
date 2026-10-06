"""Indexing, real production branch behavior, and isolated research scenarios."""
import copy, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from critical_rejection_research import *
from trading.strategy import _calc_atr

def sample(bull=True):
    rows=[{'timestamp':i*300000,'open':102.,'high':103.,'low':101.5,'close':102.2,'volume':100.} for i in range(35)]
    rows[-1].update(open=100.7,high=102.,low=100.2,close=101.7)
    d=pd.DataFrame(rows);z={'type':'BULLISH','bottom':100.,'top':101.,'ce':100.5,'candle_time':30*300000,'formed_at':31*300000,'gap_size':1.,'age':3,'touches':1,'touch_bars':1}
    if not bull:
        d[['open','high','low','close']]=pd.DataFrame({'open':202-d.open,'high':202-d.low,'low':202-d.high,'close':202-d.close})
        z.update(type='BEARISH',bottom=101.,top=102.,ce=101.5)
    return d,z

def production_gate(d,z):
    diagnostics={};validate_signal(d,z,settings=CFG,diagnostics=diagnostics)
    return next(iter(diagnostics),'PASSED_ALL')

class RejectionTests(unittest.TestCase):
    def test_bull_body(self):
        d,z=sample();self.assertNotIn(production_gate(d,z),('no_bullish_rejection','no_recent_retest'))
    def test_bear_body(self):
        d,z=sample(False);self.assertNotIn(production_gate(d,z),('no_bearish_rejection','no_recent_retest'))
    def test_bull_wrong_direction(self):
        d,z=sample();d.loc[34,'open']=101.9;self.assertEqual(production_gate(d,z),'no_bullish_rejection')
    def test_bear_wrong_direction(self):
        d,z=sample(False);d.loc[34,'open']=100.;self.assertEqual(production_gate(d,z),'no_bearish_rejection')
    def test_doji_fails(self):
        d,z=sample();d.loc[34,'open']=d.loc[34,'close'];self.assertEqual(production_gate(d,z),'no_bullish_rejection')
    def test_ce_equality_passes(self):
        d,z=sample();d.loc[34,['open','close']]=[100.3,100.5];self.assertNotEqual(production_gate(d,z),'no_bullish_rejection')
    def test_below_ce_fails(self):
        d,z=sample();d.loc[34,['open','close']]=[100.2,100.4];self.assertEqual(production_gate(d,z),'no_bullish_rejection')
    def test_no_zone(self):
        d,z=sample();self.assertEqual(production_gate(d,None),'missing_setup')
    def test_no_touch(self):
        d,z=sample();d.loc[34,'low']=101.1;self.assertEqual(production_gate(d,z),'no_recent_retest')
    def test_formation_bar_not_retest(self):
        d,z=sample();z['formed_at']=34*300000;self.assertEqual(production_gate(d,z),'no_recent_retest')
    def test_last_close_used_not_touch_close(self):
        d,z=sample();d.loc[33]=d.loc[34];d.loc[33,'timestamp']=33*300000;d.loc[34,['open','high','low','close']]=[102.3,102.4,101.5,102.];self.assertEqual(production_gate(d,z),'no_bullish_rejection')
    def test_unclosed_excluded(self):
        d,z=sample();p=closed_candles(d,'5',34*300000+299999);self.assertEqual(len(p),34)
    def test_next_close_accepted_only_at_boundary(self):
        d,z=sample();p=closed_candles(d,'5',35*300000);self.assertEqual(len(p),35);self.assertNotEqual(production_gate(p,z),'no_bullish_rejection')
    def test_future_mutation_invariance(self):
        d,z=sample();f=pd.concat([d,d.tail(1)],ignore_index=True);f.loc[35,'timestamp']=35*300000
        a=production_gate(closed_candles(f,'5',35*300000),z);f.loc[35,'close']=1000
        self.assertEqual(a,production_gate(closed_candles(f,'5',35*300000),z))
    def test_reversed_rejected_by_ingress(self):
        d,z=sample()
        with self.assertRaisesRegex(ValueError,'noncontiguous'):validate_ohlcv(d.iloc[::-1],'5')
    def test_duplicate_rejected(self):
        d,z=sample();d.loc[34,'timestamp']=d.loc[33,'timestamp']
        with self.assertRaisesRegex(ValueError,'noncontiguous'):validate_ohlcv(d,'5')
    def test_missing_bar_rejected(self):
        d,z=sample()
        with self.assertRaisesRegex(ValueError,'noncontiguous'):validate_ohlcv(d.drop(20),'5')
    def test_index_labels_irrelevant(self):
        d,z=sample();original=production_gate(d,z);d.index=range(80,115);self.assertEqual(original,production_gate(d,z))
    def test_utc_boundary(self):
        d,z=sample();self.assertEqual(int(closed_candles(d,'5',300000).timestamp.iloc[-1]),0)
    def test_wick_bull_research(self):
        d,z=sample();d.loc[34,['open','high','low','close']]=[100.9,101.1,100.1,101.];self.assertTrue(reactions(d,z)['modes']['B'])
    def test_wick_bear_research(self):
        d,z=sample(False);d.loc[34,['open','high','low','close']]=[101.1,101.9,100.9,101.];self.assertTrue(reactions(d,z)['modes']['B'])
    def test_touch_without_rejection(self):
        d,z=sample();d.loc[34,['open','high','low','close']]=[101.,101.1,100.8,100.9];self.assertFalse(reactions(d,z)['modes']['A'])
    def test_ast_current_gates_retained(self):
        d,z=sample();self.assertEqual(production_gate(d,None),'missing_setup');di={};research_gate_function('F')(d,None,settings=CFG,diagnostics=di);self.assertIn('missing_setup',di)
    def test_full_fill_invalidates(self):
        d,z=sample();d.loc[34,'low']=100.;self.assertEqual(invalidation_reason(z,d,'5',35*300000),'fvg_fully_filled')
    def test_open_ce_does_not_invalidate(self):
        d,z=sample();d.loc[34,'close']=100.4;self.assertIsNone(invalidation_reason(z,d,'5',35*300000-1))
    def test_scenario_a(self):
        self.assertEqual(research_status(dict.fromkeys(['fvg','displacement','liquidity','htf','reaction'],True)),'VALID STRUCTURE (not an executable signal)')
    def test_scenario_b(self):
        self.assertNotEqual(research_status({'fvg':True,'liquidity':False,'displacement':False}),'VALID STRUCTURE (not an executable signal)')
    def test_scenario_c(self):
        self.assertEqual(research_status({'fvg':True,'displacement':True,'liquidity':True,'htf':True,'reaction':False}),'SIGNAL MAY FORM')
    def test_scenario_d(self):
        self.assertEqual(research_status({'fvg':True},True),'INVALIDATED')
    def test_scenario_e(self):
        self.assertEqual(research_status({'fvg':True,'displacement':True,'liquidity':True,'htf':False,'reaction':True}),'SIGNAL MAY FORM')
    def test_scenario_f(self):
        self.assertEqual(research_status({'fvg':True,'displacement':True,'liquidity':True,'htf':True,'reaction':True,'vwap':False}),'SIGNAL MAY FORM')
    def test_production_missing_volume_baseline_rejects(self):
        d=pd.DataFrame([dict(timestamp=i*300000,open=100.,high=101.,low=99.,close=100.,volume=100.) for i in range(45)])
        d.loc[39,['open','high','low','close','volume']]=[100,106,99.5,105.5,300]
        d.loc[40:,['open','high','low','close']]=[104,107,103.5,105]
        prepared=d.copy();prepared['atr_previous']=_calc_atr(d).shift(1);prepared['vol_sma']=d.volume.shift(1).rolling(20).mean()
        self.assertTrue(detect_fvgs(prepared,CFG,prepared=True))
        prepared.loc[39,'vol_sma']=np.nan
        self.assertFalse(detect_fvgs(prepared,CFG,prepared=True))
    def test_cross_side_level_is_not_sell_liquidity(self):
        d,z=sample();events=liquidity(d,True)
        self.assertTrue(all(e['pool']!='equal_highs' for e in events))

if __name__=='__main__':unittest.main()
