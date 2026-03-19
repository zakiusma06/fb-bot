"""
mod_main.py - Entry point for the Moderation Telegram Bot.
Run with: python mod_main.py
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from mod_bot import build_moderation_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Moderation Bot…")
    app = build_moderation_application()
    logger.info("Moderation Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
