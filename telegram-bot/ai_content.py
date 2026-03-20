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


_LANGUAGE_NAMES = {
    "fr": "French",
    "en": "English",
    "ar": "Arabic",
    "es": "Spanish",
    "pt": "Portuguese",
}


def generate_product_content(
    raw_title: str,
    raw_description: str,
    language: str | None = None,
) -> dict:
    """
    Returns:
      {
        "title":       str,   # 5-10 words, no brand names
        "description": str,   # short structured HTML-ready text
      }
    Language defaults to SHOPIFY_CONTENT_LANGUAGE env var, fallback "fr".
    """
    client = _get_client()

    lang_code = (language or os.environ.get("SHOPIFY_CONTENT_LANGUAGE", "fr")).lower()
    lang_name = _LANGUAGE_NAMES.get(lang_code, lang_code.upper())

    prompt = f"""You are a Shopify product copywriter. You MUST write EXCLUSIVELY in {lang_name}. Do NOT use any other language under any circumstances.

Raw product data scraped from a competitor page:

TITLE: {raw_title}
DESCRIPTION: {raw_description}

Generate the following in {lang_name} ONLY:

1. TITLE
- Pure descriptive title in {lang_name}
- No brand names, no model numbers
- Describe the product function clearly
- 5–10 words

2. DESCRIPTION (in {lang_name}, structured as short paragraphs):
- One sentence benefit introduction
- One sentence about the problem it solves
- One sentence how the product solves it
- 3–4 key benefits as a short bullet list
- One short closing line

Keep everything concise and commercial.
IMPORTANT: Every single word must be in {lang_name}. Do not mix languages.

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
