"""
approval_bot.py - Approval Bot (merged: product review + pricing + Shopify creation).

Replaces the old Moderation Bot and Pricing Bot entirely.

Flow per product:
  1. Load from PENDING (with filter panel)
  2. Show product card: keyword, product URL, ads creative URL, sourcing price,
     supplier link, weight, has variants, suggested selling price,
     current PRICE / COMPARE AT PRICE (if already set)
  3. Inline buttons: Edit SC Price, Edit Weight, Enter Selling Price,
     Enter Compare At Price, Preapprove, Disapprove, Skip
  4. Preapprove → validates both prices set → runs Shopify pipeline → storefront link
  5. Post-Shopify panel: Approve Product | Regenerate | Skip
  6. Approve Product → PENDING → APPROVED
  7. Disapprove → PENDING → DISAPPROVED
  8. Skip → keeps in PENDING, advances to next

All edits (SC price, weight, selling price, compare at price) save immediately to PENDING.
Product stays in PENDING until Approve or Disapprove.
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
    load_pending_filter_counts,
    move_row,
    get_statistics,
    update_sourcing_data,
    update_pending_fields,
)
from shopify_pipeline import run_pipeline
import shopify_client
from config import USD_TO_GNF, ROUND_TO_GNF

logger = logging.getLogger(__name__)

# ── Persistent main menu ──────────────────────────────────────────────────
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
#   "awaiting": None | "edit_sourcing_price" | "edit_weight"
#               | "edit_selling_price" | "edit_compare_price"
#               | "edit_sourcing_url" | "new_title" | "new_description",
# }
_sessions: dict[int, dict] = {}

# ── Filter panel state ────────────────────────────────────────────────────
# user_id → { "price_range": str, "variants": str, "keyword": str }
_filter_sessions: dict[int, dict] = {}

# ── Shopify pending state (post-pipeline control panel) ───────────────────
# user_id → {
#   "product_id":   int,
#   "sku":          str,
#   "title":        str,
#   "description":  str,
#   "store_url":    str,
#   "admin_url":    str,
#   "images":       list[dict],
#   "panel_msg_id": int | None,
# }
_shopify_pending: dict[int, dict] = {}

# ── Manual photo upload state ──────────────────────────────────────────────
# user_id → list of Telegram file_ids collected so far
_awaiting_photos: dict[int, list] = {}

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


# ── Startup ───────────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([
            BotCommand("start",  "Start / show menu"),
            BotCommand("review", "Review pending products"),
            BotCommand("stats",  "View statistics"),
            BotCommand("stop",   "Stop current session"),
        ])
        logger.info("[approval_bot] Telegram command menu registered")
    except Exception as e:
        logger.warning(f"[approval_bot] Command menu registration failed (non-fatal): {e}")
    asyncio.create_task(_init_shopify_cache())


async def _init_shopify_cache() -> None:
    try:
        n = await shopify_cache.init_cache()
        logger.info(f"[approval_bot] Shopify cache ready: {n} product(s)")
    except Exception as e:
        logger.warning(f"[approval_bot] Shopify cache init failed (dup detection disabled): {e}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _calc_suggested_price(sourcing_usd: float, weight_gram: int) -> tuple[float, int]:
    """
    Returns (usd_subtotal, gnf_rounded).
    Formula:
      usd = sourcing + 8.50 (shipping agent) + 7.50 (marketing) + 10.00 (profit)
            + (weight_gram / 1000) × 16  (China shipping per kg)
      gnf = usd × USD_TO_GNF, rounded to nearest ROUND_TO_GNF
    """
    shipping = (weight_gram / 1000) * 16
    usd = sourcing_usd + 8.5 + 7.5 + 10.0 + shipping
    gnf = round(usd * USD_TO_GNF / ROUND_TO_GNF) * ROUND_TO_GNF
    return (usd, gnf)


def _make_product_keyboard(sku: str, has_price: bool, has_compare: bool) -> InlineKeyboardMarkup:
    can_preapprove = has_price and has_compare
    preapprove_btn = (
        InlineKeyboardButton("🚀 PREAPPROVE", callback_data=f"preapprove:{sku}")
        if can_preapprove
        else InlineKeyboardButton("🔒 PREAPPROVE  (set price first)", callback_data=f"preapprove_blocked:{sku}")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit SC Price", callback_data=f"edit_price:{sku}"),
            InlineKeyboardButton("✏️ Edit Weight",   callback_data=f"edit_weight:{sku}"),
        ],
        [
            InlineKeyboardButton("💵 Enter Selling Price",    callback_data=f"edit_selling:{sku}"),
            InlineKeyboardButton("🏷 Enter Compare At Price", callback_data=f"edit_compare:{sku}"),
        ],
        [preapprove_btn],
        [
            InlineKeyboardButton("❌ Disapprove", callback_data=f"disapprove:{sku}"),
            InlineKeyboardButton("⏭ Skip",        callback_data=f"skip:{sku}"),
        ],
        [
            InlineKeyboardButton("🔗 Edit Sourcing URL", callback_data=f"edit_url:{sku}"),
            InlineKeyboardButton("🛑 Stop",              callback_data="stop_review"),
        ],
    ])


def _make_control_panel_keyboard(image_count: int = 1) -> InlineKeyboardMarkup:
    rows = []
    if image_count == 0:
        rows.append([InlineKeyboardButton("📷 No images — Add manually", callback_data="cp_add_photos")])
    else:
        rows.append([InlineKeyboardButton("📷 Add more images", callback_data="cp_add_photos")])
    rows.append([InlineKeyboardButton("✅ APPROVE PRODUCT", callback_data="cp_approve")])
    rows.append([
        InlineKeyboardButton("🔄 REGENERATE",         callback_data="cp_regen"),
        InlineKeyboardButton("⏭ Skip (keep pending)", callback_data="cp_skip"),
    ])
    return InlineKeyboardMarkup(rows)


def _make_regen_keyboard(images: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("✏️ Change Title",       callback_data="regen_title"),
            InlineKeyboardButton("📝 Change Description", callback_data="regen_desc"),
        ],
    ]
    img_buttons = [
        InlineKeyboardButton(
            f"🗑 Delete Image {i + 1}",
            callback_data=f"regen_del_img:{img['id']}",
        )
        for i, img in enumerate(images[:5])
    ]
    for i in range(0, len(img_buttons), 2):
        rows.append(img_buttons[i:i + 2])
    rows.append([InlineKeyboardButton("⬅ Back to Panel", callback_data="cp_back")])
    return InlineKeyboardMarkup(rows)


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


def _make_filter_panel_keyboard(fs: dict, counts: dict = None) -> InlineKeyboardMarkup:
    counts = counts or {}
    pc = counts.get("price", {})
    vc = counts.get("variants", {})
    total = counts.get("total", 0)

    # Price button
    pr = fs.get("price_range", "")
    price_label = _PRICE_LABELS.get(pr, "💲 Price: ALL")
    if pr and pc.get(pr) is not None:
        price_label += f" ({pc[pr]} pending)"
    elif not pr and total:
        price_label += f" ({total} pending)"

    # Variants button
    vr = fs.get("variants", "")
    var_label = _VARIANT_LABELS.get(vr, "🔀 Variants: ALL")
    if vr and vc.get(vr) is not None:
        var_label += f" ({vc[vr]} pending)"
    elif not vr and total:
        var_label += f" ({total} pending)"

    # Keyword button
    kw_val = fs.get("keyword", "")
    kw_btn = f"🔑 {kw_val[:18]}" if kw_val else "🔑 Keyword: ALL"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(price_label, callback_data="fp:cycle_price")],
        [InlineKeyboardButton(var_label,   callback_data="fp:cycle_variants")],
        [InlineKeyboardButton(kw_btn,      callback_data="fp:pick_kw")],
        [InlineKeyboardButton("✅ Apply / Start Review", callback_data="fp:apply")],
    ])


def _make_kw_submenu_keyboard(keywords: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{kw['keyword']} ({kw['count']} pending)",
            callback_data=f"fkw_panel:{kw['keyword']}"
        )]
        for kw in keywords[:20]
    ]
    rows.append([InlineKeyboardButton("⬅ Back to Filters", callback_data="fp:back")])
    return InlineKeyboardMarkup(rows)


def _make_duplicate_keyboard(sku: str, matched_product) -> InlineKeyboardMarkup:
    open_url = matched_product.storefront_url or matched_product.admin_url
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Skip (Mark as Duplicate)", callback_data=f"dup_skip:{sku}")],
        [InlineKeyboardButton("🔗 Open Existing in Shopify",  url=open_url)],
        [InlineKeyboardButton("✅ Force Approve (Ignore Warning)", callback_data=f"dup_force:{sku}")],
    ])


async def _show_filter_panel(query, user_id: int) -> None:
    fs = _filter_sessions.get(user_id, _default_filter_state())
    _filter_sessions[user_id] = fs
    counts = await asyncio.get_event_loop().run_in_executor(None, load_pending_filter_counts)
    await query.edit_message_text(
        _make_filter_panel_text(fs),
        parse_mode="Markdown",
        reply_markup=_make_filter_panel_keyboard(fs, counts),
    )


async def _get_rmb_to_usd() -> float:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://api.exchangerate-api.com/v4/latest/CNY") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["rates"]["USD"])
    except Exception:
        pass
    return 0.14


# ── Product card ──────────────────────────────────────────────────────────

async def _send_product(bot, chat_id: int, session: dict) -> None:
    rows  = session["rows"]
    index = session["index"]

    if not rows or index >= len(rows):
        await bot.send_message(
            chat_id=chat_id,
            text="📭 <b>No more pending products.</b>\n\nTap 📋 Review Pending to reload.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    row   = rows[index]
    sku   = row.get("SKU", "—")
    total = len(rows)

    keyword      = _esc(row.get("KEYWORD", "") or "—")
    url_product  = _esc(row.get("URL PRODUCT", "") or "—")
    url_ad       = _esc(row.get("ADS LIBRARY MEDIA URL", "") or "—")
    sourcing_usd = row.get("SOURCING PRICE USD", "") or ""
    sourcing_url = _esc(row.get("SOURCING URL", "") or "")
    weight_gram  = row.get("WEIGHT GRAM", "") or ""
    has_variants = row.get("HAS VARIANTS", "") or "—"
    cur_price    = str(row.get("PRICE", "") or "").strip()
    cur_compare  = str(row.get("COMPARE AT PRICE", "") or "").strip()

    price_display    = f"${_esc(sourcing_usd)} USD" if sourcing_usd else "—"
    supplier_display = sourcing_url if sourcing_url else "—"
    weight_display   = f"{_esc(str(weight_gram))} g" if weight_gram else "—"

    # Suggested selling price (recalculates live from current SC price + weight)
    suggested_lines = ""
    try:
        if sourcing_usd and weight_gram:
            src = float(sourcing_usd)
            wt  = int(weight_gram)
            usd_sub, gnf = _calc_suggested_price(src, wt)
            ship = (wt / 1000) * 16
            suggested_lines = (
                "\n\n💡 <b>Suggested Selling Price:</b>\n"
                f"  ${src:.2f} (sourcing)\n"
                f"  + $8.50 (shipping agent)\n"
                f"  + $7.50 (marketing)\n"
                f"  + $10.00 (profit)\n"
                f"  + ${ship:.2f} (China shipping {wt}g)\n"
                f"  = <b>${usd_sub:.2f} USD ≈ {gnf:,} GNF</b>"
            )
        elif sourcing_usd:
            src = float(sourcing_usd)
            usd_sub, gnf = _calc_suggested_price(src, 0)
            suggested_lines = (
                "\n\n💡 <b>Suggested Selling Price</b> <i>(no weight — shipping = $0)</i>:\n"
                f"  ${src:.2f} + $8.50 + $7.50 + $10.00"
                f" = <b>${usd_sub:.2f} USD ≈ {gnf:,} GNF</b>"
            )
    except (ValueError, TypeError):
        pass

    price_section = ""
    if cur_price or cur_compare:
        price_section = (
            f"\n\n💵 <b>Selling Price:</b>   {_esc(cur_price) if cur_price else '—'} GNF\n"
            f"🏷 <b>Compare At Price:</b> {_esc(cur_compare) if cur_compare else '—'} GNF"
        )

    text = (
        f"<b>Product {index + 1} / {total}</b>  •  <code>{_esc(sku)}</code>\n\n"
        f"🔑 <b>Keyword:</b>\n{keyword}\n\n"
        f"🛍 <b>Product Page:</b>\n{url_product}\n\n"
        f"📢 <b>Ads Creative:</b>\n{url_ad}\n\n"
        f"💲 <b>Sourcing Price:</b> {price_display}\n"
        f"🔗 <b>Supplier Link:</b> {supplier_display}\n"
        f"⚖️ <b>Weight:</b> {weight_display}\n"
        f"🎨 <b>Has Variants:</b> {has_variants}"
        f"{suggested_lines}"
        f"{price_section}"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_make_product_keyboard(sku, bool(cur_price), bool(cur_compare)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # ── Duplicate detection ────────────────────────────────────────────────
    if shopify_cache.is_loaded() and shopify_cache.cache_size() > 0:
        try:
            dup = None
            sheet_image_url = str(row.get("IMAGE URL", "") or "").strip()
            if sheet_image_url:
                dup = await shopify_cache.check_duplicate_by_image(sheet_image_url)

            if dup is None:
                raw_img    = row.get("ADS LIBRARY MEDIA URL", "") or ""
                is_direct  = any(
                    raw_img.lower().endswith(ext)
                    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
                )
                dup = await shopify_cache.check_duplicate(
                    title=row.get("KEYWORD", "") or "",
                    description="",
                    image_urls=[raw_img] if is_direct else [],
                    source_url=row.get("URL PRODUCT", "") or "",
                )

            if dup.is_duplicate or dup.is_possible:
                matched = dup.matched_product
                level   = "🔴 DUPLICATE DETECTED" if dup.is_duplicate else "🟡 POSSIBLE DUPLICATE"
                reasons = "\n".join(f"  • {r}" for r in dup.reasons) if dup.reasons else "  • Similarity score match"
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ <b>{level}</b>\n\n"
                        f"Matched: <b>{_esc(matched.title)}</b>\n"
                        f"Score: {dup.score}/100\n\n"
                        f"<b>Signals:</b>\n{reasons}\n\n"
                        f"Choose an action:"
                    ),
                    parse_mode="HTML",
                    reply_markup=_make_duplicate_keyboard(sku, matched),
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.debug(f"[approval_bot] Duplicate check error for {sku}: {e}")


# ── Post-Shopify control panel ────────────────────────────────────────────

async def _send_control_panel(bot, chat_id: int, user_id: int) -> None:
    pending = _shopify_pending.get(user_id)
    if not pending:
        return

    old_msg_id = pending.get("panel_msg_id")
    if old_msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=old_msg_id, reply_markup=None
            )
        except Exception:
            pass

    images = pending.get("images", [])
    img_count = len(images)
    img_line = f"🖼 <b>Images:</b> {img_count}" if img_count > 0 else "🖼 <b>Images:</b> None — tap below to add manually"
    msg = await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 <b>Shopify product created!</b>\n\n"
            f"📝 <b>Title:</b> {_esc(pending['title'])}\n\n"
            f"🌐 <b>Storefront link:</b>\n{_esc(pending['store_url'])}\n\n"
            f"{img_line}"
        ),
        reply_markup=_make_control_panel_keyboard(image_count=img_count),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    pending["panel_msg_id"] = msg.message_id


# ── Shopify pipeline runner ────────────────────────────────────────────────

async def _run_shopify_pipeline(
    bot,
    chat_id: int,
    user_id: int,
    sku: str,
    price: str,
    compare_at_price: str,
    url_product: str,
) -> None:
    status_msg = await bot.send_message(
        chat_id=chat_id,
        text="⏳ Starting Shopify product pipeline…",
        parse_mode="HTML",
    )

    async def send_status(text: str):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            pass

    result = await run_pipeline(
        send_status=send_status,
        url_product=url_product,
        price=price,
        compare_at_price=compare_at_price,
        sku=sku,
    )

    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
    except Exception:
        pass

    if not result.get("ok"):
        error_msg = result.get("error", "Unknown error")
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_pending_fields(sku, LAST_ERROR=error_msg)
        )
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Shopify pipeline failed</b>\n\n"
                f"{_esc(error_msg)}\n\n"
                f"Product kept in PENDING. Tap ⏭ Skip on the product card to continue."
            ),
            parse_mode="HTML",
        )
        return

    _shopify_pending[user_id] = {
        "product_id":   result["product_id"],
        "sku":          sku,
        "title":        result["title"],
        "description":  result.get("description", ""),
        "store_url":    result["store_url"],
        "admin_url":    result["admin_url"],
        "images":       result.get("images", []),
        "panel_msg_id": None,
    }
    await _send_control_panel(bot, chat_id, user_id)


# ── Session helpers ────────────────────────────────────────────────────────

async def _start_review_with_rows(bot, chat_id: int, user_id: int, rows: list[dict]) -> None:
    if not rows:
        await bot.send_message(
            chat_id=chat_id,
            text="📭 <b>No products match that filter.</b>\n\nTry a different filter or show all.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return
    _sessions[user_id] = {"rows": rows, "index": 0, "awaiting": None}
    logger.info(f"[approval_bot] User {user_id} started review — {len(rows)} product(s)")
    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ <b>Loaded {len(rows)} product(s).</b> Starting review…",
        parse_mode="HTML",
    )
    await _send_product(bot, chat_id, _sessions[user_id])


def _advance_session(session: dict) -> None:
    session["index"] += 1
    rows = session["rows"]
    if session["index"] >= len(rows):
        session["index"] = len(rows)


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Approval Bot*\n\n"
        "Review PENDING products one by one.\n"
        "Set pricing, create a Shopify product, then approve or disapprove.\n\n"
        "Tap *📋 Review Pending* to begin.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _filter_sessions[user_id] = _default_filter_state()
    fs = _filter_sessions[user_id]
    counts = await asyncio.get_event_loop().run_in_executor(None, load_pending_filter_counts)
    await update.message.reply_text(
        _make_filter_panel_text(fs),
        parse_mode="Markdown",
        reply_markup=_make_filter_panel_keyboard(fs, counts),
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
        f"⏳ Pending:      *{stats.get('PENDING', 0)}*",
        f"✅ Approved:     *{stats.get('APPROVED', 0)}*",
        f"❌ Disapproved:  *{stats.get('DISAPROVED', 0)}*",
        f"\n📈 Approval Rate: *{stats.get('approval_rate', 'N/A')}*",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_MENU
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in _sessions:
        _sessions.pop(user_id)
        await update.message.reply_text("🛑 Review session stopped.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("No active review session.", reply_markup=MAIN_MENU)


# ── Text input handler ────────────────────────────────────────────────────

async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos sent by user for manual product image upload."""
    user_id = update.effective_user.id
    if user_id not in _awaiting_photos:
        return  # Not in photo collection mode — ignore

    photos = _awaiting_photos[user_id]
    if len(photos) >= 5:
        await update.message.reply_text("Maximum 5 photos reached. Tap ✅ Done to upload.")
        return

    # Take the highest-resolution version of the photo
    photo = update.message.photo[-1]
    photos.append(photo.file_id)
    n = len(photos)

    await update.message.reply_text(
        f"✅ Photo {n}/5 received.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Done ({n} photo{'s' if n > 1 else ''})",
                callback_data="cp_photos_done"
            )
        ]]),
    )


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = (update.message.text or "").strip()

    if text == _BTN_REVIEW: await cmd_review(update, context); return
    if text == _BTN_STATS:  await cmd_stats(update, context);  return
    if text == _BTN_STOP:   await cmd_stop(update, context);   return

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

    # ── Edit sourcing price (RMB → USD) ───────────────────────────────────
    if awaiting == "edit_sourcing_price":
        session["awaiting"] = None
        try:
            rmb = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid amount. Enter a number in RMB (e.g. 25):"
            )
            session["awaiting"] = "edit_sourcing_price"
            return

        rate    = await _get_rmb_to_usd()
        usd     = round(rmb * rate, 2)
        usd_str = f"{usd:.2f}"

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_sourcing_data(sku, price_usd=usd_str)
        )
        if ok:
            row["SOURCING PRICE USD"] = usd_str
            await update.message.reply_text(
                f"✅ Sourcing price updated: ¥{rmb} RMB → ${usd_str} USD\n\nShowing updated card…"
            )
        else:
            await update.message.reply_text("⚠️ Failed to save sourcing price. Continuing…")
        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Edit weight ────────────────────────────────────────────────────────
    if awaiting == "edit_weight":
        session["awaiting"] = None
        try:
            grams = int(round(float(text.replace(",", "."))))
            if grams <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid. Enter whole grams (e.g. 350):"
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
                f"✅ Weight updated: {gram_str} g\n\nShowing updated card…"
            )
        else:
            await update.message.reply_text("⚠️ Failed to save weight. Continuing…")
        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Enter selling price (GNF, saved immediately) ──────────────────────
    if awaiting == "edit_selling_price":
        session["awaiting"] = None
        val = text.strip()
        if not val:
            await update.message.reply_text(
                "⚠️ Enter a price value in GNF (e.g. 99000):"
            )
            session["awaiting"] = "edit_selling_price"
            return

        try:
            selling_int = int(float(val))
        except (ValueError, TypeError):
            await update.message.reply_text("⚠️ Invalid price. Enter a number in GNF (e.g. 280000):")
            session["awaiting"] = "edit_selling_price"
            return

        compare_val = str(selling_int + 200_000)

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_pending_fields(sku, PRICE=str(selling_int), **{"COMPARE AT PRICE": compare_val})
        )
        if ok:
            row["PRICE"] = str(selling_int)
            row["COMPARE AT PRICE"] = compare_val
            await update.message.reply_text(
                f"✅ Selling price: <b>{selling_int:,} GNF</b>\n"
                f"✅ Compare at price: <b>{int(compare_val):,} GNF</b> (auto-set)\n\n"
                f"Showing updated card…",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Failed to save selling price. Continuing…")
        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Enter compare at price (GNF, saved immediately) ───────────────────
    if awaiting == "edit_compare_price":
        session["awaiting"] = None
        val = text.strip()
        if not val:
            await update.message.reply_text(
                "⚠️ Enter a compare-at price in GNF (e.g. 130000):"
            )
            session["awaiting"] = "edit_compare_price"
            return

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_pending_fields(sku, **{"COMPARE AT PRICE": val})
        )
        if ok:
            row["COMPARE AT PRICE"] = val
            await update.message.reply_text(
                f"✅ Compare at price saved: <b>{_esc(val)} GNF</b>\n\nShowing updated card…",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Failed to save compare at price. Continuing…")
        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Edit sourcing URL ──────────────────────────────────────────────────
    if awaiting == "edit_sourcing_url":
        session["awaiting"] = None
        url = text.strip()
        if not url.startswith("http"):
            await update.message.reply_text(
                "⚠️ Must start with http:// or https://. Paste the URL again:"
            )
            session["awaiting"] = "edit_sourcing_url"
            return

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_sourcing_data(sku, sourcing_url=url)
        )
        if ok:
            row["SOURCING URL"] = url
            await update.message.reply_text(
                f"✅ Sourcing URL saved:\n<code>{_esc(url)}</code>\n\nShowing updated card…",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Failed to save sourcing URL. Continuing…")
        await _send_product(context.bot, update.effective_chat.id, session)
        return

    # ── Shopify: new title ─────────────────────────────────────────────────
    if awaiting == "new_title":
        session["awaiting"] = None
        pending = _shopify_pending.get(user_id)
        if not pending:
            await update.message.reply_text("⚠️ No active Shopify product.")
            return
        from shopify_client import update_product_field
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_product_field(pending["product_id"], title=text)
        )
        if ok:
            pending["title"] = text
            await update.message.reply_text(
                f"✅ Title updated:\n<b>{_esc(text)}</b>", parse_mode="HTML"
            )
        else:
            await update.message.reply_text("⚠️ Failed to update title on Shopify.")
        await _send_control_panel(context.bot, update.effective_chat.id, user_id)
        return

    # ── Shopify: new description ───────────────────────────────────────────
    if awaiting == "new_description":
        session["awaiting"] = None
        pending = _shopify_pending.get(user_id)
        if not pending:
            await update.message.reply_text("⚠️ No active Shopify product.")
            return
        from shopify_client import update_product_field
        body_html = text.replace("\n", "<br>")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_product_field(pending["product_id"], body_html=body_html)
        )
        if ok:
            pending["description"] = text
            await update.message.reply_text("✅ Description updated on Shopify.")
        else:
            await update.message.reply_text("⚠️ Failed to update description on Shopify.")
        await _send_control_panel(context.bot, update.effective_chat.id, user_id)
        return


