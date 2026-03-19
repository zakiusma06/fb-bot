"""
utils.py - Shared utility helpers.
"""

import re
import logging
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Return a canonical URL for deduplication (strip query params, lowercase host)."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",   # params
            "",   # query
            "",   # fragment
        ))
        return normalized
    except Exception:
        return url.strip().lower()


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace for text comparison."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def clean_product_name(name: str) -> str:
    """Strip common noise from extracted product names."""
    if not name:
        return ""
    # Remove trailing punctuation, common suffixes
    name = re.sub(r"[|–—\-]+.*$", "", name)  # cut at separator
    name = name.strip(" .,|–—-")
    return name


def safe_str(val) -> str:
    """Convert any value to a safe string for sheet writing."""
    if val is None:
        return ""
    return str(val).strip()


def truncate(text: str, max_len: int = 500) -> str:
    """Truncate a string to max_len characters."""
    if text and len(text) > max_len:
        return text[:max_len] + "…"
    return text or ""
