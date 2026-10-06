"""Evaluate local closed-candle CSVs without accessing production DB or Telegram."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.backtest import run_backtest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="timestamp (milliseconds),open,high,low,close,volume")
    parser.add_argument("--htf-csv")
    parser.add_argument("--timeframe", default="15")
    parser.add_argument("--htf", default="60")
    parser.add_argument("--output", default="data/evaluation_v2.json")
    args = parser.parse_args()
    cfg.timeframe = args.timeframe
    report = run_backtest(pd.read_csv(args.csv), pd.read_csv(args.htf_csv) if args.htf_csv else None, args.htf)
    Path(args.output).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, indent=2, allow_nan=False))
