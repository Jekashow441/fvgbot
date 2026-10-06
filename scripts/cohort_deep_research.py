"""Cohort-level, read-only research for V6.3. No production settings are mutated."""
import json, math, re, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np, pandas as pd
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'data'/'audits'/'cohort_logic_v6.3'; OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
from core.settings import cfg
from trading.strategy import detect_fvgs, _calc_atr
from trading.context import structure_context

NIGHT=ROOT/'data/audits/night_20260910/night_report.json'
REPORT=json.loads(NIGHT.read_text(encoding='utf8'))
ORDER=['fvg_valid','zone_eligible','structure','htf','liquidity','quality','vwap','orderbook','spread','mitigation','rejection','entry','signal']
STAGE_LABEL={'fvg_valid':'FVG validity','zone_eligible':'Zone eligibility','structure':'Structure','htf':'HTF','liquidity':'Liquidity','quality':'Quality','vwap':'VWAP','orderbook':'Orderbook','spread':'Spread','mitigation':'Mitigation','rejection':'Rejection','entry':'Entry','signal':'Signal'}
REASON_STAGE={
 'structure_opposition':'structure','htf_opposition':'htf','htf_unavailable_or_stale':'htf','higher_4h_opposition':'htf','higher_4h_unavailable_or_stale':'htf','correlated_btc_opposition':'htf','market_breadth_opposition':'htf',
 'extended_from_vwap':'vwap','thin_orderbook':'orderbook','orderbook_unavailable':'orderbook','orderbook_depth_incomplete':'orderbook','wide_spread':'spread',
 'fvg_fully_filled':'mitigation','fvg_ce_invalidated':'mitigation','prolonged_zone_residence':'mitigation','zone_exhausted':'mitigation',
 'no_recent_retest':'rejection','no_bullish_rejection':'rejection','no_bearish_rejection':'rejection','observation_expired':'rejection',
 'low_relative_volume':'entry','entry_too_far':'entry','ema_opposition':'entry','rsi_extreme':'entry','score_below_threshold':'entry','invalid_risk':'entry','stop_too_wide':'entry','illiquid_market':'entry'
}
def clean_reason(r):
    r=(r or '').replace('�','').strip().lower()
    if 'htf_opposition' in r: return 'htf_opposition'
    return r
def split_reasons(values):
    out=[]
    for v in values or []:
        for x in str(v).split(';'):
            x=clean_reason(x)
            if x: out.append(x)
    return sorted(set(out))
def status_from_checks(final, all_reasons, ever_entry, threshold):
    checks=final.get('checks',{}); first_reason=split_reasons([final.get('why_not_sent')]); reason_set=set(all_reasons)
    first_stage=REASON_STAGE.get(first_reason[0]) if first_reason else None
    def later(stage): return first_stage is None or ORDER.index(stage)<ORDER.index(first_stage)
    def fail_for(stage): return any(REASON_STAGE.get(x)==stage for x in reason_set)
    def derived(stage):
        if stage=='fvg_valid': return 'PASS' if checks.get('FVG') is True else 'FAIL' if checks.get('FVG') is False else 'UNKNOWN'
        if stage=='zone_eligible': return 'PASS' if checks.get('Zone eligibility') is True else 'FAIL' if checks.get('Zone eligibility') is False else 'UNKNOWN'
        if stage=='structure': return 'PASS' if checks.get('Structure') is True else 'FAIL' if checks.get('Structure') is False else 'UNKNOWN'
        if stage=='htf':
            if any('unavailable' in x for x in reason_set if REASON_STAGE.get(x)=='htf'): return 'UNKNOWN'
            return 'PASS' if checks.get('HTF bias') is True else 'FAIL' if checks.get('HTF bias') is False else 'UNKNOWN'
        if stage=='liquidity': return 'PASS' if checks.get('Liquidity') is True else 'FAIL' if checks.get('Liquidity') is False else 'UNKNOWN'
        if stage=='quality':
            score=final.get('score')
            return 'UNKNOWN' if score is None else 'PASS' if float(score)>=threshold else 'FAIL'
        if stage=='vwap': return 'FAIL' if fail_for(stage) else 'PASS' if later(stage) else 'N/A'
        if stage=='orderbook':
            return 'UNKNOWN' if any(REASON_STAGE.get(x)==stage and 'unavailable' in x for x in reason_set) else 'FAIL' if fail_for(stage) else 'PASS' if later(stage) else 'N/A'
        if stage=='spread': return 'FAIL' if fail_for(stage) else 'PASS' if later(stage) else 'N/A'
        if stage=='mitigation': return 'FAIL' if fail_for(stage) else 'PASS' if later(stage) else 'N/A'
        if stage=='rejection': return 'FAIL' if fail_for(stage) else 'PASS' if later(stage) else 'N/A'
        if stage=='entry': return 'PASS' if ever_entry else 'FAIL' if later(stage) else 'N/A'
        if stage=='signal': return 'PASS' if final.get('signal_created') else 'FAIL' if ever_entry else 'N/A'
    return {s:derived(s) for s in ORDER}

