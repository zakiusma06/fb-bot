"""
category_detector.py - Automatically classify a product into a fixed category using AI.

Two-step classification:
  Step 1 — Product Understanding: AI describes the product in its own words.
  Step 2 — Category Mapping: AI maps the description to the closest fixed category.

This 2-step approach avoids "Other" defaults by forcing the model to first reason
about the product before constrained classification.
"""

import difflib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from openai import OpenAI

from config import PRODUCT_CATEGORIES

logger = logging.getLogger(__name__)

_CATEGORY_LIST = ", ".join(PRODUCT_CATEGORIES)


def _get_client() -> OpenAI:
    api_key  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def _closest_category(raw: str) -> str:
    """Return the closest PRODUCT_CATEGORIES match to raw, or 'Other' if nothing is close."""
    raw_lower = raw.lower().strip()
    for cat in PRODUCT_CATEGORIES:
        if cat.lower() == raw_lower:
            return cat
    matches = difflib.get_close_matches(raw_lower, [c.lower() for c in PRODUCT_CATEGORIES], n=1, cutoff=0.6)
    if matches:
        for cat in PRODUCT_CATEGORIES:
            if cat.lower() == matches[0]:
                return cat
    return "Other"


def detect_category(
    product_name: str,
    product_description: str = "",
    image_url: str = "",
) -> str:
    """
    Classify the product using a 2-step AI process.

    Step 1: Ask AI to understand and describe the product in plain language.
    Step 2: Map that description to one fixed category from PRODUCT_CATEGORIES.

    Args:
        product_name:        Canonical product title.
        product_description: Optional description text scraped from the product page.
        image_url:           Optional product image URL (used in step 1 if provided).

    Returns:
        One of the PRODUCT_CATEGORIES strings (default "Other" on any error).
    """
    if not product_name.strip():
        return "Other"

    client = _get_client()

    user_text_step1 = f"Product: {product_name}"
    if product_description:
        user_text_step1 += f"\nDescription: {product_description[:500]}"

    messages_step1: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are a product analyst. "
                "Your task is to describe what a product IS and what it DOES in 1-2 short sentences. "
                "Focus on its physical nature, function, and typical use case. "
                "Be specific and concrete."
            ),
        },
    ]

    if image_url:
        messages_step1.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_text_step1},
                {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
            ],
        })
    else:
        messages_step1.append({"role": "user", "content": user_text_step1})

    try:
        resp1 = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages_step1,
            max_tokens=80,
            temperature=0,
        )
        product_understanding = (resp1.choices[0].message.content or "").strip()
        logger.info(f"[category] step1 understanding: '{product_understanding[:80]}'")
    except Exception as e:
        logger.warning(f"[category] step1 failed for '{product_name[:50]}': {e}")
        product_understanding = product_name

    step2_prompt = (
        f"Product: {product_name}\n"
        f"What it is: {product_understanding}\n\n"
        f"Choose the BEST category from this list:\n{_CATEGORY_LIST}\n\n"
        "Rules:\n"
        "- Pick the closest match even if not perfect\n"
        "- Only choose 'Other' if the product truly cannot fit ANY other category\n"
        "- Most products fit at least one non-Other category\n"
        "- Reply with ONLY the category name — nothing else"
    )

    try:
        resp2 = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a product categorisation assistant. "
                        "You must always choose the CLOSEST category from the given list. "
                        "Do NOT default to 'Other' — only use it as a last resort."
                    ),
                },
                {"role": "user", "content": step2_prompt},
            ],
            max_tokens=15,
            temperature=0,
        )
        raw = (resp2.choices[0].message.content or "").strip()
        cat = _closest_category(raw)
        logger.info(f"[category] '{product_name[:50]}' → {cat} (raw: '{raw}')")
        return cat
    except Exception as e:
        logger.warning(f"[category] step2 failed for '{product_name[:50]}': {e}")
        return "Other"
