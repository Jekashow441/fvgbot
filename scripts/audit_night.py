"""Frozen-night evidence. Stage totals are distinct setups, not duplicated observations."""
import json
import sqlite3
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path

folder=Path('data/audits/night_20260910');con=sqlite3.connect((folder/'before.db').resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
start=datetime(2026,9,9,19,47,20,tzinfo=timezone.utc).timestamp()
def utc(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
events=[dict(r) for r in con.execute('SELECT * FROM setup_events WHERE observed_at>=? ORDER BY id',(start,))]
groups=defaultdict(list)
for e in events:e['payload']=json.loads(e['payload']);groups[e['setup_id']].append(e)
bands=Counter();first_bands=Counter();filters=defaultdict(set);entry_filters=defaultdict(set);checks=defaultdict(set);stages=defaultdict(set);details=[];entry_events=[]
def band(s):return 'UNKNOWN' if s is None else '0-49' if s<50 else '50-59' if s<60 else '60-64' if s<65 else '65-69' if s<70 else '70-74' if s<75 else '75-79' if s<80 else '80+'
for key,history in groups.items():
    first=history[0]['payload'];scores=[];trail=[];all_reasons=set();entry=False
    for e in history:
        p=e['payload'];a=p.get('assessment',{});score=p.get('fvg_quality');stages[e['status']].add(key)
        if score is not None:scores.append(score)
        satisfied=a.get('checks',{})
        for k,v in satisfied.items():
            if v:checks[k].add(key)
        if satisfied.get('HTF bias') and satisfied.get('Structure'):checks['HTF_and_structure'].add(key)
        reasons=set(e['reason'].split('; '))|set(a.get('rejections',[]))|set(a.get('execution_rejections',[]))
        for reason in reasons:filters[reason].add(key)
        all_reasons|=reasons
        if satisfied.get('Entry'):
            entry=True;entry_events.append(e)
            for reason in e['reason'].split('; '):entry_filters[reason].add(key)
        item={'at':utc(e['observed_at']),'event_id':e['id'],'score':score,'status':e['status'],'why_not_sent':e['reason'],'checks':satisfied,'analysis_bar':a.get('analysis_bar'),'policy':a.get('policy')}
        if not trail or any(trail[-1][k]!=item[k] for k in ('score','status','why_not_sent','analysis_bar')):trail.append(item)
    max_score=max(scores) if scores else None;bands[band(max_score)]+=1;first_bands[band(first.get('fvg_quality'))]+=1
    conditions='Entry prerequisite was achieved; live execution/context gates rejected it. Historical book/4h values not archived, so independent execution verification unavailable.' if entry else 'No recorded observation satisfied all technical entry prerequisites. Retest/rejection can occur before expiry; permanently exhausted zones cannot recover under the same ID.'
    details.append(dict(setup_id=key,asset=first['asset'],direction=first['direction'],timeframe=first['timeframe'],fvg=first['fvg'],first_score=first.get('fvg_quality'),max_observed_score=max_score,last_status=history[-1]['status'],ever_entry_candidate=entry,ever_potential=any(e['status']=='POTENTIAL' for e in history),why_not_sent=sorted(all_reasons),assessment=conditions,observations=trail))
entry_keys={e['setup_id'] for e in entry_events}
outbox=[dict(r) for r in con.execute('SELECT signal_id,created,state,attempts,error,sent_at,telegram_message_id FROM signal_outbox WHERE created>=?',(start,))]
live=json.loads((folder/'status.json').read_text(encoding='utf-8'));latest_counts=Counter()
for r in live['data'].values():
    b=r.get('blockers',[])
    latest_counts['failed' if any(x in ('insufficient_closed_candles','empty_candles') or x.startswith('scan_error') for x in b) else 'stale' if any('stale' in x for x in b) else 'rows_without_recorded_data_error']+=1
result={'start_utc':utc(start),'last_observation_utc':utc(max(e['observed_at'] for e in events)),'events':len(events),'distinct_setups':len(groups),'assets_with_observed_setups':len({d['asset'] for d in details}),'night_successful_scans':None,'night_stale_scans':None,'night_failed_scans':None,'night_skipped_scans':None,'reason_scan_counts_unknown':'V6.2 did not persist scan-cycle outcomes; configured/processed counts cannot establish historical freshness.',
    'stages':{k:len(v) for k,v in stages.items()},'checks':{k:len(v) for k,v in checks.items()},'entry_candidate_setups':len(entry_keys),'generated':len(outbox),'queued':len(outbox),'sent':sum(o['state']=='SENT' for o in outbox),'score_bands_max_observed':dict(bands),'score_bands_first_night_observation':dict(first_bands),
    'filters':[{'filter':k,'setups':len(v),'percent_of_all_setups':round(100*len(v)/len(groups),2)} for k,v in sorted(filters.items(),key=lambda kv:-len(kv[1]))],
    'entry_filters':[{'filter':k,'setups':len(v),'percent_of_technical_candidates':round(100*len(v)/len(entry_keys),2)} for k,v in sorted(entry_filters.items(),key=lambda kv:-len(kv[1]))],
    'policy_history':[dict(at=utc(r['observed_at']),**json.loads(r['payload'])) for r in con.execute('SELECT * FROM runtime_policy_events ORDER BY id')],
    'current_scan':live['scan'],'current_row_error_counts':dict(latest_counts),'setups':details,'technical_entry_observations':entry_events}
(folder/'night_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# Все ночные сетапы с наблюдавшимся Score ≥65','',f"Окно {result['start_utc']} — {result['last_observation_utc']}. Для группировки используется максимум реально записанных оценок за окно; исходные оценки и все изменения сохранены отдельно в JSON. Высокий Score не отменяет обязательные условия входа.",'','| ID / инструмент | Max Score | Последний статус | Прошёл базовый вход | Почему не отправлен за окно |','|---|---:|---|---|---|']
for d in details:
    if d['max_observed_score'] is not None and d['max_observed_score']>=65:lines.append(f"| {d['setup_id']} | {d['max_observed_score']} | {d['last_status']} | {d['ever_entry_candidate']} | {'; '.join(d['why_not_sent'])} |")
Path('NIGHT_SETUPS_2026-09-10.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k not in ('setups','technical_entry_observations','filters')},ensure_ascii=True,indent=2))
