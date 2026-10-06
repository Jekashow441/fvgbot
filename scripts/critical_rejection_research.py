"""Offline forensic research. Never imports engine, writes settings or sends messages.

python scripts/critical_rejection_research.py
python scripts/critical_rejection_research.py --debug-setup ID
"""
import argparse, ast, copy, hashlib, inspect, json, math, random, sys, urllib.request
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.settings import cfg
from trading.profiles import strategy_settings
from trading.context import structure_context, closed_candles
from trading.strategy import validate_signal, detect_fvgs, _calc_atr, market_context, _score_signal
from trading.zone_lifecycle import invalidation_reason
from trading.data_quality import validate_ohlcv

OUT=ROOT/'data/audits/critical_rejection_v6.3'
BASE=ROOT/'data/audits/cohort_logic_v6.3'
NIGHT=ROOT/'data/audits/night_20260910'
CFG=strategy_settings(cfg)
FEATURES=('wick','body','close_ce','engulfing','displacement','mss_choch','multiple')

@lru_cache(maxsize=4096)
def cached_structure(values,pivot):
    d=pd.DataFrame(np.frombuffer(values,dtype=np.float64).reshape(-1,3),columns=['high','low','close'])
    return structure_context(d,pivot)

def save(name,value):
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=lambda x:x.item() if hasattr(x,'item') else str(x),allow_nan=False),encoding='utf8')

@lru_cache(maxsize=36)
def frame(asset,tf='5'):
    paths=[BASE/'input_snapshot'/f'{asset}_{tf}.csv',NIGHT/f'replay_{asset}_{tf}.json',ROOT/f'data/audits/deep_logic_v6.3/mtf/{asset}_{tf}.json']
    for path in paths:
        if path.exists():
            d=pd.read_csv(path) if path.suffix=='.csv' else pd.read_json(path,convert_dates=False,precise_float=True)
            d=d.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            d['timestamp']=pd.to_numeric(d.timestamp).astype('int64')
            return d
    return pd.DataFrame()

def known(asset,tf,decision):
    d=frame(asset,tf)
    return d[d.timestamp+int(tf)*60000<=decision].tail(350).reset_index(drop=True) if not d.empty else d

def research_gate_function(mode):
    """Replace ONLY the two production rejection branches in an offline AST copy.
    All later gates and score calculations remain the real source implementation.
    """
    tree=ast.parse(inspect.getsource(validate_signal))
    class Replace(ast.NodeTransformer):
        def visit_If(self,node):
            if isinstance(node.test,ast.BoolOp) and any(isinstance(n,ast.Constant) and n.value in ('no_bullish_rejection','no_bearish_rejection') for n in ast.walk(node)) and len(node.body)==1:
                return ast.parse("if not _reaction_accept(df, fvg, cfg):\n    return reject('research_reaction_failed')").body[0]
            return self.generic_visit(node)
    tree=Replace().visit(tree); ast.fix_missing_locations(tree)
    ns=dict(validate_signal.__globals__)
    ns['_reaction_accept']=lambda d,z,c: reactions(d,z,c)['modes'][mode]
    exec(compile(tree,'<offline_rejection_mode>','exec'),ns)
    return ns['validate_signal']

