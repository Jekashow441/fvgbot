"""Read the frozen database's original diagnostics, not lossy report summaries."""
import sys,json,sqlite3
from pathlib import Path
from collections import defaultdict,Counter
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'data/audits/critical_rejection_v6.3'
p=ROOT/'data/audits/night_20260910/before.db'
con=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
groups=defaultdict(list);reasons=defaultdict(set);policies=defaultdict(list)
for sid,at,raw in con.execute('select setup_id,observed_at,payload from setup_events where observed_at>=? order by observed_at,id',(1788983240,)):
    p=json.loads(raw);a=p.get('assessment') or {};rs=a.get('rejections') or [];checks=a.get('checks') or {}
    # First item comes from validate_signal; exhaustion can be appended afterwards.
    first=rs[0] if rs else None
    status='PASS' if checks.get('Entry') else 'NOT_REACHED' if first in ('missing_setup','no_recent_retest') else 'FAIL' if first in ('no_bullish_rejection','no_bearish_rejection') else 'PASS' if first else 'UNKNOWN'
    groups[sid].append({'at':at,'score':a.get('score'),'analysis_bar':a.get('analysis_bar'),'rejection_status':status,'validator_first_reason':first,'all_validator_reasons':rs,'checks':checks,'policy':a.get('policy'),'final_status':p.get('status'),'htf':p.get('htf_bias')})
    for r in rs:reasons[r].add(sid)
    if a.get('policy'):policies[str(a['policy'].get('min_score'))].append(at)
out={'scope':'Archived diagnostic traces. ever-pass/fail overlap over time; not a sequential waterfall.','cohort':len(groups),'rejection_ever':{s:sum(any(e['rejection_status']==s for e in es) for es in groups.values()) for s in ('PASS','FAIL','NOT_REACHED','UNKNOWN')},'liquidity_ever_true':sum(any(e['checks'].get('Liquidity') is True for e in es) for es in groups.values()),'liquidity_ever_false':sum(any(e['checks'].get('Liquidity') is False for e in es) for es in groups.values()),'liquidity_entry_without_sweep':sum(any(e['checks'].get('Entry') and e['checks'].get('Liquidity') is False for e in es) for es in groups.values()),'validator_reasons':{k:len(v) for k,v in reasons.items()},'policy_windows':{k:{'count':len(v),'start':min(v),'end':max(v)} for k,v in policies.items()},'cards':groups}
OUT.mkdir(parents=True,exist_ok=True);(OUT/'archive_evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in out.items() if k!='cards'},ensure_ascii=False))

