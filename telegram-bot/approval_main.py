"""
approval_main.py - Entry point for the Approval Bot.

Run with: python approval_main.py

Replaces mod_main.py (Moderation Bot) and pricing_main.py (Pricing Bot).
Uses TELEGRAM_MODERATION_BOT_TOKEN.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from approval_bot import build_approval_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting Approval Bot…")
    app = build_approval_application()
    logger.info("Approval Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