# ── Callback handler ──────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data    = query.data or ""
    chat_id = query.message.chat_id
    session = _sessions.get(user_id)
    pending = _shopify_pending.get(user_id)

    # ── Stop review ────────────────────────────────────────────────────────
    if data == "stop_review":
        _sessions.pop(user_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("🛑 Review session stopped.", reply_markup=MAIN_MENU)
        return

    # ── Filter: cycle price ────────────────────────────────────────────────
    if data == "fp:cycle_price":
        fs  = _filter_sessions.setdefault(user_id, _default_filter_state())
        cur = _PRICE_RANGE_CYCLE.index(fs.get("price_range", "")) if fs.get("price_range", "") in _PRICE_RANGE_CYCLE else 0
        fs["price_range"] = _PRICE_RANGE_CYCLE[(cur + 1) % len(_PRICE_RANGE_CYCLE)]
        await _show_filter_panel(query, user_id)
        return

    # ── Filter: cycle variants ─────────────────────────────────────────────
    if data == "fp:cycle_variants":
        fs  = _filter_sessions.setdefault(user_id, _default_filter_state())
        cur = _VARIANTS_CYCLE.index(fs.get("variants", "")) if fs.get("variants", "") in _VARIANTS_CYCLE else 0
        fs["variants"] = _VARIANTS_CYCLE[(cur + 1) % len(_VARIANTS_CYCLE)]
        await _show_filter_panel(query, user_id)
        return

    # ── Filter: keyword submenu ────────────────────────────────────────────
    if data == "fp:pick_kw":
        kws = await asyncio.get_event_loop().run_in_executor(None, load_pending_keywords)
        if not kws:
            await query.answer("No keywords found in PENDING yet.", show_alert=True)
            return
        await query.edit_message_text(
            "🔑 *Choose a keyword:*",
            parse_mode="Markdown",
            reply_markup=_make_kw_submenu_keyboard(kws),
        )
        return

    # ── Filter: back from submenu ──────────────────────────────────────────
    if data == "fp:back":
        await _show_filter_panel(query, user_id)
        return

    # ── Filter: keyword selected ───────────────────────────────────────────
    if data.startswith("fkw_panel:"):
        kw = data[len("fkw_panel:"):]
        fs = _filter_sessions.setdefault(user_id, _default_filter_state())
        fs["keyword"] = kw
        await _show_filter_panel(query, user_id)
        return

    # ── Filter: apply ──────────────────────────────────────────────────────
    if data == "fp:apply":
        fs = _filter_sessions.get(user_id, _default_filter_state())
        await query.edit_message_text("⏳ Loading products…")
        rows = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: load_pending_rows(
                price_range=fs.get("price_range", ""),
                keyword=fs.get("keyword", ""),
                variants=fs.get("variants", ""),
            ),
        )
        await _start_review_with_rows(context.bot, chat_id, user_id, rows)
        return

    # ── Duplicate: skip (disapprove as duplicate) ─────────────────────────
    if data.startswith("dup_skip:"):
        sku = data[len("dup_skip:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        rows  = session["rows"]
        pos   = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if pos is None:
            await query.answer("Product already processed.", show_alert=True)
            return
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: move_row(sku, "DISAPROVED", "DISAPROVED")
        )
        rows.pop(pos)
        session["index"] = pos if pos < len(rows) else max(0, len(rows) - 1)
        try:
            await query.edit_message_text(
                f"⏭ <b>{_esc(sku)}</b> skipped — marked as Duplicate."
                + ("" if ok else "\n⚠️ Sheet update failed — check manually."),
                parse_mode="HTML",
            )
        except Exception:
            pass
        if not rows:
            await query.message.reply_text(
                "🎉 <b>All products reviewed!</b>", parse_mode="HTML", reply_markup=MAIN_MENU
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, chat_id, session)
        return

    # ── Duplicate: force (dismiss warning) ────────────────────────────────
    if data.startswith("dup_force:"):
        try:
            await query.edit_message_text(
                "✅ Duplicate warning dismissed. Use the product card above to continue.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── Edit SC price ──────────────────────────────────────────────────────
    if data.startswith("edit_price:"):
        sku = data[len("edit_price:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "edit_sourcing_price"
        await query.message.reply_text(
            f"✏️ <b>Edit Sourcing Price</b> for <code>{_esc(sku)}</code>\n\n"
            "Enter the supplier price in <b>RMB (¥)</b>:\n"
            "<i>(e.g. 25 or 12.5 — auto-converted to USD)</i>",
            parse_mode="HTML",
        )
        return

    # ── Edit weight ────────────────────────────────────────────────────────
    if data.startswith("edit_weight:"):
        sku = data[len("edit_weight:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "edit_weight"
        await query.message.reply_text(
            f"✏️ <b>Edit Weight</b> for <code>{_esc(sku)}</code>\n\n"
            "Enter the product weight in <b>grams</b>:\n<i>(e.g. 350)</i>",
            parse_mode="HTML",
        )
        return

    # ── Enter selling price ────────────────────────────────────────────────
    if data.startswith("edit_selling:"):
        sku = data[len("edit_selling:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "edit_selling_price"
        row = session["rows"][session["index"]] if session["rows"] else {}
        cur = str(row.get("PRICE", "") or "").strip()
        note = f"\nCurrent: <b>{_esc(cur)} GNF</b>" if cur else ""
        await query.message.reply_text(
            f"💵 <b>Enter Selling Price</b> for <code>{_esc(sku)}</code>{note}\n\n"
            "Enter the selling price in <b>GNF</b>:\n<i>(e.g. 99000)</i>",
            parse_mode="HTML",
        )
        return

    # ── Enter compare at price ─────────────────────────────────────────────
    if data.startswith("edit_compare:"):
        sku = data[len("edit_compare:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "edit_compare_price"
        row = session["rows"][session["index"]] if session["rows"] else {}
        cur = str(row.get("COMPARE AT PRICE", "") or "").strip()
        note = f"\nCurrent: <b>{_esc(cur)} GNF</b>" if cur else ""
        await query.message.reply_text(
            f"🏷 <b>Enter Compare At Price</b> for <code>{_esc(sku)}</code>{note}\n\n"
            "Enter the compare-at price in <b>GNF</b>:\n<i>(e.g. 130000)</i>",
            parse_mode="HTML",
        )
        return

    # ── Edit sourcing URL ──────────────────────────────────────────────────
    if data.startswith("edit_url:"):
        sku = data[len("edit_url:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "edit_sourcing_url"
        row = session["rows"][session["index"]] if session["rows"] else {}
        cur = str(row.get("SOURCING URL", "") or "").strip()
        note = f"\nCurrent: <code>{_esc(cur)}</code>" if cur else ""
        await query.message.reply_text(
            f"🔗 <b>Edit Sourcing URL</b> for <code>{_esc(sku)}</code>{note}\n\n"
            "Paste the new supplier URL:",
            parse_mode="HTML",
        )
        return

    # ── Preapprove: blocked (missing prices) ──────────────────────────────
    if data.startswith("preapprove_blocked:"):
        await query.answer(
            "⚠️ Enter SELLING PRICE and COMPARE AT PRICE before preapproving.",
            show_alert=True,
        )
        return

    # ── Preapprove: run pipeline ───────────────────────────────────────────
    if data.startswith("preapprove:"):
        sku = data[len("preapprove:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        rows  = session["rows"]
        index = session["index"]
        if not rows or index >= len(rows):
            await query.answer("No active product.", show_alert=True)
            return
        row = rows[index]

        price   = str(row.get("PRICE", "") or "").strip()
        compare = str(row.get("COMPARE AT PRICE", "") or "").strip()

        if not price or not compare:
            await query.answer(
                "⚠️ Set SELLING PRICE and COMPARE AT PRICE first.", show_alert=True
            )
            return

        url_product = str(row.get("URL PRODUCT", "") or "").strip()
        if not url_product:
            await query.answer("⚠️ No product URL found for this SKU.", show_alert=True)
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        asyncio.create_task(
            _run_shopify_pipeline(
                bot=context.bot,
                chat_id=chat_id,
                user_id=user_id,
                sku=sku,
                price=price,
                compare_at_price=compare,
                url_product=url_product,
            )
        )
        return

    # ── Skip product (keep in PENDING) ────────────────────────────────────
    if data.startswith("skip:"):
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        session["index"] += 1
        if session["index"] >= len(session["rows"]):
            await query.message.reply_text(
                "📭 <b>No more products in this session.</b>\n\nTap 📋 Review Pending to reload.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, chat_id, session)
        return

    # ── Disapprove ────────────────────────────────────────────────────────
    if data.startswith("disapprove:"):
        sku = data[len("disapprove:"):]
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        rows  = session["rows"]
        pos   = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if pos is None:
            await query.answer("Product already processed.", show_alert=True)
            return

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: move_row(sku, "DISAPROVED", "DISAPROVED")
        )
        rows.pop(pos)
        remaining = len(rows)
        session["index"] = pos if pos < remaining else max(0, remaining - 1)

        try:
            await query.edit_message_text(
                f"❌ <b>{_esc(sku)}</b> → DISAPPROVED\n"
                f"<i>{remaining} product(s) remaining.</i>"
                + ("" if ok else "\n⚠️ Sheet update failed — check manually."),
                parse_mode="HTML",
            )
        except Exception:
            pass

        if remaining == 0:
            await query.message.reply_text(
                "🎉 <b>All products reviewed!</b>",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, chat_id, session)
        return

    # ── Control panel: Approve Product ────────────────────────────────────
    if data == "cp_approve":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if not pending:
            await query.message.reply_text("⚠️ No active Shopify product to approve.")
            return

        sku       = pending["sku"]
        title     = pending["title"]
        store_url = pending["store_url"]

        # Save product name + landing page URL to PENDING before the row moves
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: update_pending_fields(
                sku, **{"PRODUCT NAME": title, "URL LANDING PAGE": store_url}
            ),
        )

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: move_row(sku, "APPROVED", "APPROVED")
        )

        # Remove from in-memory session queue
        if session:
            rows = session["rows"]
            pos  = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
            if pos is not None:
                rows.pop(pos)
                session["index"] = pos if pos < len(rows) else max(0, len(rows) - 1)

        _shopify_pending.pop(user_id, None)

        await query.message.reply_text(
            f"✅ <b>Product approved!</b>\n\n"
            f"📝 <b>{_esc(title)}</b>\n"
            f"🌐 {_esc(store_url)}\n\n"
            + ("📊 Moved to APPROVED tab." if ok else "⚠️ Sheet move failed — check manually."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        if session and session["rows"] and session["index"] < len(session["rows"]):
            await _send_product(context.bot, chat_id, session)
        elif session:
            await query.message.reply_text(
                "🎉 <b>All products reviewed!</b>",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        return

    # ── Control panel: Skip (keep in PENDING, continue) ───────────────────
    if data == "cp_skip":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _shopify_pending.pop(user_id, None)
        if session:
            session["index"] += 1
            if session["index"] < len(session["rows"]):
                await query.message.reply_text(
                    "⏭ Product kept in PENDING. Loading next…", parse_mode="HTML"
                )
                await _send_product(context.bot, chat_id, session)
            else:
                await query.message.reply_text(
                    "📭 <b>No more products in this session.</b>\n\nTap 📋 Review Pending to reload.",
                    parse_mode="HTML",
                    reply_markup=MAIN_MENU,
                )
                _sessions.pop(user_id, None)
        else:
            await query.message.reply_text(
                "⏭ Product kept in PENDING.", parse_mode="HTML", reply_markup=MAIN_MENU
            )
        return

    # ── Control panel: Regenerate → show regen submenu ────────────────────
    if data == "cp_regen":
        if not pending:
            await query.answer("No active Shopify product.", show_alert=True)
            return
        try:
            await query.edit_message_reply_markup(
                reply_markup=_make_regen_keyboard(pending.get("images", []))
            )
        except Exception:
            pass
        return

    # ── Regen: back to control panel ──────────────────────────────────────
    if data == "cp_back":
        if not pending:
            await query.answer("No active Shopify product.", show_alert=True)
            return
        try:
            await query.edit_message_reply_markup(
                reply_markup=_make_control_panel_keyboard(image_count=len(pending.get("images", [])))
            )
        except Exception:
            pass
        return

    # ── Add images manually ────────────────────────────────────────────────
    if data == "cp_add_photos":
        if not pending:
            await query.answer("No active Shopify product.", show_alert=True)
            return
        _awaiting_photos[user_id] = []
        await query.answer()
        await query.message.reply_text(
            "📷 Send me your photos one by one (up to 5).\n"
            "Each photo you send will be added to the product.\n"
            "Tap <b>✅ Done</b> when finished.",
            parse_mode="HTML",
        )
        return

    # ── Done collecting photos ─────────────────────────────────────────────
    if data == "cp_photos_done":
        pending = _shopify_pending.get(user_id)
        file_ids = _awaiting_photos.pop(user_id, [])
        if not pending or not file_ids:
            await query.answer("No photos to upload.", show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(f"⬆️ Uploading {len(file_ids)} photo(s) to Shopify…")
        product_id = pending["product_id"]
        uploaded = []
        for file_id in file_ids:
            try:
                tg_file = await context.bot.get_file(file_id)
                img_bytes = bytes(await tg_file.download_as_bytearray())
                img_dict = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda b=img_bytes: shopify_client.upload_image_bytes_to_product(product_id, b)
                )
                uploaded.append(img_dict)
            except Exception as e:
                logger.error(f"[approval] Failed to upload manual photo: {e}")
        pending["images"].extend(uploaded)
        img_count = len(pending["images"])
        await query.message.reply_text(
            f"✅ {len(uploaded)} image(s) added! Product now has {img_count} image(s)."
        )
        await _send_control_panel(context.bot, query.message.chat_id, user_id)
        return

    # ── Regen: change title ────────────────────────────────────────────────
    if data == "regen_title":
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "new_title"
        await query.message.reply_text("✏️ Enter the new product title:")
        return

    # ── Regen: change description ─────────────────────────────────────────
    if data == "regen_desc":
        if not session:
            await query.answer("No active session.", show_alert=True)
            return
        session["awaiting"] = "new_description"
        await query.message.reply_text("📝 Enter the new product description:")
        return

    # ── Regen: delete image ────────────────────────────────────────────────
    if data.startswith("regen_del_img:"):
        if not pending:
            await query.answer("No active Shopify product.", show_alert=True)
            return
        try:
            image_id = int(data[len("regen_del_img:"):])
        except ValueError:
            await query.answer("Invalid image ID.", show_alert=True)
            return
        from shopify_client import delete_product_image
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: delete_product_image(pending["product_id"], image_id)
        )
        if ok:
            pending["images"] = [img for img in pending["images"] if img["id"] != image_id]
            await query.answer("🗑 Image deleted.", show_alert=False)
        else:
            await query.answer("⚠️ Failed to delete image.", show_alert=True)
        await _send_control_panel(context.bot, chat_id, user_id)
        return


# ── Application builder ───────────────────────────────────────────────────

def build_approval_application() -> Application:
    token = os.environ.get("TELEGRAM_APPROVAL_BOT_TOKEN", "")
    if not token:
        raise ValueError(
            "TELEGRAM_APPROVAL_BOT_TOKEN is not set. "
            "Add the bot token as a secret."
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    return app
