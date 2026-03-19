"""
product_extractor.py - Enrich ads with landing page data.

Extracts:
  - Page <title>
  - og:title / og:description
  - H1 / H2 headings
  - Short description text
  - Bullet point list items
  - Repeated product-relevant phrases
  - A clean keyword list for second-pass searching
"""

import asyncio
import logging
import re
from collections import Counter
from urllib.parse import urlparse

import aiohttp

# ── Non-product URL detection ─────────────────────────────────────────────────

# Language-only paths: /fr  /en  /fr-fr  /en-us  /fr-ch  /en-gb
_LANG_PATH_RE = re.compile(r"^/[a-z]{2}(-[a-z]{2,4})?$", re.IGNORECASE)

# Paths that are definitively not product pages
_NON_PRODUCT_PATHS: frozenset[str] = frozenset({
    "/blog", "/blogs", "/news",
    "/about", "/about-us", "/qui-sommes-nous",
    "/contact", "/contact-us", "/nous-contacter",
    "/pages/about", "/pages/contact", "/pages/home",
    "/pages/homepage", "/pages/index",
    "/search", "/recherche",
    "/account", "/login", "/register",
    "/cart", "/checkout", "/panier",
    "/collections",          # bare Shopify collections root
    "/sitemap", "/faq", "/help", "/aide",
    "/privacy", "/terms", "/legal", "/mentions-legales",
})

def is_collection_page(url: str, html: str = "") -> tuple[bool, str]:
    """
    Return (True, reason) only when the PAGE is a true multi-product listing.
    Returns (False, '') for genuine single-product pages — including product pages
    that contain upsell / cross-sell / 'frequently bought together' sections.

    Logic (two-stage):
      Stage 1 — primary-product proof: if ANY strong single-product signal is
                found, the page is definitively a product page and we return
                immediately (no matter how many upsell cards are present).
      Stage 2 — multi-product count: strip known upsell/recommendation containers
                from the HTML, then count remaining product cards and add-to-cart
                buttons.  Only flag as collection if the cleaned content still
                shows multiple products.
    """
    if not html:
        return False, ""

    try:
        # ── Stage 1: prove it is a product page ───────────────────────────────
        #
        # Signal A: JSON-LD @type:"Product" — most reliable structured signal
        if re.search(r'"@type"\s*:\s*"Product"', html, re.IGNORECASE):
            return False, ""

        # Signal B: Shopify product context (present on every Shopify PDP)
        if re.search(
            r'window\.ShopifyAnalytics|"product"\s*:\s*\{\s*"id"',
            html, re.IGNORECASE,
        ):
            return False, ""

        # Signal C: WooCommerce / generic single-product body class
        if re.search(
            r'class=["\'][^"\']*\b(?:single-product|woocommerce-product)\b[^"\']*["\']',
            html, re.IGNORECASE,
        ):
            return False, ""

        # Signal D: canonical URL contains /products/ or /product/ (Shopify / generic)
        if re.search(r'rel=["\']canonical["\'][^>]*/product(?:s)?/', html, re.IGNORECASE):
            return False, ""

        # ── Stage 2: strip upsell/recommendation blocks, then count ──────────
        #
        # Remove any <section|div|aside|ul> whose class contains upsell-related
        # keywords.  This prevents "frequently bought together" / related-product
        # carousels from inflating the multi-product count.
        _UPSELL_RE = re.compile(
            r'<(?:section|div|aside|ul)(?:\s[^>]*)?\s+class=["\'][^"\']*'
            r'(?:frequently.bought|upsell|cross.sell|related.product|cross.product'
            r'|you.may.also|also.bought|recommendation|similar.product'
            r'|bundle|product-suggest|complementary)[^"\']*["\'][^>]*>'
            r'.*?</(?:section|div|aside|ul)>',
            re.IGNORECASE | re.DOTALL,
        )
        cleaned = _UPSELL_RE.sub("<!-- upsell-removed -->", html)

        # Count add-to-cart buttons in the cleaned page
        cart_hits = len(re.findall(
            r'add.?to.?cart|ajouter.?au.?panier|btn.?cart|cart.?btn',
            cleaned, re.IGNORECASE,
        ))
        if cart_hits >= 4:
            return True, f"multi-product page ({cart_hits} add-to-cart buttons, upsells excluded)"

        # Count product-card/grid elements in the cleaned page
        card_hits = len(re.findall(
            r'class=["\'][^"\']*(?:product-card|product-item|product-grid__item'
            r'|ProductCard|collection-product|grid-product)[^"\']*["\']',
            cleaned, re.IGNORECASE,
        ))
        if card_hits >= 3:
            return True, f"product grid page ({card_hits} cards, upsells excluded)"

        # JSON-LD CollectionPage / ItemList with multiple listed products
        if re.search(r'"@type"\s*:\s*"(?:CollectionPage|ItemList)"', html, re.IGNORECASE):
            item_hits = len(re.findall(
                r'"@type"\s*:\s*"(?:Product|ListItem)"', html, re.IGNORECASE
            ))
            if item_hits >= 3:
                return True, f"collection page (JSON-LD CollectionPage + {item_hits} items)"

    except Exception:
        pass

    return False, ""


