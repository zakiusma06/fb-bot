"""
product_scraper.py - Scrape product title, description and images from a competitor page.

Returns a dict:
  {
    "title":       str,
    "description": str,
    "image_urls":  list[str],  # 2-5 validated, deduplicated product images
  }
"""

import hashlib
import json
import logging
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_IMG_EXTENSIONS = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.IGNORECASE)

_BAD_URL_PATTERNS = re.compile(
    r"("
    r"logo|icon|avatar|sprite|badge|banner|favicon|placeholder|blank|pixel|1x1"
    r"|paypal|visa|mastercard|amex|stripe|payment|pay-|checkout"
    r"|fedex|ups|usps|dhl|shipping|delivery"
    r"|footer|header|navbar|nav-|topbar|menubar"
    r"|decoration|ornament|divider|separator|spacer"
    r"|flag|country|social|facebook|instagram|twitter|pinterest|tiktok"
    r"|star-rating|review-star|trustpilot|rating-"
    r"|arrow|button|close|menu|search|cart"
    r"|guarantee|secure|ssl|certified|award|warranty"
    r"|watermark|overlay|bg-|background-"
    r")",
    re.IGNORECASE,
)

_BAD_ALT_PATTERNS = re.compile(
    r"(logo|icon|badge|payment|shipping|guarantee|secure|social|banner|"
    r"decoration|divider|arrow|button|background|watermark|visa|paypal|"
    r"mastercard|fedex|ups|star|rating|review|facebook|instagram|twitter)",
    re.IGNORECASE,
)

_BAD_CLASS_PATTERNS = re.compile(
    r"(logo|icon|badge|banner|footer|header|nav|payment|shipping|social|"
    r"guarantee|decoration|divider|background|watermark|sprite|flag|review)",
    re.IGNORECASE,
)

_MIN_DIMENSION = 200


def _is_good_image_url(url: str) -> bool:
    if not _IMG_EXTENSIONS.search(url):
        return False
    if _BAD_URL_PATTERNS.search(url):
        return False
    return True


def _is_good_image_tag(img_tag) -> bool:
    alt = (img_tag.get("alt") or "").strip()
    if alt and _BAD_ALT_PATTERNS.search(alt):
        return False

    classes = " ".join(img_tag.get("class") or [])
    if _BAD_CLASS_PATTERNS.search(classes):
        return False

    parent = img_tag.parent
    if parent:
        parent_classes = " ".join(parent.get("class") or [])
        parent_id = parent.get("id") or ""
        combined = f"{parent_classes} {parent_id}"
        if _BAD_CLASS_PATTERNS.search(combined):
            return False

    try:
        w = int(img_tag.get("width") or 0)
        h = int(img_tag.get("height") or 0)
        if (w > 0 and w < _MIN_DIMENSION) or (h > 0 and h < _MIN_DIMENSION):
            return False
    except (ValueError, TypeError):
        pass

    return True


def _dedupe_by_url(urls: list) -> list:
    seen, result = set(), []
    for u in urls:
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key)
            result.append(u)
    return result


def _download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10, stream=True)
        if resp.status_code != 200:
            return None
        data = resp.content
        if len(data) < 2000:
            return None
        return data
    except Exception:
        return None


def _dedupe_by_hash(urls: list) -> list:
    seen_hashes: set = set()
    result = []
    for url in urls:
        data = _download_image(url)
        if data is None:
            logger.debug(f"[scraper] Skipping undownloadable image: {url}")
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen_hashes:
            logger.debug(f"[scraper] Duplicate image (hash match): {url}")
            continue
        seen_hashes.add(h)
        result.append(url)
    return result


def _shopify_product_images(url: str) -> list:
    """
    Try to fetch images directly from Shopify's product JSON API.
    Works for any Shopify store — returns up to 5 CDN image URLs.
    """
    try:
        parsed = urlparse(url)
        # Build the .json API URL: /products/<handle>.json
        path = parsed.path.rstrip("/")
        json_url = f"{parsed.scheme}://{parsed.netloc}{path}.json"
        resp = requests.get(json_url, headers=_HEADERS, timeout=6)
        if resp.status_code != 200:
            return []
        data = resp.json()
        images = data.get("product", {}).get("images", [])
        result = []
        for img in images:
            src = img.get("src", "")
            # Remove Shopify size suffix (e.g. _800x800) to get full-size
            src_clean = re.sub(r"_\d+x\d*(\.\w+)$", r"\1", src.split("?")[0])
            if src_clean and _is_good_image_url(src_clean):
                result.append(src_clean)
            elif src and _is_good_image_url(src):
                result.append(src)
        logger.info(f"[scraper] Shopify JSON API returned {len(result)} images")
        return result[:5]
    except Exception as e:
        logger.debug(f"[scraper] Shopify JSON API failed: {e}")
        return []


def scrape_product_page(url: str) -> dict:
    logger.info(f"[scraper] Fetching {url}")
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[scraper] Failed to fetch {url}: {e}")
        return {"title": "", "description": "", "image_urls": []}

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Title ──────────────────────────────────────────────────────────────
    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        tag = soup.find("title")
        if tag:
            title = tag.get_text(" ", strip=True)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    # ── Description ────────────────────────────────────────────────────────
    description = ""
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        description = og_desc["content"].strip()
    if not description:
        meta_desc = soup.find("meta", {"name": "description"})
        if meta_desc and meta_desc.get("content"):
            description = meta_desc["content"].strip()
    if not description:
        p = soup.find("p")
        if p:
            description = p.get_text(" ", strip=True)[:500]

    # ── Images ─────────────────────────────────────────────────────────────
    candidates = []

    # Strategy 1: Shopify product JSON API (best — returns all gallery images, no hash dedup needed)
    shopify_imgs = _shopify_product_images(url)
    if shopify_imgs:
        # Already clean and unique — skip expensive hash dedup
        image_urls = shopify_imgs[:5]
        logger.info(f"[scraper] Using {len(image_urls)} images from Shopify JSON API")
    else:
        # Fallback strategies for non-Shopify or failed JSON API

        # Strategy 2: Shopify JSON embedded in page <script> tags
        for script in soup.find_all("script", type="application/json"):
            try:
                data = json.loads(script.string or "")
                product = data.get("product", data)
                imgs = product.get("images", product.get("media", []))
                for img in imgs:
                    src = img.get("src", "") or img.get("preview_image", {}).get("src", "")
                    if src and _is_good_image_url(src):
                        candidates.append(src.split("?")[0])
            except Exception:
                pass

        # Strategy 3: og:image meta tags
        for tag in soup.find_all("meta", property="og:image"):
            src = (tag.get("content") or "").strip()
            if src and _is_good_image_url(src):
                candidates.append(src)

        # Strategy 4: HTML img tags (data-src for lazy-loaded galleries)
        for img in soup.find_all("img"):
            src = (
                img.get("data-src") or
                img.get("data-lazy-src") or
                img.get("data-srcset", "").split()[0] or
                img.get("src") or ""
            ).strip()
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            if not src.startswith("http"):
                continue
            if not _is_good_image_url(src):
                continue
            if not _is_good_image_tag(img):
                continue
            candidates.append(src)

        candidates = _dedupe_by_url(candidates)
        image_urls = candidates[:5]

    logger.info(
        f"[scraper] title={title!r}, desc_len={len(description)}, "
        f"images={len(image_urls)}"
    )
    return {
        "title":       title,
        "description": description,
        "image_urls":  image_urls,
    }
