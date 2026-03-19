"""
shopify_cache.py — In-memory Shopify product cache for instant duplicate detection.

Loaded once at startup; updated immediately when products change.
Webhook-based cross-process updates write to shopify_cache_state.json which any
process checks (via mtime) before running a duplicate lookup.

Duplicate risk score:
  Image very similar   (Hamming < 8)   → +80
  Image possibly similar (Hamming 8-12) → +50
  Title strong match   (>70%)           → +20
  Title moderate       (50-70%)         → +10
  Desc strong match    (>60%)           → +20
  Desc moderate        (40-60%)         → +10

  score ≥ 80  → DUPLICATE
  score 50-79 → POSSIBLE DUPLICATE
  score < 50  → NEW PRODUCT
"""

import asyncio
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "shopify_cache_state.json",
)
_EVENTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "shopify_webhook_events.jsonl",
)
_events_file_pos: int = 0   # byte offset — tracks how far we've read the events file

API_VERSION = "2024-01"


# ── Cache entry ────────────────────────────────────────────────────────────────

@dataclass
class CachedProduct:
    product_id:    int
    title:         str
    description:   str          # plain text, lowercased, stripped
    handle:        str
    image_hashes:  list         # phash hex strings, one per image
    admin_url:     str
    main_image_url: str
    source_url:    str = ""     # original supplier/product page URL (stored as tag)
    storefront_url: str = ""    # public customer-facing URL: /products/<handle>


@dataclass
class DuplicateResult:
    score:           int
    is_duplicate:    bool        # score >= 80
    is_possible:     bool        # score 50-79
    matched_product: Optional[CachedProduct]
    reasons:         list


# ── In-memory store ─────────────────────────────────────────────────────────────

_cache: dict = {}               # product_id → CachedProduct
_cache_loaded: bool = False
_cache_load_time: float = 0.0


# ── Shopify helpers ────────────────────────────────────────────────────────────

def _store() -> str:
    s = os.environ.get("SHOPIFY_STORE_URL", "").rstrip("/")
    if s and not s.startswith("http"):
        s = "https://" + s
    return s


def _token() -> str:
    return os.environ.get("SHOPIFY_ACCESS_TOKEN", "")


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": _token(),
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return f"{_store()}/admin/api/{API_VERSION}"


# ── Text helpers ───────────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to",
    "is", "it", "at", "be", "this", "that", "with", "by", "from",
}


def _normalize_title(title: str) -> str:
    title = re.sub(r"[^\w\s]", "", title.lower())
    return " ".join(w for w in title.split() if w not in _STOP_WORDS)


def _title_similarity(a: str, b: str) -> float:
    """Fuzzy similarity 0-100 between two product titles."""
    try:
        from rapidfuzz import fuzz
        na, nb = _normalize_title(a), _normalize_title(b)
        if not na or not nb:
            return 0.0
        return float(max(
            fuzz.ratio(na, nb),
            fuzz.token_sort_ratio(na, nb),
            fuzz.partial_ratio(na, nb),
        ))
    except Exception:
        return 0.0


def _desc_similarity(a: str, b: str) -> float:
    """Fuzzy similarity 0-100 between two plain-text descriptions."""
    try:
        from rapidfuzz import fuzz
        if not a or not b:
            return 0.0
        return float(fuzz.partial_ratio(a[:500], b[:500]))
    except Exception:
        return 0.0


# ── Image hashing ──────────────────────────────────────────────────────────────

async def _phash_from_url(url: str) -> str:
    """Download image and compute perceptual hash. Returns '' on failure."""
    if not url or not url.startswith("http"):
        return ""
    try:
        import imagehash
        from PIL import Image
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cli:
            resp = await cli.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ShopifyBot/1.0)"
            })
            if resp.status_code != 200:
                return ""
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            return str(imagehash.phash(img, hash_size=8))
    except Exception as e:
        logger.debug(f"[shopify_cache] phash failed for {url[:60]}: {e}")
        return ""


def _hamming(h1: str, h2: str) -> int:
    """Hamming distance between two phash hex strings. Returns 999 on error."""
    if not h1 or not h2:
        return 999
    try:
        import imagehash
        return int(imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2))
    except Exception:
        return 999