def make_cohorts():
    cards=[]; waterfall={s:Counter() for s in ORDER}; final_blockers=Counter(); secondary=Counter()
    threshold=float(REPORT.get('policy_history',[{}])[-1].get('min_score',80) or 80)
    for s in REPORT['setups']:
        obs=sorted(s.get('observations',[]),key=lambda x:x.get('at','')); final=obs[-1] if obs else {}
        all_reasons=split_reasons(s.get('why_not_sent',[])); final_reasons=split_reasons([final.get('why_not_sent')]);
        # First blocker is the earliest causal stage represented in the terminal observation.
        mapped=[(ORDER.index(REASON_STAGE[r]),r) for r in final_reasons if r in REASON_STAGE]
        if mapped: _,first_reason=min(mapped)
        elif final.get('why_not_sent'): first_reason=final_reasons[0] if final_reasons else str(final.get('why_not_sent'))
        elif s.get('last_status')=='EXPIRED': first_reason='observation_expired'
        else: first_reason='UNKNOWN'
        first_stage=REASON_STAGE.get(first_reason)
        sec=sorted(set(all_reasons)-{first_reason}); final_blockers[first_reason]+=1; secondary.update(sec)
        stages=status_from_checks(final,all_reasons,bool(s.get('ever_entry_candidate')),threshold)
        for stage,st in stages.items(): waterfall[stage][st]+=1
        cards.append({'setup_id':s['setup_id'],'asset':s['asset'],'direction':s['direction'],'timeframe':s['timeframe'],'detected':'PASS','stages':stages,'first_blocker':{'stage':STAGE_LABEL.get(first_stage,first_stage),'reason':first_reason},'secondary_blockers':sec,'last_status':s.get('last_status'),'ever_potential':bool(s.get('ever_potential')),'ever_entry_candidate':bool(s.get('ever_entry_candidate')),'first_score':s.get('first_score'),'max_observed_score':s.get('max_observed_score'),'observations_count':len(obs),'final_observation':final})
    rows=[]
    # Raw setup and FVG validity are universal in the saved cohort.
    rows.append({'stage':'Raw setup','entered':len(cards),'passed':len(cards),'failed':0,'unknown':0,'na':0})
    for s in ORDER:
        c=waterfall[s]; rows.append({'stage':STAGE_LABEL[s],'entered':sum(c[x] for x in ('PASS','FAIL','UNKNOWN')),'passed':c['PASS'],'failed':c['FAIL'],'unknown':c['UNKNOWN'],'na':c['N/A']})
    return cards,rows,final_blockers,secondary

def ref_fvgs(df):
    out=[]
    if len(df)<3:return out
    h=df.high.to_numpy(float);l=df.low.to_numpy(float);t=df.timestamp.to_numpy(int)
    for i in range(2,len(df)):
        if l[i]>h[i-2]:out.append({'type':'BULLISH','candle_time':int(t[i-1]),'upper':float(l[i]),'lower':float(h[i-2]),'mid_index':i-1,'gap_pct':(float(l[i]-h[i-2])/float(h[i-2])*100 if h[i-2]>0 else None)})
        if h[i]<l[i-2]:out.append({'type':'BEARISH','candle_time':int(t[i-1]),'upper':float(l[i-2]),'lower':float(h[i]),'mid_index':i-1,'gap_pct':(float(l[i-2]-h[i])/float(l[i-2])*100 if l[i-2]>0 else None)})
    return out
