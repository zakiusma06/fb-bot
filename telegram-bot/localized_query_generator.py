"""
localized_query_generator.py - Language-aware, semantically smart keyword generation.

For a given product + target country, generates up to N search queries in the
correct language that an advertiser would realistically use on Meta Ads Library.

Uses:
  1. AI (Replit OpenAI integration) for smart synonym/use-case queries when available.
  2. Rule-based sub-phrase generation as fallback.

Example:
  product: "Détecteur d'angle numérique magnétique", country: France
  → ["détecteur d'angle numérique magnétique",
     "niveau d'angle numérique",
     "inclinomètre numérique",
     "rapporteur d'angle numérique",
     "outil angle numérique",
     "mesure d'angle magnétique",
     "digital angle finder",
     ...]
"""

import asyncio
import logging
import os
import re
from itertools import combinations

logger = logging.getLogger(__name__)

# ── Country → language mapping ───────────────────────────────────────────────
_COUNTRY_LANGUAGE: dict[str, tuple[str, str]] = {
    # (language_code, language_name)
    "France":        ("fr", "French"),
    "Belgium":       ("fr", "French"),
    "Switzerland":   ("fr", "French"),
    "Canada":        ("fr", "French"),
    "Germany":       ("de", "German"),
    "Austria":       ("de", "German"),
    "Spain":         ("es", "Spanish"),
    "Mexico":        ("es", "Spanish"),
    "Colombia":      ("es", "Spanish"),
    "Argentina":     ("es", "Spanish"),
    "Italy":         ("it", "Italian"),
    "Portugal":      ("pt", "Portuguese"),
    "Brazil":        ("pt", "Portuguese"),
    "Netherlands":   ("nl", "Dutch"),
    "Poland":        ("pl", "Polish"),
    "Romania":       ("ro", "Romanian"),
    "Czech Republic": ("cs", "Czech"),
    "Hungary":       ("hu", "Hungarian"),
    "Sweden":        ("sv", "Swedish"),
    "Denmark":       ("da", "Danish"),
    "Finland":       ("fi", "Finnish"),
    "Norway":        ("no", "Norwegian"),
    "Greece":        ("el", "Greek"),
    "Turkey":        ("tr", "Turkish"),
    "Japan":         ("ja", "Japanese"),
    "South Korea":   ("ko", "Korean"),
    "China":         ("zh", "Chinese"),
    "All":           ("en", "English"),
    "United States": ("en", "English"),
    "United Kingdom": ("en", "English"),
    "Australia":     ("en", "English"),
    "Ireland":       ("en", "English"),
}

_STOP_WORDS = {
    # French
    "le", "la", "les", "de", "du", "des", "un", "une", "en", "et", "ou",
    "pour", "sur", "avec", "par", "dans", "au", "aux", "se", "est", "ce",
    "qui", "que", "quoi", "dont", "où", "plus", "très", "bien", "comme",
    "son", "sa", "ses", "mon", "ma", "mes", "ton", "ta", "tes",
    "votre", "notre", "nos", "vos", "leur", "leurs",
    # English
    "the", "a", "an", "and", "or", "for", "of", "with", "in", "on", "to",
    "by", "at", "is", "it", "be", "as", "are", "from", "this", "that",
    "you", "your", "our", "my", "we", "i", "he", "she", "they", "its",
    "free", "buy", "shop", "get", "now", "best", "new", "sale", "off",
    "up", "out", "about", "more", "most", "all", "just", "only", "also",
    # German
    "die", "der", "das", "ein", "eine", "und", "oder", "für", "von", "mit",
    "im", "an", "auf", "zu", "bei", "aus", "ist", "es", "se", "in",
    # Spanish
    "el", "la", "los", "las", "de", "del", "un", "una", "y", "o",
    "para", "por", "con", "en", "a", "al",
    # Italian
    "il", "lo", "la", "i", "gli", "le", "di", "del", "un", "una",
    "e", "o", "per", "con", "in",
}


def generate_localized_queries(
    product_title: str,
    page_title: str = "",
    description_text: str = "",
    bullet_points: list[str] = None,
    country: str = "All",
    max_queries: int = 10,
) -> list[str]:
    """
    Generate language-aware, semantically diverse search query variations.

    First tries AI generation (async → runs sync wrapper via asyncio.run).
    Falls back to rule-based generation.

    Args:
        product_title:    Main product name extracted from the page.
        page_title:       HTML <title> content.
        description_text: og:description or meta description.
        bullet_points:    Product feature bullet points.
        country:          User-selected country (maps to a language).
        max_queries:      Maximum number of queries to return.

    Returns:
        List of search query strings, most specific first.
    """
    lang_code, lang_name = _COUNTRY_LANGUAGE.get(country, ("en", "English"))
    logger.info(
        f"[localized_query] country='{country}' "
        f"detected_language='{lang_name}' ({lang_code})"
    )

    best_name = (product_title or page_title or "").strip()
    logger.info(f"[localized_query] extracted_product='{best_name[:80]}'")

    # Try AI generation first (blocking call via asyncio)
    ai_queries = _try_ai_generation(
        product_title=best_name,
        description_text=description_text,
        bullet_points=bullet_points or [],
        lang_name=lang_name,
        max_queries=max_queries,
    )
    if ai_queries:
        logger.info(
            f"[localized_query] generated {len(ai_queries)} AI queries for '{lang_name}'"
        )
        for q in ai_queries:
            logger.info(f"  • '{q}'")
        return ai_queries

    # Fallback: rule-based generation
    queries = _rule_based_queries(
        product_title=best_name,
        page_title=page_title,
        description_text=description_text,
        bullet_points=bullet_points or [],
        max_queries=max_queries,
    )
    logger.info(
        f"[localized_query] generated {len(queries)} rule-based queries (AI unavailable)"
    )
    for q in queries:
        logger.info(f"  • '{q}'")
    return queries