# ── Fetch from Shopify ─────────────────────────────────────────────────────────

async def _fetch_all_products() -> list:
    """Fetch all products via Shopify Admin API (handles pagination)."""
    products = []
    url = (
        f"{_base_url()}/products.json"
        "?limit=250&fields=id,title,body_html,handle,images,tags"
    )
    async with httpx.AsyncClient(timeout=30, headers=_headers()) as cli:
        while url:
            resp = await cli.get(url)
            if resp.status_code != 200:
                logger.error(f"[shopify_cache] HTTP {resp.status_code} fetching products")
                break
            data = resp.json()
            batch = data.get("products", [])
            products.extend(batch)
            # Shopify pagination via Link header
            link_header = resp.headers.get("Link", "")
            url = None
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    m = re.search(r"<([^>]+)>", part)
                    if m:
                        url = m.group(1)
    return products


async def _build_entry(product: dict) -> CachedProduct:
    """Build a CachedProduct from Shopify product dict, computing pHashes."""
    images = product.get("images", [])
    image_urls = [img.get("src", "") for img in images if img.get("src")]

    # Compute pHash for each image concurrently (cap at 5)
    hashes_raw = await asyncio.gather(
        *[_phash_from_url(u) for u in image_urls[:5]],
        return_exceptions=True,
    )
    image_hashes = [h for h in hashes_raw if isinstance(h, str) and h]

    product_id = int(product.get("id", 0))
    store_domain = _store().replace("https://", "").replace("http://", "")

    # Extract source_url from Shopify tags (tag format: "source_url:<url>")
    source_url = ""
    tags_raw = product.get("tags", "")
    for tag in (tags_raw.split(",") if isinstance(tags_raw, str) else []):
        tag = tag.strip()
        if tag.startswith("source_url:"):
            source_url = tag[len("source_url:"):]
            break

    handle = product.get("handle", "")
    return CachedProduct(
        product_id=product_id,
        title=product.get("title", ""),
        description=_html_to_text(product.get("body_html", "")),
        handle=handle,
        image_hashes=image_hashes,
        admin_url=f"https://{store_domain}/admin/products/{product_id}",
        main_image_url=image_urls[0] if image_urls else "",
        source_url=source_url,
        storefront_url=f"https://{store_domain}/products/{handle}" if handle else "",
    )


# ── Cache initialisation ───────────────────────────────────────────────────────

async def init_cache() -> int:
    """
    Fetch all Shopify products, compute pHashes, populate in-memory cache.
    Returns number of products loaded.
    Call once at bot startup.
    """
    global _cache, _cache_loaded, _cache_load_time

    if not _store() or not _token():
        logger.warning("[shopify_cache] Shopify credentials not set — duplicate detection disabled")
        return 0

    logger.info("[shopify_cache] Loading Shopify product cache…")
    try:
        products = await _fetch_all_products()
        logger.info(f"[shopify_cache] Fetched {len(products)} products from Shopify")

        new_cache: dict = {}
        # Process in batches of 10 to avoid too many concurrent image downloads
        for i in range(0, len(products), 10):
            batch = products[i:i + 10]
            entries = await asyncio.gather(
                *[_build_entry(p) for p in batch],
                return_exceptions=True,
            )
            for entry in entries:
                if isinstance(entry, CachedProduct):
                    new_cache[entry.product_id] = entry

        _cache = new_cache
        _cache_loaded = True
        _cache_load_time = time.time()

        logger.info(
            f"[shopify_cache] Cache ready — {len(_cache)} products, "
            f"{sum(len(p.image_hashes) for p in _cache.values())} image hashes"
        )
        _persist()
        return len(_cache)

    except Exception as e:
        logger.error(f"[shopify_cache] init_cache failed: {e}")
        return 0


# ── Persistence (cross-process sharing via JSON file) ──────────────────────────

def _product_to_dict(p: "CachedProduct") -> dict:
    return {
        "id":             p.product_id,
        "title":          p.title,
        "description":    p.description,
        "handle":         p.handle,
        "image_hashes":   p.image_hashes,
        "admin_url":      p.admin_url,
        "main_image_url": p.main_image_url,
        "source_url":     p.source_url,
        "storefront_url": p.storefront_url,
    }


