"""Re-identify ALL old expanded-high-quality zones, including production zones."""
from critical_rejection_research import *
from trading.strategy import _calc_atr
from cohort_deep_research import ref_fvgs,zone_features
rows=[]
for path in sorted((BASE/'input_snapshot').glob('*_5.csv')):
    d=pd.read_csv(path).sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
    w=d.tail(max(30,int(cfg.fvg_lookback)+int(cfg.fvg_confirmations)+5)).reset_index(drop=True)
    for z in ref_fvgs(w):
        old=zone_features(w,z)
        if not old['expanded_liquidity_sweep'] or old['category'] not in ('A','B'):continue
        k=int(np.flatnonzero(d.timestamp.to_numpy()==z['candle_time'])[-1]);p=d.iloc[max(0,k-350):k+2].reset_index(drop=True);bull=z['type']=='BULLISH';asset=path.stem[:-2]
        sweep=liquidity(p.iloc[:-1],bull);st=structure_context(p,CFG.structure_pivot);atr=_calc_atr(p).iloc[-3];body=abs(p.close.iloc[-2]-p.open.iloc[-2]);h=known(asset,'60',int(p.timestamp.iloc[-1])+300000);htf=market_context(h,CFG)['trend'] if len(h)>=60 else 'UNKNOWN'
        checks={'fvg':True,'typed_liquidity':bool(sweep),'displacement':bool(np.isfinite(atr) and body>=CFG.displacement_atr*atr),'structure':st['direction']==('UP' if bull else 'DOWN'),'htf':None if htf=='UNKNOWN' else htf==('UP' if bull else 'DOWN')}
        rows.append({'asset':asset,'candle_time':z['candle_time'],'old_category':old['category'],'checks':checks,'pools':sweep,'htf':htf})
save('liquidity_362.json',rows)
result={'count':len(rows),'typed_sweep':sum(r['checks']['typed_liquidity'] for r in rows),'sweep_displacement_structure':sum(all(r['checks'][k] for k in ('typed_liquidity','displacement','structure')) for r in rows),'including_known_htf':sum(all(v is True for v in r['checks'].values()) for r in rows),'htf_unknown':sum(r['checks']['htf'] is None for r in rows)}
save('liquidity_362_summary.json',result);print(json.dumps(result))
