"""Public-data research only: no Telegram, orders, production balance or settings writes."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.settings import cfg
from core.bybit import close_bybit_session, get_market_tickers
from trading.context import ticker_context
from trading.news import refresh_news, NEWS_STATE
from trading.research import research_coin, qualify


async def main(args):
    settings = cfg.model_copy(deep=True)
    settings.research_days = args.days
    try:
        await refresh_news()
        print("NEWS", json.dumps(NEWS_STATE), flush=True)
        symbols = args.symbols
        if args.all:
            market = ticker_context(await get_market_tickers(), cfg.min_turnover_24h, cfg.max_spread_bps)
            symbols = [s for s, row in sorted(market.items(), key=lambda x: x[1]["volume_rank"]) if row["eligible"]]
        for symbol in symbols:
            print("RESEARCH", symbol, settings.research_days, "days", flush=True)
            try:
                report = await research_coin(symbol, settings)
                print(symbol, json.dumps({"qualification": qualify(report, settings), "baseline": report["baseline"], "enhanced": report["enhanced"]}), flush=True)
            except Exception as exc:
                print(symbol, "ERROR", type(exc).__name__, str(exc), flush=True)
    finally:
        await close_bybit_session()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--days", type=int, choices=range(7, 366), default=90, metavar="7..365")
    parser.add_argument("--all", action="store_true", help="Sequentially research every currently liquid USDT contract")
    asyncio.run(main(parser.parse_args()))