def _persist() -> None:
    """Write in-memory cache to JSON for cross-process visibility.

    When the current process has a FULL cache (build_cache() was called), the
    in-memory state is authoritative and is written directly.

    When the cache is PARTIAL (e.g. the Pricing Bot only knows about the product
    it just created), we merge our entries ON TOP of whatever is already in the
    state file.  This prevents a process with an incomplete cache from wiping out
    the 74-product set built by the Moderation Bot.
    """
    try:
        if _cache_loaded and _cache_load_time > 0:
            # Full cache — write authoritatively
            products_final = [_product_to_dict(p) for p in _cache.values()]
            loaded_at_final = _cache_load_time
        else:
            # Partial cache — read existing file and overlay our entries
            existing: dict[int, dict] = {}
            loaded_at_final = _cache_load_time
            if os.path.exists(_STATE_FILE):
                try:
                    with open(_STATE_FILE, encoding="utf-8") as f:
                        raw = json.load(f)
                    for entry in raw.get("products", []):
                        existing[int(entry["id"])] = entry
                    # Preserve the authoritative timestamp from the full-cache process
                    loaded_at_final = raw.get("loaded_at", _cache_load_time)
                except Exception:
                    pass
            # Overlay our in-memory products (add / update)
            for p in _cache.values():
                existing[p.product_id] = _product_to_dict(p)
            products_final = list(existing.values())

        data = {"loaded_at": loaded_at_final, "products": products_final}
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[shopify_cache] persist failed: {e}")


def load_from_file() -> bool:
    """
    Load cache from JSON file written by another process (e.g. webhook handler).
    Returns True if successfully loaded.
    """
    global _cache, _cache_loaded, _cache_load_time
    try:
        if not os.path.exists(_STATE_FILE):
            return False
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        new_cache = {}
        for p in data.get("products", []):
            entry = CachedProduct(
                product_id=int(p["id"]),
                title=p.get("title", ""),
                description=p.get("description", ""),
                handle=p.get("handle", ""),
                image_hashes=p.get("image_hashes", []),
                admin_url=p.get("admin_url", ""),
                main_image_url=p.get("main_image_url", ""),
                source_url=p.get("source_url", ""),
                storefront_url=p.get("storefront_url", ""),
            )
            new_cache[entry.product_id] = entry
        _cache = new_cache
        _cache_loaded = True
        _cache_load_time = data.get("loaded_at", 0.0)
        logger.info(f"[shopify_cache] Reloaded {len(_cache)} products from state file")
        return True
    except Exception as e:
        logger.debug(f"[shopify_cache] load_from_file failed: {e}")
        return False


def _reload_if_stale() -> None:
    """Reload from the JSON file if it has been updated by another process."""
    global _cache_load_time
    try:
        if not os.path.exists(_STATE_FILE):
            return
        mtime = os.path.getmtime(_STATE_FILE)
        if mtime > _cache_load_time + 1:   # 1 s grace to avoid race
            load_from_file()
    except Exception:
        pass


# ── Live cache mutations ───────────────────────────────────────────────────────

async def add_or_update(product_id: int, product_data: dict = None) -> None:
    """
    Add or refresh a single product in the cache.
    If product_data is None, fetches fresh from the Shopify API.
    Call this after create_and_publish_product() to keep cache current.
    """
    global _cache
    try:
        if product_data is None:
            async with httpx.AsyncClient(timeout=15, headers=_headers()) as cli:
                resp = await cli.get(
                    f"{_base_url()}/products/{product_id}.json"
                    "?fields=id,title,body_html,handle,images,tags"
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"[shopify_cache] fetch product {product_id}: "
                        f"HTTP {resp.status_code}"
                    )
                    return
                product_data = resp.json().get("product", {})

        entry = await _build_entry(product_data)
        _cache[entry.product_id] = entry
        _persist()
        logger.info(f"[shopify_cache] Cached product {product_id} (title: {entry.title[:40]})")
    except Exception as e:
        logger.error(f"[shopify_cache] add_or_update({product_id}) failed: {e}")


def _persist_remove(product_id: int) -> None:
    """Surgically remove one product from the state file (safe for partial-cache processes)."""
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data["products"] = [p for p in data.get("products", []) if int(p["id"]) != product_id]
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[shopify_cache] persist_remove failed: {e}")


