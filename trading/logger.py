# trading/logger.py
"""Centralized logging configuration for the bot.
Provides a logger named 'bot' that writes INFO level messages to logs/bot.log with rotation.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

# Ensure logs directory exists
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_PATH = os.path.join(LOG_DIR, "bot.log")

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

def log_info(message: str):
    """Log an informational message.
    """
    logger.info(message)
