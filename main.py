import asyncio
import sys
import logging
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from core.settings import cfg
from tg.handlers import router
from trading.engine import trading_loop
from app import app

logging.basicConfig(level=logging.INFO)

async def start_dashboard():
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()

async def start_bot():
    if not cfg.tg_token:
        logging.error("No Telegram Token found! Bot will not start.")
        return
        
    bot = Bot(token=cfg.tg_token)
    dp = Dispatcher()
    dp.include_router(router)
    
    try:
        await dp.start_polling(bot)
    except TelegramNetworkError as e:
        logging.error(f"Telegram Network Error: {e}. Check your connection.")
    except TelegramRetryAfter as e:
        logging.error(f"Telegram Rate Limit. Retry after {e.retry_after} seconds.")
    except Exception as e:
        logging.error(f"Telegram Bot crashed: {e}")

async def main():
    tasks = [
        asyncio.create_task(start_dashboard()),
        asyncio.create_task(trading_loop(Bot(token=cfg.tg_token) if cfg.tg_token else None)),
        asyncio.create_task(start_bot())
    ]
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logging.info("Bot stopped.")
        sys.exit(0)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