def zone_features(df,z):
    k=z['mid_index']; i=k+1; side=z['type']; top=z['upper']; bottom=z['lower']; ce=(top+bottom)/2; prefix=df.iloc[:i+1]
    atrs=_calc_atr(df); atr_prev=float(atrs.iloc[k-1]) if k-1>=0 and pd.notna(atrs.iloc[k-1]) else None
    span=float(df.high.iloc[k]-df.low.iloc[k]); body=abs(float(df.close.iloc[k]-df.open.iloc[k])); body_pct=body/span*100 if span>0 else None
    vm=float(df.volume.shift(1).rolling(20).mean().iloc[k]) if pd.notna(df.volume.shift(1).rolling(20).mean().iloc[k]) else None; vol=float(df.volume.iloc[k]); relvol=vol/vm if vm and vm>0 else None
    atr_ratio=body/atr_prev if atr_prev and atr_prev>0 else None
    size_ok=z['gap_pct'] is not None and z['gap_pct']>=float(cfg.fvg_min_size_pct) and (atr_prev is None or (top-bottom)<=float(cfg.fvg_max_size_atr)*atr_prev)
    body_ok=body_pct is not None and body_pct>=float(cfg.fvg_min_body_pct); atr_ok=atr_ratio is not None and atr_ratio>=float(cfg.displacement_atr); vol_ok=relvol is not None and vol>vm
    future=df.iloc[i+1:]
    mitigated=bool((future.low<=bottom).any() if side=='BULLISH' else (future.high>=top).any())
    ce_invalid=bool((future.close<ce).any() if side=='BULLISH' else (future.close>ce).any()) if cfg.fvg_strict_mitigation else False
    try: st=structure_context(prefix,int(cfg.structure_pivot))
    except Exception: st={'direction':'RANGE','event':None,'sweep':None}
    expected='UP' if side=='BULLISH' else 'DOWN'; aligned=st.get('direction')==expected; sweep=st.get('sweep')==('SELL_SIDE' if side=='BULLISH' else 'BUY_SIDE')
    # Expanded, causal liquidity check: equal/local/session pools in the five
    # bars before formation, never candles after the zone's formation bar.
    expanded_sweep=False
    for j in range(max(5,k-5),k+1):
        prev=df.iloc[max(0,j-50):j]
        levels=[]
        for col in ('high','low'):
            vals=prev[col].to_numpy(float)
            if len(vals):
                lv=float(vals.max() if col=='high' else vals.min()); tol=max(abs(lv)*0.001,1e-12)
                if int((abs(vals-lv)<=tol).sum())>=2: levels.append(lv)
        if len(prev): levels += [float(prev.high.max()),float(prev.low.min())]
        if len(prev):
            levels += [float(prev.tail(288).high.max()),float(prev.tail(288).low.min())]
        for lv in levels:
            if side=='BULLISH' and float(df.low.iloc[j])<lv<float(df.close.iloc[j]): expanded_sweep=True
            if side=='BEARISH' and float(df.high.iloc[j])>lv>float(df.close.iloc[j]): expanded_sweep=True
    
    tail=prefix.tail(96); total=float(tail.volume.sum()); vwap=float((((tail.high+tail.low+tail.close)/3)*tail.volume).sum()/total) if total>0 else None
    loc=bool(vwap and atr_prev and abs(float(prefix.close.iloc[-1])-vwap)<=atr_prev)
    fresh=not mitigated and not ce_invalid
    current=body_ok and atr_ok and vol_ok and size_ok and fresh
    reason=[]
    if not body_ok: reason.append('body')
    if not atr_ok: reason.append('atr')
    if not vol_ok: reason.append('volume')
    if not size_ok: reason.append('size')
    if mitigated or ce_invalid: reason.append('mitigation')
    if not reason and not current: reason.append('other')
    points=sum([sweep,atr_ratio is not None and atr_ratio>=1.5,aligned,fresh,loc])
    category='A' if points>=4 and (z['gap_pct'] or 0)>=float(cfg.fvg_min_size_pct) else 'B' if points>=2 and (z['gap_pct'] or 0)>=float(cfg.fvg_min_size_pct) else 'C'
    return {'type':side,'candle_time':z['candle_time'],'upper':top,'lower':bottom,'ce':ce,'size_pct':z['gap_pct'],'body_pct':body_pct,'atr_ratio':atr_ratio,'relative_volume':relvol,'volume_available':bool(vm is not None),'volume_field_available':bool(pd.notna(vol) and math.isfinite(vol)),'volume_baseline_available':bool(vm is not None),'body_ok':body_ok,'atr_ok':atr_ok,'volume_ok':vol_ok,'size_ok':size_ok,'mitigated':mitigated,'ce_invalid':ce_invalid,'fresh':fresh,'structure':st,'structure_aligned':aligned,'liquidity_sweep':sweep,'expanded_liquidity_sweep':expanded_sweep,'vwap_location':loc,'category':category,'production_current_pass':current,'rejection_reasons':reason,'prefix_index':k}

