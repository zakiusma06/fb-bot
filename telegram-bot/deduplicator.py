"""
deduplicator.py - Deduplication logic for scraped ads AND product clusters.

Two levels of deduplication:
  1. Ad-level  (Deduplicator class) — within a single scrape run
  2. Cluster-level (is_cluster_duplicate) — new clusters vs. saved Google Sheet rows
"""

import logging
import uuid
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests
from rapidfuzz import fuzz

try:
    import imagehash
    from PIL import Image
    IMAGE_HASH_AVAILABLE = True
except ImportError:
    IMAGE_HASH_AVAILABLE = False

from config import TEXT_DEDUP_THRESHOLD, IMAGE_DEDUP_THRESHOLD
from utils import normalize_url, normalize_text


# ── URL normalisation for cluster dedup ──────────────────────────────────────
# IMPORTANT: Only strip tracking params (utm_, fbclid, etc.) — NOT all query params.
# Stores like Shopify use ?id=123, ?product=456 as product identifiers.
# Stripping all query params (as normalize_url does) causes false duplicates.

_DEDUP_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "fbclid", "gclid", "ttclid", "msclkid", "twclid",
    "ref", "referrer", "affiliate", "source", "mc_eid",
    "_ga", "_gl", "igshid", "s_kwcid",
})


