"""Offline frequency/quality comparison on fixed cached periods; no tuning or sending."""
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
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    results = {}
    directory = Path(__file__).resolve().parents[1]/"data/research/candles"
    for symbol in args.symbols:
        df = pd.read_csv(directory/f"{symbol}_{cfg.timeframe}.csv")
        htf = pd.read_csv(directory/f"{symbol}_60.csv")
        end = int(df.timestamp.iloc[-1])
        start = end-args.days*86400000
        # Keep pre-period warmup; metrics count entries only within the requested period.
        start_index = max(260, int(df.timestamp.searchsorted(start)))
        df = df.iloc[max(0, start_index-350):].reset_index(drop=True)
        start_index = int(df.timestamp.searchsorted(start))
        results[symbol] = {"period_start_ms": start, "period_end_ms": end, "profiles": {}}
        for profile in ("legacy", "balanced"):
            settings = cfg.model_copy(deep=True)
            settings.strategy_profile = profile
            print(symbol, profile, "started", flush=True)
            report = run_backtest(df, htf, settings=settings, start_index=start_index)
            results[symbol]["profiles"][profile] = {"metrics": report["all"], "last_third": report["last_third"],
                "open_positions": report["open_positions"], "rejected": report["rejected"]}
            print(symbol, profile, json.dumps(results[symbol]["profiles"][profile]), flush=True)
        output = directory.parent.parent/"profile_comparison.json"
        output.write_text(json.dumps({"days": args.days, "timeframe": cfg.timeframe, "results": results,
            "limitation": "Price-only fixed-stop replay; live news, orderbook, funding, research gate, portfolio limits and trailing excluded. No parameter tuning."}, indent=2), encoding="utf-8")