def remove(product_id: int) -> None:
    """Remove a product from the cache (call after deleting from Shopify)."""
    global _cache
    _cache.pop(int(product_id), None)
    if _cache_loaded and _cache_load_time > 0:
        _persist()           # full cache in memory — overwrite is safe
    else:
        _persist_remove(int(product_id))   # partial cache — targeted deletion only
    logger.info(f"[shopify_cache] Removed product {product_id} from cache")


def apply_webhook_event(event: str, product_data: dict) -> None:
    """
    Apply a Shopify webhook event synchronously.
    event: "create" | "update" | "delete"
    Spawns async tasks for create/update (image hashing).
    """
    product_id = int(product_data.get("id", 0))
    if not product_id:
        return

    if event == "delete":
        remove(product_id)
        return

    # create / update — re-hash images in background
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(add_or_update(product_id, product_data))
    except RuntimeError:
        # No running loop — best-effort synchronous skip
        logger.debug(f"[shopify_cache] No running loop for webhook event {event}/{product_id}")


# ── Webhook event processing ───────────────────────────────────────────────────

async def process_webhook_events() -> None:
    """
    Read new lines from the webhook events JSONL file (written by the API server)
    and apply them to the in-memory cache.

    Uses a byte-offset to only process lines we haven't seen yet, so this is
    O(new_events) even when the file grows large.
    """
    global _events_file_pos

    if not os.path.exists(_EVENTS_FILE):
        return

    try:
        file_size = os.path.getsize(_EVENTS_FILE)
        if file_size <= _events_file_pos:
            return   # nothing new

        with open(_EVENTS_FILE, "r", encoding="utf-8") as f:
            f.seek(_events_file_pos)
            new_lines = f.readlines()
            _events_file_pos = f.tell()

        tasks = []
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            event      = ev.get("event", "update")
            product_id = int(ev.get("product_id", 0))
            product_data = ev.get("product_data") or {}

            if not product_id:
                continue

            if event == "delete":
                remove(product_id)
            elif event in ("create", "update"):
                # product_data from webhook may include images — use it directly
                tasks.append(_build_and_store(product_id, product_data))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        logger.warning(f"[shopify_cache] process_webhook_events failed: {e}")


async def _build_and_store(product_id: int, product_data: dict) -> None:
    """Helper: build cache entry from product_data and store it."""
    try:
        entry = await _build_entry(product_data)
        _cache[entry.product_id] = entry
        _persist()
        logger.info(f"[shopify_cache] Webhook: updated product {product_id} in cache")
    except Exception as e:
        logger.debug(f"[shopify_cache] _build_and_store({product_id}) failed: {e}")


