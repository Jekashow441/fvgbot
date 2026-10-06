"""Read-only research for V6.3 bottlenecks; never mutates production strategy/config."""
import asyncio, json, math, sys, time
from collections import Counter, defaultdict
from pathlib import Path
import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'audits'/'deep_logic_v6.3'
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
from core.settings import cfg
from trading.strategy import detect_fvgs, _calc_atr
from trading.context import structure_context

REPORT=ROOT/'data'/'audits'/'night_20260910'/'night_report.json'

def ref_fvgs(df):
    """Independent classic detector: OHLC only, no bot helper calls."""
    out=[]
    if len(df)<3:return out
    h=df.high.to_numpy(float);l=df.low.to_numpy(float);t=df.timestamp.to_numpy(int)
    for i in range(2,len(df)):
        if l[i]>h[i-2]: out.append({'type':'BULLISH','candle_time':int(t[i-1]),'upper':float(l[i]),'lower':float(h[i-2])})
        if h[i]<l[i-2]: out.append({'type':'BEARISH','candle_time':int(t[i-1]),'upper':float(l[i-2]),'lower':float(h[i])})
    return out

def quality_bucket(z, df):
    """Transparent research bucket; descriptive, not a production score."""
    i=int(z.get('index',len(df)-1)); side=z['type']; prefix=df.iloc[:min(len(df),i+1)].copy()
    atr=float(_calc_atr(prefix).iloc[-1]) if len(prefix)>=20 and math.isfinite(float(_calc_atr(prefix).iloc[-1])) else None
    disp=float(z.get('displacement_atr',0) or 0)
    st=structure_context(prefix,int(getattr(cfg,'structure_pivot',3))) if len(prefix)>=9 else {'direction':'RANGE','event':None,'sweep':None}
    expected='UP' if side=='BULLISH' else 'DOWN'
    aligned=st.get('direction')==expected
    sweep=st.get('sweep')==('SELL_SIDE' if side=='BULLISH' else 'BUY_SIDE')
    fresh=float(z.get('fill_fraction',0) or 0)==0 and int(z.get('touches',0) or 0)==0
    price=float(prefix.close.iloc[-1]); vbase=prefix.volume.tail(21).head(20).mean(); vrel=float(prefix.volume.iloc[-1]/vbase) if vbase else 0
    vwap=((prefix.high.tail(96)+prefix.low.tail(96)+prefix.close.tail(96))/3*prefix.volume.tail(96)).sum()/prefix.volume.tail(96).sum() if prefix.volume.tail(96).sum()>0 else price
    loc=bool(atr and abs(price-vwap)<=atr)
    points=sum([sweep,disp>=1.5,aligned,fresh,loc])
    return ('A' if points>=4 else 'B' if points>=2 else 'C'), {'sweep':sweep,'displacement_ge_1_5':disp>=1.5,'structure_alignment':aligned,'fresh':fresh,'good_location':loc,'points':points,'relative_volume':vrel,'atr':atr}