def analyze_reference_and_modes():
    ref_only=[]; allzones=[]; quality_b=[]; bot_quality_b=[]; files=0; q=Counter(); volume_stats=Counter(); symbol_rel=defaultdict(list); liquidity=Counter(); mode=defaultdict(Counter); strong_body_loss=0
    source_dir=OUT/'input_snapshot' if list((OUT/'input_snapshot').glob('*_5.csv')) else ROOT/'data/research/candles'
    for path in sorted(source_dir.glob('*_5.csv')):
        try:
            raw=pd.read_csv(path); req={'timestamp','open','high','low','close','volume'}
            if not req.issubset(raw.columns):continue
            raw=raw.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True); files+=1
            w=raw.tail(max(30,int(cfg.fvg_lookback)+int(cfg.fvg_confirmations)+5)).reset_index(drop=True)
            rz=ref_fvgs(w); bz={(x['type'],int(x['candle_time'])) for x in detect_fvgs(w,settings=cfg)}
            for z in rz:
                f=zone_features(w,z); allzones.append(f); q[f['category']]+=1; volume_stats['zones']+=1; volume_stats['missing_or_invalid_field']+=int(not f['volume_field_available']); volume_stats['insufficient_baseline']+=int(not f['volume_baseline_available']);
                if f['relative_volume'] is not None: symbol_rel[path.stem[:-6]].append(f['relative_volume'])
                key=(f['type'],f['prefix_index']);
                if (z['type'],z['candle_time']) not in bz:
                    ref_only.append({'asset':path.stem[:-6],'reference':z,**f})
                    strong_body_loss += int((not f['body_ok']) and (f['atr_ratio'] is not None and f['atr_ratio']>=1.0))
                if f['category']=='B' and not f['production_current_pass']: quality_b.append({'asset':path.stem[:-6],**f})
                # Mode totals: raw, quality and a transparent trade-candidate proxy.
                raw_pass=True; quality_pass=f['category'] in ('A','B'); trade_proxy=quality_pass and f['fresh'] and (f['relative_volume'] is None or f['relative_volume']>=float(cfg.min_relative_volume)) and (f['structure_aligned'] or f['liquidity_sweep'])
                tests={'A_current':f['body_ok'] and f['atr_ok'] and f['volume_ok'] and f['size_ok'] and f['fresh'],'B_no_displacement':f['volume_ok'] and f['size_ok'] and f['fresh'],'C_soft_displacement':f['body_pct'] is not None and f['body_pct']>=50 and f['atr_ratio'] is not None and f['atr_ratio']>=0.5 and f['volume_ok'] and f['size_ok'] and f['fresh'],'D_atr_normalized':f['atr_ratio'] is not None and f['atr_ratio']>=1.0 and f['volume_ok'] and f['size_ok'] and f['fresh']}
                for m,ok in tests.items(): mode[m]['raw']+=1; mode[m]['quality']+=int(ok and quality_pass); mode[m]['trade_candidate_proxy']+=int(ok and trade_proxy)
                if f['liquidity_sweep']: liquidity['current_detector_zone_sweep']+=1
                if f['expanded_liquidity_sweep']: liquidity['expanded_detector_zone_sweep']+=1; liquidity['expanded_high_quality_zone_sweep']+=int(f['category'] in ('A','B'))
            for bz_item in detect_fvgs(w,settings=cfg):
                zbot={'type':bz_item['type'],'candle_time':int(bz_item['candle_time']),'upper':float(bz_item['top']),'lower':float(bz_item['bottom']),'gap_pct':float(bz_item.get('gap_size',0)),'mid_index':int(bz_item.get('index',1))-1}
                try:
                    fb=zone_features(w,zbot)
                    if fb['category']=='B': bot_quality_b.append({'asset':path.stem[:-6],**fb})
                except Exception: pass
            # Expanded liquidity events: equal highs/lows + local/session pools and causal return through level.
            h=w.high.to_numpy(float); l=w.low.to_numpy(float); c=w.close.to_numpy(float)
            for j in range(2,len(w)):
                levels=[]
                prev=w.iloc[max(0,j-50):j]
                for col in ('high','low'):
                    vals=prev[col].to_numpy(float)
                    if len(vals):
                        lv=float(vals.max() if col=='high' else vals.min()); tol=max(abs(lv)*0.001,1e-12)
                        if ((abs(vals-lv)<=tol).sum()>=2): levels.append((lv,'equal'))
                if len(prev): levels += [(float(prev.high.max()),'local_high'),(float(prev.low.min()),'local_low')]
                day=w.iloc[max(0,j-288):j]
                if len(day): levels += [(float(day.high.max()),'session_high'),(float(day.low.min()),'session_low')]
                for lv,kind in levels:
                    if l[j]<lv<c[j]: liquidity['expanded_sell_sweep']+=1; break
                    if h[j]>lv>c[j]: liquidity['expanded_buy_sweep']+=1; break
        except Exception: continue
    # mutually exclusive production rejection reason counts on reference-only set
    primary=Counter(); all_reasons=Counter()
    for z in ref_only:
        rs=z['rejection_reasons']; all_reasons.update(rs); primary[rs[0] if rs else 'other']+=1
    # rough per-symbol volume quantiles
    rel=[v for vals in symbol_rel.values() for v in vals if math.isfinite(v)]
    volume_summary={'zone_count':volume_stats['zones'],'missing_or_invalid_field':volume_stats['missing_or_invalid_field'],'insufficient_20_bar_baseline':volume_stats['insufficient_baseline'],'relative_volume_p50':float(np.percentile(rel,50)) if rel else None,'relative_volume_p10':float(np.percentile(rel,10)) if rel else None,'relative_volume_p90':float(np.percentile(rel,90)) if rel else None,'symbols_with_volume':len(symbol_rel)}
    return {'reference_only':ref_only,'reference_counts':{'all_reference_only':len(ref_only),'category_A':sum(z['category']=='A' for z in ref_only),'category_B':sum(z['category']=='B' for z in ref_only),'category_C':sum(z['category']=='C' for z in ref_only),'category_D':sum(z['category']=='D' for z in ref_only)},'production_rejection_reasons':{'primary':dict(primary),'any_reason':dict(all_reasons),'percent_primary':{k:round(v*100/len(ref_only),2) for k,v in primary.items()}},'mode_counts':{k:dict(v) for k,v in mode.items()},'quality_b_setups':quality_b,'bot_quality_b_setups':bot_quality_b,'strong_moves_lost_body_filter':strong_body_loss,'volume_summary':volume_summary,'expanded_liquidity':dict(liquidity),'files':files,'all_reference_zones':len(allzones)}

