"""Fixed-configuration regression replay, not a parameter optimization."""
import json
import hashlib
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.backtest import run_backtest, metrics
from trading.research import fingerprint


def main():
    root=Path(__file__).resolve().parents[1]
    report={'complete':False,'fingerprint':fingerprint(cfg),'symbols':{},
            'limitations':'Retrospective previously studied data. Fixed SL/TP core replay only, no live portfolio/news/book/funding/trailing/learning.'}
    trades=[]
    output=root/'data/audit_replay.json'
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','AVAXUSDT','LINKUSDT'):
        path=root/'data/research/candles'/f'{symbol}_{cfg.timeframe}.csv'
        raw=path.read_bytes()
        df=pd.read_csv(path)
        htf=pd.read_csv(path.with_name(f'{symbol}_{cfg.context_timeframe}.csv'))
        start=max(350,int(len(df)*.7))
        result=run_backtest(df,htf,cfg.context_timeframe,cfg,start_index=start)
        trades.extend(result['trade_log'])
        report['symbols'][symbol]={'metrics':result['all'],'open':result['open_positions'],'pending':result['pending_signals'],
            'start':int(df.timestamp.iloc[start]),'end':int(df.timestamp.iloc[-1]),'data_sha256':hashlib.sha256(raw).hexdigest(),'rejected':result['rejected']}
        report['pooled']=metrics(sorted(trades,key=lambda t:t['timestamp']))
        output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
        print(symbol,json.dumps(result['all']),flush=True)
    report['complete']=True
    output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print('TOTAL',json.dumps(report['pooled']),flush=True)


if __name__ == '__main__':
    main()
