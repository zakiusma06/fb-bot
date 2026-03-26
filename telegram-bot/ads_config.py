"""
ads_config.py - Persistent configuration for the Ads Launch bot.
Stores Meta API defaults (ad account, page, pixel, etc.) in ads_config.json.
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "ads_config.json")

_DEFAULT_CONFIG = {
    "ad_account_id":    "",        # act_XXXXXXXXX
    "ad_account_name":  "",
    "page_id":          "",
    "page_name":        "",
    "pixel_id":         "",
    "pixel_name":       "",
    "conversion_event": "Purchase",
    "country":          "GN",
    "daily_budget":     5000.0,    # in account's native currency (NOT USD)
    "objective":        "OUTCOME_SALES",
    "cta":              "SHOP_NOW",
    "timezone":         "Africa/Conakry",
    "currency":         "GNF",
    "meta_access_token": "",
    "meta_access_token": "",
}

OBJECTIVES = {
    "OUTCOME_SALES":       "Sales / Conversions",
    "OUTCOME_TRAFFIC":     "Traffic",
    "OUTCOME_LEADS":       "Leads",
    "OUTCOME_AWARENESS":   "Awareness",
    "OUTCOME_ENGAGEMENT":  "Engagement",
}

CONVERSION_EVENTS = [
    "Purchase", "AddToCart", "InitiateCheckout", "Lead",
    "CompleteRegistration", "ViewContent", "Subscribe",
]

CTA_TYPES = ["SHOP_NOW", "LEARN_MORE", "ORDER_NOW", "GET_OFFER", "SIGN_UP", "SUBSCRIBE"]


def load_config() -> dict:
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r") as f:
                saved = json.load(f)
            cfg = dict(_DEFAULT_CONFIG)
            cfg.update(saved)
            # Migrate old key: daily_budget_usd → daily_budget
            if "daily_budget_usd" in cfg and "daily_budget" not in saved:
                cfg["daily_budget"] = cfg.pop("daily_budget_usd")
                logger.info("[ads_config] Migrated daily_budget_usd → daily_budget")
            elif "daily_budget_usd" in cfg:
                cfg.pop("daily_budget_usd", None)
            return cfg
        except Exception as e:
            logger.error(f"[ads_config] Failed to load: {e}")
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info("[ads_config] Saved")
    except Exception as e:
        logger.error(f"[ads_config] Failed to save: {e}")


def update_config(**kwargs) -> dict:
    cfg = load_config()
    cfg.update(kwargs)
    save_config(cfg)
    return cfg


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("ad_account_id") and cfg.get("page_id") and cfg.get("pixel_id"))
