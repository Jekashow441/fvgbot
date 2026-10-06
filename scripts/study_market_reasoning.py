"""Two predeclared hypotheses, one frozen period; never edits live settings."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.backtest import run_backtest, metrics
from trading.quality import summarize_outcomes


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root/"data/research/candles"
    output = root/"data/market_reasoning_study.json"
    result = {"profiles_tested": ["balanced", "contextual"], "symbols": {},
              "limitations": "Retrospective, previously explored data. Core price-only strategy; no news/orderbook/funding/portfolio/trailing simulation. Future forward validation required."}
    pooled = {"balanced": [], "contextual": []}
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "AVAXUSDT", "LINKUSDT"]:
        path = directory/f"{symbol}_{cfg.timeframe}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        htf = pd.read_csv(directory/f"{symbol}_{cfg.context_timeframe}.csv")
        start_index = max(350, int(len(df)*.7))
        result["symbols"][symbol] = {"start_ms": int(df.timestamp.iloc[start_index]), "end_ms": int(df.timestamp.iloc[-1])}
        for profile in result["profiles_tested"]:
            settings = cfg.model_copy(deep=True)
            settings.strategy_profile = profile
            print(symbol, profile, "started", flush=True)
            report = run_backtest(df, htf, htf=cfg.context_timeframe, settings=settings, start_index=start_index)
            trades = report["trade_log"]
            pooled[profile].extend(trades)
            result["symbols"][symbol][profile] = {"metrics": report["all"], "open": report["open_positions"],
                "by_setup": {setup: metrics([t for t in trades if t["setup"] == setup]) for setup in set(t["setup"] for t in trades)},
                "rejected": report["rejected"]}
            print(symbol, profile, json.dumps(report["all"]), flush=True)
        result["pooled"] = {profile: {"metrics": metrics(sorted(rows, key=lambda t:t["timestamp"])),
            "target_check": summarize_outcomes([t["net_r"] for t in rows], cfg.target_winrate_pct, cfg.target_min_trades)} for profile, rows in pooled.items()}
        # The target summary's avg_pnl_pct is in R here; correct its label explicitly.
        for item in result["pooled"].values():
            item["target_check"]["avg_net_r"] = item["target_check"].pop("avg_pnl_pct")
        output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print("RESULT", json.dumps(result.get("pooled", {})), flush=True)


if __name__ == "__main__":
    main()
