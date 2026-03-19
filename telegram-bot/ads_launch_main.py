"""
ads_launch_main.py - Entry point for the Ads Launch Bot.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    token = os.environ.get("TELEGRAM_ADS_LAUNCH_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_ADS_LAUNCH_BOT_TOKEN is not set — cannot start Ads Launch Bot")
        sys.exit(1)

    import ads_launch_sheet as sheet

    logger.info("Migrating READY FOR ADS rows (old format → SHEET_COLUMNS)…")
    try:
        sheet.migrate_ready_for_ads()
    except Exception as e:
        logger.warning(f"READY FOR ADS migration failed (non-fatal): {e}")

    logger.info("Syncing all sheet tab headers…")
    try:
        sheet.ensure_all_tabs()
    except Exception as e:
        logger.warning(f"Sheet header sync failed (non-fatal): {e}")

    from ads_launch_bot import build_app
    app = build_app(token)
    logger.info("Ads Launch Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
