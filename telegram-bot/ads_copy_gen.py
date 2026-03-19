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

    if n_texts == 0:
        style = tone or "persuasive, benefit-focused"
        prompt = f"""You are an expert Facebook ads copywriter.
Generate {n_headlines} short ad HEADLINE options (3–7 words each) for:
PRODUCT: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language}
WRITING STYLE (follow exactly, overrides defaults): {style}
Rules: punchy, include benefit or price signal, all in {language}, no hashtags, no "Facebook"/"Meta", each meaningfully different. Apply the WRITING STYLE above.
Format EXACTLY:
HEADLINES:
1. <headline>
2. <headline>
"""
    elif n_headlines == 0:
        style = tone or "persuasive, benefit-focused"
        prompt = f"""You are an expert Facebook ads copywriter.
Generate {n_texts} PRIMARY TEXT options for:
PRODUCT: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language}
WRITING STYLE (follow exactly, overrides defaults): {style}
Rules: hook + benefit + soft CTA, all in {language}, no hashtags, no "Facebook"/"Meta", each meaningfully different. Length and emoji usage must match the WRITING STYLE above.
Format EXACTLY:
PRIMARY TEXTS:
1. <text>
2. <text>
"""
    else:
        style = tone or "persuasive, benefit-focused"
        prompt = f"""You are an expert Facebook ads copywriter.

Generate Facebook ad copy for the following product:

PRODUCT NAME: {product_name}
KEYWORD: {keyword}
PRICE: {price}
LANGUAGE: {language}

WRITING STYLE — follow this EXACTLY, it overrides everything else:
{style}

Rules:
- Primary texts: hook + benefit + soft CTA. Length and emoji usage must match the WRITING STYLE above.
- Headlines: short (3–7 words), punchy, include either benefit or price signal. Apply the same WRITING STYLE.
- Write all copy in {language}.
- Do NOT include hashtags.
- Do NOT mention "Facebook" or "Meta".
- Keep each option meaningfully different (vary angle, hook, or benefit emphasis).

Generate exactly {n_texts} PRIMARY TEXT options and exactly {n_headlines} HEADLINE options.

Format your response EXACTLY like this (no extra text before or after):

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


def _fallback_copy(product_name: str, price: str, language: str, n_texts: int, n_headlines: int) -> dict:
    logger.info("[ads_copy_gen] Using fallback template copy")
    texts = [
        f"Discover {product_name}. Order yours today!",
        f"Looking for {product_name}? Get yours now for only {price}.",
        f"{product_name} — trusted quality, fast delivery.",
        f"Don't miss out on {product_name}. Limited stock available!",
        f"Transform your life with {product_name}. Shop now.",
    ]
    heads = [
        f"Get {product_name} Now",
        f"Only {price} — Order Today",
        f"Shop {product_name}",
        f"Limited Offer — {price}",
        f"Fast Delivery Available",
    ]
    return {
        "primary_texts": texts[:n_texts],
        "headlines":     heads[:n_headlines],
    }
