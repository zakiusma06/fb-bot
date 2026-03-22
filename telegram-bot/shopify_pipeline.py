"""
shopify_pipeline.py - Orchestrates the full Shopify product creation flow.

Steps:
  1. Scrape competitor product page (in thread, hard 25s timeout)
  2. AI-generate French title + description
  3. Create & publish Shopify product (with whatever images were found)
  4. Return result for Telegram control panel
"""

import asyncio
import logging
import sys
import os
from urllib.parse import urlparse, unquote

sys.path.insert(0, os.path.dirname(__file__))

from product_scraper import scrape_product_page
from ai_content      import generate_product_content
from shopify_client  import create_and_publish_product

logger = logging.getLogger(__name__)


async def run_pipeline(
    send_status,
    url_product:      str,
    price:            str,
    compare_at_price: str,
    sku:              str,
    language:         str = None,
) -> dict:
    """
    Full pipeline. Returns a result dict:
      {
        "ok":           bool,
        "error":        str,
        "title":        str,
        "description":  str,
        "admin_url":    str,
        "store_url":    str,
        "product_id":   int,
        "handle":       str,
        "images":       [ {"id": int, "src": str, "position": int} ],
        "images_found": int,   # how many images were scraped (0 = ask user to add manually)
      }
    """
    loop = asyncio.get_event_loop()

    # ── Step 1: Scrape (thread + hard 25s timeout) ──────────────────────────
    await send_status("🔍 <b>Step 1/3</b> — Scraping product page…")
    try:
        scraped = await asyncio.wait_for(
            loop.run_in_executor(None, scrape_product_page, url_product),
            timeout=25,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[pipeline] Scraper timed out for {url_product} — continuing without images")
        scraped = {"title": "", "description": "", "image_urls": []}
    except Exception as e:
        logger.error(f"[pipeline] Scraper error: {e}")
        scraped = {"title": "", "description": "", "image_urls": []}

    image_count = len(scraped.get("image_urls", []))
    logger.info(f"[pipeline] {image_count} image(s) found for {url_product}")

    # ── Log & fallback scraped content ────────────────────────────────────
    raw_title       = scraped.get("title", "").strip()
    raw_description = scraped.get("description", "").strip()
    logger.info(f"[pipeline] scraped title={raw_title!r}, desc_len={len(raw_description)}")

    if not raw_title:
        path_parts = urlparse(url_product).path
        segments   = [s for s in path_parts.split("/") if s]
        slug       = unquote(segments[-1]) if segments else ""
        raw_title  = slug.replace("-", " ").replace("_", " ").strip() or url_product
        logger.warning(f"[pipeline] empty scraped title — URL slug fallback: {raw_title!r}")

    if not raw_description:
        raw_description = f"Product: {raw_title}. URL: {url_product}"
        logger.warning(f"[pipeline] empty scraped description — URL-based fallback used")

    # Never block on missing images — user can add them manually after creation

    # ── Step 2: AI content ─────────────────────────────────────────────────
    await send_status("🤖 <b>Step 2/3</b> — Generating title and description…")
    ai = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: generate_product_content(
                raw_title=raw_title,
                raw_description=raw_description,
                language=language,
            )
        ),
        timeout=30,
    )

    if not ai.get("title"):
        return {"ok": False, "error": "AI failed to generate a product title."}

    # ── Step 3: Create & publish Shopify product ───────────────────────────
    await send_status("🛍 <b>Step 3/3</b> — Creating and publishing Shopify product…")

    desc_lines = [
        line for line in ai["description"].splitlines()
        if line.strip() not in ("---", "—--", "--", "———")
    ]
    desc_html = "<br>".join(desc_lines)
    body_html = f'<div style="text-align:center">{desc_html}</div>'

    result = await loop.run_in_executor(
        None,
        lambda: create_and_publish_product(
            title=ai["title"],
            body_html=body_html,
            price=price,
            compare_at_price=compare_at_price,
            image_urls=scraped.get("image_urls", []),
            source_url=url_product,
        )
    )

    if not result:
        return {"ok": False, "error": "Shopify product creation failed. Check credentials."}

    return {
        "ok":           True,
        "title":        ai["title"],
        "description":  body_html,
        "admin_url":    result["admin_url"],
        "store_url":    result["store_url"],
        "product_id":   result["id"],
        "handle":       result["handle"],
        "images":       result["images"],
        "images_found": image_count,
    }
