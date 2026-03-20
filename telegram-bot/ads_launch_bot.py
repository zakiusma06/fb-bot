"""
ads_launch_bot.py - Telegram bot for launching and managing Meta ads.

Phase 1: product browsing, creative selection, AI copy generation,
         Meta campaign creation, sheet movement.
Phase 2 stub: stats, manual stop, rule evaluation.
"""

import asyncio
import html
import logging
import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

BOT_COMMANDS = [
    BotCommand("launch",     "🚀 Browse products and launch ads"),
    BotCommand("setup_meta", "⚙️ Configure Meta account settings"),
    BotCommand("stats",      "📊 View running campaigns + manual control"),
    BotCommand("rules",      "⚖️ View and edit campaign judgment rules"),
    BotCommand("cancel",     "❌ Cancel current session"),
    BotCommand("help",       "📋 Show all commands"),
    BotCommand("start",      "👋 Start the bot"),
]

from ads_config import load_config, save_config, update_config, is_configured, OBJECTIVES, CONVERSION_EVENTS, CTA_TYPES
import ads_launch_sheet as sheet
import meta_ads_service  as meta
import ads_copy_gen      as copy_gen
import ads_rules         as rules_mod

logger = logging.getLogger(__name__)

# ── Access control ─────────────────────────────────────────────────────────────
# Set ALLOWED_TELEGRAM_USER_IDS in your .env as a comma-separated list of IDs.
# Example: ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
# If the env var is not set, the bot rejects everyone and logs a warning.
def _allowed_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


async def _auth(update: Update) -> bool:
    """Return True if the user is allowed. Silently ignores unknown users."""
    allowed = _allowed_ids()
    if not allowed:
        logger.warning(
            "[auth] ALLOWED_TELEGRAM_USER_IDS is not set — all users are blocked. "
            "Add your Telegram user ID to the env var."
        )
        await update.effective_message.reply_text(
            "⚠️ Bot not configured: `ALLOWED_TELEGRAM_USER_IDS` is not set.\n"
            "Add your Telegram user ID to the environment variables.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    uid = update.effective_user.id
    if uid not in allowed:
        logger.warning(f"[auth] Blocked unauthorised user {uid}")
        return False
    return True

# ── Session storage ────────────────────────────────────────────────────────────
_sessions: dict[int, dict] = {}

# Separate lightweight sessions for /stats and /rules flows
# (don't interfere with the launch session)
_stats_sessions: dict[int, dict] = {}
_rules_sessions: dict[int, dict] = {}

# ── States ────────────────────────────────────────────────────────────────────
S_BROWSE          = "browse"
S_CREATIVE_SELECT = "creative_select"
S_COPY_LANG       = "copy_lang"
S_COPY_TONE       = "copy_tone"
S_COPY_N_TEXTS    = "copy_n_texts"
S_COPY_N_HEADS    = "copy_n_heads"
S_COPY_GENERATING = "copy_generating"
S_COPY_PICK_TEXT  = "copy_pick_text"
S_COPY_PICK_HEAD  = "copy_pick_head"
S_SETTINGS_REVIEW = "settings_review"
S_SETTINGS_EDIT   = "settings_edit"
S_SCHEDULING      = "scheduling"
S_FINAL_SUMMARY   = "final_summary"
S_PUBLISHING      = "publishing"
# Setup states
S_SETUP_ACCTS    = "setup_accts"
S_SETUP_PAGES    = "setup_pages"
S_SETUP_PIXELS   = "setup_pixels"
S_SETUP_EVENTS   = "setup_events"
S_SETUP_COUNTRY  = "setup_country"
S_SETUP_BUDGET   = "setup_budget"
S_SETUP_OBJ      = "setup_obj"
S_SETUP_CTA      = "setup_cta"
S_SETUP_REVIEW   = "setup_review"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sess(uid: int) -> dict | None:
    return _sessions.get(uid)


def _new_sess(uid: int, **kwargs) -> dict:
    s = {
        "state":               S_BROWSE,
        "products":            [],
        "idx":                 0,
        "product":             {},
        "creative_urls":       [],
        "selected_urls":       [],
        "copy_language":       "French",
        "copy_tone":           "",
        "copy_n_texts":        3,
        "copy_n_heads":        3,
        "generated_texts":     [],
        "generated_heads":     [],
        "selected_text":       [],
        "selected_headline":   [],
        "settings":            load_config(),
        "publish_mode":        "NOW",
        "scheduled_time_iso":  "",
        "awaiting_setup_field": None,
        "source_tab":          sheet.TAB_READY,
    }
    s.update(kwargs)
    _sessions[uid] = s
    return s


def _clear(uid: int):
    _sessions.pop(uid, None)


async def _reply(update: Update, text: str, keyboard=None, parse_mode=ParseMode.MARKDOWN):
    kwargs = {"parse_mode": parse_mode}
    if keyboard:
        kwargs["reply_markup"] = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, **kwargs)
        except Exception:
            await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


async def _answer(update: Update, text: str = ""):
    if update.callback_query:
        try:
            await update.callback_query.answer(text)
        except Exception:
            pass


def _kb(*rows):
    """Build InlineKeyboardMarkup from rows of (text, callback_data) tuples."""
    return [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]


def _creative_labels(creative_urls: list[str], selected: list[str]) -> list[str]:
    labels = []
    for url in creative_urls:
        mark = "✅" if url in selected else "⬜"
        short = url[:50] + "…" if len(url) > 50 else url
        labels.append(f"{mark} {short}")
    return labels


def _format_settings(cfg: dict) -> str:
    obj_label = OBJECTIVES.get(cfg.get("objective", ""), cfg.get("objective", "?"))
    return (
        f"*Saved Meta Setup:*\n"
        f"• Ad Account: `{cfg.get('ad_account_id') or '—'}` ({cfg.get('ad_account_name') or '?'})\n"
        f"• Page ID: `{cfg.get('page_id') or '—'}` ({cfg.get('page_name') or '?'})\n"
        f"• Pixel ID: `{cfg.get('pixel_id') or '—'}` ({cfg.get('pixel_name') or '?'})\n"
        f"• Conversion Event: `{cfg.get('conversion_event', 'Purchase')}`\n"
        f"• Country: `{cfg.get('country', 'GN')}`\n"
        f"• Daily Budget: `{cfg.get('daily_budget', 5000.0):,.0f} {cfg.get('currency', 'USD')}`\n"
        f"• Objective: `{obj_label}`\n"
        f"• CTA: `{cfg.get('cta', 'SHOP_NOW')}`\n"
        f"• Timezone: `{cfg.get('timezone', 'Africa/Conakry')}`"
    )


# ── /start & /help ────────────────────────────────────────────────────────────

