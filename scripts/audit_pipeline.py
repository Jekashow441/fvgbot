"""Read-only forensic export. Unknown instrumentation is never counted as zero."""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def utc(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None


def audit(db_path,cutoff):
    con=sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table):return [dict(r) for r in con.execute('SELECT * FROM '+table)] if table in tables else []
    trades=rows('signals');outbox=rows('signal_outbox');events=rows('setup_events')
    snapshots={r['key'][9:]:json.loads(r['value']) for r in rows('settings_text') if r['key'].startswith('snapshot:')}
    by_setup=defaultdict(list)
    for e in events:
        e['payload']=json.loads(e['payload']);by_setup[e['setup_id']].append(e)
    for t in trades:t['features']=json.loads(t.get('ml_features') or '{}')
    flows=[];linked=set()
    for o in outbox:
        raw=json.loads(o['payload']);s=raw['signal'];key=s.get('journal_id');history=by_setup.get(key,[])
        matches=[t for t in trades if t.get('signal_id')==o['signal_id'] or (key and t['features'].get('journal_id')==key)]
        link='explicit'
        if not matches:
            matches=[t for t in trades if t['symbol']==raw['symbol'] and t['side']==s['signal'] and abs(t['entry']-s['entry'])<1e-9 and 0<=t['entry_ts']/1000-o['created']<5]
            link='inferred exact asset/side/entry and <5s; legacy source has no journal ID'
        linked.update(t['id'] for t in matches)
        flows.append(dict(signal_id=o['signal_id'],setup_id=key,asset=raw['symbol'],generated_utc=utc(o['created']),sent_state=o['state'],sent_utc=utc(o.get('sent_at')),telegram_message_id=o.get('telegram_message_id'),delivery_delay_seconds=(o['sent_at']-o['created']) if o.get('sent_at') else None,
            score=s.get('score'),rr=s.get('rr'),turnover=s.get('market',{}).get('turnover_24h'),old_thresholds_pass=s.get('score',0)>=80 and s.get('market',{}).get('turnover_24h',0)>=5_000_000,
            original_levels={k:s.get(k) for k in ('entry','sl','tp')},source_quote_ms=s.get('microstructure',{}).get('book_timestamp'),link_basis=link,paper_ids=[t['id'] for t in matches],
            journal=[dict(id=e['id'],status=e['status'],observed_utc=utc(e['observed_at']),score=e['payload'].get('fvg_quality')) for e in history]))
    paper=[]
    for t in trades:
        f=t['features'];flow=next((x for x in flows if t['id'] in x['paper_ids']),None);checks=[]
        if not flow:checks.append('source snapshot unavailable: historical execution cannot be fully audited')
        if t['outcome'] in ('WIN','LOSS') and (t['exit_price'] is None or t['pnl_pct'] is None or not t['closed_ts']):checks.append('closed trade lacks result fields')
        if t['outcome'] in ('WIN','LOSS') and t['pnl_pct'] is not None and (t['pnl_pct']>0)!=(t['outcome']=='WIN'):checks.append('outcome/pnl sign mismatch')
        if flow:
            if abs(t['entry']-flow['original_levels']['entry'])>1e-9 and f.get('signal_entry') is None:checks.append('entry mismatch without fill provenance')
            if abs(t['tp']-flow['original_levels']['tp'])>1e-9:checks.append('target differs from original')
            if t['entry_ts']/1000<float(datetime.fromisoformat(flow['generated_utc']).timestamp()):checks.append('paper opened before generation')
            q=flow['source_quote_ms']
            if q and q>t['entry_ts']:checks.append('exchange quote timestamp later than host entry: clock skew / chronology unresolved')
        original_sl=f.get('initial_sl',flow['original_levels']['sl'] if flow else None)
        level_log=[r for r in rows('paper_level_events') if r['trade_id']==t['id']]
        if original_sl is not None and t['sl']!=original_sl and not level_log:checks.append('managed SL differs; historical movement path was not recorded')
        expected=None
        if t['exit_price'] is not None and all(f.get(k) is not None for k in ('fee_bps','slippage_bps')):
            expected=(1 if t['side']=='LONG' else -1)*(t['exit_price']-t['entry'])/t['entry']*100-2*(f['fee_bps']+f['slippage_bps'])/100
            if abs(expected-t['pnl_pct'])>1e-7:checks.append('fee-adjusted pnl mismatch')
        paper.append(dict(id=t['id'],asset=t['symbol'],side=t['side'],strategy=f.get('strategy_version','legacy'),source_signal=flow['signal_id'] if flow else None,
            opened_utc=utc(t['entry_ts']/1000) if t['entry_ts'] else None,closed_local_legacy=t['closed_ts'],closed_utc=utc(t['closed_at_ms']/1000) if t.get('closed_at_ms') else None,
            entry=t['entry'],original_sl=original_sl,managed_sl=t['sl'],tp=t['tp'],exit_price=t['exit_price'],exit_reason=t.get('exit_reason'),outcome=t['outcome'],pnl_pct=t['pnl_pct'],recalculated_pnl_pct=expected,level_events=level_log,findings=checks))
    bands={label:dict(count=0,confirmed=0,invalidated=0,expired=0,paper_trades=0,wins=0,losses=0,rr=[],false_positive_rate=None) for label in ('65-69','70-74','75-79','80+','UNKNOWN','below65')}
    latest=Counter();ever=Counter();transitions=Counter();near=[];firsts=[]
    for key,history in by_setup.items():
        first=history[0];p=first['payload'];score=p.get('fvg_quality')
        label='UNKNOWN' if score is None else '80+' if score>=80 else '75-79' if score>=75 else '70-74' if score>=70 else '65-69' if score>=65 else 'below65'
        group=bands[label];group['count']+=1;statuses={e['status'] for e in history}
        for status in statuses:ever[status]+=1
        latest[history[-1]['status']]+=1
        for a,b in zip(history,history[1:]):
            if a['status']!=b['status']:transitions[a['status']+' -> '+b['status']]+=1
        group['confirmed']+=bool(statuses&{'CONFIRMED','RECOVERED CONFIRMATION'});group['invalidated']+='INVALIDATED' in statuses;group['expired']+='EXPIRED' in statuses
        ts=[t for t in trades if t['features'].get('journal_id')==key];group['paper_trades']+=len(ts);group['wins']+=sum(t['outcome']=='WIN' for t in ts);group['losses']+=sum(t['outcome']=='LOSS' for t in ts)
        group['rr'] += [e['payload']['rr'] for e in history if e['status']=='CONFIRMED' and e['payload'].get('rr') is not None][:1]
        firsts.append(first)
    for group in bands.values():
        values=group.pop('rr');group['mean_confirmed_rr']=sum(values)/len(values) if values else None
    for i,a in enumerate(firsts):
        pa=a['payload'];za=pa['fvg']
        for b in firsts[i+1:]:
            pb=b['payload'];zb=pb['fvg']
            if (pa['asset'],pa['timeframe'],pa['direction'])!=(pb['asset'],pb['timeframe'],pb['direction']):continue
            if abs(za['candle_time']-zb['candle_time'])<=6*int(pa['timeframe'])*60000 and max(za['bottom'],zb['bottom'])<=min(za['top'],zb['top']):
                near.append([a['setup_id'],b['setup_id']])
    cohort={}
    for name,lo,hi in [('old',float('-inf'),cutoff),('expanded',cutoff,float('inf'))]:
        es=[e for e in events if lo<=e['observed_at']<hi];ss=[o for o in outbox if lo<=o['created']<hi];tt=[t for t in trades if t['id'] in linked and lo<=t['entry_ts']/1000<hi]
        cohort[name]=dict(first_event_utc=utc(min((e['observed_at'] for e in es),default=None)),last_event_utc=utc(max((e['observed_at'] for e in es),default=None)),setups=len({e['setup_id'] for e in es}),unique_status_counts={s:len({e['setup_id'] for e in es if e['status']==s}) for s in ('WATCHLIST','POTENTIAL','CONFIRMED','INVALIDATED','EXPIRED')},developing=None,entry_approaching=None,generated=len(ss),sent=sum(o['state']=='SENT' for o in ss),linked_paper=len(tt))
    result=dict(database=str(db_path),cutoff_utc=utc(cutoff),cutoff_basis='coverage_change.json filesystem mtime; approximate, not a persisted policy activation event',
        delivery=dict(generated=len(set(snapshots)|{o['signal_id'] for o in outbox}),recorded_sent=sum(o['state']=='SENT' for o in outbox),receipt_verified=sum(o.get('telegram_message_id') is not None for o in outbox),failed_state=sum(o['state']=='FAILED' for o in outbox),pending=sum(o['state']=='PENDING' for o in outbox),expired=sum(o['state']=='EXPIRED' for o in outbox),missing_outbox=list(set(snapshots)-{o['signal_id'] for o in outbox}),observed_duplicate_paper=sum(max(0,len(f['paper_ids'])-1) for f in flows),external_duplicates=None,delayed=None,correctly_logged=sum(bool(f['journal']) for f in flows if f['setup_id']),journal_instrumented_signals=sum(bool(f['setup_id']) for f in flows),attempt_results=dict(Counter(r['result'] for r in rows('delivery_attempts')))),
        total_paper=len(trades),source_linked_paper=len(linked),source_unavailable_paper=len(trades)-len(linked),flows=flows,paper=paper,cohorts=cohort,
        unique_setups=len(by_setup),event_count=len(events),ever_stages=dict(ever),latest_stages=dict(latest),observed_status_transitions=dict(transitions),initial_score_bands=bands,near_duplicate_pairs=near,
        limitations=['Recorded SENT means prior API call returned; not verified human delivery/read. Old receipts/timestamps unavailable.','Score cohorts use first recorded score only. Missing T1 scores are not reconstructed.','Stages can be skipped or revisited. Marginal stage counts are not sequential conversion rates.','No saved candle/book tape for historical trades; full look-ahead or intrabar fills cannot be proven.'])
    con.close();return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('output');args=p.parse_args()
    result=audit(args.database,Path('data/coverage_change.json').stat().st_mtime)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('delivery','total_paper','source_linked_paper','unique_setups','event_count','ever_stages','latest_stages','cohorts','initial_score_bands')},ensure_ascii=True,indent=2))