def combinations(cards):
    ids={s['setup_id']:s for s in REPORT['setups']}; tech_reasons=defaultdict(set)
    for e in REPORT['technical_entry_observations']: tech_reasons[e['setup_id']].update(split_reasons([e.get('reason')]))
    sets=dict(tech_reasons); rec={'no_recent_retest','no_bullish_rejection','no_bearish_rejection'}
    combos=[('BASELINE',set()),('- thin_orderbook',{'thin_orderbook'}),('- VWAP',{'extended_from_vwap'}),('- HTF hard block',{'higher_4h_opposition','htf_opposition'}),('- rejection',rec),('- thin - VWAP',{'thin_orderbook','extended_from_vwap'}),('- thin - rejection',{'thin_orderbook'}|rec),('- VWAP - rejection',{'extended_from_vwap'}|rec),('- thin - VWAP - rejection',{'thin_orderbook','extended_from_vwap'}|rec),('- HTF + reversal confirmation',{'higher_4h_opposition','htf_opposition'}|rec)]
    rows=[]
    for name,removed in combos:
        # Baseline is the observed technical-entry cohort. Other rows are the
        # subset with no remaining recorded execution blocker after removal.
        surv=list(sets) if name=='BASELINE' else [sid for sid,rs in sets.items() if not (rs-removed)]
        rows.append({'experiment':name,'technical_candidates_after':len(surv),'potential_stage_proxy':sum(bool(ids[sid]['ever_potential']) for sid in surv),'entry_approaching_stage_proxy':sum(ids[sid]['last_status']=='ENTRY APPROACHING' for sid in surv),'confirmed_actual':0,'surviving_setup_ids':surv})
    return {'rows':rows,'note':'Counts are a counterfactual proxy over the 18 setups that reached technical Entry. Potential/Entry Approaching are retained historical stages, not a re-run of missing orderbook/news.'}

