"""Recheck every nightly Potential using saved T1 zone/HTF and closed OHLC."""
import asyncio
import json
import sqlite3
import sys
from collections import Counter,defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.bybit import public_get,close_bybit_session
from core.settings import cfg
from trading.context import closed_candles
from trading.strategy import validate_signal
from trading.profiles import strategy_settings
from trading.zone_lifecycle import invalidation_reason

folder=Path('data/audits/night_20260910')
async def main():
    report=json.loads((folder/'night_report.json').read_text(encoding='utf-8'));potentials={s['setup_id']:s for s in report['setups'] if s['ever_potential']};assets=sorted({s['asset'] for s in potentials.values()});frames={};errors={};sem=asyncio.Semaphore(3)
    async def fetch(symbol):
        path=folder/f'replay_{symbol}_5.json'
        if path.exists():frames[symbol]=pd.read_json(path,convert_dates=False,precise_float=True);return
        async with sem:
            try:
                raw=await public_get('/v5/market/kline',{'category':'linear','symbol':symbol,'interval':'5','limit':1000})
                frame=pd.DataFrame(raw['list'],columns=['timestamp','open','high','low','close','volume','turnover']).apply(pd.to_numeric).sort_values('timestamp').reset_index(drop=True)
                frame.to_json(path,orient='records',double_precision=15);frames[symbol]=frame
            except Exception as exc:errors[symbol]=type(exc).__name__
    await asyncio.gather(*(fetch(s) for s in assets));print('Fetched',len(frames),'assets',flush=True)
    con=sqlite3.connect((folder/'before.db').resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    settings=strategy_settings();results={k:{'asset':s['asset'],'observations':[],'invalidation_checks':[]} for k,s in potentials.items()};seen=set();counts=Counter()
    for row in con.execute('SELECT * FROM setup_events WHERE observed_at>=1788983240 ORDER BY id'):
        key=row['setup_id']
        if key not in potentials:continue
        p=json.loads(row['payload']);a=p.get('assessment',{});symbol=p['asset']
        if symbol not in frames:continue
        raw=frames[symbol];z=p['fvg'];as_of=row['observed_at']*1000
        if row['status']=='INVALIDATED':
            closed=closed_candles(raw,'5',as_of)
            why=invalidation_reason(z,closed,'5',as_of,p.get('strict_ce',True))
            full_event_bar=raw[raw.timestamp<=as_of]
            full_reason=invalidation_reason(z,full_event_bar,'5',as_of,p.get('strict_ce',True))
            verdict='closed_history_confirms' if why==row['reason'] else 'event_bar_final_ohlc_only_timing_unknown' if full_reason==row['reason'] else 'NOT_SUPPORTED'
            results[key]['invalidation_checks'].append({'event_id':row['id'],'reason':row['reason'],'verdict':verdict});counts[verdict]+=1
        if row['status'] not in ('POTENTIAL','ENTRY APPROACHING','DEVELOPING','WATCHLIST') or not a:continue
        bar=a.get('analysis_bar');identity=(key,bar,a.get('policy',{}).get('min_score'))
        if identity in seen:continue
        seen.add(identity)
        if bar is None:continue
        data=raw[raw.timestamp<=bar].tail(350)
        if len(data)<cfg.ema_period:counts['insufficient_replay_history']+=1;continue
        settings.min_signal_score=a.get('policy',{}).get('min_score',80)
        diagnostics={};candidate=validate_signal(data,z,{'trend':p['htf_bias']},settings=settings,diagnostics=diagnostics)
        recorded=a.get('checks',{}).get('Entry',False)
        results[key]['observations'].append({'event_id':row['id'],'analysis_bar':bar,'recorded_score':p.get('fvg_quality'),'recorded_entry':recorded,'replayed_entry':candidate is not None,'replay_reason':list(diagnostics),'recorded_reason':row['reason']})
        counts['observations']+=1;counts['technical_pass']+=candidate is not None
        if recorded!=(candidate is not None):counts['entry_replay_disagreement']+=1
        if counts['observations']%100==0:await asyncio.sleep(0)
    output={'scope':'All 345 sets ever Potential; saved zone geometry, saved HTF bias and threshold; closed bars up to recorded analysis_bar. Other settings use current configuration; market breadth/news/book not historically archived. Passing replay is not a historical trade authorization.','assets':len(assets),'fetched_assets':len(frames),'setups':len(results),'counts':dict(counts),'errors':errors,'results':results}
    (folder/'potential_replay.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in output.items() if k!='results'}));await close_bybit_session()
async def run():
    try:await main()
    finally:await close_bybit_session()
if __name__=='__main__':asyncio.run(run())
