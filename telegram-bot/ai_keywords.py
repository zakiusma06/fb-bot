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
    Generate ~10 ecommerce/product-research keywords for Meta Ads Library.
    Uses Replit AI Integrations (no personal API key needed).
    Falls back to a curated list if AI is unavailable.
    """
    try:
        from openai import AsyncOpenAI

        base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
        api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "")

        if not base_url or not api_key:
            logger.warning("Replit AI integration not configured, using fallback keywords")
            return _fallback_keywords(niche)

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        niche_hint = f" in the '{niche}' niche" if niche else " across popular ecommerce categories"
        prompt = (
            f"Generate exactly 10 product-research search keywords{niche_hint} "
            "that are useful for finding winning products in Meta Ads Library. "
            "Return ONLY a plain numbered list like:\n"
            "1. keyword one\n2. keyword two\n...\n"
            "No explanations, no extra text."
        )

        response = await client.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=400,
        )
        raw = response.choices[0].message.content or ""
        keywords = _parse_numbered_list(raw)
        if keywords:
            return keywords
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