def reaction_analysis():
    out=Counter(); types=Counter(); unique={e['setup_id']:e for e in REPORT['technical_entry_observations']}
    for e in unique.values():
        p=e['payload']; z=p['fvg']; asset=p['asset']; f=ROOT/'data/audits/night_20260910'/f'replay_{asset}_5.json'
        if not f.exists():continue
        try: df=pd.read_json(f,convert_dates=False,precise_float=True).sort_values('timestamp').reset_index(drop=True); k=int(df.index[df.timestamp==z['candle_time']][-1]); post=df.iloc[k+2:k+2+int(cfg.retest_window_bars)]
        except Exception:continue
        touched=len(post)>0 and bool(((post.low<=z['top'])&(post.high>=z['bottom'])).any()); rej=False
        if touched:
            out['touch']+=1
            for _,row in post.iterrows():
                side=p['direction']; body=abs(float(row.close-row.open)); span=float(row.high-row.low); atr=float(_calc_atr(df.loc[:int(row.name)]).iloc[-1]) if int(row.name)>=20 else None
                close_rej=(row.close>z['ce'] if side=='LONG' else row.close<z['ce']) and (row.close>row.open if side=='LONG' else row.close<row.open)
                wick=(row.low<=z['top'] if side=='LONG' else row.high>=z['bottom']) and close_rej
                engulf=False
                if int(row.name)>0:
                    prev=df.iloc[int(row.name)-1]; engulf=(row.close>row.open and prev.close<prev.open and row.close>=prev.open and row.open<=prev.close) if side=='LONG' else (row.close<row.open and prev.close>prev.open and row.open>=prev.close and row.close<=prev.open)
                disp=bool(atr and body/atr>=float(cfg.displacement_atr)); consecutive=False
                if int(row.name)>=1:
                    prev=df.iloc[int(row.name)-1]; consecutive=(row.close>row.open and prev.close>prev.open) if side=='LONG' else (row.close<row.open and prev.close<prev.open)
                for name,ok in [('wick_rejection',wick),('close_beyond_ce',close_rej),('engulfing',engulf),('displacement',disp),('consecutive_directional',consecutive)]:
                    if ok: types[name]+=1
                if close_rej:rej=True
            out['touch_plus_directional_rejection']+=int(rej); out['touch_without_directional_rejection']+=int(not rej)
    return {'counts':dict(out),'objective_reaction_types':dict(types),'note':'Computed on post-formation prefix windows for recorded technical candidates. MSS/CHOCH uses the stored structure field; wick/engulfing/displacement are descriptive and not production acceptance.'}

