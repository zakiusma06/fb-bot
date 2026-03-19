"""
shopify_pipeline.py - Orchestrates the full Shopify product creation flow.

Steps:
  1. Scrape competitor product page
  2. AI-generate French title + description
  3. Create & publish Shopify product (with validated images)
  4. Return result for Telegram control panel
"""

import logging
import sys
import os

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
) -> dict:
    """
    Full pipeline. Returns a result dict:
      {
        "ok":         bool,
        "error":      str,
        "title":      str,
        "description": str,
        "admin_url":  str,
        "store_url":  str,
        "product_id": int,
        "handle":     str,
        "images":     [ {"id": int, "src": str, "position": int} ],
      }
    """
    # ── Step 1: Scrape ──────────────────────────────────────────────────────
    await send_status("🔍 <b>Step 1/3</b> — Scraping product page and validating images…")
    scraped = scrape_product_page(url_product)

    image_count = len(scraped.get("image_urls", []))
    logger.info(f"[pipeline] {image_count} valid images found for {url_product}")

    if image_count < 1:
        return {
            "ok":    False,
            "error": (
                f"No valid product image(s) found on the page.\nURL: {url_product}"
            ),
        }

    # ── Step 2: AI content ─────────────────────────────────────────────────
    await send_status("🤖 <b>Step 2/3</b> — Generating French title and description…")
    ai = generate_product_content(
        raw_title=scraped["title"],
        raw_description=scraped["description"],
    )

    if not ai.get("title"):
        return {"ok": False, "error": "AI failed to generate a product title."}

    # ── Step 3: Create & publish Shopify product ───────────────────────────
    await send_status("🛍 <b>Step 3/3</b> — Creating and publishing Shopify product…")
    result = create_and_publish_product(
        title=ai["title"],
        body_html=ai["description"].replace("\n", "<br>"),
        price=price,
        compare_at_price=compare_at_price,
        image_urls=scraped["image_urls"],
        source_url=url_product,
    )

    if not result:
        return {"ok": False, "error": "Shopify product creation failed. Check credentials."}

    return {
        "ok":          True,
        "title":       ai["title"],
        "description": ai["description"],
        "admin_url":   result["admin_url"],
        "store_url":   result["store_url"],
        "product_id":  result["id"],
        "handle":      result["handle"],
        "images":      result["images"],
    }