def reactions(d,z,c=CFG):
    if d.empty or z is None:return {'modes':{m:False for m in 'ABCDEF'},'missing':True}
    last=d.iloc[-1]; prev=d.iloc[-2] if len(d)>1 else last
    bull=z['type']=='BULLISH'; ce=z['ce']; top=z['top']; bottom=z['bottom']
    touched=d.tail(c.retest_window_bars if c.strategy_profile in ('balanced','contextual') else 1)
    touched=touched[(touched.low<=top)&(touched.high>=bottom)&(touched.timestamp>z.get('formed_at',z['candle_time']))]
    touch=not touched.empty
    span=float(last.high-last.low); body=abs(float(last.close-last.open))
    directional=bool(last.close>last.open if bull else last.close<last.open)
    beyond=bool(last.close>=ce if bull else last.close<=ce)
    wick=float(min(last.open,last.close)-last.low if bull else last.high-max(last.open,last.close))
    touching=bool(last.low<=top and last.high>=bottom and last.timestamp>z.get('formed_at',z['candle_time']))
    wick_ok=bool(touching and span>0 and wick/span>=.4 and beyond)
    crossing=bool(beyond and (prev.close<ce if bull else prev.close>ce))
    engulf=bool(directional and (prev.close<prev.open if bull else prev.close>prev.open) and min(last.open,last.close)<=min(prev.open,prev.close) and max(last.open,last.close)>=max(prev.open,prev.close))
    atr=_calc_atr(d).shift(1).iloc[-1] if len(d)>=15 else np.nan
    displacement=bool(directional and np.isfinite(atr) and body>=atr)
    values=d[['high','low','close']].to_numpy(dtype=np.float64)
    st=cached_structure(values.tobytes(),c.structure_pivot)
    prev_st=cached_structure(values[:-1].tobytes(),c.structure_pivot)
    mss=bool(st['event']==('CHOCH_UP' if bull else 'CHOCH_DOWN') and st['direction']==('UP' if bull else 'DOWN') and prev_st['direction']==('DOWN' if bull else 'UP'))
    body_ok=bool(directional and beyond)
    flags={'wick':wick_ok,'body':body_ok,'close_ce':crossing,'engulfing':engulf,'displacement':displacement,'mss_choch':mss}
    flags['multiple']=sum(flags.values())>=2
    modes={'A':body_ok,'B':wick_ok,'C':directional,'D':body_ok and displacement,'E':body_ok and mss,'F':wick_ok or body_ok or (engulf and beyond) or (mss and beyond)}
    return {**flags,'touch':touch,'last_touch':int(touched.timestamp.iloc[-1]) if touch else None,'body_pct':body/span*100 if span else 0,'wick_pct':wick/span*100 if span else 0,'structure':st,'modes':modes,'known_through_open':int(last.timestamp),'decision_time':int(last.timestamp)+int(c.timeframe)*60000}

