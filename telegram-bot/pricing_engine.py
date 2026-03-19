"""
pricing_engine.py - Source products from fatkun.net (1688 image search proxy)
and calculate GNF pricing.

Pipeline:
  1. Take the best OG image URL from the cluster
  2. Feed it to fatkun.net/image-search (paste URL → Search Similar Items)
  3. fatkun.net searches 1688 on our behalf — no 1688 cookies required
  4. Extract supplier price (CNY) + the 1688 product listing URL
  5. Get live exchange rates (CNY→USD, USD→GNF), fall back to config
  6. Apply formula: PRICE = (supplier_usd + SHIPPING + MARGIN) × gnf_rate
                   COMPARE AT = PRICE + COMPARE_AT_EXTRA_GNF (fixed)
  7. Round to nearest ROUND_TO_GNF increment

Returns (price_str, compare_at_str, supplier_url).
All three are empty strings when pricing cannot be determined reliably.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Optional

import httpx

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from config import (
    HEADLESS,
    SHIPPING_AGENT_USD,
    EXTRA_MARGIN_USD,
    COMPARE_AT_EXTRA_GNF,
    USD_TO_GNF,
    ROUND_TO_GNF,
)

logger = logging.getLogger(__name__)

# Fallback CNY/USD rate (1 CNY ≈ 0.138 USD  ↔  7.25 CNY per USD)
_FALLBACK_CNY_TO_USD: float = 0.138

# Sanity range: reject prices outside this CNY band
# Min ¥5 (~$0.70) — anything below is UI text / accessory noise, not a real product
_MIN_PRICE_CNY: float = 5.0
_MAX_PRICE_CNY: float = 5_000.0

# Cached live rates to avoid repeated network hits within a single run
_rate_cache: dict[str, float] = {}

FATKUN_SEARCH_URL = "https://www.fatkun.net/image-search"


# ── Public API ───────────────────────────────────────────────────────────────

async def get_pricing_for_cluster(cluster) -> tuple[str, str, str, str, str]:
    """
    Returns (price_gnf_str, compare_at_price_gnf_str, supplier_url, weight_kg_str, weight_source_str).
    weight_source_str is one of: "1688", "Amazon", "Product Page", or "" when no weight found.
    All values are empty strings when pricing cannot be determined reliably.
    """
    product_name: str = (cluster.canonical_name or "").strip()

    # ── Collect product-page images only (no Facebook creatives) ─────────────
    #
    # Tier 1 (preferred): images extracted from the actual product page HTML
    #          OG image → JSON-LD → Shopify CDN → large <img> tags
    # Tier 2 (fallback): Playwright screenshot of the landing page
    #
    # Facebook thumbnails / ad creatives are NEVER used — they often contain
    # text overlays and branding that confuse the image search.
    seen_images: set[str] = set()
    product_page_images: list[str] = []

    def _add_to(lst: list, url: str):
        url = (url or "").strip()
        if url and url not in seen_images:
            seen_images.add(url)
            lst.append(url)

    # Product page images (from _product_images, OG tag, etc.)
    for ad in cluster.ads:
        for url in (ad.get("_product_images") or []):
            _add_to(product_page_images, url)
        # og_image_url is the raw OG tag value — add if not already in _product_images
        _add_to(product_page_images, ad.get("og_image_url") or "")

    # Landing page URL for screenshot fallback
    landing_url: str = ""
    for ad in cluster.ads:
        lp = (ad.get("landing_page_url") or "").strip()
        if lp:
            landing_url = lp
            break

    if not product_page_images and not landing_url:
        logger.warning(
            f"[pricing] No product-page image and no landing URL for '{product_name[:50]}' — "
            "cannot do image search"
        )
        return ("", "", "", "", "")

    logger.info(
        f"[pricing] '{product_name[:40]}' — "
        f"{len(product_page_images)} product-page image(s), "
        f"{'screenshot fallback available' if landing_url else 'no screenshot fallback'}"
    )

    supplier_price_cny: Optional[float] = None
    supplier_url: str = ""

    # ── Step 1: try product-page images (Tier 1) ─────────────────────────────
    for img_url in product_page_images[:3]:
        logger.info(f"[pricing] trying product-page image: {img_url[:70]}")
        price_cny, found_url = await _image_search_fatkun(img_url, product_name)
        if price_cny:
            supplier_price_cny = price_cny
            supplier_url = found_url
            logger.info(
                f"[pricing] fatkun match (product-page image) '{product_name[:50]}': "
                f"{supplier_price_cny:.2f} CNY | {supplier_url[:80]}"
            )
            break
        logger.info(f"[pricing] product-page image gave no result — trying next")

    # ── Step 2: screenshot fallback if product-page images failed ─────────────
    if not supplier_price_cny and landing_url:
        logger.info(
            f"[pricing] all product-page images failed — "
            f"taking screenshot of {landing_url[:60]}"
        )
        screenshot_path = await _take_product_page_screenshot(landing_url, product_name)
        if screenshot_path:
            try:
                price_cny, found_url = await _image_search_fatkun(
                    screenshot_path, product_name, is_local_path=True
                )
                if price_cny:
                    supplier_price_cny = price_cny
                    supplier_url = found_url
                    logger.info(
                        f"[pricing] fatkun match (screenshot) '{product_name[:50]}': "
                        f"{supplier_price_cny:.2f} CNY | {supplier_url[:80]}"
                    )
            finally:
                try:
                    os.remove(screenshot_path)
                except Exception:
                    pass
        else:
            logger.info(f"[pricing] screenshot also failed")

    if not supplier_price_cny:
        logger.warning(
            f"[pricing] No reliable price found for '{product_name[:50]}' "
            "— leaving PRICE / COMPARE AT PRICE empty"
        )
        return ("", "", "", "", "")

    # ── Exchange rates ────────────────────────────────────────────────────────
    cny_to_usd = await _get_cny_to_usd()
    usd_to_gnf  = await _get_usd_to_gnf()

    supplier_price_usd: float = supplier_price_cny * cny_to_usd

    # Weight extraction is disabled — WEIGHT GRAM left empty.
    weight_shipping_usd = 0.0

    # ── Pricing formula ───────────────────────────────────────────────────────
    # PRICE = (supplier + fixed_agent_fee + margin) × GNF_rate
    price_gnf      = (supplier_price_usd + SHIPPING_AGENT_USD + weight_shipping_usd + EXTRA_MARGIN_USD) * usd_to_gnf
    compare_at_gnf = price_gnf + COMPARE_AT_EXTRA_GNF

    price_rounded      = _round_gnf(price_gnf)
    compare_at_rounded = _round_gnf(compare_at_gnf)

    logger.info(
        f"[pricing] '{product_name[:40]}' | "
        f"supplier {supplier_price_cny:.2f} CNY = {supplier_price_usd:.2f} USD | "
        f"agent ${SHIPPING_AGENT_USD} | margin ${EXTRA_MARGIN_USD} | "
        f"1 USD = {usd_to_gnf:.0f} GNF | "
        f"PRICE = {price_rounded:,} GNF | COMPARE AT = {compare_at_rounded:,} GNF | "
        f"source = {supplier_url[:80]}"
    )

    return (str(price_rounded), str(compare_at_rounded), supplier_url, "", "")


async def get_sourcing_for_cluster(cluster) -> tuple[str, str, str]:
    """
    Lightweight sourcing lookup: Fatkun image search + 1688 weight extraction.
    Returns (price_usd_str, supplier_url, weight_gram_str).
    All three are empty strings when sourcing cannot be determined.

    Uses the same image-collection and Fatkun logic as get_pricing_for_cluster,
    but skips GNF pricing and Shopify fields.
    """
    product_name: str = (cluster.canonical_name or "").strip()

    seen_images: set[str] = set()
    product_page_images: list[str] = []

    def _add_to(lst: list, url: str):
        url = (url or "").strip()
        if url and url not in seen_images:
            seen_images.add(url)
            lst.append(url)

    for ad in cluster.ads:
        for url in (ad.get("_product_images") or []):
            _add_to(product_page_images, url)
        _add_to(product_page_images, ad.get("og_image_url") or "")

    landing_url: str = ""
    for ad in cluster.ads:
        lp = (ad.get("landing_page_url") or "").strip()
        if lp:
            landing_url = lp
            break

    if not product_page_images and not landing_url:
        logger.warning(
            f"[sourcing] No product-page image and no landing URL for '{product_name[:50]}' — skipping"
        )
        return ("", "", "")

    supplier_price_cny: Optional[float] = None
    supplier_url: str = ""

    for img_url in product_page_images[:3]:
        logger.info(f"[sourcing] trying product-page image: {img_url[:70]}")
        price_cny, found_url = await _image_search_fatkun(img_url, product_name)
        if price_cny:
            supplier_price_cny = price_cny
            supplier_url = found_url
            logger.info(
                f"[sourcing] fatkun match (product-page image) '{product_name[:50]}': "
                f"{supplier_price_cny:.2f} CNY | {supplier_url[:80]}"
            )
            break
        logger.info(f"[sourcing] product-page image gave no result — trying next")

    if not supplier_price_cny and landing_url:
        logger.info(f"[sourcing] all product-page images failed — taking screenshot of {landing_url[:60]}")
        screenshot_path = await _take_product_page_screenshot(landing_url, product_name)
        if screenshot_path:
            try:
                price_cny, found_url = await _image_search_fatkun(
                    screenshot_path, product_name, is_local_path=True
                )
                if price_cny:
                    supplier_price_cny = price_cny
                    supplier_url = found_url
                    logger.info(
                        f"[sourcing] fatkun match (screenshot) '{product_name[:50]}': "
                        f"{supplier_price_cny:.2f} CNY | {supplier_url[:80]}"
                    )
            finally:
                try:
                    os.remove(screenshot_path)
                except Exception:
                    pass

    if not supplier_price_cny:
        logger.warning(f"[sourcing] No price found for '{product_name[:50]}' — leaving empty")
        return ("", "", "")

    cny_to_usd = await _get_cny_to_usd()
    price_usd = supplier_price_cny * cny_to_usd
    price_usd_str = f"{price_usd:.2f}"
    logger.info(
        f"[sourcing] '{product_name[:40]}' | "
        f"{supplier_price_cny:.2f} CNY = {price_usd_str} USD | {supplier_url[:80]}"
    )

    # ── Weight extraction from 1688 product page ─────────────────────────────
    weight_gram_str = ""
    if supplier_url and "1688.com" in supplier_url:
        try:
            weight_gram_str = await asyncio.wait_for(
                _extract_weight_from_1688(supplier_url), timeout=30
            )
            if weight_gram_str:
                logger.info(f"[sourcing] weight from 1688: {weight_gram_str}g")
        except Exception as e:
            logger.warning(f"[sourcing] weight extraction failed: {e}")

    return (price_usd_str, supplier_url, weight_gram_str)


# ── 1688 weight extraction ────────────────────────────────────────────────────

async def _extract_weight_from_1688(url: str) -> str:
    """
    Visit a 1688.com product page and extract the weight in grams from the
    Packing table (重量(g) column).  Returns a string like "500" or "" if not found.
    Uses httpx (no browser) — fast and avoids Playwright overhead.
    Falls back to empty string on any error.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.1688.com/",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20, headers=headers
        ) as client:
            resp = await client.get(url)
            html = resp.text

        # Strategy 1: look for 重量 followed by digits inside the HTML text
        # Matches patterns like: 重量(g)</th>...500 or 重量：500g etc.
        patterns = [
            # table cell pattern: 重量(g) header → value in next td
            r'重量[（(][gG克][)）][^<]{0,30}?(\d+(?:\.\d+)?)',
            # JSON data embedded in page scripts (1688 often embeds product data as JSON)
            r'"weight"\s*:\s*"?(\d+(?:\.\d+)?)"?',
            r'"grossWeight"\s*:\s*"?(\d+(?:\.\d+)?)"?',
            r'"packageWeight"\s*:\s*"?(\d+(?:\.\d+)?)"?',
            # plain text weight mentions near "g" unit
            r'重量[：:]\s*(\d+(?:\.\d+)?)\s*[gG克]',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                val = float(m.group(1))
                # sanity: product weight should be between 1g and 50,000g (50kg)
                if 1 <= val <= 50000:
                    return str(int(val) if val == int(val) else val)

        return ""
    except Exception as e:
        logger.debug(f"[sourcing] _extract_weight_from_1688 error: {e}")
        return ""


# ── Product page screenshot ───────────────────────────────────────────────────

async def _take_product_page_screenshot(url: str, product_name: str) -> Optional[str]:
    """
    Visit the product landing page with Playwright and capture a screenshot of
    the main product image area (or the full viewport as fallback).
    Returns the local file path (caller must delete it), or None on failure.
    """
    out_path = f"/tmp/sourcing_screenshot_{int(time.time())}.png"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(2)

                # Try to find and screenshot the main product image element
                # Covers Shopify, WooCommerce, and generic product pages
                img_selectors = [
                    ".product__media img",             # Shopify Dawn theme
                    ".product-single__photo img",       # Shopify older
                    ".woocommerce-product-gallery img", # WooCommerce
                    "[data-main-image] img",
                    ".product-image img",
                    ".main-product-image img",
                    "figure.product img",
                    ".product img",
                    "img.product-featured-image",
                ]
                screenshotted = False
                for sel in img_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            await el.screenshot(path=out_path)
                            size = os.path.getsize(out_path)
                            if size > 5000:   # ignore tiny/broken images
                                logger.info(
                                    f"[pricing/screenshot] product image element "
                                    f"screenshotted ({size:,} bytes): {sel}"
                                )
                                screenshotted = True
                                break
                        except Exception:
                            pass

                if not screenshotted:
                    # Full page viewport screenshot as fallback
                    await page.screenshot(path=out_path, full_page=False)
                    size = os.path.getsize(out_path)
                    logger.info(
                        f"[pricing/screenshot] full-page viewport screenshot "
                        f"({size:,} bytes) for {url[:60]}"
                    )

            finally:
                await browser.close()

        return out_path
    except Exception as e:
        logger.warning(f"[pricing/screenshot] failed for {url[:60]}: {e}")
        try:
            os.remove(out_path)
        except Exception:
            pass
        return None


# ── fatkun.net search ─────────────────────────────────────────────────────────

async def _download_image(image_url: str) -> Optional[str]:
    """
    Download image_url to a temp file. Returns the local path, or None on failure.
    The caller is responsible for deleting the file.
    """
    tmp_path = f"/tmp/fatkun_img_{int(time.time())}.jpg"
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": image_url,
        }
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=headers
        ) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
        logger.info(
            f"[pricing] image downloaded: {len(resp.content):,} bytes → {tmp_path}"
        )
        return tmp_path
    except Exception as e:
        logger.warning(f"[pricing] image download failed ({image_url[:60]}): {e}")
        return None


