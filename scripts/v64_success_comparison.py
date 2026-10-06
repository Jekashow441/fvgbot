"""Frozen historical comparison and isolated real-engine experiments."""
import asyncio,inspect,json,sqlite3,tempfile,time,sys
from pathlib import Path
from collections import Counter,defaultdict
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pandas as pd
from critical_rejection_research import frame,known,reactions,liquidity,CFG,ROOT
from trading.strategy import validate_signal,detect_fvgs,market_context,_calc_atr
from trading import engine,signal_delivery as delivery
from core import database
from core.settings import cfg
import synthetic_pipeline as sp
from verify_synthetic_80 import sequence
OUT=ROOT/'data/audits/v64_success_comparison';OUT.mkdir(parents=True,exist_ok=True)
def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=lambda x:x.item() if hasattr(x,'item') else str(x)),encoding='utf8')

async def reference_success():
    capture={};source=inspect.getsource(sp.run).replace('cfg.min_signal_score=65','cfg.min_signal_score=80')
    source=source.replace("return {'mode':'synthetic temporary database'", "capture.update({'OHLC':data.to_dict('records'),'row':row,'orderbook_raw':raw,'htf_60':(await candles('TESTUSDT','60')).to_dict('records'),'htf_240':(await candles('TESTUSDT','240')).to_dict('records'),'decision_at':now});return {'mode':'synthetic temporary database'")
    ns=dict(sp.run.__globals__);ns.update(fixture=sequence,capture=capture)
    exec(compile(source,'<reference_success_capture>','exec'),ns)
    result=await ns['run']();assert result['score']==95 and result['sent']==1
    capture['cycle']=result;capture['note']='Same synthetic OHLC shape; timestamps regenerated. Mock execution context, not historical exchange data.'
    save('REFERENCE_SUCCESS_CASE.json',capture);return capture

class Sink:
    def __init__(self):self.messages=[]
    async def send_message(self,*args,**kwargs):self.messages.append(args);return SimpleNamespace(message_id=9001)

async def real_engine(ev):
    p=ev['payload'];asset=p['asset'];now=ev['observed_at'];end=int(now*1000)//300000*300000;price=p['current_price'];saved=cfg.model_copy(deep=True);sink=Sink()
    async def candles(symbol,tf,limit=351,**kwargs):
        d=known(asset,str(tf),int(now*1000))
        if str(tf)=='5':
            # Point observation only; no finalized future extrema from open candle.
            d=pd.concat([d,pd.DataFrame([dict(timestamp=end,open=price,high=price,low=price,close=price,volume=0.)])],ignore_index=True)
        d.attrs['observed_at_ms']=int(now*1000);return d.tail(limit).copy()
    market={'eligible':True,'volume_rank':1,'turnover_24h':1e8,'breadth':.5,'funding_rate':0.,'spread_bps':1}
    raw={'book':{'ts':int(now*1000),'b':[[str(price*.99995),str(1e6/price)]],'a':[[str(price*1.00005),str(1e6/price)]]},'open_interest':[]}
    try:
        cfg.min_signal_score=80;cfg.timeframe='5';cfg.strategy_profile='balanced';cfg.is_running=True;cfg.tg_chat_id='123';cfg.enable_news=False;cfg.enable_coin_research=False;cfg.loss_cooldown_minutes=0;cfg.research_gate_mode='paper'
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            stack.enter_context(patch.object(database,'DB_PATH',str(Path(tmp)/'test.db')));database.init_db()
            for name,value in [('LATEST_DATA',{}),('SENT_SIGNALS',{}),('MARKET_DATA',{asset:market}),('BENCHMARKS',{'BTCUSDT':{'trend':'RANGE','candles':known(asset,'5',int(now*1000))}})]:stack.enter_context(patch.dict(getattr(engine,name),value,clear=True))
            stack.enter_context(patch.object(engine,'MARKET_UPDATED',time.monotonic()))
            stack.enter_context(patch.object(engine,'get_klines_async',side_effect=candles));stack.enter_context(patch.object(delivery,'get_klines_async',side_effect=candles));stack.enter_context(patch.object(engine,'get_coin_microstructure',new=AsyncMock(return_value=raw)));stack.enter_context(patch.object(engine,'request_research'))
            stack.enter_context(patch('time.time',return_value=now))
            await engine.process_symbol(sink,asset);await delivery.deliver_once(sink)
            row=engine.LATEST_DATA.get(asset,{})
            with database._conn() as con:
                traces=[dict(r) for r in con.execute('SELECT * FROM pipeline_events ORDER BY id')] if con.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_events'").fetchone() else []
                trades=con.execute('SELECT count(*) FROM signals').fetchone()[0]
            return {'setup_id':ev['setup_id'],'scope':'Historical closed OHLC through same engine. Reference deep book/funding/breadth; news disabled. Not actual historical execution replay.','row':row,'traces':traces,'mock_sent':len(sink.messages),'temporary_trades':trades}
    finally:
        for k in type(cfg).model_fields:setattr(cfg,k,getattr(saved,k))

