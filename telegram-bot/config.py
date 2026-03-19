"""
config.py - Central configuration for the Telegram bot.
Loads from environment variables / .env file.
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# --- AI / OpenAI-compatible ---
# Uses Replit AI Integrations (no personal API key needed).
# Falls back to a direct OPENAI_API_KEY if set.
OPENAI_API_KEY = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", "")

# --- Google Sheets ---
# The full service-account credentials JSON (as a string or file path).
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Meta Ads Research")

# --- Scraper ---
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
MAX_ADS_TO_SCAN_PER_KEYWORD = int(os.environ.get("MAX_ADS_TO_SCAN_PER_KEYWORD", "50"))

# --- Facebook session cookies ---
# Cookie string copied from browser DevTools (see /setup command for instructions).
# Required for accessing commercial/product ads in Meta Ads Library.
FACEBOOK_COOKIES = os.environ.get("FACEBOOK_COOKIES", "")

# --- Deduplication thresholds ---
TEXT_DEDUP_THRESHOLD = float(os.environ.get("TEXT_DEDUP_THRESHOLD", "85"))   # rapidfuzz score 0-100
IMAGE_DEDUP_THRESHOLD = int(os.environ.get("IMAGE_DEDUP_THRESHOLD", "10"))   # perceptual hash distance

# --- 1688 session cookies ---
# Cookie string copied from browser DevTools after logging in to 1688.com.
# Required for the pricing engine to search 1688 without being redirected to login.
# See /setup1688 command for step-by-step instructions.
ALI_1688_COOKIES = os.environ.get("ALI_1688_COOKIES", "")

# --- Shopify ---
SHOPIFY_STORE_URL    = os.environ.get("SHOPIFY_STORE_URL", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")

# --- Pricing (1688 sourcing → GNF) ---
# PRICE = (supplier_usd + SHIPPING_AGENT_USD + EXTRA_MARGIN_USD) × gnf_rate
SHIPPING_AGENT_USD   = float(os.environ.get("SHIPPING_AGENT_USD",   "15"))   # fixed shipping agent fee (always applied)
EXTRA_MARGIN_USD     = float(os.environ.get("EXTRA_MARGIN_USD",     "10"))   # profit margin
COMPARE_AT_EXTRA_GNF = int(os.environ.get("COMPARE_AT_EXTRA_GNF", "100000"))  # fixed GNF added on top of PRICE for compare-at
# GNF exchange rate fallback (used when live rate is unavailable)
USD_TO_GNF  = float(os.environ.get("USD_TO_GNF",  "8600"))   # 1 USD ≈ 8600 GNF (Guinea Franc)
# Rounding increment for GNF prices (nearest N GNF)
# 10000 = round to nearest 10,000 GNF (e.g. 245,000 → 250,000)
ROUND_TO_GNF = int(os.environ.get("ROUND_TO_GNF", "10000"))

# --- Sheet columns (order matters — must match the Google Sheet exactly) ---

# PENDING tab — slim view, no pricing/name fields (always blank at this stage)
PENDING_COLUMNS = [
    "SKU",
    "KEYWORD",
    "URL PRODUCT",
    "ADS LIBRARY MEDIA URL",
    "STATU",
    "SOURCING PRICE USD",
    "SOURCING URL",
    "WEIGHT GRAM",
    "HAS VARIANTS",
    "IMAGE URL",
    "PRICE",
    "COMPARE AT PRICE",
    "PRODUCT NAME",
    "URL LANDING PAGE",
    "LAST_ERROR",
]

# APPROVED tab and shared default
SHEET_COLUMNS = [
    "SKU",
    "KEYWORD",
    "URL PRODUCT",
    "ADS LIBRARY MEDIA URL",
    "ADS LIBRARY MEDIA URL 2",
    "ADS LIBRARY MEDIA URL 3",
    "ADS LIBRARY MEDIA URL 4",
    "ADS LIBRARY MEDIA URL 5",
    "ADS LIBRARY MEDIA URL 6",
    "ADS LIBRARY MEDIA URL 7",
    "ADS LIBRARY MEDIA URL 8",
    "ADS LIBRARY MEDIA URL 9",
    "ADS LIBRARY MEDIA URL 10",
    "NOTE",
    "PRICE",
    "COMPARE AT PRICE",
    "URL LANDING PAGE",
    "PRODUCT NAME",
    "STATU",
    "SOURCING PRICE USD",
    "SOURCING URL",
    "WEIGHT GRAM",
    "HAS VARIANTS",
    "IMAGE URL",
]

# DISAPROVED tab
DISAPPROVED_COLUMNS = [
    "SKU",
    "KEYWORD",
    "URL PRODUCT",
    "ADS LIBRARY MEDIA URL",
    "NOTE",
    "SOURCING PRICE USD",
    "SOURCING URL",
    "WEIGHT GRAM",
    "HAS VARIANTS",
]

# READY FOR ADS, ADS RUNNING, WINNER, LOSER tabs
ADS_COLUMNS = [
    "SKU",
    "PRODUCT NAME",
    "KEYWORD",
    "URL PRODUCT",
    "URL LANDING PAGE",
    "ADS LIBRARY MEDIA URL",
    "ADS LIBRARY MEDIA URL 2",
    "ADS LIBRARY MEDIA URL 3",
    "ADS LIBRARY MEDIA URL 4",
    "ADS LIBRARY MEDIA URL 5",
    "ADS LIBRARY MEDIA URL 6",
    "ADS LIBRARY MEDIA URL 7",
    "ADS LIBRARY MEDIA URL 8",
    "ADS LIBRARY MEDIA URL 9",
    "ADS LIBRARY MEDIA URL 10",
    "CREATIVE COUNT",
    "PRICE",
    "COMPARE AT PRICE",
    "SOURCING PRICE USD",
    "SOURCING URL",
    "WEIGHT GRAM",
    "HAS VARIANTS",
    "STATU",
    "NOTE",
]


def get_google_credentials_dict() -> dict:
    """Parse the Google credentials from env var (JSON string or file path)."""
    raw = GOOGLE_SHEETS_CREDENTIALS_JSON
    if not raw:
        raise ValueError("GOOGLE_SHEETS_CREDENTIALS_JSON is not set")
    # Try parsing as JSON directly
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try as a file path
    if os.path.isfile(raw):
        with open(raw) as f:
            return json.load(f)
    raise ValueError("GOOGLE_SHEETS_CREDENTIALS_JSON is not valid JSON or a valid file path")
