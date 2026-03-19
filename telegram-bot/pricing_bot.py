"""
pricing_bot.py - Pricing bot handlers.

Commands:
  /start  — show main menu
  /price  — start pricing session
  /stats  — show pricing statistics
  /stop   — stop current pricing session
  /help   — usage guide

Flow after pricing is saved:
  1. Shopify pipeline runs automatically (scrape → AI → create & publish)
  2. Storefront link is sent
  3. Control panel is shown: APPROVE / CHANGE TITLE / CHANGE DESCRIPTION /
     DELETE IMAGE X / DELETE PRODUCT / NEXT PRODUCT
  4. NEXT PRODUCT must be pressed explicitly — bot never auto-advances
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

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

from pricing_sheet   import load_unpriced_rows, update_pricing, get_pricing_stats, publish_approved_row
from shopify_pipeline import run_pipeline
from config import USD_TO_GNF, ROUND_TO_GNF

logger = logging.getLogger(__name__)

# ── Persistent main menu ───────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["💰 Price Products", "📊 Statistics"],
        ["🛑 Stop",           "📖 Help"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_BTN_PRICE = "💰 Price Products"
_BTN_STATS = "📊 Statistics"
_BTN_STOP  = "🛑 Stop"
_BTN_HELP  = "📖 Help"

# ── Session state ──────────────────────────────────────────────────────────
# user_id → {
#   "rows":        list[dict],
#   "index":       int,
#   "awaiting":    None | "price" | "compare_at_price" | "new_title" | "new_description"
#   "current_sku": str,
#   "current_url": str,
#   "temp_price":  str,
# }
_sessions: dict[int, dict] = {}

# Shopify pending control panel: user_id → {
#   "product_id":   int,
#   "sku":          str,
#   "title":        str,
#   "description":  str,
#   "store_url":    str,
#   "admin_url":    str,
#   "images":       [ {"id": int, "src": str, "position": int} ],
#   "panel_msg_id": int | None,
# }
_shopify_pending: dict[int, dict] = {}


# ── Register Telegram command menu ─────────────────────────────────────────
async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Start the bot / show menu"),
            BotCommand("price", "Start pricing approved products"),
            BotCommand("stats", "View pricing statistics"),
            BotCommand("stop",  "Stop current pricing session"),
            BotCommand("help",  "How to use this bot"),
        ])
        logger.info("[pricing_bot] Telegram command menu registered")
    except Exception as e:
        logger.warning(f"[pricing_bot] Could not register command menu (non-fatal): {e}")


# ── Helpers ────────────────────────────────────────────────────────────────

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
        usd = sourcing_usd + 8.50 (shipping agent) + 7.50 (marketing) + 10.00 (profit)
              + (weight_gram / 1000) × 16  (China shipping cost per kg)
        gnf = usd × USD_TO_GNF, rounded to nearest ROUND_TO_GNF
    """
    shipping = (weight_gram / 1000) * 16
    usd = sourcing_usd + 8.5 + 7.5 + 10.0 + shipping
    gnf = round(usd * USD_TO_GNF / ROUND_TO_GNF) * ROUND_TO_GNF
    return (usd, gnf)


def _product_keyboard(sku: str, suggested_gnf: int = 0) -> InlineKeyboardMarkup:
    rows: list[list] = []
    if suggested_gnf:
        rows.append([
            InlineKeyboardButton(
                f"✅ Use Suggested: {suggested_gnf:,} GNF",
                callback_data=f"usesuggested:{sku}",
            )
        ])
    rows.append([
        InlineKeyboardButton("✏️ Enter Price Manually", callback_data=f"addprice:{sku}"),
        InlineKeyboardButton("⏭ SKIP",                  callback_data=f"skip:{sku}"),
    ])
    rows.append([InlineKeyboardButton("🛑 STOP", callback_data="stop_pricing")])
    return InlineKeyboardMarkup(rows)


