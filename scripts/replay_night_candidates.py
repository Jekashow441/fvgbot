"""Causal OHLC replay of all recorded technical entry candidates, never orders."""
import asyncio
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.bybit import public_get,close_bybit_session
from core.settings import cfg
from trading.context import closed_candles
from trading.strategy import validate_signal,market_context,detect_fvgs
from trading.profiles import strategy_settings

folder=Path('data/audits/night_20260910')
async def main():
    report=json.loads((folder/'night_report.json').read_text(encoding='utf-8'));observations=report['technical_entry_observations'];assets=sorted({e['payload']['asset'] for e in observations});sem=asyncio.Semaphore(4);frames={};errors={}
    async def fetch(symbol,tf):
        async with sem:
            try:
                requested_at=int(time.time()*1000)
                result=await public_get('/v5/market/kline',{'category':'linear','symbol':symbol,'interval':tf,'limit':1000})
                df=pd.DataFrame(result['list'],columns=['timestamp','open','high','low','close','volume','turnover']).apply(pd.to_numeric).sort_values('timestamp').reset_index(drop=True)
                df.attrs['observed_at_ms']=requested_at
                df.to_json(folder/f'replay_{symbol}_{tf}.json',orient='records',double_precision=15);frames[symbol,tf]=df
            except Exception as exc:errors[symbol+':'+tf]=type(exc).__name__
    await asyncio.gather(*(fetch(s,tf) for s in assets for tf in ('5','60','240')))
    settings=strategy_settings();rows=[]
    for e in observations:
        p=e['payload'];s=p['asset'];t=e['observed_at']*1000;z=p['fvg'];a=p.get('assessment',{});d={}
        if any((s,tf) not in frames for tf in ('5','60','240')):continue
        ltf=closed_candles(frames[s,'5'][frames[s,'5'].timestamp<=t].tail(351),'5',t)
        htf=closed_candles(frames[s,'60'][frames[s,'60'].timestamp<=t].tail(351),'60',t)
        higher=closed_candles(frames[s,'240'][frames[s,'240'].timestamp<=t].tail(260),'240',t)
        settings.min_signal_score=a.get('policy',{}).get('min_score',80)
        # This rechecks the observed zone, not a hindsight-selected replacement.
        candidate=validate_signal(ltf,z,market_context(htf),settings=settings,diagnostics=d)
        redetected=any(x['candle_time']==z['candle_time'] and x['type']==z['type'] for x in detect_fvgs(ltf,settings))
        completed=closed_candles(frames[s,'5'],'5',frames[s,'5'].attrs['observed_at_ms'])
        future=completed[completed.timestamp>t].head(12);price=float(ltf.iloc[-1].close)
        rows.append({'setup_id':e['setup_id'],'observed_at':e['observed_at'],'recorded_score':p.get('fvg_quality'),'recorded_rejection':e['reason'],'zone_redetected_with_current_shape_settings':redetected,'replayed_technical_candidate':candidate is not None,'replay_score':candidate.get('score') if candidate else None,'replay_rejections':list(d),'higher_4h_bias_replayed':market_context(higher)['trend'],'ltf_latest_closed_bar':int(ltf.iloc[-1].timestamp),'bars_after_observation':len(future),'next_12_bars_close_change_pct':(float(future.iloc[-1].close)/price-1)*100 if len(future) else None,'after_observation_high':float(future.high.max()) if len(future) else None,'after_observation_low':float(future.low.min()) if len(future) else None})
    output={'scope':'All recorded technical entry observations; not full nightly backtest. Current non-score parameters may differ from past configuration. Finalized historical OHLC cannot recreate contemporaneous orderbooks/news. Subsequent movement is descriptive, not a missed-profit label.','assets':len(assets),'observations':rows,'fetch_errors':errors}
    (folder/'replay_result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'assets':len(assets),'observations':len(rows),'technical_pass':sum(r['replayed_technical_candidate'] for r in rows),'redetected':sum(r['zone_redetected_with_current_shape_settings'] for r in rows),'errors':errors}))
    await close_bybit_session()
if __name__=='__main__':asyncio.run(main())
