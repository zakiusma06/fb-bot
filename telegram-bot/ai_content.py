"""
ai_content.py - Generate clean French product title and description using AI.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from openai import OpenAI

logger = logging.getLogger(__name__)


def _get_client() -> OpenAI:
    api_key  = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def generate_product_content(
    raw_title: str,
    raw_description: str,
) -> dict:
    """
    Returns:
      {
        "title":       str,   # French, 5-10 words, no brand names
        "description": str,   # French, short structured HTML-ready text
      }
    """
    client = _get_client()

    prompt = f"""You are a French Shopify product copywriter for a Guinean e-commerce store.

Given this raw product data scraped from a competitor page:

TITLE: {raw_title}
DESCRIPTION: {raw_description}

Generate:

1. TITLE
- Pure descriptive title in French
- No brand names, no model numbers
- Describe the product function clearly
- 5–10 words
- Example: "Détecteur d'Angle Numérique Magnétique"

2. DESCRIPTION (in French, structured as short paragraphs):
- One sentence benefit introduction
- One sentence about the problem it solves
- One sentence how the product solves it
- 3–4 key benefits as a short bullet list
- One short closing line

Keep everything concise and commercial.

Respond in this exact format:
TITLE: <title here>
DESCRIPTION: <description here>"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.7,
        )
        content = response.choices[0].message.content.strip()

        title       = ""
        description = ""

        for line in content.split("\n"):
            if line.startswith("TITLE:"):
                title = line[len("TITLE:"):].strip()
            elif line.startswith("DESCRIPTION:"):
                description = line[len("DESCRIPTION:"):].strip()

        # If description spanned multiple lines
        if "DESCRIPTION:" in content:
            parts = content.split("DESCRIPTION:", 1)
            if len(parts) == 2:
                description = parts[1].strip()

        logger.info(f"[ai_content] title={title!r}, desc_len={len(description)}")
        return {"title": title, "description": description}

    except Exception as e:
        logger.error(f"[ai_content] OpenAI call failed: {e}")
        return {"title": raw_title, "description": raw_description}
