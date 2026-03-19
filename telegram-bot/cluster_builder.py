"""
cluster_builder.py - Groups scraped ads into product clusters.

One cluster = one unique product, represented by:
  - Up to 5 competitor product/landing page URLs
  - Up to 5 ad creative URLs (different creatives, different advertisers)
  - A generated SKU (PRD-0001, PRD-0002, …)
  - A short NOTE summarising what was found
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from utils import normalize_text

logger = logging.getLogger(__name__)

# Tracking params to strip when normalising product URLs
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "referrer", "affiliate", "source", "mc_eid",
}


# ── Public API ─────────────────────────────────────────────────────────────

def build_clusters(ads: list[dict], start_sku: int = 1) -> list["ProductCluster"]:
    """
    Group `ads` into product clusters.
    Returns a list of ProductCluster objects sorted by SKU.
    """
    clusters: list[ProductCluster] = []

    for ad in ads:
        matched = _find_matching_cluster(ad, clusters)
        if matched:
            matched.add_ad(ad)
        else:
            cluster = ProductCluster(sku_number=start_sku + len(clusters))
            cluster.add_ad(ad)
            clusters.append(cluster)

    return clusters


# ── Cluster class ──────────────────────────────────────────────────────────

class ProductCluster:
    """Represents one unique product found across multiple ads."""

    def __init__(self, sku_number: int):
        self.sku = f"PRD-{sku_number:04d}"
        self.ads: list[dict] = []
        self._seen_product_domains: set[str] = set()
        self._seen_product_urls: set[str] = set()
        # Creative dedup — three instant URL-based layers:
        self._seen_ad_ids: set[str] = set()            # 1. Facebook ad ID
        self._seen_normalized_urls: set[str] = set()   # 2. normalized ad_library_url
        self._seen_media_content: set[str] = set()     # 3. normalized CDN media path
        self.product_urls: list[str] = []      # up to 1 real landing page URL
        self.media_urls: list[str] = []         # up to 1 unique creative URL

    # ── representative signals ─────────────────────────────────────────────

    @property
    def canonical_name(self) -> str:
        """Best product name for this cluster."""
        for ad in self.ads:
            name = normalize_text(ad.get("extracted_product_name", "") or ad.get("page_title", ""))
            if name and len(name) > 3:
                return name
        return ""

    @property
    def keyword(self) -> str:
        """The search keyword that found this cluster (most common across ads)."""
        from collections import Counter
        kws = [a.get("keyword", "").strip() for a in self.ads if a.get("keyword", "").strip()]
        if not kws:
            return ""
        return Counter(kws).most_common(1)[0][0]

    @property
    def extracted_keywords(self) -> set[str]:
        """Union of keyword tokens from all ads in the cluster."""
        tokens: set[str] = set()
        for ad in self.ads:
            kws = ad.get("_extracted_keywords", [])
            tokens.update(kws)
        return tokens

    # ── adding ads ──────────────────────────────────────────────────────────

    def add_ad(self, ad: dict):
        self.ads.append(ad)
        self._try_add_product_url(
            ad.get("landing_page_url", ""),
            ad.get("advertiser_name", ""),
        )
        self._try_add_media_url(ad)

    def _try_add_product_url(self, url: str, advertiser: str = ""):
        if not url or len(self.product_urls) >= 1:
            if not url:
                logger.debug("[cluster] product URL skipped: empty")
            return

        normalized = _normalize_product_url(url)
        if not normalized:
            return

        domain = _domain(normalized)
        if not domain:
            return

        # Skip exact duplicate (after normalisation)
        if normalized in self._seen_product_urls:
            logger.debug(f"[cluster] product URL skipped (exact duplicate): {normalized[:60]}")
            return

        # Skip same domain (likely same store/product)
        if domain in self._seen_product_domains:
            logger.debug(f"[cluster] product URL skipped (same domain {domain}): {normalized[:60]}")
            return

        self.product_urls.append(normalized)
        self._seen_product_urls.add(normalized)
        self._seen_product_domains.add(domain)
        logger.info(f"[cluster] product URL added ({len(self.product_urls)}/1): {normalized[:80]}")

    def _try_add_media_url(self, ad: dict):
        if len(self.media_urls) >= 1:
            return

        advertiser  = (ad.get("advertiser_name", "") or "").strip().lower()
        ad_lib_url  = (ad.get("ad_library_url",  "") or "").strip()

        # ── Layer 1: deduplicate by Facebook ad ID ─────────────────────────
        # The ad_library_url always encodes a unique ad ID: ?id=XXXXXXXXXX
        ad_id = _extract_fb_ad_id(ad_lib_url)
        if ad_id and ad_id in self._seen_ad_ids:
            logger.debug(f"[cluster] creative skipped (duplicate ad ID {ad_id})")
            return

        # ── Layer 2: normalized ad_library_url dedup ──────────────────────
        # (catches same ad re-discovered via a different search query)
        store_url = ad_lib_url or (ad.get("media_url", "") or "").strip() or (ad.get("thumbnail_url", "") or "").strip()
        if not store_url:
            logger.debug(f"[cluster] no usable creative URL for ad from '{advertiser}'")
            return

        norm_lib = _normalize_cdn_url(store_url)
        if norm_lib and norm_lib in self._seen_normalized_urls:
            logger.debug(f"[cluster] creative skipped (normalized lib URL duplicate): {norm_lib[:60]}")
            return

        # ── Layer 4: media content fingerprint ────────────────────────────
        # Same video/image reused across different ads/advertisers.
        # Normalize the actual CDN media path (strip all expiry tokens).
        # Two ads that show the same video will have the same CDN base path
        # even if their ad IDs, advertiser names, and token params all differ.
        raw_media = (ad.get("media_url", "") or ad.get("thumbnail_url", "") or "").strip()
        norm_media = _normalize_cdn_url(raw_media) if raw_media else ""
        if norm_media and norm_media in self._seen_media_content:
            logger.debug(
                f"[cluster] creative skipped (same media content, different ad/advertiser): "
                f"{norm_media[:80]}"
            )
            return

        # ── Accept the creative ────────────────────────────────────────────
        self.media_urls.append(store_url)
        if ad_id:
            self._seen_ad_ids.add(ad_id)
        if norm_lib:
            self._seen_normalized_urls.add(norm_lib)
        if norm_media:
            self._seen_media_content.add(norm_media)
        logger.info(
            f"[cluster] creative added ({len(self.media_urls)}/1): "
            f"ad_id={ad_id} advertiser='{advertiser[:40]}'"
        )

    # ── note generation ─────────────────────────────────────────────────────

    def build_note(self) -> str:
        n_prod = len(self.product_urls)
        n_media = len(self.media_urls)
        n_adv = len({ad.get("advertiser_name", "") for ad in self.ads if ad.get("advertiser_name")})
        resolved = sum(1 for ad in self.ads if ad.get("landing_page_url"))
        parts = []
        parts.append(f"{n_prod} product URL{'s' if n_prod != 1 else ''} found")
        parts.append(f"{n_media} different creative{'s' if n_media != 1 else ''} found")
        if n_adv > 1:
            parts.append(f"clustered from {n_adv} advertisers")
        if resolved:
            parts.append(f"landing pages resolved successfully ({resolved} ads)")
        elif n_prod == 0:
            parts.append("landing pages could not be resolved")
        return ", ".join(parts)

    def best_image_url(self) -> str:
        """Return the best available product image URL for deduplication.

        Priority mirrors get_sourcing_for_cluster() in pricing_engine.py so the
        same image that is used to find the sourcing link is the one stored in
        the sheet and later used for duplicate detection.

        Order: _product_images list (first entry) → og_image_url → thumbnail_url
               → media_url
        """
        # 1. _product_images is a list of clean product shots extracted from the
        #    landing page — this is what the pricing engine tries first.
        for ad in self.ads:
            imgs = ad.get("_product_images") or []
            if imgs:
                url = (imgs[0] or "").strip()
                if url:
                    return url

        # 2. Fallback: scalar image fields
        for key in ("og_image_url", "thumbnail_url", "media_url"):
            for ad in self.ads:
                url = (ad.get(key) or "").strip()
                if url:
                    return url

        return ""

    def to_row(self) -> dict:
        """Serialise to the sheet column format."""
        return {
            "SKU": self.sku,
            "KEYWORD": self.keyword,
            "URL PRODUCT": self.product_urls[0] if self.product_urls else "",
            "ADS LIBRARY MEDIA URL": self.media_urls[0] if self.media_urls else "",
            "STATU": "",
            "SOURCING PRICE USD": "",
            "SOURCING URL": "",
            "WEIGHT GRAM": "",
            "HAS VARIANTS": "",
            "IMAGE URL": self.best_image_url(),
        }


# ── Matching logic ─────────────────────────────────────────────────────────

def _find_matching_cluster(ad: dict, clusters: list[ProductCluster]) -> Optional[ProductCluster]:
    """Find the matching cluster for `ad`, or None if it's a new product.

    Two ads belong to the same cluster only if they are for the SAME product:
      - identical Facebook ad_library_url (same ad creative), OR
      - identical normalised product landing-page URL (same product page).

    Name/text-based fuzzy matching is intentionally excluded.  Short or generic
    product names (e.g. "Cable", "Watch", "Lamp") produce 100 % text similarity
    across completely different products and would incorrectly collapse all ads
    for a keyword into a single cluster.  URL identity is the only reliable
    signal that two ads truly advertise the same product.

    Domain-only matching is also excluded: a store can sell dozens of different
    products; same domain ≠ same product.
    """
    ad_url_raw     = (ad.get("ad_library_url", "") or "").strip()
    ad_product_url = _normalize_product_url(ad.get("landing_page_url", ""))

    for cluster in clusters:
        # 1. Exact Facebook ad_library_url already in this cluster
        if ad_url_raw and any(a.get("ad_library_url", "") == ad_url_raw for a in cluster.ads):
            return cluster

        # 2. Exact normalised product URL — different ads for the same product page
        if ad_product_url and ad_product_url in cluster._seen_product_urls:
            return cluster

    return None


# ── URL normalisation helpers ───────────────────────────────────────────────

def _normalize_product_url(url: str) -> str:
    """Strip tracking parameters and normalise a product URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(clean_params, doseq=True)
        normalised = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            clean_query,
            "",   # strip fragment
        ))
        return normalised
    except Exception:
        return url