def _control_panel_keyboard(images: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ APPROVE PRODUCT", callback_data="cp_approve")],
        [
            InlineKeyboardButton("✏️ CHANGE TITLE",       callback_data="cp_title"),
            InlineKeyboardButton("📝 CHANGE DESCRIPTION", callback_data="cp_desc"),
        ],
    ]
    img_buttons = [
        InlineKeyboardButton(f"🗑 DELETE IMAGE {i + 1}", callback_data=f"cp_del_img:{img['id']}")
        for i, img in enumerate(images[:5])
    ]
    for i in range(0, len(img_buttons), 2):
        rows.append(img_buttons[i:i + 2])

    rows.append([
        InlineKeyboardButton("❌ DELETE PRODUCT", callback_data="cp_del_product"),
        InlineKeyboardButton("⏭ NEXT PRODUCT",   callback_data="cp_next"),
    ])
    return InlineKeyboardMarkup(rows)


async def _send_control_panel(bot, chat_id: int, user_id: int) -> None:
    """Send (or re-send) the control panel for the pending Shopify product."""
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
    text = (
        f"🚀 <b>Product published!</b>\n\n"
        f"📝 <b>Title:</b> {_esc(pending['title'])}\n\n"
        f"🌐 <b>Storefront:</b>\n{_esc(pending['store_url'])}\n\n"
        f"🖼 <b>Images:</b> {len(images)}\n\n"
        f"Use the buttons below to review and edit the product."
    )
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_control_panel_keyboard(images),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    pending["panel_msg_id"] = msg.message_id


