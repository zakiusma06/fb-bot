"""
fb_login.py — One-time Facebook authentication helper.

Run this script from the Replit Shell to initialize or refresh the shared
Facebook auth state that all bots reuse automatically.

Usage
-----
  cd /home/runner/workspace/telegram-bot
  python fb_login.py

Two modes
---------
1. Seed from FACEBOOK_COOKIES env var (headless — works anywhere)
   • Set the FACEBOOK_COOKIES secret, then run this script.
   • Cookies are injected into a fresh browser, and the storage state is saved.
   • All subsequent bot runs load the saved state (no cookies needed again).

2. Interactive browser login (requires a display / VNC)
   • Run with --interactive flag to open a visible browser.
   • Log in to Facebook manually, then press Enter in the terminal.
   • The auth state is saved automatically.

After running successfully you will see:
  [fb_login] ✓ Auth state saved to fb_auth_state.json
"""

import asyncio
import logging
import os
import sys

# Allow running from project root or from telegram-bot/
sys.path.insert(0, os.path.dirname(__file__))

from playwright.async_api import async_playwright
import fb_auth
from config import FACEBOOK_COOKIES, HEADLESS

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

INTERACTIVE = "--interactive" in sys.argv


async def seed_from_cookies() -> bool:
    """
    Inject FACEBOOK_COOKIES into a fresh browser context, verify the session,
    and save the storage state to fb_auth_state.json.
    """
    if not FACEBOOK_COOKIES:
        logger.error("FACEBOOK_COOKIES env var is not set — cannot seed auth state")
        return False

    logger.info("Seeding Facebook auth state from FACEBOOK_COOKIES…")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=fb_auth._USER_AGENT,
            viewport=fb_auth._VIEWPORT,
            locale=fb_auth._LOCALE,
            timezone_id=fb_auth._TIMEZONE,
        )

        cookies = fb_auth.parse_cookie_string(FACEBOOK_COOKIES)
        if not cookies:
            logger.error("FACEBOOK_COOKIES could not be parsed — check the format")
            await browser.close()
            return False

        await context.add_cookies(cookies)
        logger.info(f"Injected {len(cookies)} cookies")

        # Warm up and verify
        page = await context.new_page()
        await fb_auth.apply_stealth(page)
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
            url   = page.url
            title = await page.title()
            if "login" in url.lower() or "Log in to Facebook" in title:
                logger.error(
                    "Session check FAILED — Facebook showed a login wall.\n"
                    "Your FACEBOOK_COOKIES may be expired. Please refresh them and try again."
                )
                await browser.close()
                return False
            logger.info(f"Session verified — URL: {url[:60]}")
        except Exception as e:
            logger.warning(f"Could not verify session: {e} — saving anyway")
        finally:
            await page.close()

        await fb_auth.save_auth_state(context)
        await browser.close()
        return True


async def interactive_login() -> bool:
    """
    Open a visible browser window, let the user log in manually, then save state.
    Requires a display (VNC or local machine).
    """
    logger.info("Launching interactive login browser (headless=False)…")
    logger.info("Log in to Facebook in the browser window, then press Enter here.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=fb_auth._USER_AGENT,
            viewport=fb_auth._VIEWPORT,
            locale=fb_auth._LOCALE,
            timezone_id=fb_auth._TIMEZONE,
        )

        page = await context.new_page()
        await fb_auth.apply_stealth(page)
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        logger.info("Browser is open. Log in, then come back here and press Enter…")
        await asyncio.get_event_loop().run_in_executor(None, input)

        await fb_auth.save_auth_state(context)
        logger.info("Auth state saved. You can close the browser.")
        await asyncio.sleep(2)
        await browser.close()
        return True


async def main():
    logger.info("=" * 55)
    logger.info(" Facebook Auth Initializer")
    logger.info("=" * 55)
    logger.info(f"  Auth state path : {fb_auth.FB_STORAGE_STATE}")
    logger.info(f"  Profile dir     : {fb_auth.FB_PROFILE_DIR}")
    logger.info(f"  Mode            : {'interactive' if INTERACTIVE else 'cookie seeding'}")
    logger.info("")

    if INTERACTIVE:
        ok = await interactive_login()
    else:
        ok = await seed_from_cookies()

    if ok:
        logger.info("")
        logger.info("✓ Success! All Facebook bots will now use the saved auth state.")
        logger.info(f"  File: {fb_auth.FB_STORAGE_STATE}")
        logger.info("  You only need to re-run this when the session fully expires.")
    else:
        logger.error("")
        logger.error("✗ Auth seeding failed. Check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
