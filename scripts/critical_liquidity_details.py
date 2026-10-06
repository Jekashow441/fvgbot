"""Recheck each formerly labelled Liquidity FAIL without inventing its cause."""
from critical_rejection_research import *

old=json.loads((BASE/'cohort_research.json').read_text(encoding='utf8'))
cards=json.loads((OUT/'cohort_cards.json').read_text(encoding='utf8'));lookup={c['setup_id']:c for c in cards}
rows=[]
for previous in sorted(old['cards'],key=lambda c:c['asset']):
    if previous['stages']['liquidity']!='FAIL':continue
    card=lookup[previous['setup_id']];obs=card['observations'];pools=[]
    for o in obs:
        p=known(card['asset'],'5',o['decision_time'])
        pools.extend(liquidity(p,card['fvg']['type']=='BULLISH'))
    unique={(e['pool'],e['sweep_open'],e['level']):e for e in pools}
    labels=sorted({e['pool'] for e in unique.values()})
    reason='INSUFFICIENT_DATA' if not obs else 'expanded_pool_or_earlier_sweep_found' if unique else 'no_sweep_under_research_definition'
    rows.append({'setup_id':card['setup_id'],'reason':reason,'production_ever_sweep':card['recorded_liquidity_ever'],'pools':labels,'events':list(unique.values()),'wrong_timeframe':'NOT_PROVEN','timestamp_bug':'NOT_PROVEN'})
save('liquidity_1623.json',rows)
summary={'rows':len(rows),'reasons':dict(Counter(r['reason'] for r in rows)),'pool_support':dict(Counter(p for r in rows for p in r['pools']))}
save('liquidity_1623_summary.json',summary);print(json.dumps(summary))
