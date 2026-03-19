"""
scheduler_main.py - Entry point for the Daily Research Scheduler Bot.
Run with: python scheduler_main.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from scheduler_bot import build_scheduler_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

TELEGRAM_SCHEDULER_BOT_TOKEN = os.environ.get("TELEGRAM_SCHEDULER_BOT_TOKEN", "")


def main() -> None:
    if not TELEGRAM_SCHEDULER_BOT_TOKEN:
        logger.error(
            "[scheduler_main] TELEGRAM_SCHEDULER_BOT_TOKEN is not set. "
            "Create a new bot via @BotFather and add the token as an environment secret."
        )
        return

    logger.info("Starting Daily Research Scheduler Bot…")
    app = build_scheduler_application(TELEGRAM_SCHEDULER_BOT_TOKEN)
    logger.info("Scheduler Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