def _normalize_url_for_dedup(url: str) -> str:
    """
    Normalize a URL for cluster dedup: lowercase scheme/host, strip fragment,
    and remove ONLY known tracking params. Product-identifying params like
    ?id=123, ?product=abc, ?variant=xyz are KEPT so different products on the
    same store are not collapsed into false duplicates.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return url.strip().lower()
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {k: v for k, v in params.items() if k.lower() not in _DEDUP_TRACKING_PARAMS}
        clean_query = urlencode(clean_params, doseq=True)
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",           # params (matrix params)
            clean_query,  # keep product-identifying query params
            "",           # strip fragment
        ))
        return normalized
    except Exception:
        return url.strip().lower()


logger = logging.getLogger(__name__)


class Deduplicator:
    """
    Maintains a registry of seen products and deduplicates new ads against it.
    Also deduplicates against already-saved rows from Google Sheets.
    """

    def __init__(self):
        # List of canonical (unique) product records
        self._registry: list[dict] = []

    def load_existing(self, saved_rows: list[dict]):
        """Pre-load existing sheet rows so we deduplicate against saved data."""
        for row in saved_rows:
            if row.get("duplicate_group_id"):
                self._registry.append(row)
            else:
                row["duplicate_group_id"] = str(uuid.uuid4())
                self._registry.append(row)

    def process(self, ad: dict) -> tuple[bool, Optional[dict]]:
        """
        Check if `ad` is a duplicate.
        Returns (is_duplicate, existing_record_or_None).
        If duplicate, the existing record's duplicates_count is incremented.
        If new, the ad is added to the registry with a fresh group ID.
        """
        existing = self._find_duplicate(ad)
        if existing:
            existing["duplicates_count"] = int(existing.get("duplicates_count") or 1) + 1
            return True, existing

        # New unique product — assign a group id and register it
        ad["duplicate_group_id"] = str(uuid.uuid4())
        ad["duplicates_count"] = 1
        self._registry.append(ad)
        return False, None

    def _find_duplicate(self, ad: dict) -> Optional[dict]:
        """Look for an existing record that matches `ad`."""
        ad_url = normalize_url(ad.get("ad_library_url", ""))
        ad_land = normalize_url(ad.get("landing_page_url", ""))
        ad_media = normalize_url(ad.get("media_url", ""))
        ad_name = normalize_text(ad.get("normalized_product_name", ""))

        for existing in self._registry:
            # 1. Exact ad_library_url match
            if ad_url and ad_url == normalize_url(existing.get("ad_library_url", "")):
                return existing

            # 2. Same normalized landing page
            if ad_land and ad_land == normalize_url(existing.get("landing_page_url", "")):
                return existing

            # 3. Same media URL
            if ad_media and ad_media == normalize_url(existing.get("media_url", "")):
                return existing

            # 4. Strong text similarity on product name
            ex_name = normalize_text(existing.get("normalized_product_name", ""))
            if ad_name and ex_name:
                text_score = fuzz.token_sort_ratio(ad_name, ex_name)
                if text_score >= TEXT_DEDUP_THRESHOLD:
                    # Optional: confirm with image hash
                    if IMAGE_HASH_AVAILABLE:
                        img_sim = _image_similarity(
                            ad.get("main_image_url", ""),
                            existing.get("main_image_url", ""),
                        )
                        # Strong text + any image similarity -> duplicate
                        if img_sim is not None and img_sim <= IMAGE_DEDUP_THRESHOLD * 2:
                            return existing
                        # Very strong text alone -> duplicate
                        if text_score >= 95:
                            return existing
                    else:
                        if text_score >= 90:
                            return existing

        return None


def _image_similarity(url1: str, url2: str) -> Optional[int]:
    """
    Compute perceptual hash distance between two image URLs.
    Returns None if images can't be fetched/compared.
    Lower distance = more similar. 0 = identical.
    """
    if not url1 or not url2:
        return None
    try:
        h1 = _fetch_hash(url1)
        h2 = _fetch_hash(url2)
        if h1 is None or h2 is None:
            return None
        return h1 - h2
    except Exception as e:
        logger.debug(f"Image similarity error: {e}")
        return None


def _fetch_hash(url: str):
    """Fetch an image and compute its perceptual hash."""
    try:
        resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        return imagehash.phash(img)
    except Exception:
        return None


# ── Cluster-level deduplication against saved sheet rows ─────────────────────

# Fuzzy name similarity threshold for POSSIBLE_DUPLICATE warnings (not auto-reject)
_CLUSTER_NAME_THRESHOLD = 82

# Perceptual hash distance threshold — 0 = identical, ≤10 = very similar product shot
_IMAGE_DEDUP_DISTANCE = 10


def is_cluster_duplicate(cluster, existing_rows: list[dict]) -> tuple[bool, str, str]:
    """
    Check whether `cluster` is already represented in `existing_rows`
    (rows read directly from Google Sheets).

    Returns a 3-tuple: (is_hard_duplicate, hard_reason, possible_reason)

    AUTO-REJECT (hard duplicate) — only if:
      • Exact normalised product URL match (tracking params stripped, product params kept)

    POSSIBLE_DUPLICATE (warn but DO NOT reject) — if:
      • Fuzzy product name similarity >= _CLUSTER_NAME_THRESHOLD
      • Returns is_hard_duplicate=False so the cluster is still saved

    All other cases: not a duplicate.

    NOTE: normalize_url() from utils strips ALL query params, which causes false
    positives for Shopify/WooCommerce stores that use ?id= or ?product= as
    product identifiers. We use _normalize_url_for_dedup() here instead, which
    only strips known tracking params (utm_*, fbclid, etc.) and keeps product params.
    """
    cluster_name: str = normalize_text(getattr(cluster, "canonical_name", "") or "")
    cluster_sku:  str = getattr(cluster, "sku", "?")

    if not existing_rows:
        logger.debug(f"[dedup] {cluster_sku} '{cluster_name[:40]}': no existing rows — PASS")
        return False, "", ""

    raw_product_urls: list[str] = getattr(cluster, "product_urls", [])
    cluster_urls: set[str] = {
        _normalize_url_for_dedup(u) for u in raw_product_urls if u
    }

    if not cluster_urls:
        logger.debug(
            f"[dedup] {cluster_sku} '{cluster_name[:40]}': no product URLs → cannot match — PASS"
        )
        return False, "", ""

    logger.debug(
        f"[dedup] {cluster_sku} '{cluster_name[:40]}': "
        f"checking {len(cluster_urls)} URL(s) against {len(existing_rows)} rows…"
    )

    possible_reason = ""

    for row in existing_rows:
        existing_sku  = row.get("SKU", "?")
        existing_tab  = row.get("_tab", "?")
        raw_existing  = str(row.get("URL PRODUCT", "") or "")
        existing_url  = _normalize_url_for_dedup(raw_existing)

        # ── Hard reject: exact URL match (tracking-stripped but product-params kept) ──
        if existing_url and existing_url in cluster_urls:
            reason = (
                f"exact URL match with {existing_sku} "
                f"(tab: {existing_tab}, url: {existing_url[:70]})"
            )
            logger.info(
                f"[dedup] {cluster_sku} '{cluster_name[:40]}' → HARD DUPLICATE: {reason}"
            )
            return True, reason, ""

        # ── Possible duplicate: fuzzy name match (warn only, do NOT reject) ──
        if cluster_name and not possible_reason:
            existing_name = normalize_text(str(row.get("PRODUCT NAME", "") or ""))
            if existing_name:
                score = fuzz.token_sort_ratio(cluster_name, existing_name)
                if score >= _CLUSTER_NAME_THRESHOLD:
                    possible_reason = (
                        f"name similar to {existing_sku} "
                        f"(tab: {existing_tab}, score: {score}%): "
                        f"'{existing_name[:40]}'"
                    )
                    logger.info(
                        f"[dedup] {cluster_sku} '{cluster_name[:40]}' → "
                        f"POSSIBLE DUPLICATE (not rejected): {possible_reason}"
                    )

    if possible_reason:
        logger.info(
            f"[dedup] {cluster_sku} '{cluster_name[:40]}' → "
            f"PASS (possible duplicate flagged but saved)"
        )
    else:
        logger.debug(
            f"[dedup] {cluster_sku} '{cluster_name[:40]}' → PASS (no match found)"
        )
    return False, "", possible_reason


def build_existing_url_set(existing_rows: list[dict]) -> set[str]:
    """Return the set of normalised product URLs already in the sheet (fast lookup)."""
    return {
        _normalize_url_for_dedup(str(row.get("URL PRODUCT", "") or ""))
        for row in existing_rows
        if row.get("URL PRODUCT")
    }
