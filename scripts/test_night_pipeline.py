import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import database,bybit
from core.settings import cfg
from trading import telemetry,system_status,signal_delivery as delivery
from trading.intelligence import microstructure_context,live_blockers
from synthetic_pipeline import run,fixture

class NightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.p=patch.object(database,'DB_PATH',str(Path(self.temp.name)/'test.db'));self.p.start();database.init_db()
        self.saved=cfg.model_copy(deep=True);self.workers=dict(telemetry.WORKERS);telemetry.WORKERS.clear()
        cfg.is_running=True;cfg.enable_system_summary=True;cfg.tg_chat_id='123';cfg.enable_news=False

    def tearDown(self):
        for f in type(cfg).model_fields:setattr(cfg,f,getattr(self.saved,f))
        telemetry.WORKERS.clear();telemetry.WORKERS.update(self.workers);self.p.stop();self.temp.cleanup()

    def test_real_rules_end_to_end_in_temporary_ledger(self):
        result=asyncio.run(run());self.assertEqual(result['sent'],1);self.assertEqual(result['temporary_paper_trades'],1)
        self.assertEqual(result['production_trades_created'],0)
        self.assertEqual(database.get_active_signals(),[])

    def test_book_rejection_retries_same_closed_bar(self):
        result=asyncio.run(run(thin_first=True));self.assertEqual(result['sent'],1)

    def test_optional_oi_failure_preserves_orderbook(self):
        async def fetch(path,params):
            if 'open-interest' in path:raise RuntimeError('temporary OI error')
            return {'ts':1000,'b':[['100','1000']],'a':[['100.01','1000']]}
        with patch.object(bybit,'public_get',side_effect=fetch):result=asyncio.run(bybit.get_coin_microstructure('TESTUSDT'))
        self.assertEqual(result['oi_status'],'UNAVAILABLE');self.assertEqual(result['book']['b'][0][0],'100')

    def test_truncated_book_is_not_claimed_complete(self):
        raw={'book':{'ts':1000,'b':[['100','1']],'a':[['100.01','1']]},'book_limit':1}
        micro=microstructure_context(raw,now_ms=1000)
        self.assertFalse(micro['bid_band_complete'])
        coin={'status':'OK','regime':'RANGE'}
        self.assertIn('orderbook_depth_incomplete',live_blockers('SHORT',coin,'RANGE',{'funding_rate':0},micro))

    def test_complete_thin_book_still_rejected(self):
        micro=microstructure_context({'book':{'ts':1000,'b':[['100','1']],'a':[['100.01','1']]},'book_limit':1000},now_ms=1000)
        self.assertIn('thin_orderbook',live_blockers('SHORT',{'status':'OK','regime':'RANGE'},'RANGE',{'funding_rate':0},micro))

    def test_scan_counts_distinguish_failures_from_fresh(self):
        result=telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh'},'B':{'outcome':'stale'},'C':{'outcome':'failed'},'D':{'outcome':'skipped'}},['A','B','C','D'])
        self.assertEqual(result['counts'],{'fresh':1,'stale':1,'failed':1,'skipped':1})
        self.assertEqual(telemetry.system_health()['status'],'WARNING')

    def test_stalled_scanner_is_critical_not_healthy(self):
        telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh'}},['A'])
        self.assertEqual(telemetry.system_health(time.time()+181)['status'],'CRITICAL')

    def test_no_completed_scan_does_not_claim_health(self):
        self.assertEqual(telemetry.system_health()['status'],'WARNING')

    def test_fresh_ltf_with_missing_htf_is_warning(self):
        telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh','htf':'unavailable'}},['A'])
        self.assertEqual(telemetry.system_health()['status'],'WARNING')
        self.assertIn('some_required_htf_data_unavailable',telemetry.system_health()['reasons'])

    def test_zero_signal_summary_is_nontrading_and_restart_deduplicated(self):
        telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh'}},['A'])
        bot=AsyncMock();bot.send_message.return_value=SimpleNamespace(message_id=7)
        asyncio.run(system_status.maybe_send(bot));asyncio.run(system_status.maybe_send(bot))
        self.assertEqual(bot.send_message.await_count,1)
        self.assertIn('НЕ ТОРГОВЫЙ СИГНАЛ',bot.send_message.call_args.args[1]);self.assertEqual(database.get_active_signals(),[])
        with database._conn() as con:self.assertEqual(con.execute('SELECT state,message_id FROM system_reports').fetchone()['message_id'],7)

    def test_summary_failed_send_retries_persisted_payload(self):
        telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh'}},['A']);now=time.time()
        bot=AsyncMock();bot.send_message.side_effect=OSError('offline')
        asyncio.run(system_status.maybe_send(bot,now));text=bot.send_message.call_args.args[1]
        bot.send_message.side_effect=None;bot.send_message.return_value=SimpleNamespace(message_id=8)
        asyncio.run(system_status.maybe_send(bot,now+61))
        self.assertEqual(bot.send_message.call_args.args[1],text)
        with database._conn() as con:self.assertEqual(tuple(con.execute('SELECT state,attempts FROM system_reports').fetchone()),('SENT',2))

    def test_telemetry_contains_signal_id_and_timestamp(self):
        telemetry.event('SIGNAL_GENERATED','TEST_ID',score=65)
        with database._conn() as con:
            r=con.execute('SELECT * FROM pipeline_events').fetchone();self.assertEqual(r['signal_id'],'TEST_ID');self.assertGreater(r['observed_at'],0)

    def test_queued_signal_waiting_too_long_is_critical(self):
        telemetry.record_cycle(time.time(),{'A':{'outcome':'fresh'}},['A'])
        delivery.enqueue('x','TESTUSDT',{'score':65},'123',time.time()-150)
        self.assertEqual(telemetry.system_health()['status'],'CRITICAL')

    def test_4h_failure_does_not_destroy_valid_book(self):
        from trading import engine
        now=time.time();data=fixture(now);market={'eligible':True,'volume_rank':1,'turnover_24h':1e8,'breadth':.5,'funding_rate':0}
        cfg.timeframe='5';cfg.min_signal_score=65;cfg.strategy_profile='balanced';cfg.enable_coin_research=False;cfg.loss_cooldown_minutes=0
        async def candles(symbol,tf,**kwargs):
            if str(tf)=='5':return data
            if str(tf)=='240':return data.iloc[:0]
            step=int(tf)*60000;end=int(now*1000)//step*step
            higher=data.copy();higher['timestamp']=[end-(350-i)*step for i in range(351)];return higher
        raw={'book':{'ts':int(now*1000),'b':[['103.49','10000']],'a':[['103.5','10000']]},'open_interest':[]}
        with patch.dict(engine.LATEST_DATA,{},clear=True),patch.dict(engine.MARKET_DATA,{'TESTUSDT':market},clear=True),patch.dict(engine.BENCHMARKS,{'BTCUSDT':{'trend':'UP','candles':data.iloc[:-1]}},clear=True),patch.object(engine,'MARKET_UPDATED',time.monotonic()),patch.object(engine,'get_klines_async',side_effect=candles),patch.object(engine,'get_coin_microstructure',new=AsyncMock(return_value=raw)),patch.object(engine,'request_research'):
            asyncio.run(engine.process_symbol(None,'TESTUSDT'));row=engine.LATEST_DATA['TESTUSDT']
            self.assertEqual(row['microstructure']['status'],'OK')
            self.assertIn('higher_4h_unavailable_or_stale',row['blockers']);self.assertNotIn('orderbook_unavailable',row['blockers'])

    def test_empty_data_has_failed_scan_result(self):
        from trading import engine
        with patch.object(engine,'get_klines_async',new=AsyncMock(return_value=fixture(time.time()).iloc[:0])):
            asyncio.run(engine.process_symbol(None,'EMPTYUSDT'))
        self.assertEqual(engine.SCAN_RESULTS['EMPTYUSDT']['outcome'],'failed')
        self.assertEqual(engine.SCAN_RESULTS['EMPTYUSDT']['reason'],'empty_candles')

    def test_one_zone_execution_rejection_does_not_block_other_zones(self):
        from trading import setup_journal as journal
        from test_setup_journal import zone,frame
        z1=zone();z2=dict(zone(),candle_time=1)
        a={'score':90,'status':'POTENTIAL','checks':{'Entry':False},'rejections':['no_recent_retest']}
        row=dict(fvg=z1,zones=[z1,z2],signal=None,htf_context={'trend':'UP'},blockers=['thin_orderbook'],base_blockers=[],current_price=101.,assessments={'0BULLISH':dict(a,execution_rejections=['thin_orderbook']),'1BULLISH':a})
        journal.observe('TESTUSDT','5',row,frame(),cfg,650)
        states={r['setup_id']:r['status'] for r in journal.latest_events()}
        self.assertEqual(states[journal.setup_id('TESTUSDT','5',z1)],'WATCHLIST')
        self.assertEqual(states[journal.setup_id('TESTUSDT','5',z2)],'POTENTIAL')

    def test_no_fvg_cached_scan_remains_fresh(self):
        from trading import engine
        data=fixture(time.time());data[['open','high','low','close']]=[100.,101.,99.,100.]
        cfg.timeframe='5';cfg.enable_coin_research=False
        with patch.dict(engine.LATEST_DATA,{},clear=True),patch.dict(engine.BENCHMARKS,{'BTCUSDT':{'trend':'RANGE','candles':data.iloc[:-1]}},clear=True),patch.object(engine,'MARKET_UPDATED',time.monotonic()),patch.object(engine,'get_klines_async',new=AsyncMock(return_value=data)),patch.object(engine,'request_research'):
            asyncio.run(engine.process_symbol(None,'TESTUSDT'));asyncio.run(engine.process_symbol(None,'TESTUSDT'))
            self.assertEqual(engine.SCAN_RESULTS['TESTUSDT']['outcome'],'fresh')
            self.assertEqual(engine.LATEST_DATA['TESTUSDT']['zones'],[])

    def test_position_worker_error_is_visible(self):
        from trading import engine
        with patch.object(engine,'get_active_signals',return_value=[{'symbol':'TESTUSDT'}]),patch.object(engine,'get_klines_async',new=AsyncMock(side_effect=ValueError('bad feed'))),patch.object(engine.asyncio,'sleep',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):asyncio.run(engine._position_loop(None))
        with database._conn() as con:self.assertEqual(con.execute("SELECT count(*) FROM pipeline_events WHERE kind='POSITION_WORKER_FAILED'").fetchone()[0],1)

if __name__=='__main__':unittest.main()