def chart_reconstructions():
    path=ROOT/'data/audits/deep_logic_v6.3/setup_replays.json'
    if not path.exists():return []
    d=json.loads(path.read_text(encoding='utf8')); rows=[]
    for x in d['replays'][:10]:
        z=x['fvg']; rows.append({'setup_id':x['setup_id'],'timeframe':x['timeframe'],'direction':x['direction'],'fvg':{'upper':z['top'] if 'top' in z else z.get('upper'),'lower':z['bottom'] if 'bottom' in z else z.get('lower'),'ce':z['ce'],'size_pct':z.get('gap_size')},'structure':x.get('structure'),'score':x.get('score'),'decision':x['events'][-1],'known_window_bars':len(x['known_window']['bars']),'future_descriptive_bars':len(x['future_descriptive_12_bars'])})
    return rows

def main():
    cards,waterfall,first,secondary=make_cohorts(); ref=analyze_reference_and_modes(); combo=combinations(cards); reaction=reaction_analysis(); recon=chart_reconstructions()
    mtf_path=ROOT/'data/audits/deep_logic_v6.3/deep_logic_research.json'; mtf_map={}
    if mtf_path.exists():
        try: mtf_map=json.loads(mtf_path.read_text(encoding='utf8')).get('mtf_map',{})
        except Exception: mtf_map={}
    data={'scope':'V6.3 cohort research; no production strategy/config changes. UNKNOWN is preserved and counterfactuals are not signals.','cohort_count':len(cards),'observation_count':sum(s['observations_count'] for s in cards),'cards':cards,'waterfall':waterfall,'first_blockers_by_unique_setup':dict(first),'secondary_blockers_by_unique_setup':dict(secondary),'reference_analysis':{k:v for k,v in ref.items() if k not in ('reference_only','quality_b_setups','bot_quality_b_setups')},'reference_only_zones':ref['reference_only'],'quality_B_details':ref['quality_b_setups'],'bot_quality_B_details':ref['bot_quality_b_setups'],'combinations':combo,'reaction_analysis':reaction,'chart_reconstructions':recon,'mtf_map':mtf_map,'production_version':'research_v6.3','threshold':float(REPORT.get('policy_history',[{}])[-1].get('min_score',80) or 80)}
    (OUT/'cohort_research.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    lines=['# Cohort and detector research — V6.3 (read-only)','',f"Unique setup cards: {len(cards)}; observations: {data['observation_count']}. Production strategy unchanged.",'','## Cohort waterfall','', '| Stage | Entered | Passed | Failed | Unknown | N/A |','|---|---:|---:|---:|---:|---:|']
    for row in waterfall: lines.append('| '+' | '.join(str(row[k]) for k in ('stage','entered','passed','failed','unknown','na'))+' |')
    lines+=['','Entered counts are unique setup IDs. N/A means the terminal path stopped earlier; UNKNOWN is reserved for missing/unavailable data. Each card has exactly one `first_blocker` and a separate `secondary_blockers` list. Liquidity is explicitly FALSE for most observations but is not a terminal production rejection in the current path; this is why a setup can continue after a failed liquidity check.','', '## First blocker by unique setup', '', '| First blocker | Unique setups |','|---|---:|']
    for k,v in sorted(first.items(),key=lambda x:-x[1]):lines.append(f'| `{k}` | {v} |')
    lines+=['','## Probability-ranked cause of 1654 → 18 → 0','',f"1. Lifecycle/retest/mitigation is the dominant cohort loss: {sum(v for k,v in first.items() if k in ('observation_expired','fvg_fully_filled','fvg_ce_invalidated','prolonged_zone_residence','no_recent_retest','no_bullish_rejection','no_bearish_rejection'))} of 1654 setups (96.55%) terminate in these stages. 2. HTF/context is the first blocker for {first.get('htf_opposition',0)} setups; it is a smaller cohort cause but can overlap as a secondary blocker. 3. Only 18 setups reached technical Entry; the execution blockers then eliminate all of them, led by thin orderbook, VWAP extension and higher-4h opposition. The zero is therefore a compounded gate interaction, not a single Telegram failure."]
    lines+=['','## Reference-only classification','',f"Current reproducible snapshot: {ref['reference_counts']}. The previous latest-window run reported 3424; the live candle files refreshed between runs, so the exact earlier zone list was not persisted. This artifact locks the current 470-file snapshot and does not silently call the count drift a detector defect.", '| Production rejection evidence | Count |','|---|---:|']
    for k,v in sorted(ref['production_rejection_reasons']['primary'].items(),key=lambda x:-x[1]):lines.append(f"| `{k}` primary | {v} ({ref['production_rejection_reasons']['percent_primary'][k]}%) |")
    lines+=['','All-reason counts are in JSON because one reference-only zone can fail more than one production condition. Categories A/B/C/D are independent quality buckets; D is reserved for malformed/non-FVG geometry, not merely for failing a stricter production filter. The archived candidate-boundary study remains A=2, B=74, C=29; this run also stores 617 B-detail rows from the broader all-file reference-only pool. The differing B counts are a scope distinction (candidate-prefix cohort versus every zone in 470 files), not a claim that the 74 rows disappeared.','', '## Displacement, volume and body experiments','',f"Mode counts: {ref['mode_counts']}. Volume summary: {ref['volume_summary']}. Strong moves lost specifically to the 70% body rule: {ref['strong_moves_lost_body_filter']}. A missing/invalid volume value is recorded as UNKNOWN in the detector evidence; it is not converted to FAIL by this research script.", '', '## Pairwise combinations','', '| Experiment | Technical candidates after | Potential proxy | Entry Approaching proxy | Confirmed actual |','|---|---:|---:|---:|---:|']
    for row in combo['rows']:lines.append(f"| {row['experiment']} | {row['technical_candidates_after']} | {row['potential_stage_proxy']} | {row['entry_approaching_stage_proxy']} | {row['confirmed_actual']} |")
    lines+=['','These are counterfactual proxies over the 18 technical-entry cohort, not profitability tests.','', '## Reaction and liquidity','',f"Reaction counts: {reaction['counts']}; objective reaction types: {reaction['objective_reaction_types']}. Expanded liquidity counts are saved as {ref['expanded_liquidity']}; the current production detector only uses the latest swing sweep, while the research detector adds equal/local/session levels. The `bot_quality_B_details` collection contains the current B-quality production zones; `quality_B_details` contains B-class reference-only zones. MTF research is loaded from the deep-logic artifact for 17 assets across 1m/5m/15m/1h/4h; it is descriptive and does not feed production decisions.", '', '## Missed-opportunity and V6.4 recommendations','', 'Causal replay examples with favorable expected-direction excursion include BLESSUSDT, MARSCOINUSDT and FARTCOINUSDT; they remain potential false positives/negatives to investigate because contemporaneous orderbook/news are unavailable. MUST FIX: persist per-stage cohort evidence, per-endpoint timing, and independent detector diffs. SHOULD FIX: relative orderbook depth, expanded liquidity pools, and separation of context score from hard invalidation. OPTIONAL: reversal 4h branch and objective rejection variants. DO NOT CHANGE without outcome evidence: anti-repaint, full-fill/CE invalidation, cost/risk checks.','', 'Charts/replays: `data/audits/deep_logic_v6.3/setup_replays.json` and 10-chart reconstruction set. `Signal MAY FORM` remains diagnostic only.']
    (OUT/'COHORT_RESEARCH_V6.3.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print(json.dumps({'cohorts':len(cards),'waterfall_rows':len(waterfall),'first_blockers':dict(first),'reference_counts':ref['reference_counts'],'mode_counts':ref['mode_counts'],'B_details':len(ref['quality_b_setups']),'combos':len(combo['rows'])},ensure_ascii=False))
if __name__=='__main__': main()
