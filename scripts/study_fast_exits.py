"""Fixed fast-exit hypothesis; retrospective research, no live setting writes."""
import json
import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.backtest import run_backtest, metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hypothesis', choices=['fast', 'delayed'], default='fast')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = root/'data/research/candles'
    output = root/('data/fast_exit_study.json' if args.hypothesis == 'fast' else 'data/delayed_retest_study.json')
    variants = ('balanced', 'fast_30m' if args.hypothesis == 'fast' else 'retest_5bars')
    pooled = {name: [] for name in variants}
    description = 'Same balanced FVG entries, fixed 1.25R target, maximum 30 minutes, unchanged cost/risk/score gates.' if args.hypothesis == 'fast' else 'Balanced with five-bar retest confirmation window; unchanged distance, cost, risk, volume and score gates.'
    report = {'complete': False, 'hypothesis': description,
              'limitations': 'Previously explored historical data; no independent holdout, live news, book, funding, portfolio or trailing. Never auto-promote.',
              'symbols': {}, 'variants': list(variants)}
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','AVAXUSDT','LINKUSDT'):
        df = pd.read_csv(directory/f'{symbol}_{cfg.timeframe}.csv')
        htf = pd.read_csv(directory/f'{symbol}_{cfg.context_timeframe}.csv')
        report['symbols'][symbol] = {'data': {'start_ms':int(df.timestamp.iloc[max(350,int(len(df)*.7))]),
            'end_ms':int(df.timestamp.iloc[-1]),'sha256':hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()}}
        for variant in variants:
            settings = cfg.model_copy(deep=True)
            settings.strategy_profile = 'balanced'
            fast = variant == 'fast_30m'
            if fast:
                settings.dynamic_rr = False
                settings.risk_reward = 1.25
                settings.rr_min = 1.0
            if variant == 'retest_5bars':
                settings.retest_window_bars = 5
            result = run_backtest(df, htf, htf=cfg.context_timeframe, settings=settings,
                                  start_index=max(350,int(len(df)*.7)),
                                  max_hold_bars=max(1,30//int(cfg.timeframe)) if fast else 0)
            trades = result['trade_log']
            pooled[variant].extend(trades)
            summary = {'metrics':result['all'],'exit_reasons':dict(Counter(t['exit_reason'] for t in trades)),
                       'average_minutes':sum(t['held_bars']*int(cfg.timeframe) for t in trades)/len(trades) if trades else None,
                       'rejected':result['rejected'],'open_positions':result['open_positions'],
                       'pending_signals':result['pending_signals']}
            report['symbols'][symbol][variant] = summary
            print(symbol, variant, json.dumps(summary['metrics']), flush=True)
        report['pooled'] = {name:metrics(sorted(trades,key=lambda t:t['timestamp'])) for name,trades in pooled.items()}
        output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    report['complete'] = True
    output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print('RESULT',json.dumps(report['pooled']),flush=True)


if __name__ == '__main__':
    main()