_HELP_TEXT = (
    "👋 *Ads Launch Bot*\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🚀 /launch — Browse *READY TO ADS* products one by one and launch Meta ad campaigns\n\n"
    "⚙️ /setup\_meta — Configure your Meta account _(ad account, page, pixel, budget…)_\n"
    "  Auto-fetches everything from your Meta account — no manual typing\n\n"
    "📊 /stats — View all running campaigns with live metrics\n"
    "  Buttons: *STOP CAMPAIGN · MARK WINNER · MARK LOSER · KEEP RUNNING*\n\n"
    "⚖️ /rules — View and edit automatic campaign judgment rules\n"
    "  _(global spend limit, Day 1 CPR limit, Day 2 winner CPR threshold)_\n\n"
    "❌ /cancel — Cancel any active session\n\n"
    "📋 /help — Show this message\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "*Quick start:*\n"
    "1️⃣ Run /setup\_meta once to link your Meta account\n"
    "2️⃣ Run /launch to browse products and publish your first ad\n"
    "3️⃣ Use /stats to monitor and control campaigns manually\n"
    "4️⃣ Use /rules to configure auto-stop thresholds"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    _save_monitor_chat_id(update.effective_chat.id)
    await _reply(update, _HELP_TEXT)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    await _reply(update, _HELP_TEXT)


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    uid = update.effective_user.id
    _clear(uid)
    _stats_sessions.pop(uid, None)
    _rules_sessions.pop(uid, None)
    await _reply(update, "✅ Session cancelled.")


# ── /setup_meta — fully auto-discovered from Meta API ────────────────────────

def _setup_sess(uid: int) -> dict:
    s = {
        "state":    S_SETUP_ACCTS,
        "settings": load_config(),
        "_accts":   [],   # fetched ad accounts
        "_pages":   [],   # fetched pages
        "_pixels":  [],   # fetched pixels
    }
    _sessions[uid] = s
    return s


async def cmd_setup_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    uid = update.effective_user.id
    s   = _setup_sess(uid)
    await _reply(update, "⏳ Fetching your Meta ad accounts…")
    loop = asyncio.get_running_loop()
    try:
        accts = await loop.run_in_executor(None, meta.fetch_ad_accounts)
    except Exception as e:
        await _reply(update, f"❌ Could not fetch ad accounts: `{e}`\n\nCheck that your `META_ACCESS_TOKEN` is valid.")
        _clear(uid)
        return
    if not accts:
        await _reply(update, "❌ No ad accounts found for this token.")
        _clear(uid)
        return
    s["_accts"] = accts
    s["state"]  = S_SETUP_ACCTS
    rows = [[InlineKeyboardButton(
        f"🏦 {a.get('name', a['id'])}  |  {a.get('currency','')}  |  {a.get('timezone_name','')}",
        callback_data=f"su_acct:{i}"
    )] for i, a in enumerate(accts)]
    await _reply(update, "✅ Found your ad accounts. Choose one:", rows)


async def _cb_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route all su_* callbacks through the guided setup wizard."""
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sessions.get(uid)
    if not s:
        return
    loop = asyncio.get_running_loop()

    # ── Step 1: Ad account selected ──────────────────────────────────────────
    if data.startswith("su_acct:"):
        idx   = int(data.split(":")[1])
        acct  = s["_accts"][idx]
        s["settings"].update({
            "ad_account_id":   acct["id"],
            "ad_account_name": acct.get("name", ""),
            "timezone":        acct.get("timezone_name", "Africa/Conakry"),
            "currency":        acct.get("currency", "GNF"),
        })
        await _reply(update, f"✅ *{acct.get('name')}* selected.\n\n⏳ Fetching your Facebook Pages…")
        try:
            pages = await loop.run_in_executor(None, meta.fetch_pages)
        except Exception as e:
            await _reply(update, f"❌ Could not fetch pages: `{e}`")
            return
        if not pages:
            await _reply(update, "❌ No Facebook Pages found for this token.")
            return
        s["_pages"] = pages
        s["state"]  = S_SETUP_PAGES
        rows = [[InlineKeyboardButton(
            f"📄 {p.get('name', p['id'])}  (ID: {p['id']})",
            callback_data=f"su_page:{i}"
        )] for i, p in enumerate(pages)]
        await _reply(update, "Choose the Facebook Page to run ads from:", rows)

    # ── Step 2: Page selected ─────────────────────────────────────────────────
    elif data.startswith("su_page:"):
        idx  = int(data.split(":")[1])
        page = s["_pages"][idx]
        s["settings"].update({
            "page_id":   page["id"],
            "page_name": page.get("name", ""),
        })
        acct_id = s["settings"]["ad_account_id"]
        await _reply(update, f"✅ *{page.get('name')}* selected.\n\n⏳ Fetching pixels for this ad account…")
        try:
            pixels = await loop.run_in_executor(None, lambda: meta.fetch_pixels(acct_id))
        except Exception as e:
            await _reply(update, f"❌ Could not fetch pixels: `{e}`")
            return
        if not pixels:
            await _reply(update,
                "⚠️ No pixels found for this ad account.\n"
                "Please type your Pixel ID manually:"
            )
            s["state"] = S_SETUP_PIXELS
            return
        s["_pixels"] = pixels
        s["state"]   = S_SETUP_PIXELS
        rows = [[InlineKeyboardButton(
            f"📍 {px.get('name', 'Unnamed')}  (ID: {px['id']})",
            callback_data=f"su_pixel:{i}"
        )] for i, px in enumerate(pixels)]
        await _reply(update, "Choose the pixel to track conversions:", rows)

    # ── Step 3: Pixel selected ────────────────────────────────────────────────
    elif data.startswith("su_pixel:"):
        idx   = int(data.split(":")[1])
        pixel = s["_pixels"][idx]
        s["settings"].update({
            "pixel_id":   pixel["id"],
            "pixel_name": pixel.get("name", ""),
        })
        await _setup_ask_event(update, s)

    # ── Step 4: Conversion event selected ────────────────────────────────────
    elif data.startswith("su_event:"):
        event = data.split(":", 1)[1]
        s["settings"]["conversion_event"] = event
        s["state"] = S_SETUP_COUNTRY
        await _reply(update,
            f"✅ Conversion event: *{event}*\n\n"
            "🌍 Enter the *2-letter country code* to target\n"
            "_(e.g. `GN` for Guinea, `FR` for France, `US` for USA)_"
        )

    # ── Step 5: Objective selected ────────────────────────────────────────────
    elif data.startswith("su_obj:"):
        obj = data.split(":", 1)[1]
        s["settings"]["objective"] = obj
        await _setup_ask_cta(update, s)

    # ── Step 6: CTA selected ──────────────────────────────────────────────────
    elif data.startswith("su_cta:"):
        cta = data.split(":", 1)[1]
        s["settings"]["cta"] = cta
        s["state"] = S_SETUP_REVIEW
        await _show_setup_review(update, s)

    # ── Review actions ────────────────────────────────────────────────────────
    elif data == "su_save":
        save_config(s["settings"])
        _clear(uid)
        await _reply(update,
            "✅ *Meta settings saved!*\n\n"
            + _format_settings(s["settings"]) +
            "\n\nUse /launch to start running ads."
        )

    elif data == "su_restart":
        _clear(uid)
        await cmd_setup_meta(update, ctx)


async def _setup_ask_event(update: Update, s: dict):
    s["state"] = S_SETUP_EVENTS
    rows = [[InlineKeyboardButton(ev, callback_data=f"su_event:{ev}")] for ev in CONVERSION_EVENTS]
    await _reply(update,
        f"✅ Pixel *{s['settings'].get('pixel_name') or s['settings'].get('pixel_id')}* selected.\n\n"
        "🎯 Choose the *conversion event* to optimise for:",
        rows
    )


async def _setup_ask_objective(update: Update, s: dict):
    s["state"] = S_SETUP_OBJ
    rows = [[InlineKeyboardButton(label, callback_data=f"su_obj:{key}")] for key, label in OBJECTIVES.items()]
    await _reply(update,
        f"✅ Country: *{s['settings'].get('country', 'GN')}*\n\n"
        "🎯 Choose the *campaign objective*:",
        rows
    )


async def _setup_ask_cta(update: Update, s: dict):
    s["state"] = S_SETUP_CTA
    rows = [[InlineKeyboardButton(c, callback_data=f"su_cta:{c}")] for c in CTA_TYPES]
    obj_label = OBJECTIVES.get(s["settings"].get("objective", ""), "?")
    await _reply(update,
        f"✅ Objective: *{obj_label}*\n\n"
        "📣 Choose the *call-to-action* button on the ad:",
        rows
    )


async def _show_setup_review(update: Update, s: dict):
    cfg = s["settings"]
    text = (
        "📋 *Review your Meta setup:*\n\n"
        + _format_settings(cfg) +
        "\n\nSave this configuration?"
    )
    kb = _kb(
        [("💾 Save & Finish", "su_save")],
        [("🔄 Start Over", "su_restart")],
    )
    await _reply(update, text, kb)


# ── _cb_setup_val — kept for settings-edit flow inside launch session ─────────
async def _cb_setup_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline button selections inside launch session settings edit."""
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data  # settings_val:field:value
    s    = _sess(uid)
    if not s:
        return
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    field = parts[1]
    value = parts[2]
    s["settings"][field] = value
    s["state"] = S_SETTINGS_REVIEW
    s["awaiting_setup_field"] = None
    await _show_settings_review(update, s)


# ── /launch ───────────────────────────────────────────────────────────────────

async def cmd_launch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    uid = update.effective_user.id
    _rules_sessions.pop(uid, None)
    _stats_sessions.pop(uid, None)
    cfg = load_config()
    if not is_configured(cfg):
        await _reply(update,
            "⚠️ Meta settings not configured yet.\n"
            "Run /setup\\_meta first to set your ad account, page, and pixel."
        )
        return

    _new_sess(uid)
    kb = _kb(
        [("📂 READY FOR ADS", "launch:src_ready"), ("🔁 ADS ERROR (retry)", "launch:src_error")],
    )
    await _reply(update, "📋 Which tab would you like to launch from?", kb)


async def _show_product(update: Update, s: dict):
    products = s["products"]
    idx      = s["idx"]
    if idx >= len(products):
        await _reply(update, "✅ No more products to review.")
        _clear(update.effective_user.id)
        return

    p = products[idx]
    s["product"] = p

    creative_urls = [
        p.get(f"ADS LIBRARY MEDIA URL{'' if i == 1 else f' {i}'}", "").strip()
        for i in range(1, 6)
    ]
    s["creative_urls"] = [u for u in creative_urls if u]

    def _e(v): return html.escape(str(v) if v else "—")

    price      = _e(p.get("PRICE", "—"))
    compare_at = _e(p.get("COMPARE AT PRICE", "—"))
    name       = _e(p.get("PRODUCT NAME", "—"))
    sku        = _e(p.get("SKU", "—"))
    url        = _e(p.get("URL PRODUCT", "—"))
    landing    = _e(p.get("URL LANDING PAGE", "—"))
    keyword    = _e(p.get("KEYWORD", "—"))
    note       = _e(p.get("NOTE", ""))

    creatives_text = ""
    if s["creative_urls"]:
        lines = [f"  {i+1}. {html.escape(u)}" for i, u in enumerate(s["creative_urls"])]
        creatives_text = "\n<b>Creatives:</b>\n" + "\n".join(lines)
    else:
        creatives_text = "\n⚠️ No creatives found"

    text = (
        f"📦 Product <b>{idx + 1} of {len(products)}</b>\n\n"
        f"<b>SKU:</b> <code>{sku}</code>\n"
        f"<b>Name:</b> {name}\n"
        f"<b>Price:</b> {price}  |  <b>Compare At:</b> {compare_at}\n"
        f"<b>Product URL:</b> {url}\n"
        f"<b>Landing Page:</b> {landing}\n"
        f"<b>Keyword:</b> {keyword}\n"
        + (f"<b>Note:</b> {note}\n" if note else "")
        + creatives_text
    )

    kb = _kb(
        [("🚀 Launch This Product", "launch:go"), ("⏭ Skip to Next", "launch:skip")],
        [("🔄 Preview Again", "launch:preview"), ("🛑 Stop", "launch:stop")],
    )
    await _reply(update, text, kb, parse_mode=ParseMode.HTML)


async def _cb_launch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        await _reply(update, "No active session. Use /launch to start.")
        return

    action = data.replace("launch:", "")

    if action in ("src_ready", "src_error"):
        is_error = action == "src_error"
        tab_name = sheet.TAB_ERROR if is_error else sheet.TAB_READY
        label    = "ADS ERROR" if is_error else "READY FOR ADS"
        await _reply(update, f"📂 Loading products from *{label}*…")
        loader = sheet.load_ads_error if is_error else sheet.load_ready_to_ads
        products = await asyncio.get_running_loop().run_in_executor(None, loader)
        if not products:
            await _reply(update, f"No products found in *{label}*.")
            _clear(uid)
            return
        s["products"]   = products
        s["idx"]        = 0
        s["source_tab"] = tab_name
        await _show_product(update, s)
        return

    if action == "stop":
        _clear(uid)
        await _reply(update, "Session stopped.")

    elif action == "skip":
        s["idx"] += 1
        await _show_product(update, s)

    elif action == "preview":
        await _show_product(update, s)

    elif action == "go":
        if not s["creative_urls"]:
            await _reply(update,
                "⚠️ No creative URLs found for this product.\n"
                "Cannot launch without at least 1 creative.",
                _kb([("⏭ Skip Product", "launch:skip"), ("🛑 Stop", "launch:stop")])
            )
            return
        s["state"] = S_CREATIVE_SELECT
        s["selected_urls"] = []
        await _show_creative_select(update, s)


# ── Creative selection ────────────────────────────────────────────────────────

async def _show_creative_select(update: Update, s: dict):
    labels = _creative_labels(s["creative_urls"], s["selected_urls"])
    rows = [[InlineKeyboardButton(lbl, callback_data=f"csel:{i}")] for i, lbl in enumerate(labels)]
    rows.append([
        InlineKeyboardButton("✅ Done Selecting", callback_data="csel:done"),
        InlineKeyboardButton("🛑 Cancel",         callback_data="csel:cancel"),
    ])
    count = len(s["selected_urls"])
    text = (
        f"🎬 *Select creatives to use*\n\n"
        f"Tap to toggle. {count} selected.\n"
        f"1 creative → Normal ad\n"
        f"2+ creatives → Flexible/dynamic ad\n\n"
        + "\n".join(f"{i+1}. {u}" for i, u in enumerate(s["creative_urls"]))
    )
    await _reply(update, text, rows)


async def _cb_creative_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s or s.get("state") != S_CREATIVE_SELECT:
        return

    key = data.replace("csel:", "")

    if key == "cancel":
        s["state"] = S_BROWSE
        await _show_product(update, s)
        return

    if key == "done":
        if not s["selected_urls"]:
            await update.callback_query.answer("Select at least 1 creative first", show_alert=True)
            return
        s["state"] = S_COPY_LANG
        await _reply(update,
            "📝 *Copy Generation*\n\n"
            f"Selected {len(s['selected_urls'])} creative(s).\n\n"
            "What *language* should the ad copy be written in?\n"
            "_(e.g. French, English, Arabic)_"
        )
        return

    try:
        idx = int(key)
        url = s["creative_urls"][idx]
        if url in s["selected_urls"]:
            s["selected_urls"].remove(url)
        else:
            s["selected_urls"].append(url)
        await _show_creative_select(update, s)
    except (ValueError, IndexError):
        pass


# ── Copy generation flow ──────────────────────────────────────────────────────

async def _handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = update.message.text.strip()
    loop  = asyncio.get_running_loop()

    # ── Rules value input ─────────────────────────────────────────────────────
    rules_sess = _rules_sessions.get(uid)
    if rules_sess and rules_sess.get("editing"):
        field_key = rules_sess["editing"]
        field_map = {
            "global": ("GLOBAL_NO_RESULT_SPEND", "Global no-result spend limit"),
            "day1":   ("DAY1_CPR_LIMIT",         "Day 1 CPR limit"),
            "day2":   ("DAY2_WINNER_CPR",         "Day 2 winner CPR threshold"),
        }
        if field_key in field_map:
            rule_key, label = field_map[field_key]
            try:
                value = float(text.replace(",", "."))
                if value <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Please enter a valid positive number (e.g. `2.5`).", parse_mode=ParseMode.MARKDOWN)
                return
            rules_mod.update_rule(rule_key, value)
            _rules_sessions.pop(uid, None)
            r = rules_mod.load_rules()
            await update.message.reply_text(
                f"✅ *{label}* updated to `${value:.2f}`\n\n" + _rules_text(r),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(_rules_kb()),
            )
        return

    # ── Stats note input ──────────────────────────────────────────────────────
    stats_sess = _stats_sessions.get(uid, {})
    note_pending = stats_sess.get("note_pending")
    if note_pending:
        sku      = note_pending.get("sku", "")
        dest_tab = note_pending.get("tab", "")
        if sku and dest_tab:
            await loop.run_in_executor(
                None,
                lambda: sheet.update_row_in_tab(sku, dest_tab, {
                    "NOTE":        text,
                    "MANUAL NOTE": text,
                })
            )
        stats_sess.pop("note_pending", None)
        await update.message.reply_text(
            f"✅ Note saved for `{sku}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Launch / setup session text inputs ────────────────────────────────────
    s     = _sessions.get(uid)

    if not s:
        return

    state = s.get("state")

    # ── Setup wizard text inputs ──────────────────────────────────────────────
    if state == S_SETUP_PIXELS:
        # User typed a pixel ID manually (no pixels were found via API)
        pid = text.strip()
        s["settings"].update({"pixel_id": pid, "pixel_name": ""})
        await _setup_ask_event(update, s)
        return

    if state == S_SETUP_COUNTRY:
        country = text.strip().upper()
        if len(country) != 2:
            await _reply(update, "Please enter a valid 2-letter country code (e.g. `GN`, `FR`, `US`).")
            return
        s["settings"]["country"] = country
        s["state"] = S_SETUP_BUDGET
        currency = s["settings"].get("currency", "USD")
        await _reply(update,
            f"✅ Country: *{country}*\n\n"
            f"💵 Enter the *daily budget in {currency}* _(in your ad account's native currency)_\n"
            f"_e.g. `5000` for {currency}_:"
        )
        return

    if state == S_SETUP_BUDGET:
        try:
            budget = float(text.replace(",", ".").replace(" ", ""))
            if budget <= 0:
                raise ValueError
        except ValueError:
            currency = s["settings"].get("currency", "")
            await _reply(update, f"Please enter a valid positive number (e.g. `5000` for {currency}).")
            return
        s["settings"]["daily_budget"] = budget
        await _setup_ask_objective(update, s)
        return

    # ── Launch session text inputs ────────────────────────────────────────────
    if state == S_COPY_LANG:
        s["copy_language"] = text
        s["state"] = S_COPY_TONE
        await _reply(update,
            f"Language: *{text}* ✅\n\n"
            "✍️ Describe how you want the text to be.\n"
            "_(e.g. \"Short with emojis\", \"Urgent and punchy\", \"Lifestyle story\", \"Problem then solution\")_"
        )

    elif state == S_COPY_TONE:
        s["copy_tone"] = "" if text.lower() == "skip" else text
        # Auto-set counts based on number of assets (default 3 if not yet known)
        n_assets = len(s.get("assets", [])) or 3
        s["copy_n_texts"] = n_assets
        s["copy_n_heads"] = n_assets
        s["state"] = S_COPY_GENERATING
        await _reply(update, "⏳ Generating ad copy…")
        await _run_copy_generation(update, s)
        return

    elif state == S_SETTINGS_EDIT:
        await _handle_settings_edit_input(update, text, s)
        return


async def _run_copy_generation(update: Update, s: dict):
    p = s["product"]
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: copy_gen.generate_ad_copy(
                product_name  = p.get("PRODUCT NAME", ""),
                product_url   = p.get("URL PRODUCT", ""),
                landing_url   = p.get("URL LANDING PAGE", ""),
                price         = p.get("PRICE", ""),
                keyword       = p.get("KEYWORD", ""),
                language      = s["copy_language"],
                tone          = s["copy_tone"],
                n_texts       = s["copy_n_texts"],
                n_headlines   = s["copy_n_heads"],
            )),
            timeout=25,
        )
    except asyncio.TimeoutError:
        logger.warning("[ads_launch] Ad copy generation timed out — using fallback templates")
        result = copy_gen.fallback_copy(
            product_name = p.get("PRODUCT NAME", ""),
            price        = p.get("PRICE", ""),
            language     = s["copy_language"],
            n_texts      = s["copy_n_texts"],
            n_headlines  = s["copy_n_heads"],
        )
        result["is_fallback"] = True
        await _reply(update, "⚠️ *AI copy timed out* — showing template copy. Hit 🔄 Regenerate All to retry.")
    s["generated_texts"]   = result.get("primary_texts", [])
    s["generated_heads"]   = result.get("headlines", [])
    s["selected_text"]     = list(s["generated_texts"])
    s["selected_headline"] = list(s["generated_heads"])
    s["state"] = S_COPY_PICK_TEXT
    # Warn if AI failed silently (non-timeout) and template copy is being shown
    if result.get("is_fallback") and "timed out" not in result.get("_fallback_reason", "timed out"):
        await _reply(update, "⚠️ *AI copy failed* — showing template copy. Hit 🔄 Regenerate All to retry once your API key is working.")
    await _show_copy_review(update, s)


