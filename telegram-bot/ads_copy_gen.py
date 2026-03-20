"""
ads_copy_gen.py - AI-powered ad copy generation for the Ads Launch bot.

Generates primary texts and headlines using OpenAI (via Replit AI Integration).
"""

import logging
import os
import sys
import re

sys.path.insert(0, os.path.dirname(__file__))

from openai import OpenAI

logger = logging.getLogger(__name__)


_TIMEOUT = 20  # seconds — hard cap on the OpenAI call


def _client() -> OpenAI:
    api_key  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url, timeout=_TIMEOUT)
    return OpenAI(api_key=api_key, timeout=_TIMEOUT)


def generate_ad_copy(
    product_name:    str,
    product_url:    str,
    landing_url:    str,
    price:           str,
    keyword:         str,
    language:        str,
    tone:            str,
    n_texts:         int,
    n_headlines:     int,
) -> dict:
    """
    Generate ad primary texts and headlines.
    Returns:
      {
        "primary_texts": ["text1", "text2", ...],
        "headlines":     ["h1", "h2", ...],
      }
    Falls back to template copy if AI is unavailable.
    """
    # If only one type is needed, build a focused prompt
    if n_texts == 0 and n_headlines == 0:
        return {"primary_texts": [], "headlines": []}

    style = tone.strip() if tone and tone.strip() else "persuasive, benefit-focused"

    # Shared strict style block — the user's instruction is the absolute law
    style_block = f"""USER INSTRUCTION (THIS IS A HARD RULE — obey it literally above everything else):
"{style}"

If the instruction says "1 line" → each text must be exactly 1 line.
If it says "2 emojis" → use exactly 2 emojis, no more, no less.
If it says "short" → keep it short, do not add extra sentences.
If it says "no emojis" → use zero emojis.
The instruction overrides ALL other rules below. Do not add structure (hook/CTA/etc.) unless the instruction asks for it."""

    if n_texts == 0:
        prompt = f"""You are a Facebook ads copywriter. Follow the user instruction below to the letter.

PRODUCT: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language} — write ONLY in {language}.

{style_block}

Additional rules (apply only if they don't conflict with the user instruction above):
- No hashtags. Do not mention "Facebook" or "Meta".
- Each option must be meaningfully different.

Generate exactly {n_headlines} HEADLINE options.

Format EXACTLY (no extra text):
HEADLINES:
1. <headline>
2. <headline>
"""
    elif n_headlines == 0:
        prompt = f"""You are a Facebook ads copywriter. Follow the user instruction below to the letter.

PRODUCT: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language} — write ONLY in {language}.

{style_block}

Additional rules (apply only if they don't conflict with the user instruction above):
- No hashtags. Do not mention "Facebook" or "Meta".
- Each option must be meaningfully different.

Generate exactly {n_texts} PRIMARY TEXT options.

Format EXACTLY (no extra text):
PRIMARY TEXTS:
1. <text>
2. <text>
"""
    else:
        prompt = f"""You are a Facebook ads copywriter. Follow the user instruction below to the letter.

PRODUCT: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language} — write ONLY in {language}.

{style_block}

Additional rules (apply only if they don't conflict with the user instruction above):
- No hashtags. Do not mention "Facebook" or "Meta".
- Each option must be meaningfully different.

Generate exactly {n_texts} PRIMARY TEXT options and exactly {n_headlines} HEADLINE options.

Format EXACTLY (no extra text before or after):

PRIMARY TEXTS:
1. <text>
2. <text>
...

HEADLINES:
1. <headline>
2. <headline>
...
"""

    try:
        c = _client()
        resp = c.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.85,
        )
        raw = resp.choices[0].message.content.strip()
        return _parse_copy_response(raw, n_texts, n_headlines, product_name, price, language)
    except Exception as e:
        logger.error(f"[ads_copy_gen] AI call failed: {e}")
        result = _fallback_copy(product_name, price, language, n_texts, n_headlines)
        result["is_fallback"] = True
        return result


def _parse_copy_response(raw: str, n_texts: int, n_headlines: int, product_name: str, price: str, language: str) -> dict:
    primary_texts = []
    headlines     = []

    sections = re.split(r"\n(?=PRIMARY TEXTS:|HEADLINES:)", raw, flags=re.IGNORECASE)
    for section in sections:
        section = section.strip()
        if re.match(r"^PRIMARY TEXTS:", section, re.IGNORECASE):
            lines = section.split("\n")[1:]
            for line in lines:
                line = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
                if line:
                    primary_texts.append(line)
        elif re.match(r"^HEADLINES:", section, re.IGNORECASE):
            lines = section.split("\n")[1:]
            for line in lines:
                line = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
                if line:
                    headlines.append(line)

    if not primary_texts or not headlines:
        return _fallback_copy(product_name, price, language, n_texts, n_headlines)

    return {
        "primary_texts": primary_texts[:n_texts],
        "headlines":     headlines[:n_headlines],
    }


def fallback_copy(product_name: str, price: str, language: str, n_texts: int, n_headlines: int) -> dict:
    return _fallback_copy(product_name, price, language, n_texts, n_headlines)


_FALLBACK_TEMPLATES = {
    "fr": {
        "texts": [
            "✨ Découvrez {product_name}. Commandez dès maintenant !",
            "🎯 {product_name} — qualité garantie, livraison rapide. Seulement {price} !",
            "Ne manquez pas {product_name}. Stock limité !",
            "Transformez votre quotidien avec {product_name}. Achetez maintenant.",
            "{product_name} — la solution qu'il vous faut. Commandez aujourd'hui !",
        ],
        "headlines": [
            "Obtenez {product_name} maintenant",
            "Seulement {price} — Commandez aujourd'hui",
            "Achetez {product_name}",
            "Offre limitée — {price}",
            "Livraison rapide disponible",
        ],
    },
    "en": {
        "texts": [
            "✨ Discover {product_name}. Order yours today!",
            "🎯 Looking for {product_name}? Get yours now for only {price}.",
            "Don't miss out on {product_name}. Limited stock available!",
            "Transform your life with {product_name}. Shop now.",
            "{product_name} — trusted quality, fast delivery.",
        ],
        "headlines": [
            "Get {product_name} Now",
            "Only {price} — Order Today",
            "Shop {product_name}",
            "Limited Offer — {price}",
            "Fast Delivery Available",
        ],
    },
    "ar": {
        "texts": [
            "✨ اكتشف {product_name}. اطلبه الآن!",
            "🎯 {product_name} — جودة مضمونة وتوصيل سريع. بسعر {price} فقط!",
            "لا تفوّت {product_name}. الكمية محدودة!",
            "غيّر حياتك مع {product_name}. اشترِ الآن.",
            "{product_name} — الحل الذي تحتاجه. اطلبه اليوم!",
        ],
        "headlines": [
            "احصل على {product_name} الآن",
            "فقط {price} — اطلب اليوم",
            "تسوّق {product_name}",
            "عرض محدود — {price}",
            "توصيل سريع متاح",
        ],
    },
}


def _fallback_copy(product_name: str, price: str, language: str, n_texts: int, n_headlines: int) -> dict:
    logger.info("[ads_copy_gen] Using fallback template copy")
    lang_key = (language or "fr").lower()[:2]
    templates = _FALLBACK_TEMPLATES.get(lang_key, _FALLBACK_TEMPLATES["fr"])
    texts = [t.format(product_name=product_name, price=price) for t in templates["texts"]]
    heads = [h.format(product_name=product_name, price=price) for h in templates["headlines"]]
    return {
        "primary_texts": texts[:n_texts],
        "headlines":     heads[:n_headlines],
    }
