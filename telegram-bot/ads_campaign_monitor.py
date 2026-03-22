"""
ads_campaign_monitor.py - Background campaign monitoring for the Ads Launch bot.

Periodically evaluates running campaigns against configurable rules from ads_rules.json.
If a rule fires, the campaign is paused and moved to WINNER or LOSER in the sheet.
"""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 30 * 60  # run every 30 minutes

PURCHASE_ACTION_TYPES = frozenset({
    "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase",
    "omni_purchase",
    "purchase",
})


def extract_results(insights: dict) -> int:
    """Sum conversion actions from a Meta insights dict."""
    total = 0
    for action in insights.get("actions", []):
        if action.get("action_type") in PURCHASE_ACTION_TYPES:
            try:
                total += int(float(action.get("value", "0")))
            except (ValueError, TypeError):
                pass
    return total


def hours_since(timestamp_str: str) -> float:
    """Return hours elapsed since an ISO timestamp string."""
    try:
        if not timestamp_str:
            return 0.0
        ts = timestamp_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 0.0


def evaluate_campaign(row: dict, insights: dict, rules: dict) -> tuple[str | None, str]:
    """
    Apply the configured rules to a running campaign.

    Returns (action, reason):
      action  = "WINNER" | "LOSER" | None
      reason  = human-readable explanation
    """
    if str(row.get("OVERRIDE ACTIVE", "")).strip().upper() == "TRUE":
        return None, "Override active — skipping"

    spend = 0.0
    try:
        spend = float(str(insights.get("spend", "0") or "0"))
    except (ValueError, TypeError):
        pass

    results = extract_results(insights)
    cpr     = spend / results if results > 0 else float("inf")

    global_limit   = float(rules.get("GLOBAL_NO_RESULT_SPEND", 3.0))
    day1_cpr_limit = float(rules.get("DAY1_CPR_LIMIT", 2.0))
    day2_winner    = float(rules.get("DAY2_WINNER_CPR", 2.0))

    # Global rule — no timestamp required
    if spend >= global_limit and results == 0:
        return "LOSER", f"Auto: spend >= ${global_limit:.2f} and 0 results"

    # Time-based rules require a valid start timestamp
    start_time = (row.get("EFFECTIVE START TIME", "") or row.get("PUBLISHED AT", "")).strip()
    if not start_time:
        return None, "SKIP_NO_TIMESTAMP"

    hours = hours_since(start_time)

    # Day 1 window: >= 24h and < 48h
    if hours >= 24 and hours < 48 and cpr >= day1_cpr_limit:
        return "LOSER", f"Auto: Day 1 CPR >= ${day1_cpr_limit:.2f}"

    # Day 2 window: >= 48h
    if hours >= 48:
        if cpr < day2_winner:
            return "WINNER", f"Auto: Day 2 CPR < ${day2_winner:.2f}"
        else:
            return "LOSER", f"Auto: Day 2 CPR >= ${day2_winner:.2f}"

    return None, ""


