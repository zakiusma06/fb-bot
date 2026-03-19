"""
ads_rules.py - Configurable campaign judgment rules for the Ads Launch bot.

Rules are stored in ads_rules.json in the same directory.
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

_RULES_PATH = os.path.join(os.path.dirname(__file__), "ads_rules.json")

_DEFAULT_RULES: dict = {
    "GLOBAL_NO_RESULT_SPEND": 3.0,
    "DAY1_CPR_LIMIT":         2.0,
    "DAY2_WINNER_CPR":        2.0,
}


def load_rules() -> dict:
    if os.path.isfile(_RULES_PATH):
        try:
            with open(_RULES_PATH, "r") as f:
                saved = json.load(f)
            rules = dict(_DEFAULT_RULES)
            rules.update(saved)
            return rules
        except Exception as e:
            logger.error(f"[ads_rules] Failed to load: {e}")
    return dict(_DEFAULT_RULES)


def save_rules(rules: dict) -> None:
    try:
        with open(_RULES_PATH, "w") as f:
            json.dump(rules, f, indent=2)
        logger.info("[ads_rules] Saved")
    except Exception as e:
        logger.error(f"[ads_rules] Failed to save: {e}")


def update_rule(key: str, value: float) -> dict:
    rules = load_rules()
    rules[key] = value
    save_rules(rules)
    return rules
