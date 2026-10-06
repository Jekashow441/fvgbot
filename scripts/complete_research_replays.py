"""Add available seeded replacements; retain missing original IDs as UNKNOWN."""
from critical_rejection_research import *
replays=json.loads((OUT/'causal_replays.json').read_text(encoding='utf8'))
cards=json.loads((OUT/'cohort_cards.json').read_text(encoding='utf8'))
have={r['setup_id'] for r in replays}
count=sum(bool(r['events']) and r['selection'].startswith('seeded_random') for r in replays)
candidates=[c for c in cards if c['coverage']=='AVAILABLE' and c['setup_id'] not in have]
random.Random(6311).shuffle(candidates)
for card in candidates:
    if count>=20:break
    d=frame(card['asset']);z=card['fvg'];end=max(o['known_through_open'] for o in card['observations']);events=[]
    for t in d[(d.timestamp>=z['formed_at'])&(d.timestamp<=end)].timestamp:
        p=d[d.timestamp<=t].tail(350).reset_index(drop=True);r=reactions(p,z);why=invalidation_reason(z,p,'5',int(t)+300000,True)
        checks={'fvg':True,'displacement':z.get('displacement_atr',0)>=CFG.displacement_atr,'liquidity':bool(liquidity(p,z['type']=='BULLISH')),'reaction':r['touch'] and r['modes']['A'],'htf':None}
        events.append({'decision_close_ms':int(t)+300000,'known_through_open_ms':int(t),'reaction':r,'status':research_status(checks,bool(why)),'blocker':why,'unavailable':['historical orderbook','news','exact runtime market context']})
    if events:
        replays.append({'setup_id':card['setup_id'],'selection':'seeded_random_replacement','events':events});count+=1
for replay in replays:
    card=next(c for c in cards if c['setup_id']==replay['setup_id']);z=card['fvg'];bull=z['type']=='BULLISH'
    for event in replay['events']:
        p=known(card['asset'],'5',event['decision_close_ms']);reaction=event['reaction']
        checks={'fvg':True,'displacement':z.get('displacement_atr',0)>=CFG.displacement_atr,'liquidity':bool(liquidity(p,bull)),'structure':reaction['structure']['direction']==('UP' if bull else 'DOWN'),'reaction':reaction['touch'] and reaction['modes']['A'],'htf':None}
        event['current']=checks;event['missing']=[k for k,v in checks.items() if v is not True]
        event['next_trigger']='Wait for '+', '.join(event['missing']) if event['missing'] else 'Validate execution price, costs, book and news'
        event['invalidation']={'full_fill_price':z['bottom'] if bull else z['top'],'closed_ce':z['ce'],'strict_ce':True}
        event['status']=research_status(checks,bool(event['blocker']))
save('causal_replays.json',replays)
# Descriptive excursions after the first recorded technical decision (or first
# available research reaction). Never feed this back into candidate selection.
rows=[]
for r in replays:
    if not r['events']:continue
    card=next(c for c in cards if c['setup_id']==r['setup_id']);d=frame(card['asset']);z=card['fvg']
    ev=next((e for e in r['events'] if e['reaction']['touch'] and e['reaction']['modes']['A'] and not e['blocker']),r['events'][0]);t=ev['known_through_open_ms'];price=float(d.loc[d.timestamp==t,'close'].iloc[0]);future=d[d.timestamp>t].head(12)
    bull=z['type']=='BULLISH'
    mfe=float((future.high.max()/price-1)*100 if bull else (1-future.low.min()/price)*100) if len(future) else None
    mae=float((1-future.low.min()/price)*100 if bull else (future.high.max()/price-1)*100) if len(future) else None
    rows.append({'setup_id':r['setup_id'],'reference_open_ms':t,'future_bars':len(future),'favorable_excursion_pct':mfe,'adverse_excursion_pct':mae,'not_profitability':True,'selection':r['selection']})
save('missed_opportunity_excursions.json',rows)
result=json.loads((OUT/'research.json').read_text(encoding='utf8'))
result['cohort'].update(replay_count=len(replays),random_replay_count=count,replays_without_bars=[r['setup_id'] for r in replays if not r['events']])
save('research.json',result);print(json.dumps({'total':len(replays),'random_covered':count,'missing':result['cohort']['replays_without_bars']}))
