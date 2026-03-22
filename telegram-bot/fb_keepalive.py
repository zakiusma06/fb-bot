"""
fb_keepalive.py — Keeps the Facebook session alive by visiting the Ads Library
every few hours. Run via pm2 or cron on the VPS.

Usage (run once, stays running):
    python fb_keepalive.py

It wakes up every 4 hours, opens a headless browser using the saved auth state,
visits facebook.com and the Ads Library, then saves the refreshed cookies back
to fb_auth_state.json so the session stays warm.
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_dir, "secrets.env")) or load_dotenv(os.path.join(_dir, ".env"))

from playwright.async_api import async_playwright
import fb_auth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

INTERVAL_HOURS = 1.5  # visit Facebook every 4 hours


async def ping_facebook() -> bool:
    """
    Open a browser with the saved auth state, visit Facebook + Ads Library,
    save the refreshed cookies, and close. Returns True on success.
    """
    logger.info("[keepalive] Starting Facebook session ping…")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=fb_auth._LAUNCH_ARGS,
            )
            context = await browser.new_context(
                user_agent=fb_auth._USER_AGENT,
                viewport=fb_auth._VIEWPORT,
                locale=fb_auth._LOCALE,
                timezone_id=fb_auth._TIMEZONE,
                storage_state=str(fb_auth.FB_STORAGE_STATE) if fb_auth.FB_STORAGE_STATE.exists() else None,
            )
            page = await context.new_page()
            await fb_auth.apply_stealth(page)

            # Step 1: visit facebook.com
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            url = page.url
            title = await page.title()

            if "login" in url.lower() or "Log in to Facebook" in title:
                logger.warning("[keepalive] ⚠️ Session expired — login wall detected. Need fresh cookies.")
                await browser.close()
                return False

            logger.info(f"[keepalive] facebook.com ✓ — {title[:50]}")

            # Step 2: visit Ads Library to keep that session warm too
            await page.goto(
                "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&media_type=all",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(3)
            logger.info("[keepalive] Ads Library ✓")

            # Step 3: save refreshed cookies
            await fb_auth.save_auth_state(context)
            await browser.close()

        logger.info("[keepalive] ✅ Session ping complete — cookies refreshed")
        return True

    except Exception as e:
        logger.error(f"[keepalive] ❌ Error during ping: {e}")
        return False


async def main():
    logger.info("=" * 50)
    logger.info(" Facebook Keep-Alive Service")
    logger.info(f" Interval: every {INTERVAL_HOURS} hours")
    logger.info("=" * 50)

    while True:
        ok = await ping_facebook()
        if not ok:
            logger.warning("[keepalive] Session dead — waiting 30 min before retry")
            await asyncio.sleep(30 * 60)
        else:
            logger.info(f"[keepalive] Next ping in {INTERVAL_HOURS} hours")
            await asyncio.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
