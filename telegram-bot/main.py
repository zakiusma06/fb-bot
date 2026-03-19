"""
main.py - Entry point for the Telegram bot.
Run with: python main.py
"""

import asyncio
import logging
import sys
import os

# Ensure the telegram-bot directory is on the path
sys.path.insert(0, os.path.dirname(__file__))

from bot import build_application

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Meta Ads Research Telegram Bot…")
    app = build_application()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    # Run the bot in polling mode (works in Replit)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