async def _show_copy_review(update: Update, s: dict):
    """Show all generated copy with full controls: confirm, keep one, or regenerate."""
    sel_texts = s["selected_text"]   if isinstance(s["selected_text"],    list) else [s["selected_text"]]
    sel_heads = s["selected_headline"] if isinstance(s["selected_headline"], list) else [s["selected_headline"]]

    text_lines = "\n".join(f"*Text {i+1}:* _{t}_" for i, t in enumerate(sel_texts))
    head_lines = "\n".join(f"*Headline {i+1}:* `{h}`" for i, h in enumerate(sel_heads))

    msg = (
        "📝 *AD COPY REVIEW*\n\n"
        f"{text_lines}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"{head_lines}\n\n"
        "✅ Confirm all, or pick one to keep / regenerate:"
    )

    rows = []
    rows.append([("✅ Use All & Continue", "copy:confirm")])
    if len(sel_texts) > 1:
        rows.append([(f"Keep Text {i+1} Only", f"copy:keep_text:{i}") for i in range(len(sel_texts))])
    if len(sel_heads) > 1:
        rows.append([(f"Keep H{i+1} Only", f"copy:keep_head:{i}") for i in range(len(sel_heads))])
    rows.append([("🔄 New Texts", "copy:regen_texts"), ("🔄 New Headlines", "copy:regen_heads")])
    rows.append([("🔄 Regenerate All", "copy:regen_all"), ("🛑 Cancel", "launch:stop")])

    await _reply(update, msg, _kb(*rows))


