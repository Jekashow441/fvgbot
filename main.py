import asyncio
import logging
import sys
from typing import Optional

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from core.settings import cfg, reload_settings
from core.bybit import close_bybit_session
from tg.handlers import router
from trading.engine import trading_loop
from app import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def start_dashboard() -> None:
    reload_settings()
    config = uvicorn.Config(app, host=cfg.dashboard_host, port=cfg.dashboard_port, log_level="info",ws="websockets-sansio")
    server = uvicorn.Server(config)
    await server.serve()


async def start_bot(bot: Optional[Bot]) -> None:
    if not bot:
        logging.error("No Telegram token found. Telegram bot polling will not start.")
        return
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    except TelegramNetworkError as e:
        logging.error("Telegram network error: %s", e)
    except TelegramRetryAfter as e:
        logging.error("Telegram rate limit. Retry after %s seconds.", e.retry_after)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("Telegram bot crashed: %s", e)


async def main() -> None:
    from core.database import init_db
    init_db()
    reload_settings()
    from trading.paper_account import balance
    cfg.paper_balance=balance(cfg.paper_balance)
    bot = Bot(token=cfg.tg_token) if cfg.tg_token else None
    from trading.setup_visibility import visibility_worker
    tasks = [
        asyncio.create_task(visibility_worker(bot), name="setup_visibility"),
        asyncio.create_task(start_dashboard(), name="dashboard"),
        asyncio.create_task(trading_loop(bot), name="trading_loop"),
        asyncio.create_task(start_bot(bot), name="telegram_bot"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await close_bybit_session()
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutdown complete.")