def _domain(url: str) -> str:
    """Extract root domain from a URL."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", host)
    except Exception:
        return ""


def _extract_fb_ad_id(ad_library_url: str) -> str:
    """
    Extract the numeric Facebook Ad ID from an Ads Library URL.
    e.g. 'https://www.facebook.com/ads/library/?id=1234567890' → '1234567890'
    Returns empty string if not found.
    """
    if not ad_library_url:
        return ""
    m = re.search(r"[?&]id=(\d+)", ad_library_url)
    return m.group(1) if m else ""


def _normalize_cdn_url(url: str) -> str:
    """
    Normalize a CDN URL for deduplication by removing expiry/token query parameters.
    Facebook CDN URLs like:
      https://video.cdninstagram.com/o1/...?efg=TOKEN&_nc_ht=...&_nc_cat=...&ccb=...
    become:
      https://video.cdninstagram.com/o1/...  (path only, no params)

    For non-CDN URLs (like ad_library_url), returns the full URL lowercased.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # CDN domains — strip all query params (they contain expiry tokens)
        cdn_domains = (
            "cdninstagram.com", "fbcdn.net", "fbsbx.com",
            "akamaihd.net", "cloudfront.net", "scontent",
        )
        if any(d in host for d in cdn_domains):
            # Keep only scheme + host + path (strip all query params)
            return f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")

        # For stable URLs (ad_library_url, product pages): strip only tracking params
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(clean, doseq=True)
        return urlunparse((
            parsed.scheme.lower(),
            host,
            parsed.path.rstrip("/") or "/",
            parsed.params,
            clean_query,
            "",
        ))
    except Exception:
        return url.lower()