async def _cb_copy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        return

    # ── Confirm all ───────────────────────────────────────────────────────────
    if data == "copy:confirm":
        s["state"] = S_SETTINGS_REVIEW
        await _show_settings_review(update, s)
        return

    # ── Keep one text only ────────────────────────────────────────────────────
    if data.startswith("copy:keep_text:"):
        idx = int(data.split(":")[2])
        all_texts = s["generated_texts"] if isinstance(s["generated_texts"], list) else [s["generated_texts"]]
        s["selected_text"] = [all_texts[idx]]
        await _show_copy_review(update, s)
        return

    # ── Keep one headline only ────────────────────────────────────────────────
    if data.startswith("copy:keep_head:"):
        idx = int(data.split(":")[2])
        all_heads = s["generated_heads"] if isinstance(s["generated_heads"], list) else [s["generated_heads"]]
        s["selected_headline"] = [all_heads[idx]]
        await _show_copy_review(update, s)
        return

    # ── Regenerate texts only ─────────────────────────────────────────────────
    if data == "copy:regen_texts":
        s["state"] = S_COPY_GENERATING
        await _reply(update, "⏳ Regenerating texts…")
        loop = asyncio.get_running_loop()
        p = s["product"]
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: copy_gen.generate_ad_copy(
                    product_name=p.get("PRODUCT NAME", ""), product_url=p.get("URL PRODUCT", ""),
                    landing_url=p.get("URL LANDING PAGE", ""), price=p.get("PRICE", ""),
                    keyword=p.get("KEYWORD", ""), language=s["copy_language"], tone=s["copy_tone"],
                    n_texts=s["copy_n_texts"], n_headlines=0,
                )), timeout=25,
            )
            new_texts = result.get("primary_texts", [])
        except asyncio.TimeoutError:
            new_texts = copy_gen.fallback_copy(p.get("PRODUCT NAME",""), p.get("PRICE",""), s["copy_language"], s["copy_n_texts"], 0).get("primary_texts", [])
        if new_texts:
            s["generated_texts"] = new_texts
            s["selected_text"]   = list(new_texts)
        s["state"] = S_COPY_PICK_TEXT
        await _show_copy_review(update, s)
        return

    # ── Regenerate headlines only ─────────────────────────────────────────────
    if data == "copy:regen_heads":
        s["state"] = S_COPY_GENERATING
        await _reply(update, "⏳ Regenerating headlines…")
        loop = asyncio.get_running_loop()
        p = s["product"]
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: copy_gen.generate_ad_copy(
                    product_name=p.get("PRODUCT NAME", ""), product_url=p.get("URL PRODUCT", ""),
                    landing_url=p.get("URL LANDING PAGE", ""), price=p.get("PRICE", ""),
                    keyword=p.get("KEYWORD", ""), language=s["copy_language"], tone=s["copy_tone"],
                    n_texts=0, n_headlines=s["copy_n_heads"],
                )), timeout=25,
            )
            new_heads = result.get("headlines", [])
        except asyncio.TimeoutError:
            new_heads = copy_gen.fallback_copy(p.get("PRODUCT NAME",""), p.get("PRICE",""), s["copy_language"], 0, s["copy_n_heads"]).get("headlines", [])
        if new_heads:
            s["generated_heads"]   = new_heads
            s["selected_headline"] = list(new_heads)
        s["state"] = S_COPY_PICK_TEXT
        await _show_copy_review(update, s)
        return

    # ── Regenerate all ────────────────────────────────────────────────────────
    if data == "copy:regen_all":
        s["state"] = S_COPY_GENERATING
        await _reply(update, "⏳ Regenerating all copy…")
        await _run_copy_generation(update, s)
        return


# ── Settings review ───────────────────────────────────────────────────────────

async def _show_settings_review(update: Update, s: dict):
    cfg = s["settings"]
    text = _format_settings(cfg) + "\n\nApprove these settings or edit before publishing:"
    kb = _kb(
        [("✅ Approve Settings", "settings:approve"), ("✏️ Edit Ad Account", "settings:edit:ad_account_id")],
        [("✏️ Edit Page ID", "settings:edit:page_id"), ("✏️ Edit Pixel ID", "settings:edit:pixel_id")],
        [("✏️ Edit Country", "settings:edit:country"), ("✏️ Edit Budget", "settings:edit:daily_budget")],
        [("✏️ Edit Objective", "settings:edit:objective"), ("✏️ Edit Event", "settings:edit:conversion_event")],
        [("✏️ Edit CTA", "settings:edit:cta"), ("✏️ Edit Timezone", "settings:edit:timezone")],
        [("🛑 Cancel", "launch:stop")],
    )
    await _reply(update, text, kb)


async def _cb_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        return

    if data == "settings:approve":
        s["state"] = S_SCHEDULING
        await _show_scheduling(update, s)
        return

    if data.startswith("settings:edit:"):
        field = data.replace("settings:edit:", "")

        if field == "objective":
            kb = _kb(*[[(label, f"settings_val:objective:{key}")] for key, label in OBJECTIVES.items()])
            kb.append([InlineKeyboardButton("← Back", callback_data="settings:approve_back")])
            await _reply(update, "Choose objective:", kb)
            return

        if field == "conversion_event":
            kb = _kb(*[[(ev, f"settings_val:conversion_event:{ev}")] for ev in CONVERSION_EVENTS])
            kb.append([InlineKeyboardButton("← Back", callback_data="settings:approve_back")])
            await _reply(update, "Choose conversion event:", kb)
            return

        if field == "cta":
            kb = _kb(*[[(c, f"settings_val:cta:{c}")] for c in CTA_TYPES])
            kb.append([InlineKeyboardButton("← Back", callback_data="settings:approve_back")])
            await _reply(update, "Choose CTA:", kb)
            return

        prompts = {
            "ad_account_id":    "Enter Ad Account ID (act\\_XXXXXXXXX):",
            "page_id":          "Enter Facebook Page ID:",
            "pixel_id":         "Enter Meta Pixel ID:",
            "country":          "Enter 2-letter country code (GN, FR, US…):",
            "daily_budget":      f"Enter daily budget in {s['settings'].get('currency', 'USD')} (e.g. 5000):",
            "timezone":         "Enter timezone (e.g. Africa/Conakry):",
        }
        s["state"] = S_SETTINGS_EDIT
        s["awaiting_setup_field"] = field
        await _reply(update, prompts.get(field, f"Enter new value for {field}:"))
        return

    if data == "settings:approve_back":
        s["state"] = S_SETTINGS_REVIEW
        s["awaiting_setup_field"] = None
        await _show_settings_review(update, s)
        return