async def _image_search_fatkun(
    image_url: str,
    product_name: str,
    is_local_path: bool = False,
) -> tuple[Optional[float], str]:
    """
    Search 1688 via fatkun.net by uploading the actual image file.

    If is_local_path=True, image_url is already a local file path (e.g. screenshot)
    and the download step is skipped.

    Flow:
      1. Download the image to a local temp file (avoids hotlink blocking)
         — skipped when is_local_path=True
      2. Open fatkun.net/image-search
      3. Set the hidden <input type="file"> with the local file
      4. Click "Search Similar Items"
      5. Wait for results, extract price + supplier URL

    Falls back to URL-paste if the file input cannot be found.
    Returns (CNY price, supplier_url) or (None, '').
    """
    if not image_url:
        return (None, "")

    # Use the file directly if it's already local, otherwise download it
    if is_local_path:
        tmp_path = image_url if os.path.isfile(image_url) else None
        owns_tmp = False   # caller is responsible for cleanup
        if not tmp_path:
            logger.warning(f"[pricing] local image path not found: {image_url}")
            return (None, "")
        logger.info(f"[pricing] using local screenshot for fatkun: {image_url}")
    else:
        tmp_path = await _download_image(image_url)
        owns_tmp = True   # we created it, we clean it up

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await ctx.new_page()

            try:
                logger.debug(f"[pricing] Opening fatkun: {FATKUN_SEARCH_URL}")
                await page.goto(FATKUN_SEARCH_URL, wait_until="load", timeout=30_000)
                await asyncio.sleep(2)

                # ── Select 1688 platform ───────────────────────────────────────
                try:
                    btn = page.get_by_text("1688", exact=True)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await asyncio.sleep(0.5)
                        logger.debug("[pricing] fatkun: 1688 platform selected")
                except Exception:
                    pass

                searched = False

                # ── Strategy A: upload file via hidden input ───────────────────
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        file_input = page.locator('input[type="file"]').first
                        if await file_input.count() > 0:
                            await file_input.set_input_files(tmp_path)
                            logger.info(
                                f"[pricing] fatkun: uploaded file {tmp_path}"
                            )
                            await asyncio.sleep(1.5)
                            # After file upload fatkun may auto-submit — check
                            # if results already loaded, otherwise click Search
                            searched = True
                    except Exception as e:
                        logger.debug(f"[pricing] fatkun: file upload failed: {e}")

                # After file upload, click Search if not auto-submitted
                # Also used as Strategy B (URL paste) if file upload failed
                if not searched:
                    # Fallback: paste the URL
                    url_input = None
                    for sel in [
                        'input[placeholder*="image URL"]',
                        'input[placeholder*="URL"]',
                        'input[type="url"]',
                        'input[type="text"]',
                        'input:not([type="file"])',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                url_input = loc
                                break
                        except Exception:
                            pass

                    if url_input:
                        await url_input.click()
                        await url_input.fill(image_url)
                        logger.info(
                            f"[pricing] fatkun: URL paste fallback → {image_url[:80]}"
                        )
                        await asyncio.sleep(1)
                    else:
                        logger.warning(
                            "[pricing] fatkun: neither file input nor URL input found"
                        )
                        await browser.close()
                        return (None, "")

                # ── Click "Search Similar Items" ───────────────────────────────
                submit_btn = None
                for sel in [
                    'button[type="submit"]',
                    'button:has-text("Search Similar Items")',
                    'button:has-text("Search")',
                ]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            submit_btn = loc
                            logger.debug(
                                f"[pricing] fatkun: submit button via '{sel}'"
                            )
                            break
                    except Exception:
                        pass

                if submit_btn:
                    await submit_btn.click()
                    logger.info("[pricing] fatkun: clicked Search Similar Items")
                else:
                    logger.warning(
                        "[pricing] fatkun: submit button not found — may have auto-submitted"
                    )

                # ── Wait for results ───────────────────────────────────────────
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeout:
                    pass
                await asyncio.sleep(3)

                # ── Debug screenshot ───────────────────────────────────────────
                try:
                    await page.screenshot(
                        path="/tmp/fatkun_debug_results.png", full_page=False
                    )
                    logger.info(
                        f"[pricing] fatkun screenshot → /tmp/fatkun_debug_results.png"
                        f" | URL: {page.url[:100]}"
                    )
                except Exception as e:
                    logger.debug(f"[pricing] fatkun screenshot failed: {e}")

                # ── Extract price and supplier URL ─────────────────────────────
                price_cny = await _extract_fatkun_price(page)
                supplier_url = await _click_first_product_get_url(ctx, page)
                logger.info(
                    f"[pricing] fatkun result: price={price_cny} CNY | "
                    f"url={supplier_url[:80]}"
                )

                await browser.close()
                return (price_cny, supplier_url)

            except PlaywrightTimeout:
                logger.warning("[pricing] fatkun search timed out")
                await browser.close()
                return (None, "")

    except Exception as e:
        logger.warning(f"[pricing] fatkun search failed: {e}")
        return (None, "")
    finally:
        # Only clean up if we downloaded the file (not for caller-owned screenshots)
        if tmp_path and owns_tmp:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _extract_fatkun_price(page) -> Optional[float]:
    """
    Extract the best CNY supplier price from the fatkun.net results page.
    Scans ¥ price text nodes, filters UI artifacts (1688 badge), returns median low price.
    """
    _BLACKLIST = {1688.0}

    try:
        prices = await page.evaluate("""
            () => {
                const BLACKLIST = new Set([1688]);
                const nums = [];
                // Walk all text nodes looking for ¥ prices
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                let node;
                while ((node = walker.nextNode())) {
                    const txt = node.textContent.trim();
                    const m = txt.match(/^[¥￥]\\s*(\\d+(?:\\.\\d+)?)$/);
                    if (m) {
                        const p = parseFloat(m[1]);
                        if (!isNaN(p) && p >= 0.5 && p <= 5000 && !BLACKLIST.has(p))
                            nums.push(p);
                    }
                }
                return nums;
            }
        """)
    except Exception as e:
        logger.debug(f"[pricing] fatkun price JS failed: {e}")
        prices = []

    # HTML regex fallback
    if not prices:
        try:
            html = await page.content()
            for m in re.findall(r"[¥￥]\s*(\d+(?:\.\d+)?)", html)[:60]:
                p = float(m)
                if _MIN_PRICE_CNY <= p <= _MAX_PRICE_CNY and p not in _BLACKLIST:
                    prices.append(p)
        except Exception:
            pass

    if not prices:
        logger.debug("[pricing] fatkun: no prices found on results page")
        return None

    # Take median of the lower half (supplier wholesale prices, not retail)
    sample = sorted(prices)[:max(1, len(prices) // 2 + 1)]
    price = sample[len(sample) // 2]
    logger.info(f"[pricing] fatkun price: {sorted(prices[:8])} → {price:.2f} CNY")
    return price


async def _click_first_product_get_url(ctx, page) -> str:
    """
    Get the 1688 supplier URL from the first fatkun.net result card.

    fatkun product cards use React onClick handlers (no <a href>) so we cannot
    just read DOM links — we must actually click and observe what happens:

      A. New tab opens  → capture its URL (most common on desktop Chromium)
      B. Same tab navigates to fatkun product page → follow and look for 1688 link
      C. Nothing navigates → try DOM link scan as last resort
    """
    # ── Step 1: find the first visible product card / image ───────────────────
    first_card = None
    card_selectors = [
        'img[src*="alicdn"]',           # 1688 CDN product images
        'img[src*="1688"]',
        'img[src*="img.alicdn"]',
        '[class*="ProductCard"] img',
        '[class*="product-card"] img',
        '[class*="card"] img',
        '[class*="grid"] img',
        'main img',                     # any image in main content
    ]
    for sel in card_selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                if await el.is_visible():
                    first_card = el
                    logger.debug(f"[pricing] fatkun: card found via '{sel}'")
                    break
            if first_card:
                break
        except Exception:
            pass

    if not first_card:
        logger.warning("[pricing] fatkun: no product card image found to click")
        return ""

    # ── Step 2: hover to reveal "View Product" overlay button ─────────────────
    try:
        await first_card.hover()
        await asyncio.sleep(1.0)
    except Exception:
        pass

    view_btn = None
    for sel in [
        'button:has-text("View Product")',
        'a:has-text("View Product")',
        'button:has-text("查看商品")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                view_btn = loc
                logger.debug(f"[pricing] fatkun: View Product button found via '{sel}'")
                break
        except Exception:
            pass

    target = view_btn if view_btn else first_card
    old_url = page.url

    # ── Step 3: register popup listener, then click ───────────────────────────
    popup_url: list[str] = []
    popup_event = asyncio.Event()

    async def _on_popup(new_page):
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=12_000)
            u = new_page.url
            if u and u != "about:blank":
                popup_url.append(u)
                logger.info(f"[pricing] fatkun popup tab: {u[:100]}")
            await new_page.close()
        except Exception:
            pass
        popup_event.set()

    ctx.on("page", _on_popup)
    try:
        await target.click()
    except Exception as e:
        logger.debug(f"[pricing] fatkun click error: {e}")

    # ── Step 4: wait up to 10 s for popup OR same-tab navigation ─────────────
    try:
        await asyncio.wait_for(popup_event.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass
    finally:
        ctx.remove_listener("page", _on_popup)

    if popup_url:
        return popup_url[0]

    # Check if the current tab navigated
    await asyncio.sleep(1.5)
    new_url = page.url
    if new_url and new_url != old_url:
        logger.info(f"[pricing] fatkun same-tab nav → {new_url[:100]}")
        # If we landed on a fatkun product detail page, scrape the 1688 link
        if "fatkun" in new_url:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                link = await page.evaluate("""
                    () => {
                        const a = Array.from(document.querySelectorAll('a[href]'))
                            .find(a => a.href.includes('1688.com') ||
                                       a.href.includes('taobao.com') ||
                                       a.href.includes('tmall.com'));
                        return a ? a.href : '';
                    }
                """)
                if link:
                    logger.info(f"[pricing] fatkun product page link: {link[:100]}")
                    return link
            except Exception:
                pass
        return new_url

    # ── Step 5: last-resort DOM scan ─────────────────────────────────────────
    try:
        href = await page.evaluate("""
            () => {
                const NAV = new Set(['Download Plugin','Image Search','Image Tools',
                    'AI Studio','Product Collection','Futoo Desktop','E-commerce Tools']);
                // 1688 / Taobao direct link
                const direct = Array.from(document.querySelectorAll('a[href]'))
                    .find(a => a.href.includes('1688.com') || a.href.includes('taobao.com'));
                if (direct) return direct.href;
                // fatkun product page link (not nav)
                const fp = Array.from(document.querySelectorAll('a[href]'))
                    .find(a => a.href.includes('fatkun.net') &&
                               (a.href.includes('/product') || a.href.includes('/detail'))
                               && !NAV.has(a.textContent.trim()));
                return fp ? fp.href : '';
            }
        """)
        if href:
            logger.info(f"[pricing] fatkun DOM fallback href: {href[:100]}")
            return href
    except Exception as e:
        logger.debug(f"[pricing] fatkun DOM scan failed: {e}")

    logger.warning("[pricing] fatkun: could not capture supplier URL")
    return ""


# ── Exchange rate helpers ─────────────────────────────────────────────────────

async def _get_cny_to_usd() -> float:
    """Live CNY→USD rate, falls back to _FALLBACK_CNY_TO_USD."""
    if "cny_usd" in _rate_cache:
        return _rate_cache["cny_usd"]
    try:
        with urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=CNY&to=USD", timeout=6
        ) as resp:
            data = json.loads(resp.read())
            rate = float(data["rates"]["USD"])
            _rate_cache["cny_usd"] = rate
            logger.info(f"[pricing] Live CNY→USD rate: {rate:.6f}")
            return rate
    except Exception as e:
        logger.warning(
            f"[pricing] Live CNY→USD unavailable ({e}) — using fallback {_FALLBACK_CNY_TO_USD}"
        )
        return _FALLBACK_CNY_TO_USD


async def _get_usd_to_gnf() -> float:
    """
    Live USD→GNF rate.
    Tries frankfurter.app, then open.er-api.com, then falls back to config.
    """
    if "usd_gnf" in _rate_cache:
        return _rate_cache["usd_gnf"]

    try:
        with urllib.request.urlopen(
            "https://api.frankfurter.app/latest?from=USD&to=GNF", timeout=6
        ) as resp:
            data = json.loads(resp.read())
            rate = float(data["rates"]["GNF"])
            _rate_cache["usd_gnf"] = rate
            logger.info(f"[pricing] Live USD→GNF rate (frankfurter): {rate:.2f}")
            return rate
    except Exception:
        pass

    try:
        with urllib.request.urlopen(
            "https://open.er-api.com/v6/latest/USD", timeout=6
        ) as resp:
            data = json.loads(resp.read())
            rate_raw = data.get("rates", {}).get("GNF")
            if rate_raw:
                rate = float(rate_raw)
                _rate_cache["usd_gnf"] = rate
                logger.info(f"[pricing] Live USD→GNF rate (er-api): {rate:.2f}")
                return rate
    except Exception as e:
        logger.warning(f"[pricing] Live USD→GNF unavailable ({e})")

    logger.info(f"[pricing] Using configured USD→GNF: {USD_TO_GNF}")
    return float(USD_TO_GNF)


# ── Utility helpers ──────────────────────────────────────────────────────────

def _round_gnf(value: float) -> int:
    """Round value to the nearest ROUND_TO_GNF increment."""
    r = max(int(ROUND_TO_GNF), 1)
    return int(round(value / r) * r)


def _is_video_url(url: str) -> bool:
    """Return True if the URL points to a video file."""
    return bool(re.search(r"\.(mp4|mov|webm|avi|m3u8)", url, re.IGNORECASE))
