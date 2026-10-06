import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core import database
from core.settings import cfg
from trading import setup_journal as journal
from trading.zone_lifecycle import invalidation_reason
from trading.signal_delivery import signal_zone_reason, price_valid


def zone():
    return dict(type='BULLISH',bottom=99.,top=100.,ce=99.5,candle_time=0,formed_at=0,age=2)


def frame():
    return pd.DataFrame([dict(timestamp=t,open=100.5,high=102.,low=100.2,close=101.,volume=100.) for t in (300000,600000)])


class ZoneTests(unittest.TestCase):
    def test_price_recovered_but_zone_already_invalid(self):
        f=frame();f.loc[1,'low']=98.9
        signal=dict(signal='LONG',entry=101.,sl=98.,tp=107.,timeframe='5',fvg=zone(),analyzed_at=500)
        self.assertTrue(price_valid(signal,101.))
        self.assertEqual(signal_zone_reason(signal,f,650000),'fvg_fully_filled')

    def test_unclosed_ce_does_not_invalidate(self):
        f=frame();f.loc[1,['open','low','close']]=[99.8,99.2,99.3]
        self.assertIsNone(invalidation_reason(zone(),f,'5',650000))
        self.assertEqual(invalidation_reason(zone(),f,'5',900000),'fvg_ce_invalidated')

    def test_unclosed_wick_can_invalidate(self):
        f=frame();f.loc[1,'low']=99
        self.assertEqual(invalidation_reason(zone(),f,'5',650000),'fvg_fully_filled')

    def test_future_wick_not_used(self):
        f=frame();f.loc[1,'low']=98
        self.assertIsNone(invalidation_reason(zone(),f,'5',599999))

    def test_bearish_full_fill(self):
        z=dict(type='BEARISH',bottom=100,top=103,ce=101.5,formed_at=0)
        f=frame();f.loc[1,'high']=103
        self.assertEqual(invalidation_reason(z,f,'5',650000),'fvg_fully_filled')

    def test_incomplete_invalidation_history_blocks_delivery(self):
        signal=dict(fvg=zone(),timeframe='5',analyzed_at=200)
        self.assertEqual(signal_zone_reason(signal,frame(),650000),'invalidation_history_incomplete')


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.dbpatch=patch.object(database,'DB_PATH',str(Path(self.tmp.name)/'test.db'))
        self.dbpatch.start()
        self.row=dict(fvg=zone(),htf_context={'trend':'UP'},signal=None,blockers=[],current_price=101.)
        self.key=journal.setup_id('TESTUSDT','5',zone())

    def tearDown(self):
        self.dbpatch.stop();self.tmp.cleanup()

    def test_potential_has_no_fabricated_levels(self):
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,650)
        event=journal.history(self.key)[0]
        self.assertEqual(event['status'],'POTENTIAL')
        self.assertIsNone(event['payload']['entry'])
        self.assertIsNone(event['payload']['confidence'])

    def test_opposite_htf_is_ignore_not_potential(self):
        self.row['htf_context']={'trend':'DOWN'}
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,650)
        event=journal.report(650)[0]
        self.assertEqual(event['status'],'WATCHLIST')
        self.assertEqual(event['category'],'🔴 INVALID / IGNORE')

    def test_invalidation_preserves_original(self):
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,650)
        initial=journal.history(self.key)[0]
        broken=frame();broken.loc[1,'low']=98.9
        journal.observe('TESTUSDT','5',self.row,broken,cfg,660)
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,670)
        events=journal.history(self.key)
        self.assertEqual(events[0],initial)
        self.assertEqual(events[-1]['status'],'INVALIDATED')
        self.assertEqual(len(events),2)

    def test_confirmation_and_paper_result_are_new_events(self):
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,650)
        self.row.update(signal=dict(entry=101,sl=98,tp=107,rr=2,score=85,paper_opened=True),signal_created=660)
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,660)
        journal.record_trade_result(self.key,'SL',-.5)
        events=journal.history(self.key)
        self.assertEqual([x['status'] for x in events],['POTENTIAL','CONFIRMED','SL HIT'])
        self.assertIsNone(events[0]['payload']['entry'])
        self.assertEqual(events[-1]['payload']['result']['pnl_pct'],-.5)

    def test_multiple_fvg_recorded_separately(self):
        second={**zone(),'candle_time':1}
        self.row['zones']=[zone(),second]
        journal.observe('TESTUSDT','5',self.row,frame(),cfg,650)
        self.assertEqual(len(journal.latest_events()),2)

    def test_invalidated_outbox_never_sends(self):
        from trading import signal_delivery as delivery
        signal=dict(signal='LONG',entry=101.,sl=98.,tp=107.,rr=2,timeframe='5',fvg=zone(),analyzed_at=500)
        f=frame();f.loc[1,'low']=98.9
        with patch.object(cfg,'is_running',True),patch.object(cfg,'enable_news',False),patch.object(cfg,'enable_risk_guard',False),patch.object(cfg,'tg_chat_id','123'),patch.object(delivery,'get_klines_async',new=AsyncMock(return_value=f)):
            delivery.enqueue('test','TESTUSDT',signal,'123')
            bot=AsyncMock()
            asyncio.run(delivery.deliver_once(bot))
            bot.send_message.assert_not_called()
            with database._conn() as con:
                row=con.execute('SELECT state,error FROM signal_outbox').fetchone()
                self.assertEqual(tuple(row),('EXPIRED','fvg_fully_filled'))


if __name__=='__main__':
    unittest.main()
