import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import database
from core.settings import cfg
from trading import paper_account as account, signal_delivery as delivery, setup_journal as journal
from trading.pipeline_recovery import reconcile
from test_setup_journal import zone, frame


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.p=patch.object(database,'DB_PATH',str(Path(self.tmp.name)/'test.db'));self.p.start()
        database.init_db()
        self.key=journal.setup_id('TESTUSDT','5',zone())
        self.features=dict(position_size_usdt=100,journal_id=self.key,initial_sl=98.,initial_tp=107.,signal_entry=101.)
        self.sig=dict(entry_ts=660000,symbol='TESTUSDT',side='LONG',score=72,entry=101.,sl=98.,tp=107.,rsi=50,trend='UP',initial_risk=3.,rr=2.,signal_id='s1',ml_features=self.features)
        self.source=dict(signal='LONG',entry=101.,sl=98.,tp=107.,rr=2.,score=72,timeframe='5',fvg=zone(),journal_id=self.key)

    def tearDown(self):
        self.p.stop();self.tmp.cleanup()

    def test_close_commits_account_and_trade_once_across_restart(self):
        tid=database.log_signal(self.sig)
        self.assertEqual(account.settle(tid,'WIN',107,5,'TP',1000),(5,1005))
        self.assertIsNone(account.settle(tid,'WIN',107,5,'TP',1000))
        self.assertEqual(account.balance(1000),1005)
        self.assertIsNotNone(database.get_signal(tid)['closed_at_ms'])
        with database._conn() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM paper_cashflows').fetchone()[0],1)

    def test_crash_during_settlement_rolls_back_both(self):
        tid=database.log_signal(self.sig);account.balance(1000)
        with database._conn() as con:
            con.execute("CREATE TRIGGER fail_cashflow BEFORE INSERT ON paper_cashflows BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            account.settle(tid,'WIN',107,5,'TP',1000)
        self.assertEqual(database.get_signal(tid)['outcome'],'OPEN')
        self.assertEqual(account.balance(),1000)
        with database._conn() as con:
            con.execute('DROP TRIGGER fail_cashflow')
        self.assertEqual(account.settle(tid,'WIN',107,5,'TP',1000),(5,1005))

    def test_missing_json_mirror_does_not_lose_committed_balance(self):
        tid=database.log_signal(self.sig);account.settle(tid,'LOSS',98,-3,'SL',1000)
        with patch('core.settings.load_settings',return_value=cfg.model_copy(update={'paper_balance':1000})):
            from core.settings import reload_settings
            saved=cfg.paper_balance
            try:
                reload_settings();self.assertEqual(cfg.paper_balance,997)
            finally:
                cfg.paper_balance=saved

    def test_simultaneous_settlements_only_one_cashflow(self):
        tid=database.log_signal(self.sig);account.balance(1000)
        with ThreadPoolExecutor(4) as pool:
            result=list(pool.map(lambda _:account.settle(tid,'WIN',107,5,'TP',1000),range(4)))
        self.assertEqual(sum(r is not None for r in result),1)
        self.assertEqual(account.balance(),1005)

    def test_duplicate_trade_cannot_reopen_closed_signal(self):
        tid=database.log_signal(self.sig);account.settle(tid,'WIN',107,5,'TP',1000)
        self.assertIsNone(database.log_signal(self.sig))

    def test_simultaneous_duplicate_signal_one_trade(self):
        with ThreadPoolExecutor(4) as pool:
            result=list(pool.map(lambda _:database.log_signal(self.sig),range(4)))
        self.assertEqual(sum(r is not None for r in result),1)

    def test_simultaneous_assets_respect_portfolio_limit(self):
        with ThreadPoolExecutor(4) as pool:
            result=list(pool.map(lambda n:database.log_signal(dict(self.sig,signal_id=str(n),symbol=str(n),max_active_limit=2)),range(4)))
        self.assertEqual(sum(r is not None for r in result),2)

    def test_original_stop_and_trailing_history_survive(self):
        tid=database.log_signal(self.sig)
        database.update_signal_levels(tid,sl=102,reason='TRAIL')
        with database._conn() as con:
            self.assertEqual([tuple(r) for r in con.execute('SELECT sl,tp,reason FROM paper_level_events ORDER BY id')],[(98.,107.,'INITIAL'),(102.,107.,'TRAIL')])
        self.assertEqual(json.loads(database.get_signal(tid)['ml_features'])['initial_sl'],98)

    def test_recovery_creates_honest_late_event_and_result_once(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123',created=660)
        tid=database.log_signal(self.sig);account.settle(tid,'WIN',107,5,'TP',1000)
        self.assertEqual(reconcile(),1);self.assertEqual(reconcile(),0)
        events=journal.history(self.key)
        self.assertEqual([e['status'] for e in events],['RECOVERED CONFIRMATION','TP HIT'])
        self.assertEqual(events[0]['payload']['source_observed_at'],660)
        self.assertGreater(events[0]['observed_at'],660)
        self.assertEqual(account.balance(),1005)

    def test_recovery_never_invents_trade(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123',created=660);reconcile()
        self.assertEqual(database.get_active_signals(),[])

    def test_wrong_price_link_is_not_repaired_blindly(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123',created=660)
        tid=database.log_signal(dict(self.sig,signal_id=None,ml_features=dict(self.features,signal_entry=103)))
        reconcile();self.assertIsNone(database.get_signal(tid)['signal_id'])

    def test_wrong_trade_result_cannot_be_attached_during_recovery(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123',created=660)
        tid=database.log_signal(dict(self.sig,signal_id=None,ml_features=dict(self.features,signal_entry=103)))
        account.settle(tid,'WIN',107,5,'TP',1000);reconcile()
        self.assertEqual([e['status'] for e in journal.history(self.key)],['RECOVERED CONFIRMATION'])

    def test_score_change_is_new_event_t1_stays_72(self):
        payload=journal.snapshot('TESTUSDT','5',dict(fvg=zone(),assessments={'0BULLISH':{'score':72}},current_price=101))
        journal.record(self.key,'POTENTIAL','awaiting',payload,650)
        later={**payload,'assessment':{'score':84},'fvg_quality':84}
        journal.record(self.key,'POTENTIAL','awaiting',later,660)
        history=journal.history(self.key)
        self.assertEqual([e['payload']['fvg_quality'] for e in history],[72,84])
        self.assertEqual(history[0]['observed_at'],650)

    def test_receipt_and_sent_state_commit_together(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123')
        attempt=delivery.begin_attempt('s1');delivery.finish_attempt(attempt,'s1','API_ACCEPTED',42)
        with database._conn() as con:
            row=con.execute('SELECT state,sent_at,telegram_message_id FROM signal_outbox').fetchone()
            self.assertEqual(row['state'],'SENT');self.assertEqual(row['telegram_message_id'],42);self.assertIsNotNone(row['sent_at'])
        self.assertEqual(delivery.outbox_rows(),[])

    def test_crash_during_send_remains_explicitly_unknown(self):
        delivery.enqueue('s1','TESTUSDT',self.source,'123');delivery.begin_attempt('s1')
        with database._conn() as con:
            row=con.execute('SELECT result,finished_at FROM delivery_attempts').fetchone()
            self.assertEqual(tuple(row),('STARTED',None))
            self.assertEqual(con.execute('SELECT state FROM signal_outbox').fetchone()[0],'PENDING')

    def test_361_pair_cycle_processes_every_asset_once(self):
        from trading import engine
        universe={f'TEST{i}':{'eligible':True,'volume_rank':i} for i in range(361)}
        calls=[]
        async def process(bot,symbol): calls.append(symbol)
        async def stop(delay): raise asyncio.CancelledError
        with patch.dict(engine.MARKET_DATA,universe,clear=True),patch.object(engine,'MARKET_UPDATED',time.monotonic()),patch.object(engine,'reload_settings'),patch.object(engine,'get_active_signals',return_value=[]),patch.object(engine,'process_symbol',side_effect=process),patch.object(engine.asyncio,'sleep',side_effect=stop),patch.object(cfg,'is_running',True),patch.object(cfg,'scan_all_symbols',True),patch.object(cfg,'enable_coin_research',False):
            with self.assertRaises(asyncio.CancelledError): asyncio.run(engine._scan_loop(None))
        self.assertEqual(len(calls),361);self.assertEqual(len(set(calls)),361)
        self.assertEqual(engine.SCAN_STATUS['processed'],361)

    def test_t2_fill_keeps_t1_signal_and_uses_t2_quote(self):
        from trading.paper_trading import prepare_paper_entry
        original=dict(self.source,microstructure={'book_timestamp':1000})
        fill,reason=prepare_paper_entry(original,101.1,{'book_timestamp':2000},cfg)
        self.assertIsNone(reason);self.assertEqual(fill['entry'],101.1)
        self.assertEqual(fill['signal_entry'],101.)
        self.assertEqual(fill['microstructure']['book_timestamp'],2000)
        self.assertEqual(original['entry'],101.)
        self.assertEqual(original['microstructure']['book_timestamp'],1000)
        self.assertEqual(fill['score'],72)

    def test_t2_moved_market_cannot_fill_at_t1_price(self):
        from trading.paper_trading import prepare_paper_entry
        fill,reason=prepare_paper_entry(self.source,104,{},cfg)
        self.assertIsNone(fill);self.assertIsNotNone(reason)

    def test_may_form_requires_structure_liquidity_and_htf(self):
        from trading import setup_assessment as assessment
        def rejected(*args,**kwargs):kwargs['diagnostics']['no_recent_retest']=1
        with patch.object(assessment,'market_context',return_value={'atr_pct':1,'trend':'UP','rsi':50,'adx':25}),patch.object(assessment,'structure_context',return_value={'direction':'UP','sweep':'SELL_SIDE'}),patch.object(assessment,'trendline_context',return_value={'bullish_retest':False,'bearish_retest':False}),patch.object(assessment,'validate_signal',side_effect=rejected):
            good=assessment.assess_zones(frame(),[zone()],{'trend':'UP'},{},cfg)['0BULLISH']
            bad=assessment.assess_zones(frame(),[zone()],{'trend':'DOWN'},{},cfg)['0BULLISH']
        self.assertTrue(good['signal_may_form']);self.assertFalse(good['checks']['Entry'])
        self.assertFalse(bad['signal_may_form']);self.assertEqual(bad['status'],'WATCHLIST')

    def test_invalidated_may_form_disappears(self):
        payload=journal.snapshot('TESTUSDT','5',dict(fvg=zone(),assessments={'0BULLISH':{'score':72,'signal_may_form':True}}))
        payload['expires_at']=1000
        journal.record(self.key,'ENTRY APPROACHING','waiting',payload,650)
        self.assertTrue(journal.report(660)[0]['signal_may_form'])
        journal.record(self.key,'INVALIDATED','full_fill',payload,670)
        self.assertFalse(journal.report(680)[0]['signal_may_form'])

    def test_assessment_score_ignores_unfinished_and_future_bars(self):
        from test_audit import bars
        from trading.data_quality import decision_candles
        from trading.setup_assessment import assess_zones
        data=bars();now=249*300000
        first=assess_zones(decision_candles(data,'5',now,200),[zone()],{'trend':'UP'},{},cfg)
        data.loc[249,['high','close','volume']]=[1000,900,1e9]
        second=assess_zones(decision_candles(data,'5',now,200),[zone()],{'trend':'UP'},{},cfg)
        self.assertEqual(first,second)

    def test_policy_change_keeps_actual_activation_history(self):
        from trading.pipeline_recovery import record_policy
        settings=cfg.model_copy(update={'min_signal_score':65});record_policy(settings);record_policy(settings)
        settings.min_signal_score=80;record_policy(settings)
        with database._conn() as con:
            values=[json.loads(r[0]) for r in con.execute('SELECT payload FROM runtime_policy_events ORDER BY id')]
        self.assertEqual([v['min_score'] for v in values],[65,80])
        self.assertNotIn('tg_chat_id',values[0]);self.assertNotIn('tg_bot_token',values[0])

    def test_exhausted_zone_cannot_be_presented_as_approaching(self):
        from test_audit import bars
        from trading.setup_assessment import assess_zones
        z=dict(zone(),touch_bars=cfg.fvg_max_touch_bars+1)
        result=assess_zones(bars(),[z],{'trend':'UP'},{},cfg)['0BULLISH']
        self.assertEqual(result['status'],'WATCHLIST')
        self.assertFalse(result['signal_may_form'])
        self.assertIn('prolonged_zone_residence',result['rejections'])


if __name__=='__main__': unittest.main()
