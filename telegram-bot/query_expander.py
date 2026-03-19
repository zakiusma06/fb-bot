"""
query_expander.py - Generate multiple search query variations from product page content.

Given a product title, page title, description and keywords extracted from a landing
page, produces a diverse set of search queries to find the same product sold by
different advertisers on Meta Ads Library.

Example:
  title = "Détecteur d'angle numérique magnétique"
  →  ["Détecteur d'angle numérique magnétique",
      "détecteur d'angle numérique",
      "angle numérique magnétique",
      "détecteur angle magnétique",
      "numérique magnétique",
      ...]
"""

import logging
import re
from itertools import combinations

logger = logging.getLogger(__name__)

# Multilingual stop words (French + English) — excluded when building sub-phrases
_STOP_WORDS = {
    # French
    "le", "la", "les", "de", "du", "des", "un", "une", "en", "et", "ou",
    "pour", "sur", "avec", "par", "dans", "au", "aux", "se", "est", "ce",
    "qui", "que", "quoi", "dont", "où", "plus", "très", "bien", "comme",
    "son", "sa", "ses", "mon", "ma", "mes", "ton", "ta", "tes", "votre",
    "notre", "nos", "vos", "leur", "leurs",
    # English
    "the", "a", "an", "and", "or", "for", "of", "with", "in", "on", "to",
    "by", "at", "is", "it", "be", "as", "are", "from", "this", "that",
    "you", "your", "our", "my", "we", "i", "he", "she", "they", "its",
    "free", "buy", "shop", "get", "now", "best", "new", "sale", "off",
    "up", "out", "about", "more", "most", "all", "just", "only", "also",
}


def expand_product_queries(
    product_title: str = "",
    page_title: str = "",
    description_text: str = "",
    bullet_points: list[str] = None,
    keywords: list[str] = None,
    max_queries: int = 10,
) -> list[str]:
    """
    Generate a diverse list of search query variations for a product.

    Args:
        product_title: The main product name (og:title, H1, etc.)
        page_title:    The <title> tag content (may differ)
        description_text: Short description or meta description text
        bullet_points: List of bullet point strings from the product page
        keywords:      Already-extracted keyword tokens
        max_queries:   Maximum number of queries to return

    Returns:
        List of search query strings, most specific first.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str):
        q = re.sub(r"\s+", " ", q).strip()
        key = q.lower()
        if q and key not in seen and len(q) > 2 and not key.isnumeric():
            queries.append(q)
            seen.add(key)

    best_name = (product_title or page_title or "").strip()

    # ── 1. Full product title as-is ────────────────────────────────────────
    if best_name:
        _add(best_name)
        logger.info(f"[query_expand] product title: '{best_name[:80]}'")

    # ── 2. Page title (if different from product title) ───────────────────
    if page_title and page_title.strip().lower() != best_name.lower():
        # Strip site name suffix (e.g. "Product | My Shop" → "Product")
        clean_page_title = re.split(r"\s*[|–—-]\s*", page_title.strip())[0].strip()
        if clean_page_title:
            _add(clean_page_title)

    # ── 3. Sub-phrases from the product title ─────────────────────────────
    if best_name:
        tokens = _significant_tokens(best_name)
        logger.info(f"[query_expand] significant tokens: {tokens}")

        if len(tokens) >= 2:
            # Sliding windows: 4, 3, 2 words
            for size in (4, 3, 2):
                for i in range(len(tokens) - size + 1):
                    _add(" ".join(tokens[i : i + size]))

            # Drop first word
            if len(tokens) >= 3:
                _add(" ".join(tokens[1:]))
                _add(" ".join(tokens[:-1]))

            # Non-contiguous 3-word combos from the significant tokens
            if len(tokens) >= 4:
                for combo in combinations(tokens, 3):
                    _add(" ".join(combo))
                    if len(queries) >= max_queries * 2:
                        break

    # ── 4. Description phrases ─────────────────────────────────────────────
    if description_text:
        desc_tokens = _significant_tokens(description_text)
        if desc_tokens:
            _add(" ".join(desc_tokens[:4]))
            _add(" ".join(desc_tokens[:3]))
            # Combine best product tokens + first desc token
            if best_name:
                prod_tokens = _significant_tokens(best_name)
                if prod_tokens and desc_tokens:
                    _add(" ".join(prod_tokens[:2]) + " " + desc_tokens[0])

    # ── 5. Bullet point phrases ────────────────────────────────────────────
    for bullet in (bullet_points or [])[:3]:
        bt = _significant_tokens(bullet)
        if len(bt) >= 2:
            _add(" ".join(bt[:4]))
            _add(" ".join(bt[:3]))

    # ── 6. Raw extracted keywords ──────────────────────────────────────────
    for kw in (keywords or [])[:6]:
        _add(kw)

    # ── 7. Title in a different word order ────────────────────────────────
    if best_name:
        words = best_name.split()
        if len(words) >= 3:
            # Move last word to front
            _add(words[-1] + " " + " ".join(words[:-1]))

    result = queries[:max_queries]
    logger.info(f"[query_expand] generated {len(result)} queries:")
    for q in result:
        logger.info(f"  • '{q}'")

    return result


def _significant_tokens(text: str) -> list[str]:
    """
    Extract meaningful word tokens from text.
    Keeps accented characters; removes punctuation; filters stop words.
    """
    # Normalise: keep letters (incl. accented), digits, spaces
    cleaned = re.sub(
        r"[^\w\sàáâãäåæçèéêëìíîïðñòóôõöùúûüýþÿÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖÙÚÛÜÝÞ]",
        " ",
        text,
    )
    words = cleaned.split()
    return [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]