def is_non_product_url(url: str) -> tuple[bool, str]:
    """
    Return (True, reason) if the URL is a homepage, language root, or other
    non-product page that should be skipped.
    Returns (False, '') for normal product / funnel pages.
    """
    if not url:
        return False, ""
    try:
        path = urlparse(url).path.rstrip("/").lower() or "/"

        # 1. Root path → homepage
        if path in ("", "/"):
            return True, "redirects to homepage (root path)"

        # 2. Language-code-only path: /fr  /en-us  /fr-fr  /fr-ch
        if _LANG_PATH_RE.match(path):
            return True, f"redirects to language homepage ({path})"

        # 3. Known non-product paths (exact match or exact prefix)
        for bad in _NON_PRODUCT_PATHS:
            if path == bad or path.startswith(bad + "/") and len(path) == len(bad):
                return True, f"non-product page ({path})"

    except Exception:
        pass
    return False, ""

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=12)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Stop-words to skip when extracting keywords (English + French)
STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "be", "as",
    "are", "was", "were", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "your", "our", "my", "its", "their", "you", "we", "i", "he", "she",
    "free", "buy", "shop", "get", "now", "best", "new", "sale", "off",
    "more", "most", "all", "just", "only", "up", "out", "about",
    "no", "not", "so", "if", "then", "than", "other", "what", "how",
    "when", "where", "who", "which",
    # French
    "le", "la", "les", "de", "du", "des", "un", "une", "en", "et", "ou",
    "pour", "sur", "avec", "par", "dans", "au", "aux", "se", "est", "ce",
    "qui", "que", "quoi", "dont", "son", "sa", "ses",
}


async def enrich_with_page_data(ad: dict) -> dict:
    """
    Fetch the landing page and extract:
      - page_title, og:title, og:description
      - H1/H2 headings
      - bullet points from <li> elements
      - extracted product keywords
    Also saves the final resolved URL (after redirects) back into landing_page_url.
    Updates ad in-place and returns it.
    """
    url = ad.get("landing_page_url", "")
    if not url or url.startswith("javascript") or not url.startswith("http"):
        logger.debug(f"[enrich] skipping ad — no valid landing page URL")
        return ad

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers=HEADERS) as session:
            async with session.get(url, allow_redirects=True, ssl=False) as resp:
                if resp.status not in (200, 201):
                    logger.debug(f"[enrich] {url[:60]} returned HTTP {resp.status}")
                    return ad

                # Save the final resolved URL (after all redirects)
                final_url = str(resp.url)
                if final_url and final_url != url and final_url.startswith("http"):
                    ad["landing_page_url"] = final_url
                    logger.info(f"[enrich] redirect: {url[:60]} → {final_url[:60]}")
                else:
                    logger.info(f"[enrich] resolved: {url[:60]}")

                # Check if the resolved URL is a homepage / non-product page
                skip, skip_reason = is_non_product_url(ad["landing_page_url"])
                if skip:
                    ad["_skip_non_product"] = True
                    ad["_skip_reason"] = skip_reason
                    logger.info(f"[enrich] SKIP non-product: {skip_reason}")
                    return ad

                html = await resp.text(encoding="utf-8", errors="replace")

                # Check if this is a collection / multi-product page (before heavy extraction)
                is_coll, coll_reason = is_collection_page(ad["landing_page_url"], html)
                if is_coll:
                    ad["_skip_collection"] = True
                    ad["_skip_reason"] = coll_reason
                    logger.info(f"[enrich] SKIP collection: {coll_reason} — {ad['landing_page_url'][:60]}")
                    return ad

        # ── Extract structured page data ───────────────────────────────────
        title      = _extract_title(html)
        headings   = _extract_headings(html)
        og_title   = _extract_og_title(html)
        og_desc    = _extract_og_description(html)
        meta_desc  = _extract_meta_description(html)
        bullets    = _extract_bullet_points(html)
        first_para = _extract_first_paragraph(html)

        # Best description: og:description > meta description > first paragraph
        best_desc = og_desc or meta_desc or first_para or ""

        # Store on ad for downstream use
        if title and not ad.get("page_title"):
            ad["page_title"] = title

        best_product_name = og_title or (headings[0] if headings else "") or title
        if best_product_name and not ad.get("extracted_product_name"):
            ad["extracted_product_name"] = best_product_name[:120]

        if ad.get("extracted_product_name"):
            ad["normalized_product_name"] = ad["extracted_product_name"].lower().strip()

        # Store enriched page data for query expansion
        ad["_page_description"]  = best_desc[:400]
        ad["_page_bullets"]      = bullets[:6]
        ad["_page_headings"]     = headings[:4]

        # Extract all clean product images from the landing page.
        # og_image_url = the canonical OG image (used as primary search image)
        # _product_images = ordered list of product shots for fallback searches
        og_image = _extract_og_image(html)
        if og_image:
            ad["og_image_url"] = og_image

        all_product_images = _extract_product_images(html, ad["landing_page_url"])
        if all_product_images:
            ad["_product_images"] = all_product_images

        # Build keyword list from title + og + headings
        raw_text = " ".join(filter(None, [og_title, title] + headings[:3]))
        keywords = _extract_keywords(raw_text)
        ad["_extracted_keywords"] = keywords

        # Detect product variants (color / size / style / quantity options)
        ad["has_variants"] = detect_has_variants(html)

        logger.info(
            f"[enrich] extracted: name='{ad.get('extracted_product_name','')[:50]}' "
            f"desc={len(best_desc)}ch bullets={len(bullets)} kw={len(keywords)} "
            f"variants={ad['has_variants']}"
        )

    except Exception as e:
        logger.debug(f"[enrich] error fetching {url[:60]}: {e}")

    return ad


