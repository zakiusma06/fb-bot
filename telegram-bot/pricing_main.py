"""
pricing_main.py - Entry point for the Pricing Telegram Bot.
Run with: python pricing_main.py
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from pricing_bot import build_pricing_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Pricing Bot…")
    app = build_pricing_application()
    logger.info("Pricing Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
