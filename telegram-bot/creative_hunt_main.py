"""
creative_hunt_main.py - Entry point for the Creative Hunt Telegram Bot.
Run with: python creative_hunt_main.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from creative_hunt_bot import build_creative_hunt_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Creative Hunt Bot…")
    app = build_creative_hunt_application()
    logger.info("Creative Hunt Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
