"""Isolated end-to-end fixture: real rules, temporary ledger, optional labelled Telegram test."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core import database
from core.settings import cfg
from trading import engine,signal_delivery as delivery,setup_journal as journal

def fixture(now):
    step=300000;end=int(now*1000)//step*step
    df=pd.DataFrame([dict(timestamp=end-(350-i)*step,open=100.,high=101.,low=99.,close=100.,volume=100.) for i in range(351)])
    df.loc[338:343,'close']=[101,99,101,99,101,100]
    df.loc[344,['open','high','low','close','volume']]=[100,106,99.5,105.5,300]
    df.loc[345:348,['open','high','low','close']]=[104,107,103.5,105]
    df.loc[345,'low']=103
    df.loc[349:350,['open','high','low','close']]=[102.5,104,102,103.5]
    df.attrs['observed_at_ms']=int(now*1000)
    return df

class TestBot:
    def __init__(self,real=None):self.real=real;self.messages=[]
    async def send_message(self,chat,text,**kwargs):
        assert 'TESTUSDT' in text
        labelled='🧪 SYNTHETIC PIPELINE TEST — НЕ ТОРГОВЫЙ СИГНАЛ. ВРЕМЕННАЯ ТЕСТОВАЯ БАЗА.\n\n'+text
        self.messages.append(labelled)
        if self.real:return await self.real.send_message(chat,labelled,**kwargs)
        return SimpleNamespace(message_id=424242)

async def run(real_bot=None,thin_first=False):
    saved=cfg.model_copy(deep=True);now=time.time();data=fixture(now);bot=TestBot(real_bot)
    market={'eligible':True,'volume_rank':1,'turnover_24h':1e8,'breadth':.5,'funding_rate':0.,'spread_bps':1}
    async def candles(symbol,timeframe,limit=351,**kwargs):
        if str(timeframe)=='5':return data.copy()
        step=int(timeframe)*60000;end=int(now*1000)//step*step
        return pd.DataFrame([dict(timestamp=end-(350-i)*step,open=80+i*.05,high=81+i*.05,low=79+i*.05,close=80.2+i*.05,volume=100.) for i in range(351)])
    raw={'book':{'ts':int(now*1000),'b':[['103.49','10000']],'a':[['103.50','10000']]},'open_interest':[]}
    try:
        cfg.timeframe='5';cfg.min_signal_score=65;cfg.strategy_profile='balanced';cfg.is_running=True
        cfg.tg_chat_id=cfg.tg_chat_id if real_bot else '123';cfg.enable_news=False;cfg.enable_coin_research=False
        cfg.loss_cooldown_minutes=0;cfg.paper_balance=10000;cfg.research_gate_mode='paper'
        with tempfile.TemporaryDirectory() as directory,patch.object(database,'DB_PATH',str(Path(directory)/'test.db')):
            database.init_db()
            thin={'book':dict(raw['book'],b=[['103.49','1']],a=[['103.50','1']]),'open_interest':[]}
            mock_book=AsyncMock(side_effect=[thin,raw]) if thin_first else AsyncMock(return_value=raw)
            with patch.dict(engine.LATEST_DATA,{},clear=True),patch.dict(engine.SENT_SIGNALS,{},clear=True),patch.dict(engine.MARKET_DATA,{'TESTUSDT':market},clear=True),patch.dict(engine.BENCHMARKS,{'BTCUSDT':{'trend':'UP','candles':data.iloc[:-1].copy()}},clear=True),patch.object(engine,'MARKET_UPDATED',time.monotonic()),patch.object(engine,'get_klines_async',side_effect=candles),patch.object(engine,'get_coin_microstructure',new=mock_book),patch.object(engine,'request_research'),patch.object(delivery,'get_klines_async',side_effect=candles):
                if thin_first:
                    await engine.process_symbol(bot,'TESTUSDT')
                    assert engine.LATEST_DATA['TESTUSDT']['signal'] is None
                    assert 'thin_orderbook' in engine.LATEST_DATA['TESTUSDT']['blockers']
                await engine.process_symbol(bot,'TESTUSDT')
                row=engine.LATEST_DATA['TESTUSDT']
                if not row.get('signal'):
                    raise AssertionError(json.dumps({'blockers':row.get('blockers'),'diagnostics':row.get('setup_diagnostics'),'zones':row.get('zones')},ensure_ascii=True))
                await delivery.deliver_once(bot)
                await delivery.deliver_once(bot)
                with database._conn() as con:
                    counts={t:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('signals','signal_outbox','setup_events')}
                    sent=dict(con.execute('SELECT state,telegram_message_id FROM signal_outbox').fetchone())
                    events=[r[0] for r in con.execute('SELECT kind FROM pipeline_events')]
                assert counts['signals']==1 and counts['signal_outbox']==1
                assert sent['state']=='SENT' and len(bot.messages)==1
                assert any(e['status']=='CONFIRMED' for e in journal.latest_events())
                return {'mode':'synthetic temporary database','score':row['signal']['score'],'detected_zones':len(row['zones']),'confirmed':1,'generated':1,'queued':1,'sent':1,'temporary_paper_trades':1,'production_trades_created':0,'receipt':sent['telegram_message_id'],'events':events}
    finally:
        for name in type(cfg).model_fields:setattr(cfg,name,getattr(saved,name))

if __name__=='__main__':
    async def main():
        bot=None
        if '--telegram' in sys.argv:
            from aiogram import Bot
            bot=Bot(cfg.tg_token)
        try:
            result=await run(bot)
            Path('data/audits/night_20260910/synthetic_result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
            print(json.dumps(result))
        finally:
            if bot:await bot.session.close()
    asyncio.run(main())