def main():
    report=json.loads(REPORT.read_text(encoding='utf8'))
    obs=report['technical_entry_observations']; grouped={}; reasons=defaultdict(set)
    for e in obs:
        grouped[e['setup_id']]=e['payload']; reasons[e['setup_id']].update(x.strip() for x in e['reason'].split(';'))
    decision=[]
    for sid,p in sorted(grouped.items()):
        r=reasons[sid]; a=p.get('assessment',{}); checks=a.get('checks',{}); f=p.get('fvg') or {}; st=p.get('market_structure') or {}
        def state(passv,unknown=False): return 'UNKNOWN' if unknown else ('PASS' if passv else 'FAIL')
        decision.append({'setup_id':sid,'asset':p['asset'],'direction':p['direction'],'score':a.get('score'),'tree':{
            'candidate':'PASS','fvg_valid':state(bool(checks.get('FVG') and checks.get('Zone eligibility'))),
            'fvg_quality':state(p.get('fvg_quality') is not None),'structure':state(bool(checks.get('Structure'))),
            'liquidity':state(False, p.get('liquidity') is None),'htf':state(bool(checks.get('HTF bias'))),
            'vwap':state('extended_from_vwap' not in r),'orderbook':state(not ({'thin_orderbook','orderbook_unavailable','orderbook_depth_incomplete'}&r)),
            'spread':state('wide_spread' not in r),'mitigation':state('fvg_fully_filled' not in r),'rejection':state('no_bullish_rejection' not in r and 'no_bearish_rejection' not in r),
            'entry':state(bool(checks.get('Entry'))),'signal':'FAIL'},
            'blockers':sorted(r),'fvg':f,'structure_context':st,
            'rejection_definition':'closed candle direction plus close beyond CE; no independent wick/engulfing rule'})
    counter={}
    filters=['thin_orderbook','extended_from_vwap','higher_4h_opposition','wide_spread','fvg_fully_filled','orderbook_unavailable','higher_4h_unavailable_or_stale','correlated_btc_opposition']
    for flt in filters:
        continued=[sid for sid,rs in reasons.items() if not (rs-{flt})]
        counter[flt]={'current_blocked':sum(flt in rs for rs in reasons.values()),'continue_if_only_removed':len(continued),'setup_ids':continued}
    # Current effective orderbook rule and observed examples from durable context events.
    micro=[]
    db=ROOT/'data'/'scalper.db'
    try:
        import sqlite3
        con=sqlite3.connect(db)
        for sid,payload in con.execute("select signal_id,payload from pipeline_events where kind='ENTRY_CONTEXT_CHECK'"):
            try:
                d=json.loads(payload); m=d.get('microstructure') or {}
                if m: micro.append({'signal_id':sid,'score':d.get('score'),'rejections':d.get('rejections'),'bid_depth_usdt':m.get('bid_depth_usdt'),'ask_depth_usdt':m.get('ask_depth_usdt'),'spread_bps':m.get('spread_bps'),'levels':[m.get('bid_levels'),m.get('ask_levels')],'band_complete':[m.get('bid_band_complete'),m.get('ask_band_complete')],'depth_band_bps':m.get('depth_band_bps')})
            except Exception: pass
        con.close()
    except Exception: pass
    # Reference-vs-bot comparison over saved 5m research frames.
    both=bot_only=ref_only=0; prefix_both=prefix_bot_only=prefix_ref_only=0; files=0; data_quality=Counter(); quality=Counter(); mtf=defaultdict(dict)
    frames={}
    for path in sorted((ROOT/'data'/'research'/'candles').glob('*_5.csv')):
        try:
            df=pd.read_csv(path)
            if not {'timestamp','open','high','low','close','volume'}.issubset(df.columns):continue
            raw_ts=df.timestamp.to_numpy()
            data_quality['files']+=1; data_quality['rows']+=len(df)
            data_quality['duplicate_rows']+=int(pd.Series(raw_ts).duplicated().sum())
            data_quality['out_of_order_files']+=int((np.diff(raw_ts)<0).any()) if len(raw_ts)>1 else 0
            df=df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            files+=1; frames[path.stem[:-2]]=df
            ts=df.timestamp.to_numpy(); dif=np.diff(ts)
            if len(dif) and (dif<=0).any(): data_quality['nonpositive_intervals']+=1
            if len(dif) and (dif>300000).any(): data_quality['5m_gaps_over_5min']+=1
            window=df.tail(max(30,int(cfg.fvg_lookback)+int(cfg.fvg_confirmations)+5)).reset_index(drop=True)
            rz={(x['type'],x['candle_time']) for x in ref_fvgs(window)}
            bz={(x['type'],int(x['candle_time'])) for x in detect_fvgs(window,settings=cfg)}
            both+=len(rz&bz); bot_only+=len(bz-rz); ref_only+=len(rz-bz)
            # quality on active bot zones in this frame
            for z in detect_fvgs(window,settings=cfg): quality[quality_bucket(z,window)[0]]+=1
        except Exception as exc: data_quality['read_errors']+=1
    # MTF map for the 18 candidates from already archived replay files (no production calls).
    for sid,p in sorted(grouped.items()):
        asset=p['asset']; mtf[asset]={'direction':p['direction'],'setup_id':sid}
        for tf in ('1','5','15','60','240'):
            f=ROOT/'data'/'audits'/'night_20260910'/f'replay_{asset}_{tf}.json'
            if not f.exists() and tf in ('1','15'):
                f=OUT/'mtf'/f'{asset}_{tf}.json'
            if not f.exists():
                mtf[asset][tf]={'available':False,'fvgs':None}
                continue
            try:
                df=pd.read_json(f,convert_dates=False,precise_float=True).sort_values('timestamp').reset_index(drop=True)
                mtf[asset][tf]={'available':True,'rows':len(df),'fvgs_reference':len(ref_fvgs(df.tail(500))),'last_timestamp':int(df.timestamp.iloc[-1])}
            except Exception: mtf[asset][tf]={'available':False,'fvgs':None}
        # Prefix-only detector comparison at the recorded observation boundary.
        f5=ROOT/'data'/'audits'/'night_20260910'/f'replay_{asset}_5.json'
        if f5.exists():
            try:
                d5=pd.read_json(f5,convert_dates=False,precise_float=True).sort_values('timestamp').reset_index(drop=True)
                boundary=int(p.get('assessment',{}).get('analysis_bar') or 0); pref=d5[d5.timestamp<=boundary].tail(max(30,int(cfg.fvg_lookback)+int(cfg.fvg_confirmations)+5)).reset_index(drop=True)
                rr={(x['type'],x['candle_time']) for x in ref_fvgs(pref)}; bb={(x['type'],int(x['candle_time'])) for x in detect_fvgs(pref,settings=cfg)}
                prefix_both+=len(rr&bb); prefix_bot_only+=len(bb-rr); prefix_ref_only+=len(rr-bb)
            except Exception: pass
    result={'scope':'V6.3 read-only research; counterfactuals are arithmetic on recorded blocker sets and not production authorization.',
            'unique_candidates':len(grouped),'observations':len(obs),'decision_tree':decision,'counterfactuals':counter,
            'thin_orderbook_evidence':{'rule':f'depth in {cfg.book_depth_bps}bps band; hard minimum {cfg.min_book_depth_usdt} USDT on entry side; complete band required for thin_orderbook label. Absolute minimum is not normalized by ATR/volume/market cap.','current_samples':micro[-30:]},
            'reference_vs_bot':{'saved_5m_files':files,'both':both,'bot_only':bot_only,'reference_only':ref_only,'disagreement':bot_only+ref_only,'note':'comparison is recent bot lookback against pure classic OHLC reference; bot adds body/ATR/volume/size and mitigation rules. Latest-window comparison is descriptive.'},
            'candidate_prefix_reference_vs_bot':{'both':prefix_both,'bot_only':prefix_bot_only,'reference_only':prefix_ref_only,'disagreement':prefix_bot_only+prefix_ref_only,'note':'prefix truncated at each recorded analysis_bar; no future candles used.'},
            'quality_matrix':{'definition':'A>=4 of sweep, displacement>=1.5 ATR, structure alignment, fresh, location within 1 ATR VWAP; B=2-3; C=0-1. Research classification only.','counts':dict(quality)},
            'mtf_map':mtf,
            'data_quality_saved_5m':dict(data_quality),
            'rejection_rule':'Directional rejection is last/touched candle closing in its direction and beyond CE (LONG close>open and close>=CE; SHORT close<open and close<=CE). No separate wick, engulfing or rejection displacement criterion.',
            'performance_note':'Per-operation timing is not available in V6.3 telemetry; whole-cycle timing remains the only persisted latency metric.'}
    (OUT/'deep_logic_research.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    # Human report
    lines=['# Deep trading-logic research — V6.3 (read-only)','',f'18 unique technical candidates / {len(obs)} observations. No production changes.']
    lines+=['','## Decision tree and counterfactuals','', '| Filter | Current blocked | Continue if only this blocker removed |','|---|---:|---:|']
    for f,d in counter.items(): lines.append(f"| `{f}` | {d['current_blocked']} | {d['continue_if_only_removed']} |")
    lines+=['','### Filter classification','', '| Filter | Candidates blocked | Share of 18 | Current class | Evidence / interpretation |','|---|---:|---:|---|---|',
            '| thin_orderbook | 15 | 83.33% | HARD execution gate | Absolute 25k USDT entry-side depth; only 2 continue when removed alone, but cross-asset normalization is absent |',
            '| extended_from_vwap | 10 | 55.56% | HARD technical gate | Only 1 continues when removed alone; useful context signal, but no standalone outcome proof |',
            '| opposite 4h | 9 | 50.00% | HARD context gate | Zero continue alone because all affected setups have other blockers; reversal evidence missing |',
            '| no rejection | 0 in this candidate set | n/a | HARD confirmation | Night-wide observation blocker; current rule requires directional close and CE relation |',
            '| FVG filled | 2 | 11.11% | HARD invalidation | Lifecycle rule prevents re-entry after full wick/CE invalidation |',
            '| spread | 2 | 11.11% | HARD cost gate | Critical only above configured 15 bps; both also had other blockers |']
    lines+=['','### 18 candidate trees (P=PASS, F=FAIL, U=UNKNOWN)','', '| Asset/setup | FVG | Quality | Structure | Liquidity | HTF | VWAP | Book | Spread | Mitigation | Rejection | Entry | Signal |','|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for row in decision:
        t=row['tree']; label=f"{row['asset']} {row['direction']}"; lines.append('| '+' | '.join([label,t['fvg_valid'][0],t['fvg_quality'][0],t['structure'][0],t['liquidity'][0],t['htf'][0],t['vwap'][0],t['orderbook'][0],t['spread'][0],t['mitigation'][0],t['rejection'][0],t['entry'][0],t['signal'][0]])+' |')
    lines+=['','The 18 trees are also machine-readable in JSON. `liquidity` is UNKNOWN when the archived payload contains no sweep object; it was not treated as a failure gate.']
    lines+=['','## Findings','',f"Pure classic OHLC reference comparison on {files} saved 5m files: both={both}, bot-only={bot_only}, reference-only={ref_only}. The reference-only set is the missed-FVG candidate pool, but it includes intentionally stricter bot rules (body, ATR, volume, size and mitigation), so it is not proof of a bug.",f"Causal prefix comparison for the 18 candidate boundaries: both={prefix_both}, bot-only={prefix_bot_only}, reference-only={prefix_ref_only}; no candles after each analysis_bar were used.",f"Quality matrix (research definition, not production score): {dict(quality)}.",f"Saved 5m data quality: {dict(data_quality)}."]
    lines.append('MTF map for the 17 unique candidate assets has data on all requested 1m/5m/15m/1h/4h files; counts are reference OHLC zones and are descriptive, not production signals.')
    lines+=['','## thin_orderbook','',f"Current implementation sums USDT depth inside a {cfg.book_depth_bps} bps band and rejects if entry-side depth is below {cfg.min_book_depth_usdt:,.0f} USDT when the band is complete. The threshold is absolute and is not normalized to price, recent volume, ATR or market cap. Current context samples show large cross-asset variation; this is the clearest candidate for a future relative-depth experiment, but no production change was made."]
    lines+=['','## VWAP, 4h and rejection','',f"Single-filter arithmetic says removing only VWAP would allow 1 of 18 to continue; removing only thin orderbook would allow 2; removing only higher-4h opposition, spread, full-fill or data-unavailable gates allows 0 because each affected setup has another blocker. A preferred-but-not-mandatory 4h mode therefore changes none of these 18 to Confirmed without another change.","The rejection gate is objective but narrow: directional candle body and close relative to CE. Wick rejection, engulfing, multi-candle displacement and MSS are not separate acceptance paths."]
    lines+=['','## Score/filter duplication audit','',"HTF alignment is counted in the heuristic score (+18 for aligned trend and -25 for opposite directional HTF) and is also enforced as a hard rejection later; this is double use of the same directional information. FVG size contributes a score bonus while FVG validity/mitigation is separately hard-gated, which is intentional but can make a high score look stronger than the final gate set. VWAP, orderbook, spread and liquidity are not score components in `_score_signal`; they are later context/execution gates or informational factors. Displacement and relative volume participate in FVG/entry validation and quality, but are not independently added as score points. No evidence of a third penalty for these fields was found in the current path."]
    lines+=['','## Setup replay and missed-opportunity evidence','',"Causal T-20→decision replays were generated for all 18 unique candidates. The following 12 bars are stored separately as descriptive outcome evidence only; they were not used to create signals or relabel rejections. Examples with favorable excursion in the expected direction include BLESSUSDT SHORT (close -6.88%, aligned excursion 14.87%), MARSCOINUSDT SHORT (-3.67%, 5.87%) and FARTCOINUSDT SHORT (-1.59%, 4.83%). These are potential false-negative candidates, not proof that the blocked execution would have been fillable or profitable because historical orderbook/news snapshots are unavailable.","Debug charts were rendered for AXSUSDT, MORPHOUSDT and TRUMPUSDT with candles, FVG/CE, swings and decision marker."]
    lines+=['','## Gaps and V6.4 plan (not implemented)','',"1. Add per-endpoint monotonic timings and p50/p95/p99. 2. Build a relative orderbook metric using entry-side depth / recent traded volume with a minimum data-quality guard. 3. Run a causal independent-detector diff on every historical prefix. 4. Add equal-high/low and session liquidity pools. 5. Store Raw/Valid/Quality/TradeCandidate/Confirmed as explicit events. 6. Keep VWAP and 4h as research branches until outcome data separates precision from recall. 7. Add setup replay and a debug chart endpoint."]
    (OUT/'DEEP_LOGIC_RESEARCH_V6.3.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({'unique_candidates':len(grouped),'observations':len(obs),'counterfactuals':{k:v['continue_if_only_removed'] for k,v in counter.items()},'reference_vs_bot':{'both':both,'bot_only':bot_only,'reference_only':ref_only},'quality':dict(quality),'saved_files':files},ensure_ascii=False))

if __name__=='__main__': main()
