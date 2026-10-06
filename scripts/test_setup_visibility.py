import sys,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import database
from trading import setup_visibility as vis

class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(database,'DB_PATH',str(Path(self.tmp.name)/'v.db'));self.patch.start();database.init_db()
        self.saved=vis.cfg.model_copy(deep=True);vis.cfg.tg_chat_id='1';vis.cfg.is_running=True;vis.cfg.min_signal_score=80
    def tearDown(self):
        for f in type(vis.cfg).model_fields:setattr(vis.cfg,f,getattr(self.saved,f))
        self.patch.stop();self.tmp.cleanup()
    def payload(self, status='POTENTIAL', reasons=None, checks=None):
        return {'asset':'TESTUSDT','timeframe':'5','direction':'LONG','htf_bias':'UP','fvg':{'type':'BULLISH','bottom':100.,'top':102.,'ce':101.,'formed_at':1000,'displacement_atr':1.2},'fvg_quality':90,'current_price':101.,'strict_ce':True,'signal_created':None,'entry':None,'sl':None,'tp1':None,'assessment':{'status':status,'score':90,'analysis_bar':int(time.time()*1000)-310000,'checks':checks or {'FVG':True,'Zone eligibility':True,'Structure':True,'HTF bias':True,'Liquidity':True},'rejections':reasons or [],'policy':{'min_score':80}},'expires_at':time.time()+600}
    def record(self,key='TESTUSDT_5_1000_BULLISH',status='POTENTIAL',reasons=None,checks=None,at=None):
        from trading.setup_journal import record
        record(key,status,'; '.join(reasons or []),self.payload(status,reasons,checks),now=at or time.time())
    def test_requires_full_history(self):
        self.record(reasons=['no_bullish_rejection']);from trading.setup_journal import history
        d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));self.assertTrue(d['qualifies']);self.assertEqual(d['missing'],['no_bullish_rejection'])
    def test_missing_rejection_is_signal_may_form_only(self):
        self.record(reasons=['no_bullish_rejection']);from trading.setup_journal import history
        self.assertIn('SIGNAL MAY FORM',vis.render(vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))))
    def test_execution_reasons_do_not_qualify(self):
        self.record(reasons=['thin_orderbook']);from trading.setup_journal import history
        self.assertFalse(vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))['qualifies'])
    def test_early_notice_does_not_require_liquidity_sweep(self):
        checks={'FVG':True,'Zone eligibility':True,'Structure':True,'HTF bias':True,'Liquidity':False}
        self.record(reasons=['no_bullish_rejection'],checks=checks)
        from trading.setup_journal import history
        d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))
        self.assertTrue(d['qualifies']);self.assertTrue(d['early'])
        vis.cfg.enable_early_setup_alerts=False
        d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))
        self.assertFalse(d['qualifies'])

    def test_early_threshold_is_observation_only(self):
        checks={'FVG':True,'Zone eligibility':True,'Structure':True,'HTF bias':True,'Liquidity':False}
        self.record(reasons=['no_bullish_rejection'],checks=checks)
        from trading.setup_journal import history
        events=history('TESTUSDT_5_1000_BULLISH')
        events[-1]['payload']['assessment']['score']=64
        self.assertFalse(vis.diagnostic(events)['qualifies'])
        events[-1]['payload']['assessment']['score']=65
        self.assertTrue(vis.diagnostic(events)['qualifies'])
    def test_stale_does_not_qualify(self):
        self.record(reasons=['no_bullish_rejection'],at=time.time()-1000);from trading.setup_journal import history
        self.assertFalse(vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))['qualifies'])
    def test_invalidated_render(self):
        self.record('TESTUSDT_5_1000_BULLISH','INVALIDATED',['fvg_fully_filled']);from trading.setup_journal import history
        d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));self.assertEqual(d['state'],'INVALIDATED');self.assertIn('SETUP CLOSED',vis.render(d,'INVALIDATED'))
    def test_stage_deduplicates_new(self):
        from trading.setup_journal import history
        self.record(reasons=['no_bullish_rejection']);d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));vis.stage(d,now=100);vis.stage(d,now=101)
        with database._conn() as con:self.assertEqual(con.execute('select count(*) from setup_notices').fetchone()[0],1)
    def test_improved_transition(self):
        from trading.setup_journal import history
        self.record(reasons=['no_bullish_rejection','entry_too_far']);d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));vis.stage(d,now=100)
        self.record(reasons=['no_bullish_rejection']);d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));vis.stage(d,now=101)
        with database._conn() as con:self.assertEqual([r[0] for r in con.execute('select transition from setup_notices order by id')],['NEW','IMPROVED'])
    def test_no_repeat_same_state(self):
        from trading.setup_journal import history
        self.record(reasons=['no_bullish_rejection']);d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));vis.stage(d,now=100);vis.stage(d,now=101)
        with database._conn() as con:self.assertEqual(con.execute('select count(*) from setup_notices').fetchone()[0],1)
    def test_trigger_approaching_transition(self):
        from trading.setup_journal import history
        self.record(reasons=['no_bullish_rejection']);d=vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'));vis.stage(d,now=100)
        self.record(reasons=['no_bullish_rejection']);from trading.setup_journal import history as h;events=h('TESTUSDT_5_1000_BULLISH');events[-1]['payload']['assessment']['status']='ENTRY APPROACHING';d=vis.diagnostic(events);vis.stage(d,now=101)
        with database._conn() as con:self.assertEqual(con.execute('select count(*) from setup_notices').fetchone()[0],2)
    def test_render_includes_threshold_and_data(self):
        self.record(reasons=['no_bullish_rejection']);from trading.setup_journal import history
        s=vis.render(vis.diagnostic(history('TESTUSDT_5_1000_BULLISH')));self.assertIn('Threshold: 80',s);self.assertIn('Data:',s)
    def test_unknown_first_blocker_explicit(self):
        self.record(reasons=[]);from trading.setup_journal import history
        self.assertIn('UNKNOWN',vis.render(vis.diagnostic(history('TESTUSDT_5_1000_BULLISH'))))

if __name__=='__main__':unittest.main()
