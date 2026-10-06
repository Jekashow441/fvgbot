"""Build read-only candle-by-candle setup replays and debug charts for V6.3."""
import json, sys
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'data'/'audits'/'deep_logic_v6.3'; CH=OUT/'debug_charts'; CH.mkdir(parents=True,exist_ok=True)
R=json.loads((ROOT/'data/audits/night_20260910/night_report.json').read_text(encoding='utf8'))
unique={}
for e in R['technical_entry_observations']: unique[e['setup_id']]=e
replays=[]
chart_candidates=[]
for sid,e in sorted(unique.items()):
 p=e['payload']; asset=p['asset']; f=ROOT/'data/audits/night_20260910'/f'replay_{asset}_5.json'
 if not f.exists(): continue
 df=pd.read_json(f,convert_dates=False,precise_float=True).sort_values('timestamp').reset_index(drop=True); df['timestamp']=pd.to_numeric(df['timestamp'])
 z=p['fvg']; idx=(df.timestamp==z['candle_time']).to_numpy().nonzero()[0]
 if len(idx)==0: continue
 fi=int(idx[-1]); analysis=int(p.get('assessment',{}).get('analysis_bar') or e['observed_at']*1000); oi=df.index[df.timestamp<=analysis]; oi=int(oi[-1]) if len(oi) else fi
 start=max(0,fi-20); end=max(fi,min(oi,fi+20)); known=df.iloc[start:end+1]
 future=df.iloc[oi+1:oi+13]
 events=[{'event':'FVG_CREATED','timestamp':int(z['formed_at']),'known_candles_through':int(z['formed_at'])},{'event':'ANALYSIS_OBSERVATION','timestamp':analysis,'known_candles_through':int(known.timestamp.iloc[-1])},{'event':'DECISION','status':e['status'],'blockers':e['reason'].split('; '),'timestamp':analysis}]
 replays.append({'setup_id':sid,'asset':asset,'direction':p['direction'],'timeframe':'5','fvg':z,'structure':p.get('market_structure'),'score':p.get('assessment',{}).get('score'),'known_window':{'start_index':start,'end_index':end,'bars':known[['timestamp','open','high','low','close','volume']].to_dict('records')},'events':events,'future_descriptive_12_bars':future[['timestamp','open','high','low','close','volume']].to_dict('records')})
 chart_candidates.append((sid,e,known,fi-start,oi-start))
 # charts are rendered below after ranking, so five technical-entry candidates and five terminally rejected examples are visible.
 if False:
  fig,ax=plt.subplots(figsize=(12,6)); vals=known.reset_index(drop=True)
  for i,row in vals.iterrows():
   color='#16a34a' if row.close>=row.open else '#dc2626'; ax.vlines(i,row.low,row.high,color=color,linewidth=.8); ax.add_patch(Rectangle((i-.32,min(row.open,row.close)),.64,abs(row.close-row.open),facecolor=color,alpha=.75))
  ax.axhspan(float(z['bottom']),float(z['top']),color='#f59e0b',alpha=.22,label='FVG'); ax.axhline(float(z['ce']),color='#b45309',linestyle='--',label='CE')
  st=p.get('market_structure') or {}; 
  for key,col in [('swing_high','#7c3aed'),('swing_low','#2563eb')]:
   if st.get(key) is not None: ax.axhline(float(st[key]),color=col,linestyle=':',label=key)
  ax.axvline(fi-start,color='#111827',linestyle='--',label='FVG candle'); ax.axvline(oi-start,color='#059669',linestyle='--',label='decision candle')
  ax.set_title(f'{asset} {p["direction"]} — causal setup replay (future not used for decision)'); ax.set_xlabel('bars in T-20…decision window'); ax.set_ylabel('price'); ax.legend(loc='best',ncol=3); fig.tight_layout(); fig.savefig(CH/f'{sid}.png',dpi=140); plt.close(fig)
(OUT/'setup_replays.json').write_text(json.dumps({'scope':'Causal known-prefix replay; future bars are descriptive only and were not used in the decision.','replays':replays},ensure_ascii=False,indent=2),encoding='utf8')
# Five candidates that reached technical Entry and five with explicit terminal invalidation/expiry.
accepted=sorted(chart_candidates,key=lambda x:(x[1]['payload'].get('assessment',{}).get('score') or 0),reverse=True)[:5]
rejected=[x for x in chart_candidates if x not in accepted]
rejected=sorted(rejected,key=lambda x:x[1]['payload'].get('last_status',''))[:5]
for label,items in [('technical_entry',accepted),('terminal_rejected',rejected)]:
 for sid,e,known,fi_rel,oi_rel in items:
  p=e['payload']; z=p['fvg']; vals=known.reset_index(drop=True); fig,ax=plt.subplots(figsize=(12,6))
  for i,row in vals.iterrows():
   color='#16a34a' if row.close>=row.open else '#dc2626'; ax.vlines(i,row.low,row.high,color=color,linewidth=.8); ax.add_patch(Rectangle((i-.32,min(row.open,row.close)),.64,abs(row.close-row.open),facecolor=color,alpha=.75))
  ax.axhspan(float(z['bottom']),float(z['top']),color='#f59e0b',alpha=.22,label='FVG'); ax.axhline(float(z['ce']),color='#b45309',linestyle='--',label='CE')
  st=p.get('market_structure') or {}
  for key,col in [('swing_high','#7c3aed'),('swing_low','#2563eb')]:
   if st.get(key) is not None: ax.axhline(float(st[key]),color=col,linestyle=':',label=key)
  ax.axvline(fi_rel,color='#111827',linestyle='--',label='FVG candle'); ax.axvline(oi_rel,color='#059669',linestyle='--',label='decision candle')
  ax.set_title(f'{p["asset"]} {p["direction"]} — {label} causal reconstruction'); ax.set_xlabel('bars in T-20…decision window'); ax.set_ylabel('price'); ax.legend(loc='best',ncol=3); fig.tight_layout(); fig.savefig(CH/f'{label}_{sid}.png',dpi=140); plt.close(fig)
print(json.dumps({'replays':len(replays),'charts':len(list(CH.glob('*.png'))),'technical_entry_charts':len(accepted),'terminal_rejected_charts':len(rejected)}))