async def _cb_settings_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle button selections for settings (objective, event, cta)."""
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        return
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    field = parts[1]
    value = parts[2]
    s["settings"][field] = value
    s["state"] = S_SETTINGS_REVIEW
    s["awaiting_setup_field"] = None
    await _show_settings_review(update, s)


async def _handle_settings_edit_input(update: Update, text: str, s: dict):
    field = s.get("awaiting_setup_field")
    if not field:
        return
    if field == "daily_budget":
        try:
            val = float(text.replace(",", ".").replace(" ", ""))
            s["settings"]["daily_budget"] = val
        except ValueError:
            await _reply(update, "Please enter a valid number (e.g. 5.00).")
            return
    elif field == "ad_account_id":
        val = text.strip()
        if not val.startswith("act_"):
            val = "act_" + val
        s["settings"][field] = val
    else:
        s["settings"][field] = text.strip()
    s["state"] = S_SETTINGS_REVIEW
    s["awaiting_setup_field"] = None
    await _show_settings_review(update, s)


# ── Scheduling ────────────────────────────────────────────────────────────────

async def _show_scheduling(update: Update, s: dict):
    tz = s["settings"].get("timezone", "Africa/Conakry")
    kb = _kb(
        [("🚀 Publish Now", "sched:NOW"), (f"🕛 Publish at 11:59 PM ({tz})", "sched:TODAY_2359")],
        [("🛑 Cancel", "launch:stop")],
    )
    await _reply(update,
        "⏰ *When to publish?*\n\n"
        f"Timezone: `{tz}`\n\n"
        "• *Publish Now* — campaign goes live immediately\n"
        "• *11:59 PM* — campaign starts next day (spend begins tomorrow)",
        kb
    )


async def _cb_scheduling(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        return

    mode = data.replace("sched:", "")
    s["publish_mode"] = mode

    if mode == "TODAY_2359":
        tz_name = s["settings"].get("timezone", "Africa/Conakry")
        s["scheduled_time_iso"] = meta.today_at_2359_iso(tz_name)
    else:
        s["scheduled_time_iso"] = ""

    s["state"] = S_FINAL_SUMMARY
    await _show_final_summary(update, s)


# ── Final summary ─────────────────────────────────────────────────────────────

async def _show_final_summary(update: Update, s: dict):
    p   = s["product"]
    cfg = s["settings"]
    sku = p.get("SKU", "—")
    campaign_name = f"{sku} {p.get('PRODUCT NAME', '')}".strip()
    ad_type = "FLEXIBLE" if len(s["selected_urls"]) > 1 else "NORMAL"
    sched_display = s["scheduled_time_iso"] or "Now"
    obj_label = OBJECTIVES.get(cfg.get("objective", ""), cfg.get("objective", "?"))

    text = (
        "📋 *FINAL SUMMARY — Review before publishing*\n\n"
        f"*Campaign / Ad Set / Ad Name:*\n`{campaign_name}`\n\n"
        f"*Product:* {p.get('PRODUCT NAME', '—')}\n"
        f"*SKU:* `{sku}`\n"
        f"*Product URL:* {p.get('URL PRODUCT', '—')}\n"
        f"*Landing Page:* {p.get('URL LANDING PAGE', '—')}\n\n"
        f"*Ad Type:* `{ad_type}` ({len(s['selected_urls'])} creative(s))\n"
        f"*Selected Creatives:*\n" + "\n".join(f"  • {u}" for u in s["selected_urls"]) + "\n\n"
        f"*Primary Texts ({len(s['selected_text'])}):*\n"
        + "\n".join(f"  _{t}_" for t in s["selected_text"]) + "\n\n"
        + f"*Headlines ({len(s['selected_headline'])}):*\n"
        + "\n".join(f"  `{h}`" for h in s["selected_headline"]) + "\n\n"
        f"*Ad Account:* `{cfg.get('ad_account_id', '—')}`\n"
        f"*Page ID:* `{cfg.get('page_id', '—')}`\n"
        f"*Country:* `{cfg.get('country', '—')}`\n"
        f"*Daily Budget:* `{cfg.get('daily_budget', 5000.0):,.0f} {cfg.get('currency', 'USD')}`\n"
        f"*Objective:* `{obj_label}`\n"
        f"*Pixel:* `{cfg.get('pixel_id', '—')}`\n"
        f"*Conversion Event:* `{cfg.get('conversion_event', '—')}`\n"
        f"*CTA:* `{cfg.get('cta', '—')}`\n"
        f"*Timezone:* `{cfg.get('timezone', '—')}`\n"
        f"*Publish Mode:* `{s['publish_mode']}`\n"
        f"*Scheduled Time:* `{sched_display}`"
    )

    kb = _kb(
        [("✅ Approve & Publish", "final:publish")],
        [("✏️ Edit Creatives", "final:edit_creatives"), ("✏️ Edit Copy", "final:edit_copy")],
        [("✏️ Edit Settings", "final:edit_settings"), ("❌ Cancel", "launch:stop")],
    )
    await _reply(update, text, kb)


async def _cb_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data
    s    = _sess(uid)
    if not s:
        return

    if data == "final:publish":
        s["state"] = S_PUBLISHING
        await _reply(update, "⏳ *Publishing…* Please wait, do not send new commands.")
        await _do_publish(update, ctx, s)

    elif data == "final:edit_creatives":
        s["state"] = S_CREATIVE_SELECT
        await _show_creative_select(update, s)

    elif data == "final:edit_copy":
        s["state"] = S_COPY_LANG
        await _reply(update, "Language for the copy?")

    elif data == "final:edit_settings":
        s["state"] = S_SETTINGS_REVIEW
        await _show_settings_review(update, s)


# ── Publishing ────────────────────────────────────────────────────────────────

async def _do_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE, s: dict):
    uid = update.effective_user.id

    # Guard: prevent double-publish if already in progress
    if s.get("_publish_in_progress"):
        await update.effective_message.reply_text("⏳ Already publishing — please wait.")
        return
    s["_publish_in_progress"] = True

    p   = s["product"]
    cfg = s["settings"]

    sku           = p.get("SKU", "")
    product_name  = p.get("PRODUCT NAME", "")
    landing_url   = p.get("URL LANDING PAGE") or p.get("URL PRODUCT", "")
    campaign_name = f"{sku} {product_name}".strip()
    ad_account_id = cfg.get("ad_account_id", "")
    page_id       = cfg.get("page_id", "")
    pixel_id      = cfg.get("pixel_id", "")
    conversion_ev = cfg.get("conversion_event", "Purchase")
    country       = cfg.get("country", "GN")
    # Budget is stored in the account's native currency; Meta expects minor units (×100)
    daily_budget       = float(cfg.get("daily_budget", cfg.get("daily_budget_usd", 5000.0)))
    daily_budget_minor = int(daily_budget * 100)
    objective     = cfg.get("objective", "OUTCOME_SALES")
    cta           = cfg.get("cta", "SHOP_NOW")
    start_time    = s.get("scheduled_time_iso") or None
    ad_type       = "FLEXIBLE" if len(s["selected_urls"]) > 1 else "NORMAL"
    loop          = asyncio.get_running_loop()

    good_assets: list  = []
    campaign_id: str   = ""
    adset_id: str      = ""

    try:
        # ── Step 1: Prepare media (timeout: 180s for large video uploads) ─
        await update.effective_message.reply_text("📥 Downloading and uploading creatives…")
        assets = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: meta.prepare_media_assets(ad_account_id, s["selected_urls"])
            ),
            timeout=180,
        )

        good_assets = [a for a in assets if "error" not in a]
        bad_assets  = [a for a in assets if "error" in a]

        if bad_assets:
            err_lines = "\n".join(f"• {a['url'][:60]}: {a['error']}" for a in bad_assets)
            await update.effective_message.reply_text(
                f"⚠️ {len(bad_assets)} creative(s) failed to prepare:\n{err_lines}"
            )

        if not good_assets:
            raise RuntimeError("All creatives failed — cannot publish.")

        await update.effective_message.reply_text(f"✅ {len(good_assets)} creative(s) ready.")

        # ── Step 2: Validate ───────────────────────────────────────────────
        if not ad_account_id or not page_id or not pixel_id:
            raise RuntimeError("Missing ad account, page, or pixel ID in settings.")
        if not landing_url:
            raise RuntimeError("No landing page URL found for this product.")
        if not s["selected_text"]:
            raise RuntimeError("No primary text generated.")
        if not s["selected_headline"]:
            raise RuntimeError("No headline generated.")

        # ── Step 3: Create campaign ────────────────────────────────────────
        await update.effective_message.reply_text("🏗 Creating campaign…")
        campaign_id = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: meta.create_campaign(ad_account_id, campaign_name, objective, daily_budget_minor)
            ),
            timeout=30,
        )

        # ── Step 4: Create adset ──────────────────────────────────────────
        await update.effective_message.reply_text("📦 Creating ad set…")
        try:
            adset_id = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: meta.create_adset(
                        ad_account_id, campaign_id, campaign_name,
                        country, pixel_id, conversion_ev,
                        start_time_iso=start_time,
                    )
                ),
                timeout=30,
            )
        except Exception:
            # Rollback: stop the campaign we just created before propagating error
            if campaign_id:
                try:
                    await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))
                    logger.warning(f"[ads_launch] Rolled back campaign {campaign_id} after adset failure")
                except Exception as _re:
                    logger.error(f"[ads_launch] Rollback failed for campaign {campaign_id}: {_re}")
            raise

        # ── Step 5 & 6: Create creative(s) and ad(s) ──────────────────────
        texts_list = s["selected_text"] if isinstance(s["selected_text"], list) else [s["selected_text"]]
        heads_list = s["selected_headline"] if isinstance(s["selected_headline"], list) else [s["selected_headline"]]

        if ad_type == "FLEXIBLE":
            ad_ids = []
            for i, asset in enumerate(good_assets):
                ad_name = f"{campaign_name} #{i + 1}"
                # Each creative gets its own text + headline (cycle if fewer than assets)
                ad_text = texts_list[i % len(texts_list)]
                ad_head = heads_list[i % len(heads_list)]
                await update.effective_message.reply_text(f"🎨 Creating creative {i + 1}/{len(good_assets)}…")
                c_id = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda a=asset, n=ad_name, t=ad_text, h=ad_head: meta.create_creative_single(
                            ad_account_id, n, page_id, a,
                            landing_url, t, h, cta
                        )
                    ),
                    timeout=30,
                )
                await update.effective_message.reply_text(f"📣 Creating ad {i + 1}/{len(good_assets)}…")
                a_id = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda n=ad_name, c=c_id: meta.create_ad(ad_account_id, adset_id, n, c)
                    ),
                    timeout=30,
                )
                ad_ids.append(a_id)
            ad_id = ", ".join(ad_ids)
        else:
            await update.effective_message.reply_text("🎨 Creating ad creative…")
            creative_id = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: meta.create_creative_single(
                        ad_account_id, campaign_name, page_id, good_assets[0],
                        landing_url, texts_list, heads_list, cta
                    )
                ),
                timeout=30,
            )
            await update.effective_message.reply_text("📣 Creating ad…")
            ad_id = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: meta.create_ad(ad_account_id, adset_id, campaign_name, creative_id)
                ),
                timeout=30,
            )

        # ── Step 7: Move row in sheet ──────────────────────────────────────
        published_at = datetime.now(timezone.utc).isoformat()
        extra = {
            "STATU":                "ADS RUNNING",
            "CAMPAIGN NAME":        campaign_name,
            "ADSET NAME":           campaign_name,
            "AD NAME":              campaign_name,
            "META CAMPAIGN ID":     campaign_id,
            "META ADSET ID":        adset_id,
            "META AD ID":           ad_id,
            "AD TYPE":              ad_type,
            "SELECTED CREATIVES":   ", ".join(s["selected_urls"]),
            "SELECTED PRIMARY TEXT": " | ".join(s["selected_text"]) if isinstance(s["selected_text"], list) else s["selected_text"],
            "SELECTED HEADLINE":    " | ".join(s["selected_headline"]) if isinstance(s["selected_headline"], list) else s["selected_headline"],
            "PUBLISH MODE":         s["publish_mode"],
            "SCHEDULED TIME":       s.get("scheduled_time_iso", ""),
            "PUBLISHED AT":         published_at,
            "EFFECTIVE START TIME": start_time or published_at,
            "UPLOADED ASSET IDS":   "",
        }
        src_tab = s.get("source_tab", sheet.TAB_READY)
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sheet.move_product(p, sheet.TAB_RUNNING, extra, source_tab=src_tab)),
                timeout=30,
            )
        except Exception as sheet_err:
            # Campaign is live but sheet write failed — log prominently so it can be reconciled
            logger.error(
                f"[ads_launch] SHEET WRITE FAILED after successful publish for {sku}! "
                f"Campaign ID: {campaign_id} | Adset ID: {adset_id} | Ad ID: {ad_id} | Error: {sheet_err}"
            )
            await update.effective_message.reply_text(
                f"⚠️ *Ad is LIVE but sheet update failed!*\n\n"
                f"*Campaign ID:* `{campaign_id}`\n"
                f"*Ad Set ID:* `{adset_id}`\n"
                f"*Ad ID:* `{ad_id}`\n\n"
                f"Save these IDs manually — the campaign is running but not tracked in your sheet.\n"
                f"Error: `{str(sheet_err)[:200]}`",
                parse_mode=ParseMode.MARKDOWN
            )
            _clear(uid)
            return

        # ── Step 8: Done ───────────────────────────────────────────────────
        currency   = cfg.get("currency", "USD")
        sched_note = f" (starts at {start_time})" if start_time else " (live now)"
        await update.effective_message.reply_text(
            f"✅ *Ad launched successfully!{sched_note}*\n\n"
            f"*Campaign:* `{campaign_name}`\n"
            f"*Campaign ID:* `{campaign_id}`\n"
            f"*Ad Set ID:* `{adset_id}`\n"
            f"*Ad ID:* `{ad_id}`\n"
            f"*Ad Type:* `{ad_type}`\n"
            f"*Daily Budget:* `{daily_budget:,.0f} {currency}`\n"
            f"*Row moved to ADS RUNNING ✅*\n\n"
            f"Use /launch to launch the next product, or /stats to monitor campaigns.",
            parse_mode=ParseMode.MARKDOWN
        )
        _clear(uid)

    except asyncio.TimeoutError:
        err_str = "Publish timed out — Meta API did not respond in time."
        logger.error(f"[ads_launch] Publish timeout for {sku}. campaign_id={campaign_id or 'not created'}")
        # Rollback any campaign created
        if campaign_id:
            try:
                await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))
                logger.warning(f"[ads_launch] Rolled back campaign {campaign_id} after timeout")
            except Exception as _re:
                logger.error(f"[ads_launch] Rollback failed: {_re}")
        import json as _json
        extra_err = {"STATU": "ADS ERROR", "ERROR MESSAGE": err_str, "UPLOADED ASSET IDS": _json.dumps(good_assets) if good_assets else ""}
        src_tab_err = s.get("source_tab", sheet.TAB_READY)
        await loop.run_in_executor(None, lambda: sheet.move_product(p, sheet.TAB_ERROR, extra_err, source_tab=src_tab_err))
        await update.effective_message.reply_text(
            f"❌ *Publish timed out for {sku}*\n\nMeta API did not respond in time.\nProduct moved to *ADS ERROR* tab.",
            parse_mode=ParseMode.MARKDOWN
        )
        _clear(uid)

    except Exception as e:
        err_str = str(e)
        logger.error(f"[ads_launch] Publish failed for {sku}: {err_str}")
        # Rollback any campaign created before the failure
        if campaign_id and not adset_id:
            try:
                await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))
                logger.warning(f"[ads_launch] Rolled back orphaned campaign {campaign_id}")
            except Exception as _re:
                logger.error(f"[ads_launch] Rollback failed: {_re}")

        import json as _json
        extra_err = {
            "STATU":               "ADS ERROR",
            "ERROR MESSAGE":       err_str[:500],
            "UPLOADED ASSET IDS":  _json.dumps(good_assets) if good_assets else "",
        }
        src_tab_err = s.get("source_tab", sheet.TAB_READY)
        await loop.run_in_executor(None, lambda: sheet.move_product(p, sheet.TAB_ERROR, extra_err, source_tab=src_tab_err))

        await update.effective_message.reply_text(
            f"❌ *Publish failed for {sku}*\n\n"
            f"Error: `{err_str[:300]}`\n\n"
            f"Product moved to *ADS ERROR* tab.",
            parse_mode=ParseMode.MARKDOWN
        )
        _clear(uid)


# ── Helpers shared by stats & monitor ─────────────────────────────────────────

_PURCHASE_TYPES = frozenset({
    "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase",
    "omni_purchase",
    "purchase",
})


def _count_results(insights: dict) -> int:
    total = 0
    for action in insights.get("actions", []):
        if action.get("action_type") in _PURCHASE_TYPES:
            try:
                total += int(float(action.get("value", "0")))
            except (ValueError, TypeError):
                pass
    return total


# ── /stats ─────────────────────────────────────────────────────────────────────

_ERROR_STATUSES = {"DISAPPROVED", "WITH_ISSUES", "ERROR"}

def _is_error_status(s: str) -> bool:
    return s.upper() in _ERROR_STATUSES


def _fmt_status(effective_status: str) -> str:
    """Convert Meta effective_status (or internal sheet status) to a readable emoji label."""
    mapping = {
        # ── Meta API statuses ──────────────────────────────────────────────
        "ACTIVE":               "🟢 Active",
        "PAUSED":               "⏸ Paused",
        "CAMPAIGN_PAUSED":      "⏸ Paused (campaign)",
        "ADSET_PAUSED":         "⏸ Paused (ad set)",
        "PENDING_REVIEW":       "🔍 In Review",
        "IN_PROCESS":           "🔍 In Review",
        "SCHEDULED":            "🕐 Scheduled",
        "PENDING_BILLING_INFO": "💳 Pending Billing",
        "DISAPPROVED":          "🔴 Disapproved",
        "WITH_ISSUES":          "🔴 With Issues",
        "ERROR":                "🔴 Error",
        "ARCHIVED":             "🗄 Archived",
        "DELETED":              "🗑 Deleted",
        "PREAPPROVED":          "✅ Pre-Approved",
        "UNKNOWN":              "❔ Unknown",
        # ── Internal sheet statuses ────────────────────────────────────────
        "ADS RUNNING":          "🚀 Launched",
        "ADS ERROR":            "🔴 Launch Error",
        "PENDING":              "⏳ Pending",
        "REVIEWED":             "✅ Reviewed",
        "REJECTED":             "🚫 Rejected",
    }
    return mapping.get(effective_status.upper(), f"❓ {effective_status}")


async def _fetch_stats_card(uid: int, sku: str, loop) -> tuple[str, InlineKeyboardMarkup]:
    """Fetch live delivery + insights for one SKU and return (text, keyboard)."""
    row = _stats_sessions.get(uid, {}).get("data", {}).get(sku, {})
    name        = row.get("CAMPAIGN NAME") or row.get("PRODUCT NAME", "?")
    campaign_id = row.get("META CAMPAIGN ID", "").strip()

    spend   = row.get("SPEND",               "") or "—"
    results = row.get("RESULTS",             "") or "—"
    cpr     = row.get("TOTAL COST PER RZLT", "") or "—"

    status_lines = ""
    has_error    = False

    # Always show sheet status as baseline
    sheet_status = str(row.get("STATU", "") or row.get("STATUS", "") or "").strip()
    if sheet_status:
        status_lines += f"\n*Status:* {_fmt_status(sheet_status)}"

    if campaign_id:
        # Delivery status from Meta API
        try:
            delivery = await loop.run_in_executor(
                None, lambda cid=campaign_id: meta.get_delivery_status(cid)
            )
            if delivery and "campaign" in delivery:
                camp_eff = delivery["campaign"].get("effective_status", "")
                if camp_eff:
                    # Override sheet status with live Meta status
                    status_lines = f"\n*Campaign:* {_fmt_status(camp_eff)}"
                    if _is_error_status(camp_eff):
                        has_error = True

                for adset in delivery.get("adsets", []):
                    eff = adset.get("effective_status", "")
                    if eff:
                        status_lines += f"\n*Ad Set:* {_fmt_status(eff)}"
                        if _is_error_status(eff):
                            has_error = True

                for ad in delivery.get("ads", []):
                    eff = ad.get("effective_status", "")
                    if eff:
                        status_lines += f"\n*Ad:* {_fmt_status(eff)}"
                        if _is_error_status(eff):
                            has_error = True
        except Exception as e:
            logger.warning(f"[stats] Could not fetch delivery status for {sku}: {e}")

        # Live insights
        try:
            insights = await loop.run_in_executor(
                None, lambda cid=campaign_id: meta.get_campaign_insights(cid)
            )
            if insights:
                raw_spend   = float(str(insights.get("spend", "0") or "0"))
                raw_results = _count_results(insights)
                raw_cpr     = raw_spend / raw_results if raw_results > 0 else 0.0

                spend   = f"${raw_spend:.2f}"
                results = str(raw_results)
                cpr     = f"${raw_cpr:.2f}" if raw_results > 0 else "N/A"

                await loop.run_in_executor(
                    None,
                    lambda s=sku, sp=f"{raw_spend:.2f}", re=str(raw_results),
                           cp=f"{raw_cpr:.2f}" if raw_results > 0 else "0":
                    sheet.update_running_row(s, {
                        "SPEND":               sp,
                        "RESULTS":             re,
                        "TOTAL COST PER RZLT": cp,
                        "LAST METRICS SYNC":   datetime.now(timezone.utc).isoformat(),
                    })
                )
                _stats_sessions.get(uid, {}).get("data", {}).get(sku, {}).update({
                    "SPEND": spend, "RESULTS": results, "TOTAL COST PER RZLT": cpr,
                })
        except Exception as e:
            logger.warning(f"[stats] Could not fetch insights for {sku}: {e}")

    error_header = "🔴 *DELIVERY ERROR — action required*\n\n" if has_error else ""
    text = (
        f"{error_header}"
        f"📊 *SKU:* `{sku}`\n"
        f"*Name:* {name}"
        f"{status_lines}\n"
        f"\n*Spend:* {spend}\n"
        f"*Results:* {results}\n"
        f"*CPR:* {cpr}"
    )

    kb_rows = [
        [("🛑 Stop Campaign", f"stats:stop:{sku}"), ("🏆 Mark Winner", f"stats:winner:{sku}")],
        [("❌ Mark Loser",    f"stats:loser:{sku}"), ("▶️ Keep Running", f"stats:keep:{sku}")],
    ]
    if has_error:
        kb_rows.append([("🔄 Try to Republish", f"stats:republish:{sku}")])

    return text, InlineKeyboardMarkup(_kb(*kb_rows))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    uid  = update.effective_user.id
    loop = asyncio.get_running_loop()

    _save_monitor_chat_id(update.effective_chat.id)

    rows = await loop.run_in_executor(None, sheet.load_running_rows)
    if not rows:
        await update.message.reply_text(
            "No campaigns currently running in *ADS RUNNING*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _stats_sessions[uid] = {"data": {r.get("SKU", ""): r for r in rows}}

    if len(rows) == 1:
        sku = rows[0].get("SKU", "?")
        await update.message.reply_text(f"📊 Loading stats for *{sku}*…", parse_mode=ParseMode.MARKDOWN)
        text, kb = await _fetch_stats_card(uid, sku, loop)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    # Multiple campaigns — show selection list
    btn_rows = []
    for row in rows:
        sku  = row.get("SKU", "?")
        name = (row.get("CAMPAIGN NAME") or row.get("PRODUCT NAME", "?"))[:28]
        btn_rows.append([(f"{sku} — {name}", f"stats:view:{sku}")])

    await update.message.reply_text(
        "📊 *Running Campaigns*\n\nSelect a campaign to view its stats:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(_kb(*btn_rows)),
    )


async def _cb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle stats:* callback buttons."""
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data   # e.g. "stats:stop:PRD-0026"
    loop = asyncio.get_running_loop()

    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    action = parts[1]
    sku    = parts[2]

    # Retrieve cached row; fall back to sheet lookup if session expired
    stats_sess  = _stats_sessions.get(uid, {})
    row         = stats_sess.get("data", {}).get(sku, {})
    campaign_id = row.get("META CAMPAIGN ID", "").strip()

    msg = update.callback_query.message

    # ── VIEW CAMPAIGN STATS ────────────────────────────────────────────────────
    if action == "view":
        # Reload session from sheet if needed
        if not _stats_sessions.get(uid, {}).get("data"):
            rows = await loop.run_in_executor(None, sheet.load_running_rows)
            _stats_sessions[uid] = {"data": {r.get("SKU", ""): r for r in rows}}

        await msg.edit_text(f"📊 Loading stats for *{sku}*…", parse_mode=ParseMode.MARKDOWN)
        text, kb = await _fetch_stats_card(uid, sku, loop)
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    # ── REPUBLISH CAMPAIGN ─────────────────────────────────────────────────────
    elif action == "republish":
        await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nStopping old campaign…", parse_mode=ParseMode.MARKDOWN)

        # Stop old campaign (best effort)
        if campaign_id:
            await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))

        # Read config
        cfg_data = load_config()
        ad_account_id = cfg_data.get("ad_account_id", "")
        page_id       = cfg_data.get("page_id", "")
        pixel_id      = cfg_data.get("pixel_id", "")
        conversion_ev = cfg_data.get("conversion_event", "Purchase")
        country       = cfg_data.get("country", "GN")
        daily_budget       = float(cfg_data.get("daily_budget", cfg_data.get("daily_budget_usd", 5000.0)))
        objective          = cfg_data.get("objective", "OUTCOME_SALES")
        cta                = cfg_data.get("cta", "SHOP_NOW")
        daily_budget_minor = int(daily_budget * 100)

        product_name  = row.get("PRODUCT NAME", sku)
        campaign_name = f"{sku} {product_name}".strip()
        landing_url   = row.get("URL LANDING PAGE") or row.get("URL PRODUCT", "")

        raw_urls = row.get("SELECTED CREATIVES", "")
        asset_urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]

        raw_texts = row.get("SELECTED PRIMARY TEXT", "")
        texts_list = [t.strip() for t in raw_texts.split(" | ") if t.strip()]

        raw_heads = row.get("SELECTED HEADLINE", "")
        heads_list = [h.strip() for h in raw_heads.split(" | ") if h.strip()]

        if not asset_urls or not texts_list or not heads_list or not landing_url:
            await msg.edit_text(
                f"⚠️ *Cannot republish `{sku}`* — missing creatives or copy in sheet.\n"
                "Use `/launch` to manually re-launch.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        try:
            import json as _json
            cached_assets_json = row.get("UPLOADED ASSET IDS", "")
            if cached_assets_json:
                try:
                    cached = _json.loads(cached_assets_json)
                    good_assets = [a for a in cached if "error" not in a]
                    if good_assets:
                        await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nReusing {len(good_assets)} previously uploaded creative(s)…", parse_mode=ParseMode.MARKDOWN)
                    else:
                        cached_assets_json = ""
                except Exception:
                    cached_assets_json = ""

            if not cached_assets_json:
                await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nUploading creatives…", parse_mode=ParseMode.MARKDOWN)
                assets = await loop.run_in_executor(
                    None, lambda: meta.prepare_media_assets(ad_account_id, asset_urls)
                )
                good_assets = [a for a in assets if "error" not in a]

            if not good_assets:
                raise RuntimeError("All creatives failed to upload.")

            await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating campaign…", parse_mode=ParseMode.MARKDOWN)
            new_campaign_id = await loop.run_in_executor(
                None, lambda: meta.create_campaign(ad_account_id, campaign_name, objective, daily_budget_minor)
            )

            await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating ad set…", parse_mode=ParseMode.MARKDOWN)
            new_adset_id = await loop.run_in_executor(
                None, lambda: meta.create_adset(
                    ad_account_id, new_campaign_id, campaign_name,
                    country, pixel_id, conversion_ev,
                )
            )

            if len(good_assets) > 1:
                # FLEXIBLE: one normal ad per asset
                new_ad_ids = []
                for i, asset in enumerate(good_assets):
                    ad_name = f"{campaign_name} #{i + 1}"
                    await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating creative {i + 1}/{len(good_assets)}…", parse_mode=ParseMode.MARKDOWN)
                    c_id = await loop.run_in_executor(
                        None, lambda a=asset, n=ad_name: meta.create_creative_single(
                            ad_account_id, n, page_id, a,
                            landing_url, texts_list, heads_list, cta
                        )
                    )
                    await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating ad {i + 1}/{len(good_assets)}…", parse_mode=ParseMode.MARKDOWN)
                    a_id = await loop.run_in_executor(
                        None, lambda n=ad_name, c=c_id: meta.create_ad(ad_account_id, new_adset_id, n, c)
                    )
                    new_ad_ids.append(a_id)
                new_ad_id = ", ".join(new_ad_ids)
            else:
                await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating creative…", parse_mode=ParseMode.MARKDOWN)
                new_creative_id = await loop.run_in_executor(
                    None, lambda: meta.create_creative_single(
                        ad_account_id, campaign_name, page_id, good_assets[0],
                        landing_url, texts_list, heads_list, cta
                    )
                )
                await msg.edit_text(f"🔄 *Republishing `{sku}`…*\nCreating ad…", parse_mode=ParseMode.MARKDOWN)
                new_ad_id = await loop.run_in_executor(
                    None, lambda: meta.create_ad(ad_account_id, new_adset_id, campaign_name, new_creative_id)
                )

            # Update the RUNNING row with new IDs and clear cached asset IDs
            await loop.run_in_executor(None, lambda: sheet.update_running_row(sku, {
                "META CAMPAIGN ID":   new_campaign_id,
                "META ADSET ID":      new_adset_id,
                "META AD ID":         new_ad_id,
                "PUBLISHED AT":       datetime.now(timezone.utc).isoformat(),
                "ERROR MESSAGE":      "",
                "STATU":              "ADS RUNNING",
                "UPLOADED ASSET IDS": "",
            }))

            # Refresh cache
            if _stats_sessions.get(uid, {}).get("data", {}).get(sku):
                _stats_sessions[uid]["data"][sku].update({
                    "META CAMPAIGN ID": new_campaign_id,
                    "META ADSET ID":    new_adset_id,
                    "META AD ID":       new_ad_id,
                })

            await msg.edit_text(
                f"✅ *`{sku}` republished successfully!*\n\n"
                f"Campaign: `{new_campaign_id}`\n"
                f"Ad Set: `{new_adset_id}`\n"
                f"Ad: `{new_ad_id}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(_kb(
                    [("📊 View Stats", f"stats:view:{sku}")],
                )),
            )
        except Exception as e:
            logger.error(f"[stats] Republish failed for {sku}: {e}", exc_info=True)
            await msg.edit_text(
                f"❌ *Republish failed for `{sku}`*\n\n`{e}`\n\nCheck logs or use `/launch` to retry manually.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── STOP CAMPAIGN ──────────────────────────────────────────────────────────
    if action == "stop":
        if campaign_id:
            ok = await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))
            pause_msg = "⏸ Campaign stopped at all levels (campaign, ad sets, ads)." if ok else "⚠️ Could not stop via API (check logs), but you can still classify below."
        else:
            pause_msg = "⚠️ No Campaign ID on file — cannot stop via API."

        kb = InlineKeyboardMarkup(_kb(
            [("🏆 Mark Winner", f"stats:winner:{sku}"), ("❌ Mark Loser", f"stats:loser:{sku}")],
        ))
        await msg.edit_text(
            f"🛑 *Stop Campaign — `{sku}`*\n\n{pause_msg}\n\nClassify this campaign:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── MARK WINNER / MARK LOSER ───────────────────────────────────────────────
    elif action in ("winner", "loser"):
        status     = "WINNER" if action == "winner" else "LOSER"
        target_tab = sheet.TAB_WINNER if action == "winner" else sheet.TAB_LOSER
        emoji      = "🏆" if action == "winner" else "❌"

        if campaign_id:
            await loop.run_in_executor(None, lambda: meta.force_stop_campaign(campaign_id))

        extra = {
            "STATU":           status,
            "MANUAL DECISION": status,
            "STOPPED AT":      datetime.now(timezone.utc).isoformat(),
            "STOP REASON":     f"Manual: {status}",
        }
        ok = await loop.run_in_executor(
            None, lambda: sheet.move_running_product(sku, target_tab, extra)
        )

        if ok:
            # Store pending note context
            _stats_sessions.setdefault(uid, {})["note_pending"] = {
                "sku": sku,
                "tab": target_tab,
            }
            kb = InlineKeyboardMarkup(_kb(
                [("📝 Add Note", f"stats:note:{sku}"), ("⏭ Skip", f"stats:skipnote:{sku}")],
            ))
            await msg.edit_text(
                f"{emoji} *`{sku}`* marked as *{status}* and moved to {target_tab}.\n\n"
                "Do you want to add a note?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        else:
            await msg.edit_text(
                f"⚠️ Could not move `{sku}` to {target_tab}. Check logs.",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── KEEP RUNNING ───────────────────────────────────────────────────────────
    elif action == "keep":
        from datetime import timedelta
        override_until = datetime.now(timezone.utc) + timedelta(hours=8)
        await loop.run_in_executor(
            None, lambda: sheet.update_running_row(sku, {
                "OVERRIDE ACTIVE":  "TRUE",
                "OVERRIDE UNTIL":   override_until.isoformat(),
                "MANUAL DECISION":  "KEEP RUNNING",
            })
        )
        await msg.edit_text(
            f"▶️ *`{sku}`* will keep running.\n"
            f"_Automatic rules are paused for 8 hours (until {override_until.strftime('%H:%M UTC')})._",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── REQUEST NOTE ───────────────────────────────────────────────────────────
    elif action == "note":
        # note_pending was already set when winner/loser was clicked
        await msg.edit_text(
            f"📝 *Add a note for `{sku}`*\n\nType your note and send it as a message:",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── SKIP NOTE ──────────────────────────────────────────────────────────────
    elif action == "skipnote":
        # Clear pending note
        _stats_sessions.get(uid, {}).pop("note_pending", None)
        await msg.edit_text(
            f"✅ Done — `{sku}` classified, no note added.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /rules ─────────────────────────────────────────────────────────────────────

def _rules_text(r: dict) -> str:
    return (
        "⚖️ *Campaign Judgment Rules*\n\n"
        f"*Global — no-result spend limit:* `${r.get('GLOBAL_NO_RESULT_SPEND', 3.0):.2f}`\n"
        f"  → If spend ≥ this amount AND results = 0, campaign is stopped\n\n"
        f"*Day 1 CPR limit:* `${r.get('DAY1_CPR_LIMIT', 2.0):.2f}`\n"
        f"  → After 24 h: if CPR > this value, campaign is marked LOSER\n\n"
        f"*Day 2 winner CPR threshold:* `${r.get('DAY2_WINNER_CPR', 2.0):.2f}`\n"
        f"  → After 48 h: if CPR < this value → WINNER; else → LOSER"
    )


def _rules_kb() -> list:
    return _kb(
        [("✏️ Edit Global Spend Limit",  "rules:edit:global")],
        [("✏️ Edit Day 1 CPR Limit",     "rules:edit:day1")],
        [("✏️ Edit Day 2 CPR Threshold", "rules:edit:day2")],
    )


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _auth(update):
        return
    r = rules_mod.load_rules()
    await _reply(update, _rules_text(r), _rules_kb())


async def _cb_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle rules:* callback buttons."""
    await _answer(update)
    uid  = update.effective_user.id
    data = update.callback_query.data   # e.g. "rules:edit:global"

    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    field = parts[2]   # global | day1 | day2

    labels = {
        "global": ("Global no-result spend limit",   "GLOBAL_NO_RESULT_SPEND"),
        "day1":   ("Day 1 CPR limit",                "DAY1_CPR_LIMIT"),
        "day2":   ("Day 2 winner CPR threshold",     "DAY2_WINNER_CPR"),
    }
    if field not in labels:
        return

    label, _ = labels[field]
    _rules_sessions[uid] = {"editing": field}

    await update.callback_query.message.edit_text(
        f"✏️ *Editing: {label}*\n\nEnter the new value (a number, e.g. `2.5`):",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Monitor chat_id persistence ───────────────────────────────────────────────

_CHAT_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ads_monitor_chatid.txt")


def _save_monitor_chat_id(chat_id: int) -> None:
    try:
        with open(_CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))
    except Exception:
        pass


def _load_monitor_chat_id() -> int | None:
    try:
        if os.path.isfile(_CHAT_ID_FILE):
            with open(_CHAT_ID_FILE) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Register bot commands and start the background campaign monitor."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    logger.info("[ads_launch] Bot commands menu registered")

    from ads_campaign_monitor import run_monitor
    chat_id = _load_monitor_chat_id()
    asyncio.create_task(run_monitor(app.bot, chat_id))
    logger.info(f"[ads_launch] Campaign monitor started (notify chat_id={chat_id})")


def build_app(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("cancel",     cmd_cancel))
    app.add_handler(CommandHandler("launch",     cmd_launch))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("rules",      cmd_rules))
    app.add_handler(CommandHandler("setup_meta", cmd_setup_meta))

    app.add_handler(CallbackQueryHandler(_cb_launch,          pattern=r"^launch:"))
    app.add_handler(CallbackQueryHandler(_cb_setup,           pattern=r"^su_"))
    app.add_handler(CallbackQueryHandler(_cb_setup_val,       pattern=r"^settings_val:"))
    app.add_handler(CallbackQueryHandler(_cb_creative_select, pattern=r"^csel:"))
    app.add_handler(CallbackQueryHandler(_cb_copy,            pattern=r"^copy[_:]"))
    app.add_handler(CallbackQueryHandler(_cb_settings,        pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(_cb_scheduling,      pattern=r"^sched:"))
    app.add_handler(CallbackQueryHandler(_cb_final,           pattern=r"^final:"))
    app.add_handler(CallbackQueryHandler(_cb_stats,           pattern=r"^stats:"))
    app.add_handler(CallbackQueryHandler(_cb_rules,           pattern=r"^rules:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text_input))

    return app