async def run_monitor(bot=None, chat_id: int | None = None):
    """
    Background coroutine. Runs indefinitely, evaluating campaigns every
    MONITOR_INTERVAL_SECONDS. Call asyncio.create_task(run_monitor()) at startup.

    bot / chat_id: optional — if provided, sends a Telegram message on auto-decisions.
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))

    import ads_launch_sheet as sheet
    import meta_ads_service  as meta
    import ads_rules

    logger.info("[monitor] Campaign monitor started (interval=%ds)", MONITOR_INTERVAL_SECONDS)

    while True:
        try:
            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            await _evaluate_all(sheet, meta, ads_rules, bot, chat_id)
        except asyncio.CancelledError:
            logger.info("[monitor] Monitor cancelled")
            break
        except Exception as e:
            logger.error(f"[monitor] Unexpected error: {e}")


async def _evaluate_all(sheet, meta, ads_rules_mod, bot, chat_id):
    loop  = asyncio.get_running_loop()
    rules = ads_rules_mod.load_rules()

    try:
        rows = await loop.run_in_executor(None, sheet.load_running_rows)
    except Exception as e:
        logger.error(f"[monitor] Failed to load running rows: {e}")
        return

    logger.info(f"[monitor] Evaluating {len(rows)} running campaign(s)")

    for row in rows:
        sku         = row.get("SKU", "?")
        campaign_id = row.get("META CAMPAIGN ID", "").strip()
        if not campaign_id:
            try:
                from ads_config import load_config
                cfg           = load_config()
                ad_account_id = cfg.get("ad_account_id", "")
                if ad_account_id:
                    campaign_id = await loop.run_in_executor(
                        None, lambda: meta.find_campaign_by_name(ad_account_id, sku)
                    ) or ""
                    if campaign_id:
                        logger.info(f"[monitor] {sku}: recovered campaign_id={campaign_id}")
                        await loop.run_in_executor(
                            None, lambda: sheet.update_running_row(sku, {"META CAMPAIGN ID": campaign_id})
                        )
            except Exception as e:
                logger.warning(f"[monitor] {sku}: campaign_id recovery failed: {e}")
        if not campaign_id:
            logger.warning(f"[monitor] {sku}: no campaign_id found — skipping")
            continue

        # KEEP RUNNING override: skip until OVERRIDE_UNTIL timestamp, then clear
        if str(row.get("OVERRIDE ACTIVE", "")).strip().upper() == "TRUE":
            override_until_str = str(row.get("OVERRIDE UNTIL", "")).strip()
            still_active = False
            if override_until_str:
                try:
                    ts = override_until_str.replace("Z", "+00:00")
                    override_until = datetime.fromisoformat(ts)
                    if datetime.now(timezone.utc) < override_until:
                        still_active = True
                        remaining_h = (override_until - datetime.now(timezone.utc)).total_seconds() / 3600
                        logger.info(
                            f"[monitor] {sku}: KEEP RUNNING override active — "
                            f"{remaining_h:.1f}h remaining, skipping"
                        )
                except Exception:
                    pass  # bad timestamp → treat as expired

            if not still_active:
                logger.info(f"[monitor] {sku}: KEEP RUNNING override expired — clearing flag and evaluating")
                await loop.run_in_executor(
                    None,
                    lambda s=sku: sheet.update_running_row(s, {
                        "OVERRIDE ACTIVE": "",
                        "OVERRIDE UNTIL":  "",
                    })
                )
            else:
                continue

        try:
            insights = await loop.run_in_executor(
                None, lambda cid=campaign_id: meta.get_campaign_insights(cid)
            )
        except Exception as e:
            logger.error(f"[monitor] Insights fetch failed for {sku}: {e}")
            continue

        # Update sheet metrics
        try:
            raw_spend   = float(str(insights.get("spend", "0") or "0"))
            raw_results = extract_results(insights)
            raw_cpr     = raw_spend / raw_results if raw_results > 0 else 0.0
            await loop.run_in_executor(
                None,
                lambda s=sku, sp=raw_spend, re=raw_results, cp=raw_cpr: sheet.update_running_row(s, {
                    "SPEND":              f"{sp:.2f}",
                    "RESULTS":            str(re),
                    "TOTAL COST PER RZLT": f"{cp:.2f}",
                    "LAST METRICS SYNC":  datetime.now(timezone.utc).isoformat(),
                })
            )
        except Exception as e:
            logger.warning(f"[monitor] Metrics update failed for {sku}: {e}")

        action, reason = evaluate_campaign(row, insights, rules)
        if reason == "SKIP_NO_TIMESTAMP":
            logger.warning(f"[monitor] {sku}: EFFECTIVE START TIME missing — Day 1/2 rules skipped, Global rule already checked")
            if bot and chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ `{sku}`: launch timestamp missing — time-based rules skipped.",
                    parse_mode="Markdown",
                )
            continue
        if not action:
            logger.info(f"[monitor] {sku}: no action needed")
            continue

        target_tab = sheet.TAB_WINNER if action == "WINNER" else sheet.TAB_LOSER
        logger.info(f"[monitor] {sku}: {action} — {reason}")

        try:
            await loop.run_in_executor(None, lambda cid=campaign_id: meta.force_stop_campaign(cid))
        except Exception as e:
            logger.error(f"[monitor] Force stop failed for {sku} ({campaign_id}): {e}")
            if bot and chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🚨 `{sku}`: auto-stop FAILED — campaign may still be running.\n"
                        f"Action: `{action}`\nError: `{e}`"
                    ),
                    parse_mode="Markdown",
                )
            continue

        extra = {
            "STATU":          action,
            "RULE TRIGGERED": reason,
            "NOTE":           reason,
            "STOPPED AT":     datetime.now(timezone.utc).isoformat(),
            "STOP REASON":    reason,
        }
        try:
            await loop.run_in_executor(
                None,
                lambda s=sku, t=target_tab, ex=extra: sheet.move_running_product(s, t, ex)
            )
        except Exception as e:
            logger.error(f"[monitor] Move failed for {sku}: {e}")
            continue

        if bot and chat_id:
            emoji = "🏆" if action == "WINNER" else "❌"
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{emoji} *Campaign auto-classified*\n\n"
                        f"*SKU:* `{sku}`\n"
                        f"*Result:* `{action}`\n"
                        f"*Reason:* {reason}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"[monitor] Notification failed: {e}")
