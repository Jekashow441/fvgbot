"""Separate formation rejection, current lifecycle and actual production membership."""
from critical_rejection_research import *
rows=json.loads((OUT/'reference_corrected.json').read_text(encoding='utf8'));out=[]
for asset in sorted({r['asset'] for r in rows}):
    d=frame(asset).tail(350).reset_index(drop=True);accepted={(z['type'],z['candle_time']) for z in detect_fvgs(d,CFG)}
    for r in (r for r in rows if r['asset']==asset):
        z=r['fvg'];now=int(d.timestamp.iloc[-1])+300000;why=invalidation_reason(z,d,'5',now,True)
        membership=(z['type'],z['candle_time']) in accepted
        reason='accepted_with_full_warmup' if membership else r['failures_at_formation'][0] if r['failures_at_formation'] else why or 'lookback_or_confirmation_window'
        out.append({'asset':asset,'candle_time':z['candle_time'],'production_accepts_full_history':membership,'formation_failures':r['failures_at_formation'],'lifecycle':why,'current_first_reason':reason})
save('actual_detector_diff.json',out)
summary={'count':len(out),'reasons':dict(Counter(r['current_first_reason'] for r in out))}
save('actual_detector_diff_summary.json',summary);print(json.dumps(summary))
