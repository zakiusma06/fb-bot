"""
ai_keywords.py - AI-powered keyword suggestion for product research.
Uses Replit AI Integrations (OpenAI-compatible) — no personal API key required.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


async def suggest_keywords(niche: str = "") -> list[str]:
    """
    Generate search keywords for Meta Ads Library.
    When called with a product title + description (from _auto_generate_keywords),
    generates 4 fresh search angles different from the original discovery keyword.
    Falls back to a curated list if AI is unavailable.
    """
    try:
        from openai import AsyncOpenAI

        base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
        api_key  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

        if not api_key:
            logger.warning("OpenAI API key not configured, using fallback keywords")
            return _fallback_keywords(niche)

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncOpenAI(**kwargs)

        # Rich context (title + description) vs simple niche hint
        has_description = "\n" in niche or len(niche) > 60
        if has_description:
            prompt = (
                f"You are a Facebook Ads researcher. Based on the product below, "
                f"generate exactly 4 search keywords to find similar winning ads in Meta Ads Library.\n\n"
                f"PRODUCT INFO:\n{niche}\n\n"
                f"Rules:\n"
                f"- Write ALL keywords in FRENCH only\n"
                f"- Each keyword must be a different search angle (benefit, use case, audience, problem solved)\n"
                f"- Do NOT repeat the product title as a keyword\n"
                f"- Keep each keyword 2-4 words, natural and searchable\n"
                f"- Return ONLY a numbered list, no explanations:\n"
                f"1. keyword\n2. keyword\n3. keyword\n4. keyword"
            )
            n = 4
        else:
            niche_hint = f" in the '{niche}' niche" if niche else " across popular ecommerce categories"
            prompt = (
                f"Generate exactly 10 product-research search keywords{niche_hint} "
                "that are useful for finding winning products in Meta Ads Library. "
                "Return ONLY a plain numbered list like:\n"
                "1. keyword one\n2. keyword two\n...\n"
                "No explanations, no extra text."
            )
            n = 10

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        raw = response.choices[0].message.content or ""
        keywords = _parse_numbered_list(raw)
        if keywords:
            return keywords[:n]
        return _fallback_keywords(niche)

    except Exception as e:
        logger.warning(f"AI keyword suggestion failed: {e}")
        return _fallback_keywords(niche)


def _parse_numbered_list(text: str) -> list[str]:
    keywords = []
    for line in text.strip().splitlines():
        line = line.strip()
        match = re.match(r"^\d+[.)]\s*(.+)$", line)
        if match:
            kw = match.group(1).strip().strip("\"'")
            if kw:
                keywords.append(kw)
    return keywords[:10]


def _fallback_keywords(niche: str = "") -> list[str]:
    """Curated fallback keywords when AI is not available."""
    niche_lower = niche.lower() if niche else ""

    if any(w in niche_lower for w in ["health", "fitness", "wellness"]):
        return [
            "posture corrector", "massage gun", "resistance bands",
            "foam roller", "jump rope", "pull-up bar", "ab roller",
            "knee brace", "back support belt", "yoga mat",
        ]
    if any(w in niche_lower for w in ["kitchen", "cooking", "food"]):
        return [
            "portable blender", "air fryer", "electric kettle",
            "food vacuum sealer", "mandoline slicer", "immersion blender",
            "rice cooker", "silicone baking mat", "meal prep containers", "knife sharpener",
        ]
    if any(w in niche_lower for w in ["pet", "dog", "cat"]):
        return [
            "dog harness", "cat scratcher", "pet camera", "dog training collar",
            "automatic pet feeder", "cat litter box", "dog puzzle toy",
            "pet grooming brush", "dog cooling mat", "cat window perch",
        ]
    # Default general ecommerce
    return [
        "posture corrector", "portable blender", "massage gun",
        "LED strip lights", "air purifier", "electric toothbrush",
        "dog harness", "resistance bands", "smart watch", "silk pillowcase",
    ]
