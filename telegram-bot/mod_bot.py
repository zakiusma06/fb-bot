"""
mod_bot.py - Moderation bot handlers.

Commands (all clickable from the Telegram menu):
  /start   — show main menu
  /review  — load PENDING products and review them one by one
  /stats   — show live statistics from all tabs
  /stop    — cancel the current review session

Inline buttons (inside review flow):
  APPROVE            → move to APPROVED tab
  DISAPPROVE         → move to DISAPROVED tab
  Edit Sourcing Price → edit SOURCING PRICE USD (enter in RMB, auto-converts to USD)
  Edit Weight        → edit WEIGHT GRAM
  NEXT               → skip, keep in PENDING, show next product
  STOP               → end session

Filter flow (triggered by "Review Pending"):
  Multi-filter panel: price range, variants, keyword (all combinable).
  Price and variants cycle through options on tap.
  Keyword shows a submenu.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import aiohttp
import shopify_cache

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from mod_sheet import (
    load_pending_rows,
    load_pending_keywords,
    move_row,
    get_statistics,
    update_sourcing_data,
)

logger = logging.getLogger(__name__)

# ── Persistent main menu ───────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Review Pending", "📊 Statistics"],
        ["🛑 Stop"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_BTN_REVIEW = "📋 Review Pending"
_BTN_STATS  = "📊 Statistics"
_BTN_STOP   = "🛑 Stop"

# ── Session state ─────────────────────────────────────────────────────────
# user_id → {
#   "rows":     list[dict],
#   "index":    int,
#   "awaiting": None | "edit_sourcing_price" | "edit_weight",
# }
_sessions: dict[int, dict] = {}

# ── Filter panel state ─────────────────────────────────────────────────────
# user_id → { "price_range": str, "variants": str, "keyword": str }
_filter_sessions: dict[int, dict] = {}

_PRICE_RANGE_CYCLE = ["", "under5", "5to10", "10to20", "20plus"]
_VARIANTS_CYCLE    = ["", "yes", "no"]

_PRICE_LABELS = {
    "":       "💲 Price: ALL",
    "under5": "💲 Price: < $5",
    "5to10":  "💲 Price: $5 – $10",
    "10to20": "💲 Price: $10 – $20",
    "20plus": "💲 Price: $20+",
}
_VARIANT_LABELS = {
    "":    "🔀 Variants: ALL",
    "yes": "🔀 Variants: WITH VARIANTS",
    "no":  "🔀 Variants: NO VARIANTS",
}


# ── Register Telegram command menu ────────────────────────────────────────
async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([
            BotCommand("start",  "Start the bot / show menu"),
            BotCommand("review", "Review pending products"),
            BotCommand("stats",  "View statistics"),
            BotCommand("stop",   "Stop current review session"),
        ])
        logger.info("[mod_bot] Telegram command menu registered")
    except Exception as e:
        logger.warning(f"[mod_bot] Could not register command menu (non-fatal): {e}")
    # Start Shopify product cache in background (non-blocking)
    asyncio.create_task(_init_shopify_cache())


async def _init_shopify_cache() -> None:
    """Load Shopify product cache at startup for duplicate detection."""
    try:
        n = await shopify_cache.init_cache()
        logger.info(f"[mod_bot] Shopify cache ready: {n} product(s) cached")
    except Exception as e:
        logger.warning(f"[mod_bot] Shopify cache init failed (duplicate detection disabled): {e}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape HTML special characters so URLs never break the message."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _make_keyboard(sku: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ APPROVE",    callback_data=f"approve:{sku}"),
            InlineKeyboardButton("❌ DISAPPROVE", callback_data=f"disapprove:{sku}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Sourcing Price", callback_data=f"edit_price:{sku}"),
            InlineKeyboardButton("✏️ Edit Weight",         callback_data=f"edit_weight:{sku}"),
        ],
        [
            InlineKeyboardButton("🔗 Edit Sourcing URL", callback_data=f"edit_url:{sku}"),
        ],
        [
            InlineKeyboardButton("⏭ NEXT", callback_data=f"next:{sku}"),
            InlineKeyboardButton("🛑 STOP", callback_data="stop_review"),
        ],
    ])


def _default_filter_state() -> dict:
    return {"price_range": "", "variants": "", "keyword": ""}


def _make_filter_panel_text(fs: dict) -> str:
    active = []
    if fs.get("price_range"):
        active.append(_PRICE_LABELS[fs["price_range"]])
    if fs.get("variants"):
        active.append(_VARIANT_LABELS[fs["variants"]])
    if fs.get("keyword"):
        active.append(f"🔑 Keyword: {fs['keyword']}")

    if active:
        return "🔍 *Active filters:*\n" + "\n".join(f"  • {a}" for a in active) + "\n\nChange or apply:"
    return "🔍 *Configure filters* _(all optional)_\n\nTap a button to set a filter, then tap *Apply*:"


def _make_filter_panel_keyboard(fs: dict) -> InlineKeyboardMarkup:
    price_btn   = _PRICE_LABELS.get(fs.get("price_range", ""), "💲 Price: ALL")
    variant_btn = _VARIANT_LABELS.get(fs.get("variants", ""), "🔀 Variants: ALL")
    kw_val      = fs.get("keyword", "")
    kw_btn      = f"🔑 {kw_val[:18]}" if kw_val else "🔑 Keyword: ALL"

    rows = [
        [InlineKeyboardButton(price_btn,   callback_data="fp:cycle_price")],
        [InlineKeyboardButton(variant_btn, callback_data="fp:cycle_variants")],
        [InlineKeyboardButton(kw_btn,      callback_data="fp:pick_kw")],
        [InlineKeyboardButton("✅ Apply / Start Review", callback_data="fp:apply")],
    ]
    return InlineKeyboardMarkup(rows)


def _make_kw_submenu_keyboard(keywords: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(kw, callback_data=f"fkw_panel:{kw}")] for kw in keywords[:20]]
    rows.append([InlineKeyboardButton("⬅ Back to Filters", callback_data="fp:back")])
    return InlineKeyboardMarkup(rows)


async def _show_filter_panel(query, user_id: int) -> None:
    """Edit the current message to show the filter panel with current state."""
    fs = _filter_sessions.get(user_id, _default_filter_state())
    _filter_sessions[user_id] = fs
    await query.edit_message_text(
        _make_filter_panel_text(fs),
        parse_mode="Markdown",
        reply_markup=_make_filter_panel_keyboard(fs),
    )


def _make_duplicate_keyboard(sku: str, matched_product) -> InlineKeyboardMarkup:
    """Keyboard shown when a Shopify duplicate is detected."""
    # Prefer the public storefront URL; fall back to admin URL only if unavailable
    open_url = matched_product.storefront_url or matched_product.admin_url
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Skip (Mark as Duplicate)", callback_data=f"dup_skip:{sku}")],
        [InlineKeyboardButton("🔗 Open Existing in Shopify", url=open_url)],
        [InlineKeyboardButton("✅ Force Approve (Ignore Warning)", callback_data=f"dup_force:{sku}")],
    ])


async def _send_product(bot, chat_id: int, session: dict):
    """Send a fresh product card message, with duplicate warning if needed."""
    rows  = session["rows"]
    index = session["index"]

    if not rows or index >= len(rows):
        await bot.send_message(
            chat_id=chat_id,
            text="📭 <b>No more pending products.</b>\n\nRun /review again to reload.",
            parse_mode="HTML",
        )
        return

    row   = rows[index]
    sku   = row.get("SKU", "—")
    total = len(rows)

    keyword        = _esc(row.get("KEYWORD", "") or "—")
    url_product    = _esc(row.get("URL PRODUCT", "") or "—")
    url_ad         = _esc(row.get("ADS LIBRARY MEDIA URL", "") or "—")
    sourcing_price = row.get("SOURCING PRICE USD", "") or ""
    sourcing_url   = _esc(row.get("SOURCING URL", "") or "")
    weight_gram    = row.get("WEIGHT GRAM", "") or ""
    has_variants   = row.get("HAS VARIANTS", "") or "—"
    product_name   = row.get("PRODUCT NAME", "") or ""

    price_display    = f"${_esc(sourcing_price)} USD" if sourcing_price else "—"
    supplier_display = sourcing_url if sourcing_url else "—"
    weight_display   = f"{_esc(weight_gram)} g"       if weight_gram   else "—"

    text = (
        f"<b>Product {index + 1} / {total}</b>\n\n"
        f"🔑 <b>Keyword:</b>\n{keyword}\n\n"
        f"🛍 <b>Product Page:</b>\n{url_product}\n\n"
        f"📢 <b>Ads Library Creative:</b>\n{url_ad}\n\n"
        f"💲 <b>Sourcing Price:</b>\n{price_display}\n\n"
        f"🔗 <b>Supplier Link:</b>\n{supplier_display}\n\n"
        f"⚖️ <b>Weight:</b>\n{weight_display}\n\n"
        f"🎨 <b>Has Variants:</b>\n{has_variants}"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_make_keyboard(sku),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # ── Duplicate detection (only when cache is loaded) ──────────────────
    if shopify_cache.is_loaded() and shopify_cache.cache_size() > 0:
        try:
            title_for_check = product_name or row.get("KEYWORD", "") or ""
            source_url = row.get("URL PRODUCT", "") or ""

            # ── Step 1: sheet image pre-check (runs first) ────────────────
            # Use the product image stored in the sheet (og_image from the
            # product page scrape).  This is a real product photo and gives
            # much more reliable hash comparisons than the Ads Library URL.
            dup = None
            sheet_image_url = (row.get("IMAGE URL", "") or "").strip()
            if sheet_image_url:
                dup = await shopify_cache.check_duplicate_by_image(sheet_image_url)

            # ── Step 2: fallback — existing duplicate detection logic ─────
            if dup is None:
                # Use only direct image URLs for hashing; Facebook Ads Library
                # page URLs return 403 and are not suitable for image comparison.
                raw_img_url = row.get("ADS LIBRARY MEDIA URL", "") or ""
                is_direct_image = any(
                    raw_img_url.lower().endswith(ext)
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
                )
                fallback_image_url = raw_img_url if is_direct_image else ""
                dup = await shopify_cache.check_duplicate(
                    title=title_for_check,
                    description="",
                    image_urls=[fallback_image_url] if fallback_image_url else [],
                    source_url=source_url,
                )

            if dup.is_duplicate or dup.is_possible:
                matched = dup.matched_product
                level   = "🔴 DUPLICATE DETECTED" if dup.is_duplicate else "🟡 POSSIBLE DUPLICATE"
                reasons_text = "\n".join(f"  • {r}" for r in dup.reasons) if dup.reasons else "  • Similarity score match"
                warning_text = (
                    f"⚠️ <b>{level}</b>\n\n"
                    f"This product may already exist in your Shopify store.\n\n"
                    f"<b>Matched product:</b> {_esc(matched.title)}\n"
                    f"<b>Similarity score:</b> {dup.score}/100\n\n"
                    f"<b>Signals:</b>\n{reasons_text}\n\n"
                    f"Choose an action below:"
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=warning_text,
                    parse_mode="HTML",
                    reply_markup=_make_duplicate_keyboard(sku, matched),
                    disable_web_page_preview=True,
                )
        except Exception as _de:
            logger.debug(f"[mod_bot] Duplicate check error for {sku}: {_de}")


async def _start_review_with_rows(bot, chat_id: int, user_id: int, rows: list[dict]):
    """Initialise a review session with the given rows."""
    if not rows:
        await bot.send_message(
            chat_id=chat_id,
            text="📭 <b>No products match that filter.</b>\n\nTry a different filter or show all.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    _sessions[user_id] = {"rows": rows, "index": 0, "awaiting": None}
    logger.info(f"[mod] User {user_id} started review — {len(rows)} product(s)")

    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ <b>Loaded {len(rows)} product(s).</b> Starting review…",
        parse_mode="HTML",
    )
    await _send_product(bot, chat_id, _sessions[user_id])


async def _get_rmb_to_usd() -> float:
    """Fetch live CNY→USD exchange rate; falls back to 0.14 on any error."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(
                "https://api.exchangerate-api.com/v4/latest/CNY"
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["rates"]["USD"])
    except Exception:
        pass
    return 0.14


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Moderation Bot*\n\n"
        "Review products from the PENDING tab one by one.\n\n"
        "Use the menu below or the commands list (☰) to navigate.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _filter_sessions[user_id] = _default_filter_state()
    fs = _filter_sessions[user_id]
    await update.message.reply_text(
        _make_filter_panel_text(fs),
        parse_mode="Markdown",
        reply_markup=_make_filter_panel_keyboard(fs),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Loading statistics…")
    stats = get_statistics()
    if not stats:
        await update.message.reply_text(
            "⚠️ Could not load statistics. Check the sheet connection.",
            reply_markup=MAIN_MENU,
        )
        return

    lines = [
        "📊 *Sheet Statistics*\n",
        f"⏳ Pending:       *{stats.get('PENDING', 0)}*",
        f"🔍 Pre-Approved:  *{stats.get('PREAPPROVED', 0)}*",
        f"✅ Approved:      *{stats.get('APPROVED', 0)}*",
        f"❌ Disapproved:   *{stats.get('DISAPROVED', 0)}*",
        f"\n📈 Approval Rate:  *{stats.get('approval_rate', 'N/A')}*",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in _sessions:
        _sessions.pop(user_id)
        await update.message.reply_text(
            "🛑 Review session stopped.",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text(
            "No active review session.",
            reply_markup=MAIN_MENU,
        )


# ── Text input handler (for edit sourcing / weight) ───────────────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu buttons and text input during sourcing edits."""
    user_id = update.effective_user.id
    text    = (update.message.text or "").strip()

    # Main menu buttons
    if text == _BTN_REVIEW:
        await cmd_review(update, context)
        return
    if text == _BTN_STATS:
        await cmd_stats(update, context)
        return
    if text == _BTN_STOP:
        await cmd_stop(update, context)
        return

    session = _sessions.get(user_id)
    if not session or session.get("awaiting") is None:
        return

    awaiting = session["awaiting"]
    rows     = session["rows"]
    index    = session["index"]

    if not rows or index >= len(rows):
        session["awaiting"] = None
        return

    row = rows[index]
    sku = row.get("SKU", "")

    # ── Edit sourcing price ────────────────────────────────────────────────
    if awaiting == "edit_sourcing_price":
        session["awaiting"] = None
        try:
            rmb = float(text.replace(",", ".").strip())
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid amount. Please enter a number (e.g. 25 or 12.5).\n"
                "Send the price in RMB (¥) again:",
            )
            session["awaiting"] = "edit_sourcing_price"
            return

        rate = await _get_rmb_to_usd()
        usd  = round(rmb * rate, 2)
        usd_str = f"{usd:.2f}"

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_sourcing_data(sku, price_usd=usd_str)
        )
        if ok:
            row["SOURCING PRICE USD"] = usd_str
            await update.message.reply_text(
                f"✅ Sourcing price updated:\n"
                f"¥{rmb} RMB → ${usd_str} USD\n\n"
                "Showing updated product card…"
            )
        else:
            await update.message.reply_text(
                "⚠️ Failed to save sourcing price. Continuing…"
            )

        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Edit weight ────────────────────────────────────────────────────────
    if awaiting == "edit_weight":
        session["awaiting"] = None
        raw = text.strip()
        try:
            grams = int(round(float(raw.replace(",", "."))))
            if grams <= 0:
                raise ValueError("non-positive")
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid weight. Please enter a whole number in grams (e.g. 350).\n"
                "Send the weight again:",
            )
            session["awaiting"] = "edit_weight"
            return

        gram_str = str(grams)
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_sourcing_data(sku, weight_gram=gram_str)
        )
        if ok:
            row["WEIGHT GRAM"] = gram_str
            await update.message.reply_text(
                f"✅ Weight updated: {gram_str} g\n\nShowing updated product card…"
            )
        else:
            await update.message.reply_text(
                "⚠️ Failed to save weight. Continuing…"
            )

        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Edit sourcing URL ──────────────────────────────────────────────────
    if awaiting == "edit_sourcing_url":
        session["awaiting"] = None
        url = text.strip()
        if not url.startswith("http"):
            await update.message.reply_text(
                "⚠️ That doesn't look like a valid URL. It should start with http:// or https://\n"
                "Please paste the sourcing URL again:",
            )
            session["awaiting"] = "edit_sourcing_url"
            return

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_sourcing_data(sku, sourcing_url=url)
        )
        if ok:
            row["SOURCING URL"] = url
            await update.message.reply_text(
                f"✅ Sourcing URL updated:\n<code>{_esc(url)}</code>\n\nShowing updated product card…",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Failed to save sourcing URL. Continuing…")

        await _send_product(context.bot, update.effective_chat.id, session)
        return


# ── Main menu button handler ──────────────────────────────────────────────

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_text_input(update, context)


# ── Callback handler (inline buttons) ────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data    = query.data or ""

    # ── STOP button inside review ──────────────────────────────────────────
    if data == "stop_review":
        _sessions.pop(user_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            "🛑 Review session stopped.",
            reply_markup=MAIN_MENU,
        )
        return

    # ── Filter panel: cycle price ──────────────────────────────────────────
    if data == "fp:cycle_price":
        fs = _filter_sessions.setdefault(user_id, _default_filter_state())
        cur_idx = _PRICE_RANGE_CYCLE.index(fs.get("price_range", "")) if fs.get("price_range", "") in _PRICE_RANGE_CYCLE else 0
        fs["price_range"] = _PRICE_RANGE_CYCLE[(cur_idx + 1) % len(_PRICE_RANGE_CYCLE)]
        await _show_filter_panel(query, user_id)
        return

    # ── Filter panel: cycle variants ───────────────────────────────────────
    if data == "fp:cycle_variants":
        fs = _filter_sessions.setdefault(user_id, _default_filter_state())
        cur_idx = _VARIANTS_CYCLE.index(fs.get("variants", "")) if fs.get("variants", "") in _VARIANTS_CYCLE else 0
        fs["variants"] = _VARIANTS_CYCLE[(cur_idx + 1) % len(_VARIANTS_CYCLE)]
        await _show_filter_panel(query, user_id)
        return

    # ── Filter panel: show keyword submenu ────────────────────────────────
    if data == "fp:pick_kw":
        kws = await asyncio.get_event_loop().run_in_executor(None, load_pending_keywords)
        if not kws:
            await query.answer("No keywords found in PENDING yet.", show_alert=True)
            return
        await query.edit_message_text(
            "🔑 *Choose a keyword* _(sorted by approval rate)_:",
            parse_mode="Markdown",
            reply_markup=_make_kw_submenu_keyboard(kws),
        )
        return

    # ── Filter panel: back from submenu ───────────────────────────────────
    if data == "fp:back":
        await _show_filter_panel(query, user_id)
        return

    # ── Filter panel: keyword selection (return to panel) ─────────────────
    if data.startswith("fkw_panel:"):
        kw = data[len("fkw_panel:"):]
        fs = _filter_sessions.setdefault(user_id, _default_filter_state())
        fs["keyword"] = kw
        await _show_filter_panel(query, user_id)
        return

    # ── Filter panel: apply ───────────────────────────────────────────────
    if data == "fp:apply":
        fs = _filter_sessions.get(user_id, _default_filter_state())
        await query.edit_message_text("⏳ Loading products with selected filters…")
        rows = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: load_pending_rows(
                price_range=fs.get("price_range", ""),
                keyword=fs.get("keyword", ""),
                variants=fs.get("variants", ""),
            ),
        )
        await _start_review_with_rows(
            context.bot, query.message.chat_id, user_id, rows
        )
        return

    # ── Edit sourcing price button ─────────────────────────────────────────
    if data.startswith("edit_price:"):
        sku     = data[len("edit_price:"):]
        session = _sessions.get(user_id)
        if not session:
            await query.answer("No active review session.", show_alert=True)
            return
        session["awaiting"] = "edit_sourcing_price"
        await query.message.reply_text(
            f"✏️ <b>Edit Sourcing Price</b> for <code>{_esc(sku)}</code>\n\n"
            "Enter the supplier price in <b>RMB (¥)</b>:\n"
            "<i>(e.g. 25 or 12.5 — the bot will convert to USD automatically)</i>",
            parse_mode="HTML",
        )
        return

    # ── Edit weight button ─────────────────────────────────────────────────
    if data.startswith("edit_weight:"):
        sku     = data[len("edit_weight:"):]
        session = _sessions.get(user_id)
        if not session:
            await query.answer("No active review session.", show_alert=True)
            return
        session["awaiting"] = "edit_weight"
        await query.message.reply_text(
            f"✏️ <b>Edit Weight</b> for <code>{_esc(sku)}</code>\n\n"
            "Enter the product weight in <b>grams</b>:\n"
            "<i>(e.g. 350)</i>",
            parse_mode="HTML",
        )
        return

    # ── Edit sourcing URL button ───────────────────────────────────────────
    if data.startswith("edit_url:"):
        sku     = data[len("edit_url:"):]
        session = _sessions.get(user_id)
        if not session:
            await query.answer("No active review session.", show_alert=True)
            return
        rows  = session["rows"]
        index = session["index"]
        row   = rows[index] if rows and index < len(rows) else {}
        current_url = _esc(row.get("SOURCING URL", "") or "")
        current_note = f"\nCurrent: <code>{current_url}</code>" if current_url else ""
        session["awaiting"] = "edit_sourcing_url"
        await query.message.reply_text(
            f"🔗 <b>Edit Sourcing URL</b> for <code>{_esc(sku)}</code>{current_note}\n\n"
            "Paste the new supplier/sourcing URL:",
            parse_mode="HTML",
        )
        return

    # ── Duplicate warning: skip (disapprove) ─────────────────────────────
    if data.startswith("dup_skip:"):
        sku     = data[len("dup_skip:"):]
        session = _sessions.get(user_id)
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        rows  = session["rows"]
        index = session["index"]
        row_pos = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if row_pos is None:
            await query.answer("Product already processed.", show_alert=True)
            return
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: move_row(sku, "DISAPROVED", "DISAPROVED")
        )
        rows.pop(row_pos)
        session["index"] = row_pos if row_pos < len(rows) else max(0, len(rows) - 1)
        try:
            await query.edit_message_text(
                f"⏭ <b>{_esc(sku)}</b> skipped — marked as <b>Duplicate</b>.\n"
                + (
                    f"<i>Reason: Duplicate detected with Shopify catalog</i>"
                    if ok else
                    f"<i>⚠️ Sheet API error — check manually</i>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        if not rows:
            await query.message.reply_text(
                "🎉 <b>All products reviewed!</b>",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, query.message.chat_id, session)
        return

    # ── Duplicate warning: force approve (dismiss warning) ────────────────
    if data.startswith("dup_force:"):
        try:
            await query.edit_message_text(
                "✅ <b>Duplicate warning dismissed.</b>\n"
                "Use the product card above to Approve or Disapprove.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── Review action buttons (approve / disapprove / next) ───────────────
    if ":" not in data:
        return

    action, sku = data.split(":", 1)
    session     = _sessions.get(user_id)

    # ── No active session ──────────────────────────────────────────────────
    if not session:
        await query.edit_message_text(
            "⚠️ No active review session. Tap 📋 Review Pending to start.",
            parse_mode="Markdown",
        )
        return

    rows  = session["rows"]
    index = session["index"]

    # ── NEXT ───────────────────────────────────────────────────────────────
    if action == "next":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        session["index"] = index + 1

        if session["index"] >= len(rows):
            await query.message.reply_text(
                "📭 *No more pending products.*\n\n"
                "All remaining items have been skipped.\n"
                "Tap 📋 Review Pending to reload.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, query.message.chat_id, session)
        return

    # ── Double-tap guard ───────────────────────────────────────────────────
    row_pos = next(
        (i for i, r in enumerate(rows) if r.get("SKU") == sku), None
    )

    if row_pos is None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"⚠️ *{sku}* was already processed. Showing next…",
            parse_mode="Markdown",
        )
        session["index"] = index + 1
        if session["index"] < len(rows):
            await _send_product(context.bot, query.message.chat_id, session)
        else:
            await query.message.reply_text(
                "📭 *No more pending products.*\n\nTap 📋 Review Pending to reload.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        return

    # ── APPROVE / DISAPPROVE ───────────────────────────────────────────────
    if action == "approve":
        target_tab = "PREAPPROVED"
        new_status = "PREAPPROVED"
        label      = "⏳ PREAPPROVED"
    elif action == "disapprove":
        target_tab = "DISAPROVED"
        new_status = "DISAPROVED"
        label      = "❌ DISAPPROVED"
    else:
        return

    ok = await asyncio.get_event_loop().run_in_executor(
        None, lambda: move_row(sku, target_tab, new_status)
    )

    # Always pop from the in-memory list so the session keeps moving forward,
    # regardless of whether the sheet call succeeded or not.
    rows.pop(row_pos)
    session["index"] = row_pos
    remaining = len(rows)

    if ok:
        confirmation = (
            f"*{sku}* → {label}\n"
            f"Moved to *{target_tab}* tab.\n"
            f"_{remaining} product(s) remaining._"
        )
    else:
        # Sheet API failed even after retries — product may or may not have been
        # moved in the sheet.  We still advance the session so the user is not
        # forced to re-run /review.
        confirmation = (
            f"⚠️ *{sku}* — sheet API error, please check manually.\n"
            f"_{remaining} product(s) remaining._"
        )

    try:
        await query.edit_message_text(
            confirmation,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    if remaining == 0:
        await query.message.reply_text(
            "🎉 *All products reviewed!*\n\n"
            "PENDING tab is now empty.\n"
            "Tap 📋 Review Pending to check for new ones.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        _sessions.pop(user_id, None)
    else:
        if session["index"] >= remaining:
            session["index"] = remaining - 1
        await _send_product(context.bot, query.message.chat_id, session)


# ── Application builder ───────────────────────────────────────────────────

def build_moderation_application() -> Application:
    token = os.environ.get("TELEGRAM_MODERATION_BOT_TOKEN", "")
    if not token:
        raise ValueError(
            "TELEGRAM_MODERATION_BOT_TOKEN is not set. "
            "Create a second bot via @BotFather and add the token as a secret."
        )

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_input,
        )
    )

    return app