def generate_secondary_keyword(ad: dict) -> str:
    """
    Produce a clean 2–4 word search query from the ad's landing page keywords.
    Used as a fallback single-query for the second-pass search.
    """
    keywords = ad.get("_extracted_keywords", [])
    if keywords:
        return " ".join(keywords[:4])
    name = ad.get("extracted_product_name") or ad.get("page_title") or ""
    words = name.split()[:4]
    return " ".join(words) if words else ""


async def enrich_batch(ads: list[dict], concurrency: int = 5) -> list[dict]:
    """Enrich a list of ads concurrently."""
    sem = asyncio.Semaphore(concurrency)

    async def _safe_enrich(ad):
        async with sem:
            return await enrich_with_page_data(ad)

    return list(await asyncio.gather(*[_safe_enrich(ad) for ad in ads]))


# ── HTML extraction helpers ────────────────────────────────────────────────

def _extract_og_image(html: str) -> str:
    for pattern in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:image["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            url = m.group(1).strip()
            if url.startswith("http"):
                return url
    return ""


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return _clean_text(m.group(1)) if m else ""


def _extract_og_title(html: str) -> str:
    for pattern in [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:title["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return _clean_text(m.group(1))
    return ""


def _extract_og_description(html: str) -> str:
    for pattern in [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return _clean_text(m.group(1))
    return ""


def _extract_meta_description(html: str) -> str:
    for pattern in [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return _clean_text(m.group(1))
    return ""


def _extract_headings(html: str) -> list[str]:
    headings = []
    for tag in ("h1", "h2", "h3"):
        for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.IGNORECASE | re.DOTALL):
            text = _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
            if text and len(text) > 3 and text not in headings:
                headings.append(text)
    return headings[:8]


def detect_has_variants(html: str) -> str:
    """
    Detect if a product page contains variant options (color, size, style, quantity).

    Returns 'YES' if variants are found, 'NO' otherwise.
    """
    if not html:
        return "NO"

    # Shopify product JSON with more than one variant object
    shopify_match = re.search(r'"variants"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if shopify_match:
        variant_ids = re.findall(r'"id"\s*:', shopify_match.group(1))
        if len(variant_ids) > 1:
            return "YES"

    # Shopify options array with actual values
    options_match = re.search(r'"options"\s*:\s*(\[.*?\])', html, re.DOTALL)
    if options_match:
        option_vals = re.findall(r'"values"\s*:\s*\[([^\]]+)\]', options_match.group(1))
        for val_block in option_vals:
            vals = re.findall(r'"([^"]+)"', val_block)
            if len(vals) > 1:
                return "YES"

    # Select elements for color / size / style / quantity
    select_pattern = re.compile(
        r'<select[^>]*(?:name|id|class)[^>]*(?:color|colour|size|style|quantity|option|variant)[^>]*>',
        re.IGNORECASE,
    )
    if select_pattern.search(html):
        return "YES"

    # Common CSS class names for variant swatches / selectors
    swatch_pattern = re.compile(
        r'class=["\'][^"\']*\b(?:swatch|variant|color-swatch|size-swatch|size-option|'
        r'option-selector|color-option|product-option|variant-selector|option-value|'
        r'color-filter|size-filter)\b[^"\']*["\']',
        re.IGNORECASE,
    )
    if swatch_pattern.search(html):
        return "YES"

    # data-option / data-variant / data-color / data-size attributes
    data_attr_pattern = re.compile(
        r'\bdata-(?:option|variant|color|colour|size|style)\b',
        re.IGNORECASE,
    )
    if data_attr_pattern.search(html):
        return "YES"

    # WooCommerce / generic variation selects
    woo_pattern = re.compile(
        r'class=["\'][^"\']*\bvariations?\b[^"\']*["\']',
        re.IGNORECASE,
    )
    if woo_pattern.search(html):
        return "YES"

    return "NO"


def _extract_bullet_points(html: str) -> list[str]:
    """Extract text from <li> elements that look like product feature bullets."""
    bullets = []
    for m in re.finditer(r"<li[^>]*>(.*?)</li>", html, re.IGNORECASE | re.DOTALL):
        text = _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
        # Keep only concise, informative bullets (not nav links)
        if text and 5 < len(text) < 200 and text not in bullets:
            bullets.append(text)
        if len(bullets) >= 10:
            break
    return bullets


def _extract_first_paragraph(html: str) -> str:
    """Extract the first substantial paragraph of text from the page body."""
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL):
        text = _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
        if len(text) > 40:
            return text[:300]
    return ""


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful product keywords from a text snippet."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    words = text.split()
    filtered = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    bigrams = [f"{filtered[i]} {filtered[i+1]}" for i in range(len(filtered) - 1)]
    counts = Counter(filtered + bigrams)
    ranked = [term for term, _ in counts.most_common(15)]
    ranked.sort(key=lambda t: (-len(t.split()), -counts[t]))
    return ranked[:8]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


def _extract_product_images(html: str, page_url: str) -> list[str]:
    """
    Extract an ordered list of clean product image URLs from a page's HTML.
    Priority: OG image → JSON-LD product images → Shopify CDN imgs → large <img> srcs.
    Returns up to 5 unique, absolute image URLs. Empty list if none found.
    """
    seen: set[str] = set()
    images: list[str] = []

    def _add(url: str):
        url = url.strip()
        if not url or not url.startswith("http"):
            return
        # Skip obvious non-product images (icons, logos, tracking pixels)
        lower = url.lower()
        if any(x in lower for x in ("logo", "icon", "pixel", "avatar", "banner",
                                     "sprite", "tracking", "1x1", "blank", "badge")):
            return
        if url not in seen:
            seen.add(url)
            images.append(url)

    # 1. OG image (canonical product shot)
    og = _extract_og_image(html)
    if og:
        _add(og)

    # 2. JSON-LD @type=Product image field
    for m in re.finditer(r'"@type"\s*:\s*"Product".*?"image"\s*:\s*(\[.*?\]|".*?")',
                         html, re.DOTALL | re.IGNORECASE):
        raw = m.group(1)
        for img_url in re.findall(r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', raw,
                                  re.IGNORECASE):
            _add(img_url)

    # 3. Shopify CDN product images (//cdn.shopify.com/s/files/...)
    for m in re.finditer(
        r'(?:src|content|href)=["\']([^"\']*cdn\.shopify\.com/s/files/[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
        html, re.IGNORECASE
    ):
        url = m.group(1)
        if not url.startswith("http"):
            url = "https:" + url
        _add(url)

    # 4. Any large <img> with a descriptive src (skip thumbnails / small variants)
    for m in re.finditer(
        r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
        html, re.IGNORECASE
    ):
        url = m.group(1).strip()
        # Prefer full-size: skip _thumb, _small, _100x, _150x sized variants
        if re.search(r'_(?:thumb|small|icon|\d{1,3}x\d{0,3})', url, re.IGNORECASE):
            continue
        if not url.startswith("http"):
            try:
                from urllib.parse import urljoin
                url = urljoin(page_url, url)
            except Exception:
                continue
        _add(url)

    return images[:5]