def register_webhooks(callback_url_base: str) -> bool:
    """
    Register the three Shopify product webhooks (create, update, delete).
    callback_url_base should be the HTTPS base URL of the API server,
    e.g. "https://my-repl.replit.app/api".

    Returns True if all three were registered (or already exist).
    """
    import requests as _requests

    topics = ["products/create", "products/update", "products/delete"]
    webhook_url = f"{callback_url_base.rstrip('/')}/shopify/webhooks"
    headers = {
        "X-Shopify-Access-Token": _token(),
        "Content-Type": "application/json",
    }
    success = True

    for topic in topics:
        payload = {"webhook": {"topic": topic, "address": webhook_url, "format": "json"}}
        try:
            resp = _requests.post(
                f"{_base_url()}/webhooks.json",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (201, 422):   # 422 = already registered
                logger.info(f"[shopify_cache] Webhook registered: {topic} → {webhook_url}")
            else:
                logger.warning(
                    f"[shopify_cache] Webhook registration failed for {topic}: "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )
                success = False
        except Exception as e:
            logger.error(f"[shopify_cache] register_webhooks({topic}) failed: {e}")
            success = False

    return success


# ── Duplicate detection ────────────────────────────────────────────────────────

async def check_duplicate(
    title:       str,
    description: str,
    image_urls:  list,
    source_url:  str = "",
) -> DuplicateResult:
    """
    Compare a candidate product against the Shopify cache.

    Returns a DuplicateResult with score and the best-matching cached product.
    This runs in < 200 ms because image hashes are pre-computed and only the
    candidate's images need downloading (up to 3).

    source_url: the supplier/product page URL from the sheet (URL PRODUCT column).
                If it matches a cached product's source_url tag, score = 100.
    """
    # Process any webhook events written by the API server
    await process_webhook_events()
    # Check if another process updated the cache file
    _reload_if_stale()

    if not _cache:
        return DuplicateResult(
            score=0, is_duplicate=False, is_possible=False,
            matched_product=None, reasons=[],
        )

    # ── URL-based shortcut: exact source URL match is a definitive duplicate ──
    if source_url:
        norm_src = source_url.strip().rstrip("/").lower()
        for cached in _cache.values():
            if cached.source_url:
                norm_cached = cached.source_url.strip().rstrip("/").lower()
                if norm_src == norm_cached:
                    return DuplicateResult(
                        score=100, is_duplicate=True, is_possible=False,
                        matched_product=cached,
                        reasons=[f"Exact source URL match: {source_url}"],
                    )

    # Hash the candidate's images (up to 3)
    candidate_hashes = []
    for url in (image_urls or [])[:3]:
        h = await _phash_from_url(url)
        if h:
            candidate_hashes.append(h)

    plain_desc = _html_to_text(description)
    best_score = 0
    best_match: Optional[CachedProduct] = None
    best_reasons: list = []

    for cached in _cache.values():
        score = 0
        reasons = []

        # ── Image similarity (primary) ───────────────────────────────────
        if candidate_hashes and cached.image_hashes:
            min_dist = min(
                _hamming(ch, sh)
                for ch in candidate_hashes
                for sh in cached.image_hashes
            )
            if min_dist < 8:
                score += 80
                reasons.append(f"Image very similar (Hamming={min_dist})")
            elif min_dist <= 12:
                score += 50
                reasons.append(f"Image possibly similar (Hamming={min_dist})")

        # ── Title similarity ─────────────────────────────────────────────
        tsim = _title_similarity(title, cached.title)
        if tsim > 70:
            score += 20
            reasons.append(f"Title match {tsim:.0f}%")
        elif tsim > 50:
            score += 10
            reasons.append(f"Title moderate match {tsim:.0f}%")

        # ── Description similarity ───────────────────────────────────────
        dsim = _desc_similarity(plain_desc, cached.description)
        if dsim > 60:
            score += 20
            reasons.append(f"Description match {dsim:.0f}%")
        elif dsim > 40:
            score += 10
            reasons.append(f"Description moderate match {dsim:.0f}%")

        if score > best_score:
            best_score = score
            best_match = cached
            best_reasons = reasons

    return DuplicateResult(
        score=best_score,
        is_duplicate=best_score >= 80,
        is_possible=50 <= best_score < 80,
        matched_product=best_match,
        reasons=best_reasons,
    )


async def check_duplicate_by_image(image_url: str) -> Optional[DuplicateResult]:
    """
    Fast image-only pre-check against the Shopify cache.

    Downloads the image at `image_url`, computes its pHash, and compares it
    against every cached product's pre-computed hashes.

    Returns a DuplicateResult only when a confident match is found
    (Hamming distance < 8, i.e. the same score as the ≥80 threshold in
    check_duplicate).  Returns None if no confident match is found or if
    the image cannot be fetched.

    Caller should run this BEFORE check_duplicate() as the primary signal
    when a reliable product image URL is available.
    """
    if not image_url or not _cache:
        return None

    candidate_hash = await _phash_from_url(image_url)
    if not candidate_hash:
        return None

    for cached in _cache.values():
        if not cached.image_hashes:
            continue
        min_dist = min(_hamming(candidate_hash, sh) for sh in cached.image_hashes)
        if min_dist < 8:
            return DuplicateResult(
                score=80,
                is_duplicate=True,
                is_possible=False,
                matched_product=cached,
                reasons=[f"Sheet image match (Hamming={min_dist})"],
            )

    return None


def cache_size() -> int:
    """Return number of products currently in the cache."""
    return len(_cache)


def is_loaded() -> bool:
    """Return True if the cache has been initialised."""
    return _cache_loaded
