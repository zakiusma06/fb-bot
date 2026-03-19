"""
thumbnail_hasher.py - Perceptual hash (phash) of ad thumbnails for visual deduplication.

Downloads the thumbnail image for an ad and computes a 64-bit perceptual hash.
Two creatives whose phash Hamming distance is ≤ THRESHOLD are considered the same
visual content — same video/image even if served from completely different URLs,
re-encoded by Facebook, or uploaded by different advertisers.

Hamming distance scale (64-bit phash):
  0        = pixel-perfect identical
  1–4      = same image, JPEG/resize artefacts only
  5–10     = very similar (cropped, slightly colour-adjusted)
  11–20    = related but different
  > 20     = different images
"""

import asyncio
import io
import logging

import aiohttp

logger = logging.getLogger(__name__)

PHASH_THRESHOLD = 10      # ≤ 10/64 bits → same creative
_TIMEOUT = aiohttp.ClientTimeout(total=8)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


async def compute_thumbnail_phash(url: str) -> str:
    """
    Download the image at `url` and return its perceptual hash as a hex string.
    Returns "" on any failure (network error, not an image, etc.).
    """
    if not url or not url.startswith("http"):
        return ""

    try:
        import imagehash
        from PIL import Image

        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS) as session:
            async with session.get(url, ssl=False, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.debug(f"[phash] HTTP {resp.status} for {url[:60]}")
                    return ""
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type and "octet" not in content_type:
                    logger.debug(f"[phash] not an image ({content_type}): {url[:60]}")
                    return ""
                data = await resp.read()

        img = Image.open(io.BytesIO(data)).convert("RGB")
        h = imagehash.phash(img, hash_size=8)   # produces 64-bit hash
        result = str(h)
        logger.debug(f"[phash] computed {result} for {url[:60]}")
        return result

    except Exception as e:
        logger.debug(f"[phash] failed for {url[:60]}: {e}")
        return ""


def phashes_are_duplicate(hash1: str, hash2: str) -> bool:
    """
    Returns True if two phash hex strings represent visually identical/near-identical images.
    Hamming distance ≤ PHASH_THRESHOLD → duplicate.
    """
    if not hash1 or not hash2:
        return False
    try:
        import imagehash
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        dist = h1 - h2
        logger.debug(f"[phash] distance={dist} threshold={PHASH_THRESHOLD}")
        return dist <= PHASH_THRESHOLD
    except Exception as e:
        logger.debug(f"[phash] comparison failed: {e}")
        return False


async def enrich_ad_with_phash(ad: dict) -> dict:
    """
    Compute and store thumbnail_phash on an ad dict in-place.

    Only uses the ad's OWN thumbnail/media URL — never the product page og:image.
    Using the product page image would cause all creatives for the same product
    to get the same hash and incorrectly be flagged as duplicates.
    """
    if ad.get("thumbnail_phash"):
        return ad   # already computed

    # Only hash from the ad's own creative — NOT from product page images (og_image_url)
    for field in ("thumbnail_url", "media_url"):
        url = (ad.get(field, "") or "").strip()
        if url and url.startswith("http"):
            h = await compute_thumbnail_phash(url)
            if h:
                ad["thumbnail_phash"] = h
                logger.info(
                    f"[phash] ad {ad.get('ad_library_url','')[-15:]} "
                    f"hash={h} from field='{field}'"
                )
                return ad

    logger.debug(f"[phash] no ad thumbnail available for {ad.get('ad_library_url','')[-15:]}")
    return ad


async def enrich_ads_with_phash(ads: list[dict], concurrency: int = 8) -> list[dict]:
    """Compute phash for a batch of ads concurrently."""
    sem = asyncio.Semaphore(concurrency)

    async def _safe(ad):
        async with sem:
            return await enrich_ad_with_phash(ad)

    return list(await asyncio.gather(*[_safe(ad) for ad in ads]))
