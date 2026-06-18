import asyncio
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.settings import cfg
from core.bybit import get_klines_async
from trading.strategy import detect_fvg, validate_signal
from trading.paper_trading import open_paper_trade, check_active_trades
from trading.risk_manager import is_trading_allowed
from trading.logger import log_info

# In-memory store for UI to fetch latest data
LATEST_DATA = {}

# Store for tracking sent signals to avoid spam
SENT_SIGNALS = {}

async def process_symbol(bot: Bot, symbol: str):
    """
    Process a single symbol for trading signals.
    """
    try:
        # Fetch data asynchronously
        df = await get_klines_async(symbol, cfg.timeframe, limit=cfg.fvg_lookback + 50)
        if df.empty:
            return
            
        current_price = float(df.iloc[-1]['close'])
        current_high = float(df.iloc[-1]['high'])
        current_low = float(df.iloc[-1]['low'])
        
        # Check paper trades
        await check_active_trades(bot, symbol, current_high, current_low)
            
        # Find FVG
        fvg = detect_fvg(df)
        signal = None
        
        if fvg and is_trading_allowed():
            # Check if mitigated / valid signal generated
            signal = validate_signal(df, fvg)
            
            if signal:
                # Format Time
                fvg_time = datetime.fromtimestamp(fvg['candle_time'] / 1000, tz=timezone.utc).strftime('%H:%M:%S (UTC)')
                
                # Generate text for telegram
                msg = (
                    f"🚨 <b>FVG {signal['signal']} SIGNAL</b> 🚨\n"
                    f"<b>Pair:</b> {symbol}\n"
                    f"<b>Timeframe:</b> {cfg.timeframe}m\n"
                    f"<b>Time:</b> {fvg_time}\n\n"
                    f"<b>Entry:</b> {signal['entry']:.4f}\n"
                    f"<b>Stop Loss:</b> {signal['sl']:.4f}\n"
                    f"<b>Take Profit:</b> {signal['tp']:.4f}\n"
                    f"<b>R:R:</b> 1:{signal['rr']:.2f}\n"
                )
                
                # Create Preview Button
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖼 Preview Chart", callback_data=f"preview:{symbol}")]
                ])
                
                signal_id = f"{symbol}_{fvg['candle_time']}"
                
                if symbol not in SENT_SIGNALS:
                    SENT_SIGNALS[symbol] = set()
                    
                if signal_id not in SENT_SIGNALS[symbol]:
                    if cfg.tg_chat_id:
                        try:
                            await bot.send_message(cfg.tg_chat_id, msg, parse_mode="HTML", reply_markup=kb)
                        except Exception as e:
                            log_info(f"Failed to send TG message: {e}")
                            
                    SENT_SIGNALS[symbol].add(signal_id)
                    open_paper_trade(symbol, signal)
        
        # Update global state for FastAPI dashboard
        if symbol not in LATEST_DATA:
            LATEST_DATA[symbol] = {}
        LATEST_DATA[symbol]["fvg"] = fvg
        LATEST_DATA[symbol]["signal"] = signal
        LATEST_DATA[symbol]["current_price"] = current_price

    except Exception as e:
        log_info(f"Error processing {symbol}: {e}")

async def trading_loop(bot: Bot):
    """
    Main loop that periodically checks all symbols for FVG setups and sends signals.
    """
    log_info("Trading loop started.")
    while True:
        if not cfg.is_running:
            await asyncio.sleep(5)
            continue
            
        # Process all symbols concurrently using asyncio.gather
        tasks = [process_symbol(bot, symbol) for symbol in cfg.symbols]
        await asyncio.gather(*tasks)
            
        await asyncio.sleep(15) # Check every 15 seconds