def liquidity(d,bull):
    """Typed pools; lows only for sell-side, highs only for buy-side.
    Previous UTC day and current UTC 8-hour session use preceding bars only.
    """
    events=[]
    for j in range(max(20,len(d)-7),len(d)):
        p=d.iloc[:j]; r=d.iloc[j]; col='low' if bull else 'high'
        levels=[]
        for n,label in [(20,'internal'),(50,'local'),(288,'external')]:
            a=p.tail(n)[col]; levels.append((label,float(a.min() if bull else a.max())))
        vals=p.tail(50)[col].to_numpy(); extreme=float(vals.min() if bull else vals.max())
        if np.count_nonzero(abs(vals-extreme)<=abs(extreme)*.001)>=2:levels.append(('equal_lows' if bull else 'equal_highs',extreme))
        day=int(r.timestamp)//86400000; session=int(r.timestamp)//28800000
        for subset,label in [(p[p.timestamp//86400000==day-1],'previous_day'),(p[p.timestamp//28800000==session],'session')]:
            if not subset.empty:levels.append((label,float(subset[col].min() if bull else subset[col].max())))
        for label,level in levels:
            if (r.low<level<r.close if bull else r.high>level>r.close):events.append({'pool':label,'level':level,'sweep_open':int(r.timestamp)})
    return events

def research_status(checks,invalid=False):
    if invalid:return 'INVALIDATED'
    missing=[k for k,v in checks.items() if v is not True]
    if not missing:return 'VALID STRUCTURE (not an executable signal)'
    if checks.get('fvg') and checks.get('displacement') and len(missing)<=2:return 'SIGNAL MAY FORM'
    return 'DEVELOPING'

def runtime_audit(report):
    stored=json.loads((ROOT/'data/bot_config.json').read_text(encoding='utf8'))
    result={'configured':stored['min_signal_score'],'loaded_settings':cfg.min_signal_score,'effective_generator_settings':CFG.min_signal_score,'class_default':65,'environment_override':'No score key in _apply_env_overrides mapping','database':'settings loader reads JSON; database override is paper_balance only','historical_observed_policies':dict(Counter(str((o.get('policy') or {}).get('min_score')) for s in report['setups'] for o in s['observations']))}
    try:
        live=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/status',timeout=15))
        policies=[]
        for symbol,row in live['data'].items():
            for a in row.get('assessments',{}).values():
                if a.get('policy'):policies.append({'asset':symbol,'analysis_bar':a.get('analysis_bar'),'threshold':a['policy'].get('min_score')})
        result.update(runtime_dashboard_policies=policies,runtime_values=sorted(set(x['threshold'] for x in policies)),is_running=live['is_running'])
    except Exception as e:result['runtime_error']=str(e)
    return result

def cohort(report,old):
    selected={c['setup_id'] for c in old['cards'] if c['stages']['rejection']!='N/A'}
    validators={m:research_gate_function(m) for m in 'BCDEF'}; validators['A']=validate_signal
    cards=[]; matrix={k:Counter() for k in FEATURES}; modes={m:Counter() for m in 'ABCDEF'}
    random_ids=set(random.Random(6310).sample([s['setup_id'] for s in report['setups'] if s['asset'] not in ('BLESSUSDT','MARSCOINUSDT','FARTCOINUSDT')],20))
    replay=[]
    for n,s in enumerate(report['setups']):
        d=frame(s['asset']); z=s['fvg']; seen=set(); observations=[]; feature_states=defaultdict(list); mode_states=defaultdict(list)
        for o in s['observations']:
            t=o.get('analysis_bar')
            if t is None or t in seen:continue
            seen.add(t); prefix=d[d.timestamp<=t].tail(350).reset_index(drop=True) if not d.empty else d
            if prefix.empty or int(prefix.timestamp.iloc[-1])!=t or len(prefix)<30:continue
            now=int(t)+300000
            if now>int(pd.Timestamp(o['at']).timestamp()*1000):continue
            r=reactions(prefix,z); invalid=invalidation_reason(z,prefix,'5',now,CFG.fvg_strict_mitigation)
            # Observed technical candidate proves ALL preceding production gates passed.
            # Reconstruct geometric reaction independently even for subsequently expired zones.
            r.update(observed_status=o['status'],recorded_reason=o['why_not_sent'],recorded_entry=o.get('checks',{}).get('Entry'),invalid=invalid)
            r['production_reaction']='N/A' if invalid or not r['touch'] else ('PASS' if r['modes']['A'] else 'FAIL')
            for k in FEATURES:
                if r[k]:feature_states[k].append(r['production_reaction'])
            for m in modes:
                accepted=not invalid and r['touch'] and r['modes'][m]
                entry=None
                # Counterfactual: reuse recorded HTF alignment only if true; unknown context
                # cannot establish a complete execution decision.
                if accepted:
                    htf={'trend':'UP' if s['direction']=='LONG' else 'DOWN'} if o.get('checks',{}).get('HTF bias') else None
                    diagnostics={}; candidate=validators[m](prefix,z,htf,settings=CFG,diagnostics=diagnostics)
                    entry=bool(candidate)
                mode_states[m].append((accepted,entry))
            observations.append(r)
        flags={k:bool(feature_states[k]) for k in FEATURES}
        card={'setup_id':s['setup_id'],'asset':s['asset'],'fvg':z,'old_rejection_cohort':s['setup_id'] in selected,'coverage':'AVAILABLE' if observations else 'UNKNOWN','observations':observations,'ever_features':flags,'recorded_ever_entry':s['ever_entry_candidate'],'terminal_reason':s['observations'][-1]['why_not_sent'] if s['observations'] else None,'recorded_liquidity_ever':any(o.get('checks',{}).get('Liquidity') is True for o in s['observations'])}
        cards.append(card)
        if s['setup_id'] in selected:
            for k in FEATURES:
                if feature_states[k]:
                    matrix[k]['count']+=1
                    status='PASS' if 'PASS' in feature_states[k] else 'FAIL' if 'FAIL' in feature_states[k] else 'N/A'
                    matrix[k][status]+=1
            for m,states in mode_states.items():
                modes[m]['covered']+=1;modes[m]['passed']+=int(any(a for a,e in states));modes[m]['entry_without_historical_market_context']+=int(any(e for a,e in states))
        if s['setup_id'] in random_ids or s['asset'] in ('BLESSUSDT','MARSCOINUSDT','FARTCOINUSDT'):
            # Every candle, including those between recorded observations.
            events=[]
            if not d.empty and seen:
                span=d[(d.timestamp>=z['formed_at'])&(d.timestamp<=max(seen))].tail(100)
                for t in span.timestamp:
                    p=d[d.timestamp<=t].tail(350).reset_index(drop=True); r=reactions(p,z); why=invalidation_reason(z,p,'5',int(t)+300000,CFG.fvg_strict_mitigation)
                    checks={'fvg':True,'displacement':z.get('displacement_atr',0)>=CFG.displacement_atr,'liquidity':bool(liquidity(p,z['type']=='BULLISH')),'reaction':r['touch'] and r['modes']['A'],'htf':None}
                    events.append({'decision_close_ms':int(t)+300000,'known_through_open_ms':int(t),'reaction':r,'status':research_status(checks,bool(why)),'blocker':why,'unavailable':['historical orderbook','news','exact runtime market context']})
            replay.append({'setup_id':s['setup_id'],'selection':'seeded_random' if s['setup_id'] in random_ids else 'requested_asset','events':events})
        if n%200==0:print('cohort',n,flush=True)
    save('cohort_cards.json',cards);save('causal_replays.json',replay)
    return {'old_rejection_cohort':len(selected),'cards':len(cards),'covered':sum(c['coverage']=='AVAILABLE' for c in cards),'reaction_matrix':{k:dict(v) for k,v in matrix.items()},'modes':{k:{**dict(v),'potential':'UNKNOWN: no archived counterfactual execution context','confirmed':'UNKNOWN: not replayed execution'} for k,v in modes.items()},'recorded_liquidity_ever_pass':sum(c['recorded_liquidity_ever'] for c in cards),'replay_count':len(replay),'random_replay_count':sum(r['selection']=='seeded_random' for r in replay),'actual_reaction_ever_pass':sum(any(o['production_reaction']=='PASS' for o in c['observations']) for c in cards),'actual_reaction_ever_fail':sum(any(o['production_reaction']=='FAIL' for o in c['observations']) for c in cards),'recorded_entry_old_rejection_fail':sum(c['ever_entry_candidate'] and c['stages']['rejection']=='FAIL' for c in old['cards'])}

def reference(old):
    rows=[]; overlap=Counter(); primary=Counter(); archived_strong=[]; archived_volume=[]; ideal=[]
    for n,zold in enumerate(old['reference_only_zones']):
        asset=zold['asset']+'USDT'; d=frame(asset); ts=zold['candle_time']; positions=np.flatnonzero(d.timestamp.to_numpy()==ts)
        if not len(positions):continue
        k=int(positions[-1]); i=k+1
        if i>=len(d):continue
        p=d.iloc[max(0,k-350):i+1].reset_index(drop=True); mk=len(p)-2
        bull=zold['type']=='BULLISH'; body=abs(float(p.close.iloc[mk]-p.open.iloc[mk])); span=float(p.high.iloc[mk]-p.low.iloc[mk]); atr=float(_calc_atr(p).iloc[mk-1]); volmean=float(p.volume.iloc[mk-20:mk].mean()) if mk>=20 else np.nan
        atrratio=body/atr if np.isfinite(atr) and atr>0 else None; bodypct=body/span*100 if span else 0
        b=bodypct>=CFG.fvg_min_body_pct; a=atrratio is not None and atrratio>=CFG.displacement_atr
        overlap[f'body_{b}_atr_{a}']+=1
        volume='UNKNOWN' if not np.isfinite(volmean) or volmean<=0 else 'PASS' if p.volume.iloc[mk]>volmean else 'FAIL'
        size=(zold['upper']-zold['lower']); sizeok=zold['size_pct']>=CFG.fvg_min_size_pct and size<=CFG.fvg_max_size_atr*atr
        direction=bool(p.close.iloc[mk]>p.open.iloc[mk] if bull else p.close.iloc[mk]<p.open.iloc[mk])
        st=structure_context(p,CFG.structure_pivot); aligned=st['direction']==('UP' if bull else 'DOWN'); sweeps=liquidity(p.iloc[:-1],bull)
        now=int(p.timestamp.iloc[-1])+300000; h=known(asset,'60',now); htf=market_context(h,CFG)['trend'] if len(h)>=60 else 'UNKNOWN'; htfalign=htf==('UP' if bull else 'DOWN')
        failures=[key for key,ok in [('body',b),('atr',a),('direction',direction),('volume',volume=='PASS' or not CFG.fvg_require_volume),('size',sizeok)] if not ok]
        primary[failures[0] if failures else 'formation_rules_pass']+=1
        quality='A' if a and sizeok and aligned and sweeps and htfalign else 'B' if a and sizeok and aligned and sweeps else 'C'
        z={'type':zold['type'],'top':zold['upper'],'bottom':zold['lower'],'ce':(zold['upper']+zold['lower'])/2,'formed_at':int(p.timestamp.iloc[-1]),'candle_time':int(ts)}
        after=[]
        for end in range(i+1,min(i+13,len(d))):
            prefix=d.iloc[max(0,end-349):end+1].reset_index(drop=True); r=reactions(prefix,z)
            after.append({'decision_ms':int(d.timestamp.iloc[end])+300000,'reaction':r['modes']['A'],'touch':r['touch'],'invalid':invalidation_reason(z,prefix,'5',int(d.timestamp.iloc[end])+300000,True)})
        row={'asset':asset,'candle_time':int(ts),'body_pct':bodypct,'atr':atr if np.isfinite(atr) else None,'atr_ratio':atrratio,'size_pct':zold['size_pct'],'relative_volume':float(p.volume.iloc[mk]/volmean) if np.isfinite(volmean) and volmean>0 else None,'volume':volume,'structure':st,'liquidity':sweeps,'htf':htf,'quality':quality,'failures_at_formation':failures,'body_only_failure':failures==['body'],'future_descriptive_reaction':after,'old_expanded':zold['expanded_liquidity_sweep'],'old_quality':zold['category'],'old_volume':zold['volume_ok'],'fvg':z}
        rows.append(row)
        if not zold['body_ok'] and (zold['atr_ratio'] or 0)>=1:archived_strong.append(row)
        if zold['rejection_reasons'] and zold['rejection_reasons'][0]=='volume':archived_volume.append(row)
        if a and sizeok and aligned and sweeps:ideal.append(row)
        if n%500==0:print('reference',n,flush=True)
    save('reference_corrected.json',rows);save('body_152.json',archived_strong);save('volume_649.json',archived_volume);save('idealized_setups.json',ideal)
    pairs=[(r['body_pct'],r['atr_ratio']) for r in rows if r['atr_ratio'] is not None]
    return {'count':len(rows),'formation_first_failures':dict(primary),'quality':dict(Counter(r['quality'] for r in rows)),'body_atr_overlap':dict(overlap),'body_atr_pearson':float(np.corrcoef(np.array(pairs).T)[0,1]),'body_152_count':len(archived_strong),'body_152_quality':dict(Counter(r['quality'] for r in archived_strong)),'body_152_only_body_fail':sum(r['body_only_failure'] for r in archived_strong),'volume_649_count':len(archived_volume),'volume_649_recomputed':dict(Counter(r['volume'] for r in archived_volume)),'all_volume':dict(Counter(r['volume'] for r in rows)),'old_high_quality_expanded_rechecked':sum(r['old_expanded'] and r['old_quality'] in ('A','B') for r in rows),'old_high_quality_expanded_correct_sweep':sum(r['old_expanded'] and r['old_quality'] in ('A','B') and bool(r['liquidity']) for r in rows),'idealized_count':len(ideal),'idealized_quality':dict(Counter(r['quality'] for r in ideal)),'htf_unknown':sum(r['htf']=='UNKNOWN' for r in rows)}

def scores(report):
    rows=[]
    for sid,ev in {e['setup_id']:e for e in report['technical_entry_observations']}.items():
        p=ev['payload']; assessment=p['assessment']; d=known(p['asset'],'5',assessment['analysis_bar']+300000);ctx=market_context(d,CFG)
        base,factors=_score_signal(p['direction'],p['fvg'],ctx,{'trend':p.get('htf_bias')},ctx.get('rsi'),ctx.get('adx'),CFG)
        weights={'large_fvg':10,'ltf_uptrend':12,'ltf_downtrend':12,'htf_uptrend':18,'htf_downtrend':18,'htf_against':-25,'strong_adx':10,'weak_adx':-10,'healthy_rsi':5}
        rows.append({'setup_id':sid,'asset':p['asset'],'direction':p['direction'],'archived_score':assessment['score'],'threshold':assessment['policy']['min_score'],'reconstructed_components':{'base':50,**{f:weights[f] for f in factors}},'reconstructed_base_score':base,'difference_to_archived':assessment['score']-base,'note':'Indicator window can differ from original; difference may include trendline +8. No fabricated components.','final_decision':ev['status'],'blockers':ev['reason']})
    save('candidate_scores.json',rows)
    return {'count':len(rows),'bins':{label:sum(lo<=r['archived_score']<=hi for r in rows) for label,lo,hi in [('below60',0,59),('60-64',60,64),('65-69',65,69),('70-74',70,74),('75-79',75,79),('80+',80,100)]},'thresholds':dict(Counter(str(r['threshold']) for r in rows))}

def mtf(report):
    rows=[]
    for asset,ev in {e['payload']['asset']:e for e in report['technical_entry_observations']}.items():
        now=ev['payload']['assessment']['analysis_bar']+300000; mapping={}
        for tf in ('1','5','15','60','240'):
            d=known(asset,tf,now); zones=[]
            if len(d)>=30:
                local=CFG.model_copy(update={'timeframe':tf}); st=structure_context(d,local.structure_pivot)
                for i in range(max(22,len(d)-20),len(d)):
                    p=d.iloc[:i+1]; bull=p.low.iloc[-1]>p.high.iloc[-3]; bear=p.high.iloc[-1]<p.low.iloc[-3]
                    if not (bull or bear):continue
                    atr=_calc_atr(p).iloc[-3]; body=abs(p.close.iloc[-2]-p.open.iloc[-2]); disp=bool(np.isfinite(atr) and body>=atr)
                    z={'type':'BULLISH' if bull else 'BEARISH','top':float(p.low.iloc[-1] if bull else p.low.iloc[-3]),'bottom':float(p.high.iloc[-3] if bull else p.high.iloc[-1]),'formed_at':int(p.timestamp.iloc[-1]),'candle_time':int(p.timestamp.iloc[-2])};z['ce']=(z['top']+z['bottom'])/2
                    sweep=liquidity(p.iloc[:-1],bool(bull)); aligned=structure_context(p,local.structure_pivot)['direction']==('UP' if bull else 'DOWN'); invalid=invalidation_reason(z,d,tf,now,True)
                    zones.append({**z,'displacement':disp,'liquidity':bool(sweep),'structure_aligned':aligned,'invalid':invalid,'quality':disp and bool(sweep) and aligned and not invalid})
                mapping[tf]={'available':True,'structure':st,'zones':zones,'quality_count':sum(bool(z['quality']) for z in zones)}
            else:mapping[tf]={'available':False,'reason':'INSUFFICIENT DATA'}
        relationships=[]
        for high,low in [('15','5'),('60','15')]:
            for hz in mapping[high].get('zones',[]):
                for lz in mapping[low].get('zones',[]):
                    if hz['type']==lz['type'] and not hz['invalid'] and not lz['invalid'] and lz['formed_at']+int(low)*60000>=hz['formed_at']+int(high)*60000 and max(hz['bottom'],lz['bottom'])<=min(hz['top'],lz['top']):relationships.append({'htf':high,'ltf':low,'htf_fvg':hz,'ltf_fvg':lz})
        rows.append({'asset':asset,'decision_ms':now,'timeframes':mapping,'relationships':relationships})
    save('mtf_ranking.json',rows)
    return {'assets':len(rows),'quality_by_tf':{tf:sum(r['timeframes'][tf].get('quality_count',0) for r in rows) for tf in ('1','5','15','60','240')},'relationships':sum(len(r['relationships']) for r in rows)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--debug-setup');parser.add_argument('--section',choices=['cohort','reference','scores','mtf']);args=parser.parse_args()
    if args.debug_setup:
        cards=json.loads((OUT/'cohort_cards.json').read_text(encoding='utf8'))
        card=next((c for c in cards if c['setup_id']==args.debug_setup),None)
        if not card:raise SystemExit('Unknown setup ID')
        last=card['observations'][-1] if card['observations'] else {}
        archive=json.loads((OUT/'archive_evidence.json').read_text(encoding='utf8'))['cards'].get(args.debug_setup,[])
        recorded=archive[-1] if archive else {}
        debug={'MARKET':{'asset':card['asset'],'structure':last.get('structure'),'liquidity_ever':card['recorded_liquidity_ever'],'HTF':'See archived observation; not inferred from current market'},'FVG':card['fvg'],'REACTION':{k:last.get(k,'UNKNOWN') for k in FEATURES},'FILTERS':{'rejection':last.get('production_reaction','UNKNOWN'),'mitigation':last.get('invalid'),'orderbook':'UNKNOWN','spread':'UNKNOWN','VWAP':'UNKNOWN'},'FINAL':{'recorded_entry':card['recorded_ever_entry'],'terminal_reason':card['terminal_reason'],'coverage':card['coverage']},'scope':'Offline research debug, no Telegram / no trading signal'}
        debug['MARKET']['HTF']=recorded.get('htf','UNKNOWN')
        debug['FILTERS']['recorded_checks']=recorded.get('checks',{})
        debug['FINAL'].update(threshold=(recorded.get('policy') or {}).get('min_score','UNKNOWN'),first_validator_blocker=recorded.get('validator_first_reason'),secondary_recorded_reasons=recorded.get('all_validator_reasons',[]))
        debug['FINAL']['score']=recorded.get('score','UNKNOWN')
        debug['FVG']['note']='Original formation snapshot; age/touches/state are not latest values'
        debug['as_of']={'reaction_close_ms':last.get('decision_time'),'archived_observation_ms':int(recorded['at']*1000) if recorded else None}
        print(json.dumps(debug,ensure_ascii=False,indent=2));return
    OUT.mkdir(parents=True,exist_ok=True)
    report=json.loads((NIGHT/'night_report.json').read_text(encoding='utf8'));old=json.loads((BASE/'cohort_research.json').read_text(encoding='utf8'))
    if args.section:
        result=json.loads((OUT/'research.json').read_text(encoding='utf8'))
        result[args.section]={'cohort':lambda:cohort(report,old),'reference':lambda:reference(old),'scores':lambda:scores(report),'mtf':lambda:mtf(report)}[args.section]()
        save('research.json',result);print(json.dumps(result[args.section],ensure_ascii=False));return
    result={'scope':'offline causal research; no production writes','runtime':runtime_audit(report)};save('research.json',result)
    result['cohort']=cohort(report,old);save('research.json',result)
    result['reference']=reference(old);save('research.json',result)
    result['scores']=scores(report);result['mtf']=mtf(report);save('research.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='runtime'},ensure_ascii=False))

if __name__=='__main__':main()