def _try_ai_generation(
    product_title: str,
    description_text: str,
    bullet_points: list[str],
    lang_name: str,
    max_queries: int,
) -> list[str]:
    """
    Attempt to generate queries via Replit AI integration.
    Returns empty list if AI is unavailable or fails.
    """
    try:
        from openai import OpenAI

        base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
        api_key  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "")

        if not base_url or not api_key:
            return []

        client = OpenAI(api_key=api_key, base_url=base_url)

        bullets_text = ""
        if bullet_points:
            bullets_text = "\nProduct features:\n" + "\n".join(f"- {b}" for b in bullet_points[:5])

        desc_text = f"\nDescription: {description_text[:200]}" if description_text else ""

        prompt = (
            f"You are an expert Facebook advertiser researching competitor products in Meta Ads Library.\n\n"
            f"Product: {product_title}\n"
            f"{desc_text}"
            f"{bullets_text}\n\n"
            f"Generate exactly {max_queries} search queries in {lang_name} that an advertiser "
            f"would use when running ads for this product. These queries should:\n"
            f"1. Exact product phrase\n"
            f"2. Simplified commercial phrase (shorter, catchier)\n"
            f"3. Synonym phrases (different words, same product)\n"
            f"4. Use-case phrases (what the product does / solves)\n"
            f"5. Broader category phrases\n\n"
            f"Return ONLY a plain numbered list:\n"
            f"1. query one\n2. query two\n...\n"
            f"No explanations. All queries must be in {lang_name}."
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=600,
            timeout=15,
        )
        raw = response.choices[0].message.content or ""
        queries = _parse_numbered_list(raw)
        if len(queries) >= 3:
            return queries[:max_queries]

    except Exception as e:
        logger.debug(f"[localized_query] AI generation failed: {e}")

    return []


def _rule_based_queries(
    product_title: str,
    page_title: str = "",
    description_text: str = "",
    bullet_points: list[str] = None,
    max_queries: int = 10,
) -> list[str]:
    """Rule-based fallback: sub-phrases + description phrases + bullet phrases."""
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str):
        q = re.sub(r"\s+", " ", q).strip()
        key = q.lower()
        if q and key not in seen and len(q) > 2 and not key.isnumeric():
            queries.append(q)
            seen.add(key)

    best_name = (product_title or page_title or "").strip()

    # 1. Full exact title
    if best_name:
        _add(best_name)

    # 2. Cleaned page title (strip site name)
    if page_title and page_title.strip().lower() != best_name.lower():
        clean = re.split(r"\s*[|–—-]\s*", page_title.strip())[0].strip()
        if clean:
            _add(clean)

    # 3. Sub-phrases (4→3→2 word windows) from significant tokens
    if best_name:
        tokens = _significant_tokens(best_name)
        for size in (4, 3, 2):
            for i in range(len(tokens) - size + 1):
                _add(" ".join(tokens[i: i + size]))
        if len(tokens) >= 3:
            _add(" ".join(tokens[1:]))
            _add(" ".join(tokens[:-1]))
        if len(tokens) >= 4:
            for combo in combinations(tokens, 3):
                _add(" ".join(combo))
                if len(queries) >= max_queries * 2:
                    break

    # 4. Description phrases
    if description_text:
        dt = _significant_tokens(description_text)
        if dt:
            _add(" ".join(dt[:4]))
            _add(" ".join(dt[:3]))

    # 5. Bullet point phrases
    for bullet in (bullet_points or [])[:4]:
        bt = _significant_tokens(bullet)
        if len(bt) >= 2:
            _add(" ".join(bt[:4]))
            _add(" ".join(bt[:3]))

    result = queries[:max_queries]
    return result


def _significant_tokens(text: str) -> list[str]:
    """Extract meaningful word tokens, filtering stop words."""
    cleaned = re.sub(
        r"[^\w\sàáâãäåæçèéêëìíîïðñòóôõöùúûüýþÿÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖÙÚÛÜÝÞ]",
        " ",
        text,
    )
    words = cleaned.split()
    return [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]


def _parse_numbered_list(text: str) -> list[str]:
    """Parse AI output like '1. query one\n2. query two' into a list."""
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        m = re.match(r"^\d+[.)]\s*(.+)$", line)
        if m:
            q = m.group(1).strip().strip("\"'")
            if q:
                results.append(q)
    return results
