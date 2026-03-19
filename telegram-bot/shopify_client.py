"""
shopify_client.py - Shopify Admin REST API wrapper.
"""

import logging
import os
import re
import sys
import unicodedata

import requests

sys.path.insert(0, os.path.dirname(__file__))

try:
    import shopify_cache as _shopify_cache
except ImportError:
    _shopify_cache = None

logger = logging.getLogger(__name__)

API_VERSION = "2024-01"


def _store() -> str:
    return os.environ.get("SHOPIFY_STORE_URL", "").rstrip("/")


def _token() -> str:
    return os.environ.get("SHOPIFY_ACCESS_TOKEN", "")


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": _token(),
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    store = _store()
    if not store.startswith("http"):
        store = "https://" + store
    return f"{store}/admin/api/{API_VERSION}"


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text


def create_and_publish_product(
    title:            str,
    body_html:        str,
    price:            str,
    compare_at_price: str,
    image_urls:       list,
    source_url:       str = "",
) -> dict:
    """
    Create a Shopify product and publish it immediately (status=active).

    Returns:
      {
        "id":        int,
        "handle":    str,
        "admin_url": str,
        "store_url": str,
        "images":    [ {"id": int, "src": str, "position": int}, ... ],
      }
    On failure returns {}.
    """
    if not _store() or not _token():
        logger.error("[shopify] SHOPIFY_STORE_URL or SHOPIFY_ACCESS_TOKEN not set")
        return {}

    handle = _slugify(title)
    images = [{"src": url, "position": i + 1} for i, url in enumerate(image_urls[:5])]
    tags   = [f"source_url:{source_url}"] if source_url else []

    payload = {
        "product": {
            "title":     title,
            "body_html": body_html,
            "handle":    handle,
            "status":    "active",
            "images":    images,
            "tags":      ",".join(tags),
            "variants": [
                {
                    "price":            str(price),
                    "compare_at_price": str(compare_at_price),
                    "inventory_management": None,
                }
            ],
        }
    }

    try:
        resp = requests.post(
            f"{_base_url()}/products.json",
            json=payload,
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        product      = resp.json()["product"]
        product_id   = product["id"]
        final_handle = product.get("handle", handle)

        store_domain = _store().replace("https://", "").replace("http://", "")
        admin_url    = f"https://{store_domain}/admin/products/{product_id}"
        store_url    = f"https://{store_domain}/products/{final_handle}"

        raw_images = product.get("images", [])
        images_out = [
            {"id": img["id"], "src": img["src"], "position": img.get("position", idx + 1)}
            for idx, img in enumerate(raw_images)
        ]

        logger.info(
            f"[shopify] Created & published product id={product_id} "
            f"handle={final_handle} images={len(images_out)}"
        )
        result = {
            "id":        product_id,
            "handle":    final_handle,
            "admin_url": admin_url,
            "store_url": store_url,
            "images":    images_out,
        }
        # Notify the duplicate-detection cache
        if _shopify_cache:
            try:
                _shopify_cache.apply_webhook_event("create", product)
            except Exception:
                pass
        return result
    except Exception as e:
        logger.error(f"[shopify] create_and_publish_product failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"[shopify] Response: {e.response.text}")
        return {}


def update_product_field(
    product_id: int,
    title:      str | None = None,
    body_html:  str | None = None,
) -> bool:
    """Update product title and/or description."""
    payload: dict = {"product": {"id": product_id}}
    if title     is not None: payload["product"]["title"]     = title
    if body_html is not None: payload["product"]["body_html"] = body_html
    try:
        resp = requests.put(
            f"{_base_url()}/products/{product_id}.json",
            json=payload,
            headers=_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        logger.info(f"[shopify] Updated product {product_id}")
        return True
    except Exception as e:
        logger.error(f"[shopify] update_product_field({product_id}) failed: {e}")
        return False


def delete_product_image(product_id: int, image_id: int) -> bool:
    """Delete a single image from a Shopify product."""
    try:
        resp = requests.delete(
            f"{_base_url()}/products/{product_id}/images/{image_id}.json",
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"[shopify] Deleted image {image_id} from product {product_id}")
        return True
    except Exception as e:
        logger.error(f"[shopify] delete_product_image({product_id}, {image_id}) failed: {e}")
        return False


def delete_product(product_id: int) -> bool:
    """Permanently delete a Shopify product."""
    try:
        resp = requests.delete(
            f"{_base_url()}/products/{product_id}.json",
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"[shopify] Deleted product {product_id}")
        # Remove from duplicate-detection cache immediately
        if _shopify_cache:
            try:
                _shopify_cache.remove(product_id)
            except Exception:
                pass
        return True
    except Exception as e:
        logger.error(f"[shopify] delete_product({product_id}) failed: {e}")
        return False


# kept for any legacy references
def publish_product(product_id: int) -> bool:
    """Set Shopify product status → active."""
    try:
        resp = requests.put(
            f"{_base_url()}/products/{product_id}.json",
            json={"product": {"id": product_id, "status": "active"}},
            headers=_headers(),
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[shopify] publish_product({product_id}) failed: {e}")
        return False


# kept for any legacy references
def create_draft_product(
    title:            str,
    body_html:        str,
    price:            str,
    compare_at_price: str,
    image_urls:       list,
) -> dict:
    result = create_and_publish_product(title, body_html, price, compare_at_price, image_urls)
    return result