def archived_cards(report):
    con=sqlite3.connect((ROOT/'data/audits/night_20260910/before.db').resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    unique={}
    for e in report['technical_entry_observations']:unique.setdefault(e['setup_id'],e)
    cards=[];timelines=[];combos={k:[] for k in ('current','vwap_soft','book_soft','both_soft')}
    for sid,ev in unique.items():
        p=ev['payload'];z=p['fvg'];ts=p['assessment']['analysis_bar'];d=known(p['asset'],'5',ts+300000);htf={'trend':p['htf_bias']};di={};candidate=validate_signal(d,z,htf,settings=CFG,diagnostics=di);rs=ev['reason'].split('; ')
        events=[];first_irreversible=None
        for raw in con.execute('SELECT * FROM setup_events WHERE setup_id=? ORDER BY id',(sid,)):
            event=dict(raw);payload=json.loads(event.pop('payload'));a=payload.get('assessment') or {}
            event.update(previous=events[-1]['status'] if events else None,analysis_bar=a.get('analysis_bar'),score=a.get('score'),policy=a.get('policy'),checks=a.get('checks'),validator_first=(a.get('rejections') or [None])[0],execution_rejections=a.get('execution_rejections'),context=payload.get('execution_context'))
            if event['status'] in ('INVALIDATED','EXPIRED','MISSED') and first_irreversible is None:first_irreversible={'at':event['observed_at'],'reason':event['reason']}
            events.append(event)
        timeline=[]
        start=z['candle_time']-20*300000
        available=frame(p['asset']); available=available[(available.timestamp>=start)&(available.timestamp<=ts)]
        for t in available.timestamp:
            prefix=known(p['asset'],'5',int(t)+300000);exists=t>=z['formed_at']
            timeline.append({'decision_close_ms':int(t)+300000,'known_through_open_ms':int(t),'fvg_exists':bool(exists),'reaction':reactions(prefix,z) if exists else None,'htf':market_context(known(p['asset'],'60',int(t)+300000)),'execution_context':'UNKNOWN unless explicitly archived'})
        checks=p['assessment']['checks'];conditions={'FVG':checks.get('FVG'),'Structure':checks.get('Structure'),'Liquidity':checks.get('Liquidity'),'HTF':p['htf_bias'],'VWAP':'FAIL' if 'extended_from_vwap' in rs else 'NO_RECORDED_FAILURE','Orderbook':'FAIL' if any('orderbook' in r for r in rs) else 'NO_RECORDED_FAILURE','Spread':'FAIL' if 'wide_spread' in rs else 'NO_RECORDED_FAILURE','Mitigation':'FAIL' if 'fvg_fully_filled' in rs else 'NO_RECORDED_FAILURE','Rejection':True,'Entry':True,'Score':p['fvg_quality'],'Final':ev['status']}
        card={'setup_id':sid,'asset':p['asset'],'conditions':conditions,'first_technical_candidate_at':ev['observed_at'],'first_execution_blockers':rs,'first_irreversible':first_irreversible,'entry':candidate,'replay_diagnostics':di,'fvg':z,'current_price':p['current_price'],'atr':float(_calc_atr(d).iloc[-1]),'distance_absolute':abs(p['current_price']-z['ce']),'units':'price absolute; ATR price absolute; distance/ATR dimensionless','tick_size':'UNKNOWN: archived exchange instrument filters absent','original_entry_levels':{'entry':p.get('entry'),'sl':p.get('sl'),'tp':p.get('tp1')},'score_components':next(x for x in json.loads((ROOT/'data/audits/critical_rejection_v6.3/candidate_scores.json').read_text(encoding='utf8')) if x['setup_id']==sid)}
        cards.append(card);timelines.append({'setup_id':sid,'durable_events':events,'closed_candle_timeline':timeline})
        for mode,removed in [('current',set()),('vwap_soft',{'extended_from_vwap'}),('book_soft',{'thin_orderbook'}),('both_soft',{'extended_from_vwap','thin_orderbook'})]:
            remaining=[r for r in rs if r not in removed];combos[mode].append({'setup_id':sid,'remaining':remaining,'recorded_gate_proxy':not remaining})
    save('real18_cards.json',cards);save('real18_timelines.json',timelines);save('execution_counterfactual.json',combos)
    save('comparison_matrix.json',{condition:{c['setup_id']:c['conditions'][condition] for c in cards} for condition in cards[0]['conditions']})
    return list(unique.values()),{'real_candidates':len(cards),'first_execution_reasons':dict(Counter(r for c in cards for r in c['first_execution_blockers'])),'counterfactual_gate_survivors':{k:sum(x['recorded_gate_proxy'] for x in v) for k,v in combos.items()},'entry_reconstructed':sum(c['entry'] is not None for c in cards),'durable_states':dict(Counter(e['status'] for t in timelines for e in t['durable_events']))}

def cohorts():
    base=ROOT/'data/audits/critical_rejection_v6.3';ideal=json.loads((base/'idealized_setups.json').read_text(encoding='utf8'));diff=json.loads((base/'actual_detector_diff.json').read_text(encoding='utf8'));lookup={(r['asset'],r['candle_time']):r for r in diff};rows=[]
    for row in ideal:
        z=row['fvg'];dt=lookup[row['asset'],row['candle_time']];ts=z['formed_at']+300000*(CFG.fvg_confirmations+1);p=known(row['asset'],'5',ts);di={};candidate=validate_signal(p,z,settings=CFG,diagnostics=di) if len(p)>=30 else None
        rows.append({**row,'production_membership_at_snapshot':dt,'technical_at_first_confirmation':bool(candidate),'technical_diagnostics':di,'classification':'EXPECTED_RULE_REJECTION' if dt['current_first_reason']!='accepted_with_full_warmup' else 'PREVIOUS_RESEARCH_FALSE_NEGATIVE','historical_full_pipeline':'UNKNOWN: missing historical execution data'})
    save('idealized144_pipeline.json',rows)
    volume=json.loads((base/'volume_649.json').read_text(encoding='utf8'));restored=[]
    for row in volume:
        if row['volume']!='PASS':continue
        d=frame(row['asset']);k=int(d.index[d.timestamp==row['candle_time']][-1]);baseline=float(d.volume.iloc[k-20:k].mean());restored.append({'asset':row['asset'],'timestamp':row['candle_time'],'volume':float(d.volume.iloc[k]),'baseline':baseline,'required_baseline_bars':20,'baseline_available_at':row['candle_time'],'timeframe':'5','dataset':'saved Bybit linear OHLCV','original_failure':'research 30-bar truncation','production_volume_pass':float(d.volume.iloc[k])>baseline})
    save('volume163_restored.json',restored)
    return {'idealized':len(rows),'actual_snapshot_detector_accepts':sum(r['production_membership_at_snapshot']['production_accepts_full_history'] for r in rows),'classifications':dict(Counter(r['classification'] for r in rows)),'volume_restored':len(restored)}

async def main():
    capture=await reference_success();report=json.loads((ROOT/'data/audits/night_20260910/night_report.json').read_text(encoding='utf8'))
    events,result=archived_cards(report);result['cohorts']=cohorts()
    runs=[]
    for ev in events:runs.append(await real_engine(ev))
    save('historical_same_engine.json',runs);result['historical_reference_context']={'mock_sent':sum(r['mock_sent'] for r in runs),'temporary_trades':sum(r['temporary_trades'] for r in runs),'blockers':dict(Counter(x for r in runs for x in r['row'].get('blockers',[])))}
    save('summary.json',result);print(json.dumps(result))
if __name__=='__main__':asyncio.run(main())