async def _send_product(bot, chat_id: int, session: dict) -> None:
    rows  = session["rows"]
    index = session["index"]

    if not rows or index >= len(rows):
        await bot.send_message(
            chat_id=chat_id,
            text="📭 <b>No more unpriced products.</b>\n\nTap 💰 Price Products to reload.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    row   = rows[index]
    sku   = row.get("SKU", "—")
    url   = _esc(row.get("URL PRODUCT", "") or "—")
    price = _esc(row.get("PRICE", "") or "—")
    cmp   = _esc(row.get("COMPARE AT PRICE", "") or "—")
    total = len(rows)

    sourcing_price_raw = str(row.get("SOURCING PRICE USD", "") or "").strip()
    weight_gram_raw    = str(row.get("WEIGHT GRAM", "") or "").strip()

    sourcing_display = f"${_esc(sourcing_price_raw)} USD" if sourcing_price_raw else "—"
    weight_display   = f"{_esc(weight_gram_raw)} g"       if weight_gram_raw    else "—"

    # Calculate suggested price when both sourcing price and weight are available
    suggested_gnf = 0
    suggested_lines = ""
    try:
        if sourcing_price_raw and weight_gram_raw:
            src_usd  = float(sourcing_price_raw)
            wt_gram  = int(weight_gram_raw)
            usd_sub, suggested_gnf = _calc_suggested_price(src_usd, wt_gram)
            shipping_cost = (wt_gram / 1000) * 16
            suggested_lines = (
                "\n\n💡 <b>Suggested Selling Price:</b>\n"
                f"  ${src_usd:.2f} (sourcing)\n"
                f"  + $8.50 (shipping agent)\n"
                f"  + $7.50 (marketing)\n"
                f"  + $10.00 (profit)\n"
                f"  + ${shipping_cost:.2f} (China shipping {wt_gram}g)\n"
                f"  = <b>${usd_sub:.2f} USD ≈ {suggested_gnf:,} GNF</b>"
            )
        elif sourcing_price_raw:
            src_usd  = float(sourcing_price_raw)
            usd_sub, suggested_gnf = _calc_suggested_price(src_usd, 0)
            suggested_lines = (
                "\n\n💡 <b>Suggested Selling Price</b> <i>(no weight — shipping cost = $0)</i>:\n"
                f"  ${src_usd:.2f} + $8.50 + $7.50 + $10.00 = "
                f"<b>${usd_sub:.2f} USD ≈ {suggested_gnf:,} GNF</b>"
            )
    except (ValueError, TypeError):
        pass

    text = (
        f"<b>Product {index + 1} / {total}</b>\n\n"
        f"🛍 <b>Product page:</b>\n{url}\n\n"
        f"✅ <b>Status:</b> APPROVED\n\n"
        f"💲 <b>Sourcing Price:</b> {sourcing_display}\n"
        f"⚖️ <b>Weight:</b> {weight_display}"
        f"{suggested_lines}\n\n"
        f"💵 <b>Current Price:</b> {price}\n"
        f"🏷 <b>Compare at price:</b> {cmp}"
    )

    session["suggested_price"] = str(suggested_gnf) if suggested_gnf else ""

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_product_keyboard(sku, suggested_gnf),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Shopify pipeline (runs in background after pricing saved) ──────────────

async def _run_shopify_pipeline(
    bot,
    chat_id:          int,
    user_id:          int,
    sku:              str,
    price:            str,
    compare_at_price: str,
    url_product:      str,
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
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Shopify pipeline failed</b>\n\n"
                f"{_esc(result.get('error', 'Unknown error'))}\n\n"
                f"Press ⏭ NEXT PRODUCT in your product card to continue."
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


# ── Command handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Pricing Bot*\n\n"
        "I help you set prices for approved products one by one.\n\n"
        "Use the menu below or the commands list (☰) to navigate.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use the Pricing Bot*\n\n"
        "1. Tap *💰 Price Products* to start\n"
        "2. Products from APPROVED with no price are shown one by one\n"
        "3. Tap *💰 ADD PRICE* → enter PRICE → enter COMPARE AT PRICE\n"
        "4. Shopify product is created and published automatically\n"
        "5. You get a storefront link + control panel to review the product\n"
        "6. Use APPROVE / CHANGE TITLE / CHANGE DESCRIPTION / DELETE IMAGE / DELETE PRODUCT\n"
        "7. Tap *⏭ NEXT PRODUCT* to load the next product\n"
        "8. Tap *⏭ SKIP* to skip a product without pricing",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("⏳ Loading unpriced products from APPROVED…")

    rows = load_unpriced_rows()
    if not rows:
        await update.message.reply_text(
            "🎉 *All approved products are already priced!*\n\nNothing left to do right now.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    _sessions[user_id] = {
        "rows":            rows,
        "index":           0,
        "awaiting":        None,
        "current_sku":     "",
        "current_url":     "",
        "temp_price":      "",
        "suggested_price": "",
    }
    logger.info(f"[pricing_bot] User {user_id} started — {len(rows)} product(s)")

    await update.message.reply_text(
        f"✅ Loaded *{len(rows)}* unpriced product(s). Starting…",
        parse_mode="Markdown",
    )
    await _send_product(context.bot, update.effective_chat.id, _sessions[user_id])


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Loading statistics…")
    stats = get_pricing_stats()
    if not stats:
        await update.message.reply_text(
            "⚠️ Could not load statistics. Check the sheet connection.",
            reply_markup=MAIN_MENU,
        )
        return
    await update.message.reply_text(
        "📊 *Pricing Statistics*\n\n"
        f"✅ Approved total:   *{stats['approved_total']}*\n"
        f"💰 Priced:           *{stats['priced']}*\n"
        f"⏳ Not yet priced:   *{stats['unpriced']}*\n"
        f"\n📈 Completion:  *{stats['completion_pct']}*",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _sessions.pop(user_id, None)
    await update.message.reply_text("🛑 Pricing session stopped.", reply_markup=MAIN_MENU)


# ── Text input handler ─────────────────────────────────────────────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = _sessions.get(user_id)
    text    = (update.message.text or "").strip()

    # Main menu buttons
    if text == _BTN_PRICE: await cmd_price(update, context); return
    if text == _BTN_STATS: await cmd_stats(update, context); return
    if text == _BTN_STOP:  await cmd_stop(update, context);  return
    if text == _BTN_HELP:  await cmd_help(update, context);  return

    if not session or session.get("awaiting") is None:
        return

    awaiting = session["awaiting"]

    # ── Price entry ───────────────────────────────────────────────────────
    if awaiting == "price":
        if not text:
            await update.message.reply_text("Please enter a price value (e.g. 29.99).")
            return
        session["temp_price"] = text
        session["awaiting"]   = "compare_at_price"
        await update.message.reply_text(
            f"Price noted: *{text}*\n\nNow enter *COMPARE AT PRICE*:",
            parse_mode="Markdown",
        )
        return

    # ── Compare-at price entry ────────────────────────────────────────────
    if awaiting == "compare_at_price":
        if not text:
            await update.message.reply_text("Please enter a compare-at price value (e.g. 49.99).")
            return

        sku         = session["current_sku"]
        url_product = session["current_url"]
        price       = session["temp_price"]
        compare     = text

        session["awaiting"]   = None
        session["temp_price"] = ""

        ok = update_pricing(sku, price, compare)
        if ok:
            await update.message.reply_text(
                f"✅ *Pricing saved*\n\nSKU: *{sku}*\nPrice: *{price}*\nCompare at: *{compare}*\n\n"
                f"⏳ Shopify product creation starting in background…",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"⚠️ Could not save pricing for *{sku}*. Continuing anyway…",
                parse_mode="Markdown",
            )

        # Remove priced row from queue (do NOT advance to next product here)
        rows = session["rows"]
        pos  = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if pos is not None:
            rows.pop(pos)
            if session["index"] >= len(rows) and len(rows) > 0:
                session["index"] = len(rows) - 1

        # Launch Shopify pipeline in background
        if ok and url_product:
            asyncio.create_task(
                _run_shopify_pipeline(
                    bot=context.bot,
                    chat_id=update.effective_chat.id,
                    user_id=user_id,
                    sku=sku,
                    price=price,
                    compare_at_price=compare,
                    url_product=url_product,
                )
            )
        return

    # ── New title entry ───────────────────────────────────────────────────
    if awaiting == "new_title":
        session["awaiting"] = None
        pending = _shopify_pending.get(user_id)
        if not pending:
            await update.message.reply_text("⚠️ No active Shopify product found.")
            return

        from shopify_client import update_product_field
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: update_product_field(pending["product_id"], title=text)
        )
        if ok:
            pending["title"] = text
            await update.message.reply_text(f"✅ Title updated to:\n<b>{_esc(text)}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ Failed to update title on Shopify.")

        await _send_control_panel(context.bot, update.effective_chat.id, user_id)
        return

    # ── New description entry ─────────────────────────────────────────────
    if awaiting == "new_description":
        session["awaiting"] = None
        pending = _shopify_pending.get(user_id)
        if not pending:
            await update.message.reply_text("⚠️ No active Shopify product found.")
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


# ── Callback handler ───────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    user_id  = update.effective_user.id
    data     = query.data or ""
    chat_id  = query.message.chat_id
    session  = _sessions.get(user_id)
    pending  = _shopify_pending.get(user_id)

    # ── APPROVE PRODUCT ────────────────────────────────────────────────────
    if data == "cp_approve":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if not pending:
            await query.message.reply_text("⚠️ No active Shopify product to approve.")
            return

        sheet_ok = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: publish_approved_row(
                sku=pending["sku"],
                product_name=pending["title"],
                url_landing_page=pending["store_url"],
            )
        )
        _shopify_pending.pop(user_id, None)
        await query.message.reply_text(
            f"✅ <b>Product approved!</b>\n\n"
            f"📝 <b>Title:</b> {_esc(pending['title'])}\n"
            f"🌐 <b>URL:</b> {_esc(pending['store_url'])}\n\n"
            + ("📊 Sheet updated: STATU → READY FOR ADS" if sheet_ok
               else "⚠️ Sheet update failed — please update manually."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # ── CHANGE TITLE ───────────────────────────────────────────────────────
    if data == "cp_title":
        if not session:
            await query.message.reply_text("⚠️ No active session.")
            return
        session["awaiting"] = "new_title"
        await query.message.reply_text("✏️ Enter the new product title:")
        return

    # ── CHANGE DESCRIPTION ─────────────────────────────────────────────────
    if data == "cp_desc":
        if not session:
            await query.message.reply_text("⚠️ No active session.")
            return
        session["awaiting"] = "new_description"
        await query.message.reply_text("📝 Enter the new product description:")
        return

    # ── DELETE IMAGE ───────────────────────────────────────────────────────
    if data.startswith("cp_del_img:"):
        image_id = int(data.split(":")[1])
        if not pending:
            await query.message.reply_text("⚠️ No active Shopify product.")
            return

        from shopify_client import delete_product_image
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: delete_product_image(pending["product_id"], image_id)
        )

        if ok:
            pending["images"] = [img for img in pending["images"] if img["id"] != image_id]
            await query.message.reply_text(
                f"🗑 Image deleted. <b>{len(pending['images'])}</b> image(s) remaining.",
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text("⚠️ Failed to delete image.")

        await _send_control_panel(context.bot, chat_id, user_id)
        return

    # ── DELETE PRODUCT ─────────────────────────────────────────────────────
    if data == "cp_del_product":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if not pending:
            await query.message.reply_text("⚠️ No active Shopify product.")
            return

        from shopify_client import delete_product as _delete_product
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _delete_product(pending["product_id"])
        )
        _shopify_pending.pop(user_id, None)

        await query.message.reply_text(
            "❌ <b>Product deleted from Shopify.</b>\n\nSheet was not updated.\n\n"
            "Press ⏭ NEXT PRODUCT in your session to continue.",
            parse_mode="HTML",
        )
        return

    # ── NEXT PRODUCT ───────────────────────────────────────────────────────
    if data == "cp_next":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _shopify_pending.pop(user_id, None)

        if not session:
            await query.message.reply_text(
                "📭 No active pricing session. Tap 💰 Price Products to start.",
                reply_markup=MAIN_MENU,
            )
            return

        rows = session["rows"]
        if not rows or session["index"] >= len(rows):
            await query.message.reply_text(
                "🎉 <b>All products are now priced!</b>\n\nTap 📊 Statistics to see the summary.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, chat_id, session)
        return

    # ── STOP ──────────────────────────────────────────────────────────────
    if data == "stop_pricing":
        _sessions.pop(user_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("🛑 Pricing session stopped.", reply_markup=MAIN_MENU)
        return

    if ":" not in data:
        return

    action, sku = data.split(":", 1)

    if not session:
        await query.edit_message_text(
            "⚠️ No active pricing session. Tap 💰 Price Products to start."
        )
        return

    rows  = session["rows"]
    index = session["index"]

    # ── SKIP product ───────────────────────────────────────────────────────
    if action == "skip":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        session["index"] = index + 1
        if session["index"] >= len(rows):
            await query.message.reply_text(
                "📭 <b>No more unpriced products.</b>\n\nTap 💰 Price Products to reload.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            _sessions.pop(user_id, None)
        else:
            await _send_product(context.bot, chat_id, session)
        return

    # ── USE SUGGESTED PRICE ───────────────────────────────────────────────
    if action == "usesuggested":
        row_pos = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if row_pos is None:
            await query.edit_message_text(
                f"⚠️ *{sku}* is no longer in the unpriced list.",
                parse_mode="Markdown",
            )
            return

        suggested = session.get("suggested_price", "")
        if not suggested:
            await query.answer("No suggested price available.", show_alert=True)
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        session["awaiting"]    = "compare_at_price"
        session["current_sku"] = sku
        session["current_url"] = rows[row_pos].get("URL PRODUCT", "")
        session["temp_price"]  = suggested

        await query.message.reply_text(
            f"✅ Using suggested price: <b>{int(suggested):,} GNF</b>\n\n"
            f"Now enter <b>COMPARE AT PRICE</b> (GNF):",
            parse_mode="HTML",
        )
        return

    # ── ADD PRICE ─────────────────────────────────────────────────────────
    if action == "addprice":
        row_pos = next((i for i, r in enumerate(rows) if r.get("SKU") == sku), None)
        if row_pos is None:
            await query.edit_message_text(
                f"⚠️ *{sku}* is no longer in the unpriced list.",
                parse_mode="Markdown",
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        session["awaiting"]    = "price"
        session["current_sku"] = sku
        session["current_url"] = rows[row_pos].get("URL PRODUCT", "")

        await query.message.reply_text(
            f"💰 *Enter PRICE for {sku}* (GNF):",
            parse_mode="Markdown",
        )


# ── Application builder ────────────────────────────────────────────────────

def build_pricing_application() -> Application:
    token = os.environ.get("TELEGRAM_PRICING_BOT_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_PRICING_BOT_TOKEN is not set.")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    return app
