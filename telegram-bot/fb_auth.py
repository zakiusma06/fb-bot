"""
fb_auth.py — Shared persistent Facebook authentication layer.

Authentication priority for every scraping session:
  1. Persistent browser profile  (fb_profile/)        — best longevity, survives restarts
  2. Saved storage state         (fb_auth_state.json)  — portable JSON snapshot
  3. FACEBOOK_COOKIES env var                          — emergency / initial seeding

Rolling refresh:
  After every successful scrape, call save_auth_state(context) so that cookies
  Facebook refreshed during the session are captured and reused next time.
  This means you only need to provide fresh FACEBOOK_COOKIES once after a full
  session expiry — from that point the rolling refresh keeps the session alive.
"""

import asyncio
import logging
import os
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from config import HEADLESS, FACEBOOK_COOKIES

logger = logging.getLogger(__name__)

# ── Storage paths ─────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
FB_PROFILE_DIR   = _HERE / "fb_profile"       # Playwright persistent context dir
FB_STORAGE_STATE = _HERE / "fb_auth_state.json"  # Portable storage-state file

# ── Browser / context settings ────────────────────────────────────────────
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1280,900",
]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_VIEWPORT    = {"width": 1280, "height": 900}
_LOCALE      = "en-US"
_TIMEZONE    = "America/New_York"

# ── Stealth (shared instance) ─────────────────────────────────────────────
try:
    from playwright_stealth import Stealth
    _stealth = Stealth()
    STEALTH_AVAILABLE = True
except ImportError:
    _stealth = None
    STEALTH_AVAILABLE = False


# ── Public helpers ────────────────────────────────────────────────────────

async def apply_stealth(page: Page) -> None:
    """Apply playwright-stealth patches to a page if available."""
    if STEALTH_AVAILABLE and _stealth:
        await _stealth.apply_stealth_async(page)


async def save_auth_state(context: BrowserContext) -> None:
    """
    Save the current browser storage state (cookies + localStorage) to disk.
    Call this after every successful scraping session so that Facebook-refreshed
    cookies are captured and reused on the next run.
    """
    try:
        FB_STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(FB_STORAGE_STATE))
        logger.info(f"[fb_auth] Auth state saved → {FB_STORAGE_STATE}")
    except Exception as e:
        logger.warning(f"[fb_auth] Could not save auth state: {e}")


def parse_cookie_string(cookie_str: str) -> list[dict]:
    """Parse a raw browser cookie string into Playwright cookie dicts."""
    cookies = []
    for part in cookie_str.strip().split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name  = name.strip()
            value = value.strip()
            if name and value:
                cookies.append({
                    "name":     name,
                    "value":    value,
                    "domain":   ".facebook.com",
                    "path":     "/",
                    "secure":   True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                })
    return cookies


async def build_playwright_context(p) -> tuple:
    """
    Build an authenticated Playwright (browser, context) pair.

    Returns (browser, context).
      • browser is None when a persistent profile context is used — in that case
        calling context.close() is sufficient to shut everything down.

    Auth priority:
      1. Persistent browser profile (fb_profile/ directory)
      2. Saved storage state file   (fb_auth_state.json)
      3. FACEBOOK_COOKIES env var   (emergency / initial seeding)
    """

    # ── 1. Persistent browser profile ────────────────────────────────────
    if FB_PROFILE_DIR.exists():
        logger.info(f"[fb_auth] Trying persistent browser profile: {FB_PROFILE_DIR}")
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(FB_PROFILE_DIR),
                headless=HEADLESS,
                args=_LAUNCH_ARGS,
                user_agent=_USER_AGENT,
                viewport=_VIEWPORT,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
            )
            logger.info("[fb_auth] ✓ Using persistent Facebook browser profile")
            return (None, context)
        except Exception as e:
            logger.warning(f"[fb_auth] Persistent profile failed: {e} — trying next method")

    # ── 2. Saved storage state ────────────────────────────────────────────
    if FB_STORAGE_STATE.exists():
        logger.info(f"[fb_auth] Trying saved storage state: {FB_STORAGE_STATE}")
        try:
            browser = await p.chromium.launch(headless=HEADLESS, args=_LAUNCH_ARGS)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport=_VIEWPORT,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
                storage_state=str(FB_STORAGE_STATE),
            )
            logger.info("[fb_auth] ✓ Using saved Facebook storage state")
            return (browser, context)
        except Exception as e:
            logger.warning(f"[fb_auth] Saved storage state failed: {e} — falling back to cookies")

    # ── 3. FACEBOOK_COOKIES env var (fallback) ────────────────────────────
    logger.info("[fb_auth] Using FACEBOOK_COOKIES env var (emergency / initial seeding)")
    browser = await p.chromium.launch(headless=HEADLESS, args=_LAUNCH_ARGS)
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=_VIEWPORT,
        locale=_LOCALE,
        timezone_id=_TIMEZONE,
    )
    if FACEBOOK_COOKIES:
        cookies = parse_cookie_string(FACEBOOK_COOKIES)
        if cookies:
            await context.add_cookies(cookies)
            logger.info(f"[fb_auth] Injected {len(cookies)} cookies from FACEBOOK_COOKIES")
        else:
            logger.warning("[fb_auth] FACEBOOK_COOKIES is set but could not be parsed")
    else:
        logger.warning(
            "[fb_auth] No Facebook auth available — "
            "FACEBOOK_COOKIES not set, no profile/state file found"
        )
    return (browser, context)


async def close_browser_context(browser, context: BrowserContext) -> None:
    """
    Cleanly close a (browser, context) pair returned by build_playwright_context.
    For persistent contexts browser is None — context.close() handles shutdown.
    """
    try:
        await context.close()
    except Exception:
        pass
    if browser:
        try:
            await browser.close()
        except Exception:
            pass
