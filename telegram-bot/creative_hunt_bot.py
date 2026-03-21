"""
creative_hunt_bot.py - Creative Hunt Bot handlers.

Commands:
  /creativehunt  — start a new session
  /stop          — stop the current session
  /help          — show help

Per-product flow (both choices happen for every product):
  1. Show product card
  2. Ask creative type: 🎬 Video | 🖼 Image | 🎭 Both
  3. Ask keyword mode: ✏️ Enter Keyword | 🤖 Auto Search
  4. If custom → user types keyword for THIS product
  5. Search and show candidates one by one
  6. APPROVE / REJECT / NEXT PRODUCT / STOP SEARCHING
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

from sheet_writer import get_next_sku_number, append_cluster_rows
from product_scraper import scrape_product_page
from pricing_engine import get_sourcing_for_cluster
from creative_hunt_sheet import (
    load_approved_products,
    save_creative,
    finalize_to_ready_for_ads,
    get_existing_creatives,
    count_all_creatives,
    count_empty_slots,
    EXTRA_CREATIVE_COLS,
    APPROVED_TAB,
)
from ads_scraper import scrape_ads, LoginWallError
from ai_keywords import suggest_keywords

MAX_VIDEO_SECONDS = 180  # 3 minutes


async def _get_video_duration(url: str) -> float:
    """Return video duration in seconds using ffprobe. Returns 0.0 on failure."""
    if not url:
        return 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "-i", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return float(stdout.decode().strip())
        except asyncio.TimeoutError:
            proc.kill()
            return 0.0
    except Exception:
        return 0.0

logger = logging.getLogger(__name__)

# ── Persistent main menu ───────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🎬 Start Creative Hunt"],
        ["🛑 Stop", "❓ Help"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_BTN_HUNT = "🎬 Start Creative Hunt"
_BTN_STOP = "🛑 Stop"
_BTN_HELP = "❓ Help"

MEDIA_LABELS = {
    "video": "🎬 Video only",
    "image": "🖼 Image only",
    "both":  "🎭 Video + Image",
}

ACTIVE_LABELS = {
    "active":   "✅ Active only",
    "inactive": "❌ Inactive only",
    "both":     "🔄 Both",
}

# ── Session state ──────────────────────────────────────────────────────────
# setup_step:
#   "media_type"           → waiting for media type selection (once at start)
#   "await_product_kw"     → waiting for user to type keyword for current product
#   None                   → actively hunting (searching / reviewing candidates)
#
# _sessions[user_id] = {
#   setup_step:       str | None
#   media_type:       "video" | "image" | "both"
#   current_keyword:  str | None    ← set per-product, reset each time
#   products:         list[dict]
#   product_idx:      int
#   candidates:       list[dict]
#   cand_idx:         int
#   chat_id:          int
#   stopped:          bool
# }
_sessions: dict[int, dict] = {}


# ── Register Telegram command menu ────────────────────────────────────────
async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([
            BotCommand("creativehunt", "Search for creatives (video/image)"),
            BotCommand("stop",         "Stop current hunt session"),
            BotCommand("scrollrounds", "Set manual scroll rounds (0 = auto)"),
            BotCommand("help",         "Show help"),
        ])
        logger.info("[creative_hunt_bot] Telegram command menu registered")
    except Exception as e:
        logger.warning(f"[creative_hunt_bot] Could not register command menu (non-fatal): {e}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _media_type_keyboard(product_idx: int, sku: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video",  callback_data=f"ch_media_video:{product_idx}"),
            InlineKeyboardButton("🖼 Image",  callback_data=f"ch_media_image:{product_idx}"),
            InlineKeyboardButton("🎭 Both",   callback_data=f"ch_media_both:{product_idx}"),
        ],
        [
            InlineKeyboardButton("✏️ Add Manually", callback_data=f"ch_manual_add:{sku}"),
        ],
        [
            InlineKeyboardButton("🚀 READY FOR ADS", callback_data=f"ch_fin_ready:{sku}"),
            InlineKeyboardButton("⏭ Next Product",   callback_data=f"ch_skip_product:{product_idx}"),
        ],
    ])


def _product_keyword_keyboard(product_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Enter Keyword", callback_data=f"ch_prod_kw_custom:{product_idx}"),
        InlineKeyboardButton("🤖 Auto Search",   callback_data=f"ch_prod_kw_auto:{product_idx}"),
    ]])


def _active_filter_keyboard(product_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Active only",   callback_data=f"ch_active_active:{product_idx}"),
        InlineKeyboardButton("❌ Inactive only", callback_data=f"ch_active_inactive:{product_idx}"),
        InlineKeyboardButton("🔄 Both",          callback_data=f"ch_active_both:{product_idx}"),
    ]])


def _candidate_keyboard(sku: str, cand_idx: int) -> InlineKeyboardMarkup:
    tag = f"{sku}:{cand_idx}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💾 Save Creative",        callback_data=f"ch_approve:{tag}"),
            InlineKeyboardButton("❌ REJECT",                callback_data=f"ch_reject:{tag}"),
        ],
        [
            InlineKeyboardButton("📌 Save as New Product",  callback_data=f"ch_save_pending:{tag}"),
        ],
        [
            InlineKeyboardButton("🔍 New Keyword",          callback_data=f"ch_new_kw:{sku}"),
            InlineKeyboardButton("✏️ Add Manually",         callback_data=f"ch_manual_add:{sku}"),
        ],
        [
            InlineKeyboardButton("⏭ NEXT PRODUCT",         callback_data=f"ch_next:{tag}"),
            InlineKeyboardButton("🛑 STOP SEARCHING",       callback_data=f"ch_stop:{tag}"),
        ],
        [
            InlineKeyboardButton("🚀 READY FOR ADS",        callback_data=f"ch_fin_ready:{tag}"),
        ],
    ])


def _approval_confirm_keyboard(sku: str, num_creatives: int) -> InlineKeyboardMarkup:
    """
    Show confirmation options based on how many creatives currently exist.
    One button per count from 1 to num_creatives, plus "Keep looking".
    """
    rows = []
    for n in range(1, num_creatives + 1):
        label = f"🚀 Ready for ads with {n} creative" + ("s" if n > 1 else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"ch_conf:{n}:{sku}")])
    rows.append([
        InlineKeyboardButton("🔍 Keep looking for creatives", callback_data=f"ch_conf:keep:{sku}")
    ])
    return InlineKeyboardMarkup(rows)


# ── Per-product: show product card + keyword choice ───────────────────────

async def _show_product_prompt(bot, session: dict):
    """
    Show the current product card with media type buttons (step 1 per product).
    Called when we first arrive at each new product.
    """
    products    = session["products"]
    product_idx = session["product_idx"]
    chat_id     = session["chat_id"]
    total       = len(products)

    if product_idx >= len(products) or session.get("stopped"):
        await bot.send_message(
            chat_id=chat_id,
            text="🏁 <b>Creative hunt complete!</b>\n\nAll eligible products processed.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    product           = products[product_idx]
    sku               = str(product.get("SKU", "")).strip()
    product_name      = str(product.get("PRODUCT NAME", "")).strip()
    url_landing_page  = str(product.get("URL LANDING PAGE", "")).strip()
    ads_library_url   = str(product.get("ADS LIBRARY MEDIA URL", "")).strip()
    empty_slots       = count_empty_slots(product)

    name_line    = f"🛍 <b>{_esc(product_name)}</b>\n" if product_name else ""
    shop_line    = f"🔗 {_esc(url_landing_page)}\n" if url_landing_page else ""
    ads_lib_line = f"📚 {_esc(ads_library_url)}\n" if ads_library_url else ""

    text = (
        f"<b>Product {product_idx + 1} / {total}</b>\n\n"
        + name_line
        + f"🔖 SKU: <code>{_esc(sku)}</code>\n"
        + shop_line
        + ads_lib_line
        + f"\n📦 Open creative slots: <b>{empty_slots}</b>\n\n"
        f"Which type of creatives should the bot look for?"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_media_type_keyboard(product_idx, sku),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Keyword building ──────────────────────────────────────────────────────

async def _auto_generate_keywords(product: dict) -> list[str]:
    """
    Generate fresh keywords for Auto Search mode by:
      1. Taking the product title from PRODUCT NAME column
      2. Scraping the product page for a description
      3. Passing title + description to AI to get 3-4 new search angles
    Never uses the KEYWORD column — those are the keywords that found this
    product originally, searching them again would just show the same ads.
    """
    loop = asyncio.get_event_loop()
    product_name = str(product.get("PRODUCT NAME", "")).strip()
    product_url  = (
        str(product.get("URL PRODUCT", "")).strip()
        or str(product.get("URL LANDING PAGE", "")).strip()
    )

    # Scrape product page for description
    description = ""
    if product_url:
        try:
            scraped = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: scrape_product_page(product_url)),
                timeout=20,
            )
            description = scraped.get("description", "").strip()
            # Use scraped title if we don't have a product name yet
            if not product_name:
                product_name = scraped.get("title", "").strip()
        except Exception as _e:
            logger.warning(f"[creative_hunt] auto-kw scrape failed: {_e}")

    if not product_name and not description:
        logger.warning("[creative_hunt] auto-kw: no title or description — falling back to KEYWORD column")
        fallback = str(product.get("KEYWORD", "")).strip()
        return [fallback] if fallback else []

    # Build a rich context string for the AI
    context = product_name
    if description:
        context += f"\n\nDescription: {description[:400]}"

    try:
        ai_kws = await suggest_keywords(context)
        if ai_kws:
            return ai_kws[:4]
    except Exception as _e:
        logger.warning(f"[creative_hunt] auto-kw AI failed: {_e}")

    # Last resort: split product name into word groups
    keywords = []
    if product_name:
        words = product_name.split()
        if len(words) >= 3:
            keywords.append(" ".join(words[:3]))
        if len(words) >= 2:
            keywords.append(" ".join(words[:2]))
    return keywords[:4]


async def _build_keywords(product: dict, custom_keyword: str | None) -> list[str]:
    """
    Keyword selection:
      - custom_keyword typed by user → use ONLY that
      - Auto Search (no custom_keyword) → generate fresh keywords from
        product title + scraped description via AI (ignores KEYWORD column)
    """
    # If user typed a custom keyword, use ONLY that
    if custom_keyword:
        return [custom_keyword.strip()]

    # Auto Search — generate fresh keywords, never reuse the sheet KEYWORD
    return await _auto_generate_keywords(product)


# ── Streaming search ──────────────────────────────────────────────────────

async def _bg_search(session: dict, product: dict, bot) -> None:
    """
    Background task: searches all keywords and pushes each valid ad into
    session["ad_queue"] as it is found. Sets session["search_done"] when finished.
    """
    queue: asyncio.Queue     = session["ad_queue"]
    seen_urls: set[str]      = set(get_existing_creatives(product))
    custom_keyword           = session.get("current_keyword")
    media_type               = session.get("media_type", "video")
    active_filter            = session.get("active_filter", "both")
    chat_id                  = session.get("chat_id")

    async def progress(text: str):
        if bot and chat_id:
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception:
                pass

    keywords = await _build_keywords(product, custom_keyword)
    total_kw = len(keywords)

    logger.info(
        f"[creative_hunt] Product '{product.get('PRODUCT NAME', '?')}' | "
        f"keywords={keywords} | media={media_type} | active={active_filter}"
    )

    async def on_ad_found(ad: dict):
        if session.get("stopped") or session.get("search_cancelled"):
            return
        url = str(ad.get("ad_library_url", "")).strip()
        if not url or url in seen_urls:
            return
        # Per-ad video duration check
        if ad.get("media_type") == "video" and ad.get("media_url") and not (ad.get("video_duration") or 0):
            dur = await _get_video_duration(ad.get("media_url", ""))
            if dur > MAX_VIDEO_SECONDS:
                logger.info(
                    f"[creative_hunt] Skipping video {url} — "
                    f"duration {dur:.0f}s > {MAX_VIDEO_SECONDS}s"
                )
                return
        seen_urls.add(url)
        await queue.put(ad)

    try:
        for kw_idx, kw in enumerate(keywords, 1):
            if session.get("stopped") or session.get("search_cancelled"):
                break
            await progress(
                f"🔍 Searching keyword <b>{kw_idx}/{total_kw}</b>: <i>{kw}</i>…"
            )
            try:
                await scrape_ads(
                    keyword=kw,
                    country="ALL",
                    media_type_filter=media_type,
                    active_filter=active_filter,
                    progress_callback=None,
                    max_ads=20,
                    validate_html_media=True,
                    scroll_rounds=session.get("scroll_rounds", 0),
                    on_ad_found=on_ad_found,
                )
            except LoginWallError:
                if bot and chat_id:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "🔐 <b>Facebook session expired</b> — login wall detected.\n\n"
                                "Refresh your <code>FACEBOOK_COOKIES</code> secret and "
                                "restart the bot."
                            ),
                            parse_mode="HTML",
                            reply_markup=MAIN_MENU,
                        )
                    except Exception:
                        pass
                break
            except Exception as e:
                logger.warning(f"[creative_hunt] Search error for '{kw}': {e}")
    finally:
        session["search_done"] = True
        logger.info(
            f"[creative_hunt] Background search complete for "
            f"'{product.get('PRODUCT NAME', '?')}'"
        )


# ── Show candidate (queue-driven) ─────────────────────────────────────────

async def _show_candidate(bot, session: dict):
    """
    Pull the next ad from the streaming queue and display it.
    If the queue is empty and the search is still running, schedules
    _wait_and_show to retry when the next ad arrives.
    """
    chat_id     = session["chat_id"]
    products    = session["products"]
    product_idx = session["product_idx"]
    product     = products[product_idx]
    sku         = str(product.get("SKU", "")).strip()
    product_name = str(product.get("PRODUCT NAME", sku)).strip() or sku
    total_prod  = len(products)
    empty_slots = count_empty_slots(product)

    if empty_slots <= 0:
        num_creatives = count_all_creatives(product)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ All creative slots filled for:\n"
                f"<b>{_esc(product_name)}</b>\n\n"
                f"<b>{num_creatives}</b> creative(s) saved. "
                f"You can approve this product now or continue searching."
            ),
            parse_mode="HTML",
            reply_markup=_approval_confirm_keyboard(sku, num_creatives),
        )
        return

    queue: asyncio.Queue | None = session.get("ad_queue")

    # ── Try to dequeue the next ad immediately ─────────────────────────────
    ad = None
    if queue is not None:
        try:
            ad = queue.get_nowait()
        except asyncio.QueueEmpty:
            if session.get("search_done"):
                # Nothing left in queue and search finished
                num_creatives = count_all_creatives(product)
                if num_creatives > 0:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"😔 No more creatives found for <b>{_esc(product_name)}</b>.\n"
                            f"<b>{num_creatives}</b> creative(s) already saved."
                        ),
                        parse_mode="HTML",
                        reply_markup=_approval_confirm_keyboard(sku, num_creatives),
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"😔 No creatives found for <b>{_esc(product_name)}</b>.\n"
                            f"Moving to next product…"
                        ),
                        parse_mode="HTML",
                    )
                    await _advance_product(bot, session)
                return
            else:
                # Still searching — wait in background, notify user
                asyncio.create_task(_wait_and_show(bot, session))
                await bot.send_message(
                    chat_id=chat_id,
                    text="⏳ Still searching… will show the next creative as soon as one is found.",
                    parse_mode="HTML",
                )
                return

    if ad is None:
        await _advance_product(bot, session)
        return

    # ── Show this ad ───────────────────────────────────────────────────────
    session["cand_idx"]  += 1
    session["current_ad"] = ad
    cand_num     = session["cand_idx"]
    filled_slots = len(EXTRA_CREATIVE_COLS) - empty_slots
    ad_url  = str(ad.get("ad_library_url", "")).strip()
    ad_type = str(ad.get("media_type", "")).capitalize()

    text = (
        f"<b>Product {product_idx + 1} / {total_prod}</b>\n\n"
        f"🛍 <b>Product:</b> {_esc(product_name)}\n\n"
        f"🎨 <b>Candidate #{cand_num}:</b>\n"
        f"{_esc(ad_url)}\n\n"
        f"📌 Type: <b>{_esc(ad_type)}</b>\n"
        f"📦 Slots filled: <b>{filled_slots} / {len(EXTRA_CREATIVE_COLS)}</b>"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=_candidate_keyboard(sku, cand_num),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Advance to next product ───────────────────────────────────────────────

async def _advance_product(bot, session: dict):
    """Move to the next product: cancel any background search, reset per-product state."""
    # Cancel background search task cleanly
    search_task: asyncio.Task | None = session.pop("search_task", None)
    if search_task and not search_task.done():
        session["search_cancelled"] = True
        search_task.cancel()

    session["ad_queue"]        = None
    session["search_done"]     = False
    session["search_cancelled"]= False
    session["current_ad"]      = None
    session["cand_idx"]        = 0
    session["product_idx"]    += 1
    session["current_keyword"] = None
    session["media_type"]      = None
    session["active_filter"]   = None

    if session.get("stopped") or session["product_idx"] >= len(session["products"]):
        await bot.send_message(
            chat_id=session["chat_id"],
            text="🏁 <b>Creative hunt complete!</b>\n\nAll eligible products processed.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    await _show_product_prompt(bot, session)


# ── Wait for next ad when queue is temporarily empty ─────────────────────

async def _wait_and_show(bot, session: dict) -> None:
    """
    Wait for the next ad to arrive in the queue while the background search
    is still running.  Polls every 5 s so we can detect when the search is
    done and the queue is empty (→ give up immediately) vs. still running
    (→ keep waiting up to 90 s total).  Called as a fire-and-forget task.
    """
    if session.get("stopped") or session.get("search_cancelled"):
        return

    queue: asyncio.Queue | None = session.get("ad_queue")
    if queue is None:
        return

    ad = None
    MAX_WAIT = 210  # absolute ceiling in seconds (3.5 min — scraper can be slow)
    POLL     = 5    # check interval
    waited   = 0

    while waited < MAX_WAIT:
        if session.get("stopped") or session.get("search_cancelled"):
            return
        try:
            ad = await asyncio.wait_for(queue.get(), timeout=POLL)
            break  # got one
        except asyncio.TimeoutError:
            waited += POLL
            # If the background search has finished and nothing is left, stop
            if session.get("search_done") and queue.empty():
                break

    if ad is None:
        # Nothing arrived — report how many were saved and move on
        if session.get("stopped") or session.get("search_cancelled"):
            return
        product      = session["products"][session["product_idx"]]
        product_name = str(product.get("PRODUCT NAME", product.get("SKU", "?"))).strip()
        sku          = str(product.get("SKU", "")).strip()
        num_creatives = count_all_creatives(product)
        if num_creatives > 0:
            await bot.send_message(
                chat_id=session["chat_id"],
                text=f"⏱ Search timed out. <b>{num_creatives}</b> creative(s) saved so far.",
                parse_mode="HTML",
                reply_markup=_approval_confirm_keyboard(sku, num_creatives),
            )
        else:
            await bot.send_message(
                chat_id=session["chat_id"],
                text=(
                    f"⏱ Search timed out for <b>{_esc(product_name)}</b>.\n"
                    f"Moving to next product…"
                ),
                parse_mode="HTML",
            )
            await _advance_product(bot, session)
        return

    if session.get("stopped") or session.get("search_cancelled"):
        return

    # Put the ad back so _show_candidate can dequeue it
    await queue.put(ad)
    await _show_candidate(bot, session)


# ── Background search task ────────────────────────────────────────────────

async def _run_search_and_show(bot, session: dict):
    """
    Start the streaming background search and begin showing results
    as soon as the first ad arrives in the queue.
    """
    product_idx = session["product_idx"]
    products    = session["products"]

    if product_idx >= len(products) or session.get("stopped"):
        return

    product = products[product_idx]

    # Initialise streaming state
    session["ad_queue"]         = asyncio.Queue()
    session["search_done"]      = False
    session["search_cancelled"] = False
    session["cand_idx"]         = 0
    session["current_ad"]       = None

    # Start background search task
    task = asyncio.create_task(_bg_search(session, product, bot))
    session["search_task"] = task

    # Wait for the very first result (fire-and-forget watcher)
    asyncio.create_task(_wait_and_show(bot, session))


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Creative Hunt Bot</b>\n\n"
        "Searches Meta Ads Library for creatives for your <b>APPROVED</b> products.\n\n"
        "Tap <b>🎬 Start Creative Hunt</b> or use /creativehunt to begin.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


async def cmd_creativehunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    await update.message.reply_text("⏳ Loading APPROVED products…")
    products = load_approved_products()

    if not products:
        await update.message.reply_text(
            "📭 <b>No products found</b> in APPROVED.\n\n"
            "Approve products in the Approval Bot first — they will appear here.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    _sessions[user_id] = {
        "setup_step":       None,
        "media_type":       None,
        "active_filter":    None,
        "current_keyword":  None,
        "products":         products,
        "product_idx":      0,
        "cand_idx":         0,
        "current_ad":       None,
        "ad_queue":         None,
        "search_task":      None,
        "search_done":      False,
        "search_cancelled": False,
        "chat_id":          chat_id,
        "stopped":          False,
        "scroll_rounds":    context.bot_data.get("scroll_rounds", 0),
    }

    await update.message.reply_text(
        f"✅ Loaded <b>{len(products)}</b> product(s) from APPROVED.",
        parse_mode="HTML",
    )
    await _show_product_prompt(context.bot, _sessions[user_id])


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in _sessions:
        session = _sessions[user_id]
        session["stopped"] = True
        search_task = session.pop("search_task", None)
        if search_task and not search_task.done():
            session["search_cancelled"] = True
            search_task.cancel()
        _sessions.pop(user_id, None)
        await update.message.reply_text("🛑 Creative hunt stopped.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("No active hunt session.", reply_markup=MAIN_MENU)


async def cmd_scrollrounds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.bot_data.get("scroll_rounds", 0)
    auto_note = "auto" if current == 0 else str(current)
    context.user_data["awaiting_scroll_rounds"] = True
    await update.message.reply_text(
        f"🔄 <b>Scroll Rounds</b>\n\n"
        f"Current setting: <b>{auto_note}</b>\n\n"
        f"Enter the number of scroll rounds per keyword:\n"
        f"<i>(send 0 to reset to auto mode)</i>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ <b>Creative Hunt Bot — Help</b>\n\n"
        "<b>Workflow:</b>\n"
        "1. Approval Bot approves a product → goes to APPROVED\n"
        "2. This bot loads APPROVED products and searches for creatives\n"
        "3. You save creatives, then tap READY FOR ADS → goes to READY FOR ADS\n\n"
        "<b>Commands:</b>\n"
        "• /creativehunt — start a new session\n"
        "• /stop — stop the current session\n"
        "• /help — show this message\n\n"
        "<b>Per-product choices (shown for every product):</b>\n"
        "• <b>🎬 Video / 🖼 Image / 🎭 Both</b> — creative type\n"
        "• <b>✏️ Enter Keyword</b> — type a keyword specific to this product\n"
        "• <b>🤖 Auto Search</b> — bot uses KEYWORD column + PRODUCT NAME + AI\n"
        "• <b>✅ Active only / ❌ Inactive only / 🔄 Both</b> — ad status filter\n\n"
        "<b>Buttons during creative review:</b>\n"
        "• <b>💾 Save Creative</b> — save URL to next empty slot (ADS LIBRARY MEDIA URL 2-5)\n"
        "• <b>❌ REJECT</b> — skip this creative\n"
        "• <b>⏭ NEXT PRODUCT</b> — move to next product without finalizing\n"
        "• <b>🛑 STOP SEARCHING</b> — end the session\n"
        "• <b>🚀 READY FOR ADS</b> — finalize the product (always visible)\n\n"
        "<b>When you tap READY FOR ADS:</b>\n"
        "Shows confirmation based on how many creatives are saved:\n"
        "• Ready for ads with 1 creative\n"
        "• Ready for ads with 2 creatives\n"
        "• … (up to however many are saved)\n"
        "• Keep looking for creatives\n\n"
        "Finalizing moves the product from APPROVED → READY FOR ADS.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )


# ── Text input handler ────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = (update.message.text or "").strip()

    session = _sessions.get(user_id)

    # ── Capture scroll rounds reply ────────────────────────────────────────
    if context.user_data.get("awaiting_scroll_rounds"):
        context.user_data.pop("awaiting_scroll_rounds")
        try:
            value = int(text)
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid number. Please send /scrollrounds again and enter a valid number.",
                reply_markup=MAIN_MENU,
            )
            return
        context.bot_data["scroll_rounds"] = value
        if value == 0:
            msg = "✅ <b>Confirmed!</b> Scroll rounds reset to <b>auto</b> mode."
        else:
            secs = value * 1.5
            msg = (
                f"✅ <b>Confirmed!</b> Scroll rounds set to <b>{value}</b> per keyword "
                f"(~{secs:.0f}s scroll time per keyword).\n\n"
                f"Applies to all future hunts until changed."
            )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=MAIN_MENU)
        return

    # ── Capture manually-pasted creative URL ──────────────────────────────
    if session and session.get("setup_step") == "await_manual_url":
        # Menu buttons cancel manual-add mode and fall through to normal handling
        if text in (_BTN_HUNT, _BTN_STOP, _BTN_HELP):
            session["setup_step"] = None
            # fall through to menu handler below
        elif not text or not text.lower().startswith("http"):
            await update.message.reply_text(
                "❌ Invalid URL. Please paste a valid URL starting with http:\n"
                "<i>Tap 🛑 Stop to cancel.</i>",
                parse_mode="HTML",
            )
            return
        else:
            session["setup_step"] = None
            sku = str(session["products"][session["product_idx"]].get("SKU", "")).strip()
            ok, saved_col = save_creative(sku, text, tab=APPROVED_TAB)

            if ok:
                product = session["products"][session["product_idx"]]
                product[saved_col] = text
                remaining = count_empty_slots(product)
                await update.message.reply_text(
                    f"✅ <b>Saved</b> to <b>{_esc(saved_col)}</b>\n"
                    f"Remaining open slots: <b>{remaining}</b>",
                    parse_mode="HTML",
                )
                if remaining <= 0:
                    await update.message.reply_text(
                        "🎉 All creative slots for this product are now full!\n"
                        "Moving to next product…"
                    )
                    await _advance_product(context.bot, session)
            else:
                await update.message.reply_text(
                    "⚠️ Could not save creative — Google Sheets rate limit hit. "
                    "Wait a few seconds and try again."
                )
            return

    # ── Capture new keyword to restart search mid-session ─────────────────
    if session and session.get("setup_step") == "await_new_kw":
        if not text or text in (_BTN_HUNT, _BTN_STOP, _BTN_HELP):
            await update.message.reply_text("Please type the keyword to search:")
            return

        session["setup_step"]      = None
        session["current_keyword"] = text
        # Cancel current search
        session["search_cancelled"] = True
        if session.get("search_task") and not session["search_task"].done():
            session["search_task"].cancel()

        await update.message.reply_text(
            f"🔍 Searching with new keyword: <b>{_esc(text)}</b>…",
            parse_mode="HTML",
        )
        # Restart search with new keyword
        await _run_search_and_show(context.bot, session)
        return

    # ── Capture custom keyword typed by user for the current product ───────
    if session and session.get("setup_step") == "await_product_kw":
        if not text or text in (_BTN_HUNT, _BTN_STOP, _BTN_HELP):
            await update.message.reply_text(
                "Please type the keyword to use for this product:"
            )
            return

        session["current_keyword"] = text
        session["setup_step"]      = None

        await update.message.reply_text(
            f"✏️ Keyword: <b>{_esc(text)}</b>\n\n"
            f"Show which ads?",
            parse_mode="HTML",
            reply_markup=_active_filter_keyboard(session["product_idx"]),
        )
        return

    # ── Persistent main menu buttons ──────────────────────────────────────
    if text == _BTN_HUNT:
        await cmd_creativehunt(update, context)
    elif text == _BTN_STOP:
        await cmd_stop(update, context)
    elif text == _BTN_HELP:
        await cmd_help(update, context)


# ── Callback handler ──────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data    = query.data or ""

    # ── Search with a new keyword ─────────────────────────────────────────
    if data.startswith("ch_new_kw:"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return
        session["setup_step"] = "await_new_kw"
        await query.message.reply_text(
            "🔍 Type the new keyword to search for this product:"
        )
        return

    # ── Manual creative add ───────────────────────────────────────────────
    if data.startswith("ch_manual_add:"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return
        product = session["products"][session["product_idx"]]
        remaining = count_empty_slots(product)
        if remaining <= 0:
            await query.message.reply_text(
                "⚠️ All 10 creative slots are already full for this product."
            )
            return
        session["setup_step"] = "await_manual_url"
        await query.message.reply_text(
            f"📎 Paste the creative URL to save:\n"
            f"<i>({remaining} slot{'s' if remaining != 1 else ''} remaining)</i>",
            parse_mode="HTML",
        )
        return

    # ── Per-product: skip product (from product card) ─────────────────────
    if data.startswith("ch_skip_product:"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return
        try:
            btn_product_idx = int(data.split(":")[1])
        except (IndexError, ValueError):
            btn_product_idx = -1

        if btn_product_idx != session["product_idx"]:
            await query.message.reply_text(
                "⚠️ Already moved to the next product. See latest message above."
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text("⏭ Skipping product…")
        await _advance_product(context.bot, session)
        return

    # ── Per-product: media type choice ────────────────────────────────────
    if data.startswith("ch_media_"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return

        parts_media = data.split(":")
        action_media = parts_media[0]   # ch_media_video / ch_media_image / ch_media_both
        try:
            btn_product_idx = int(parts_media[1]) if len(parts_media) > 1 else -1
        except ValueError:
            btn_product_idx = -1

        # Stale button guard
        if btn_product_idx != session["product_idx"]:
            await query.message.reply_text(
                "⚠️ Already moved to the next product. See latest message above."
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        media_map = {
            "ch_media_video": "video",
            "ch_media_image": "image",
            "ch_media_both":  "both",
        }
        session["media_type"] = media_map.get(action_media, "video")
        media_label = MEDIA_LABELS[session["media_type"]]

        # Now ask keyword mode for this product
        await query.message.reply_text(
            f"🎨 Creative type: <b>{_esc(media_label)}</b>\n\n"
            f"Now choose the keyword mode for this product:",
            parse_mode="HTML",
            reply_markup=_product_keyword_keyboard(session["product_idx"]),
        )
        return

    # ── Per-product: keyword mode choice ──────────────────────────────────
    if data.startswith("ch_prod_kw_"):
        session = _sessions.get(user_id)
        if not session or session.get("setup_step") not in (None, "await_product_kw"):
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return

        # Parse: ch_prod_kw_auto:2  or  ch_prod_kw_custom:2
        parts = data.split(":")
        action_key = parts[0]   # ch_prod_kw_auto or ch_prod_kw_custom
        try:
            btn_product_idx = int(parts[1]) if len(parts) > 1 else -1
        except ValueError:
            btn_product_idx = -1

        # Stale button guard
        if btn_product_idx != session["product_idx"]:
            await query.message.reply_text(
                "⚠️ Already moved to the next product. See latest message above."
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if action_key == "ch_prod_kw_custom":
            session["setup_step"] = "await_product_kw"
            await query.message.reply_text(
                "✏️ Type the keyword to use for this product:"
            )
        else:
            # Auto search — ask active filter next
            session["current_keyword"] = None
            session["setup_step"]      = None
            await query.message.reply_text(
                "🤖 Auto search selected.\n\nShow which ads?",
                reply_markup=_active_filter_keyboard(session["product_idx"]),
            )
        return

    # ── Per-product: active filter choice ────────────────────────────────
    if data.startswith("ch_active_"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return

        parts_active = data.split(":")
        action_active = parts_active[0]
        try:
            btn_product_idx = int(parts_active[1]) if len(parts_active) > 1 else -1
        except ValueError:
            btn_product_idx = -1

        if btn_product_idx != session["product_idx"]:
            await query.message.reply_text(
                "⚠️ Already moved to the next product. See latest message above."
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        active_map = {
            "ch_active_active":   "active",
            "ch_active_inactive": "inactive",
            "ch_active_both":     "both",
        }
        session["active_filter"] = active_map.get(action_active, "both")
        active_label = ACTIVE_LABELS[session["active_filter"]]
        kw_label = (
            f"✏️ <b>{_esc(session['current_keyword'])}</b>"
            if session.get("current_keyword")
            else "🤖 Auto"
        )

        await query.message.reply_text(
            f"🔎 Ads filter: <b>{_esc(active_label)}</b>\n"
            f"🔑 Keyword: {kw_label}\n"
            f"🌍 Country: <b>ALL</b>\n\n"
            f"Searching… ⏳",
            parse_mode="HTML",
        )
        asyncio.create_task(_run_search_and_show(context.bot, session))
        return

    # ── READY FOR ADS: show confirmation menu ─────────────────────────────
    if data.startswith("ch_fin_ready:"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return

        # Parse tag: ch_fin_ready:{sku}:{cand_idx}
        rest = data[len("ch_fin_ready:"):]
        tag_parts = rest.rsplit(":", 1)
        fin_sku = tag_parts[0] if tag_parts else rest

        product = session["products"][session["product_idx"]] if session["products"] else {}
        num_creatives = count_all_creatives(product)

        if num_creatives == 0:
            await query.answer(
                "No creatives saved yet. Save at least one creative first.",
                show_alert=True,
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        product_name = str(product.get("PRODUCT NAME", product.get("SKU", "?"))).strip()
        await query.message.reply_text(
            f"🚀 <b>Mark as Ready for Ads?</b>\n\n"
            f"🛍 <b>{_esc(product_name)}</b>\n\n"
            f"<b>{num_creatives}</b> creative(s) currently saved.\n\n"
            f"Choose how many creatives to finalize with:",
            parse_mode="HTML",
            reply_markup=_approval_confirm_keyboard(fin_sku, num_creatives),
        )
        return

    # ── CONFIRM APPROVAL ──────────────────────────────────────────────────
    if data.startswith("ch_conf:"):
        session = _sessions.get(user_id)
        if not session:
            await query.edit_message_text("⚠️ Session expired. Tap 🎬 Start Creative Hunt.")
            return

        # Parse: ch_conf:{option}:{sku}
        # option is "keep" or a digit
        rest = data[len("ch_conf:"):]
        colon_pos = rest.find(":")
        if colon_pos == -1:
            return
        option  = rest[:colon_pos]
        conf_sku = rest[colon_pos + 1:]

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if option == "keep":
            queue = session.get("ad_queue")
            search_done = session.get("search_done", True)
            if queue is not None and (not queue.empty() or not search_done):
                await query.message.reply_text("🔍 Continuing to look for creatives…")
                await _show_candidate(context.bot, session)
            else:
                await query.message.reply_text("🔍 Choose creative type to continue searching:")
                await _show_product_prompt(context.bot, session)
            return

        try:
            num = int(option)
        except ValueError:
            await query.message.reply_text("⚠️ Invalid option.")
            return

        await query.message.reply_text(f"⏳ Finalizing with <b>{num}</b> creative(s)…", parse_mode="HTML")

        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: finalize_to_ready_for_ads(conf_sku, num)
        )

        if ok:
            product = session["products"][session["product_idx"]] if session["products"] else {}
            product_name = str(product.get("PRODUCT NAME", product.get("SKU", conf_sku))).strip()
            await query.message.reply_text(
                f"🎉 <b>{_esc(product_name or conf_sku)}</b>\n\n"
                f"🚀 <b>Ready for Ads</b> with <b>{num}</b> creative(s)!\n"
                f"Moved from APPROVED → READY FOR ADS.",
                parse_mode="HTML",
            )
            await _advance_product(context.bot, session)
        else:
            await query.message.reply_text(
                "⚠️ Could not finalize product. Check the sheet manually.",
            )
        return

    # ── Candidate review buttons ───────────────────────────────────────────
    if ":" not in data:
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    action = parts[0]
    sku    = parts[1]
    try:
        cand_idx = int(parts[2])
    except ValueError:
        return

    session = _sessions.get(user_id)
    if not session or session.get("setup_step") is not None:
        await query.edit_message_text(
            "⚠️ No active hunt session. Tap 🎬 Start Creative Hunt."
        )
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # ── STOP SEARCHING ────────────────────────────────────────────────────
    if action == "ch_stop":
        session["stopped"] = True
        # Cancel any running search task
        search_task = session.pop("search_task", None)
        if search_task and not search_task.done():
            session["search_cancelled"] = True
            search_task.cancel()
        _sessions.pop(user_id, None)
        await query.message.reply_text("🛑 Creative hunt stopped.", reply_markup=MAIN_MENU)
        return

    # ── NEXT PRODUCT ──────────────────────────────────────────────────────
    if action == "ch_next":
        await _advance_product(context.bot, session)
        return

    # Stale button guard — cand_idx in callback must match session["cand_idx"]
    if cand_idx != session["cand_idx"]:
        await query.message.reply_text(
            "⚠️ This creative was already processed. See the latest message above."
        )
        return

    # ── SAVE CREATIVE (to APPROVED slot) ──────────────────────────────────
    if action == "ch_approve":
        ad = session.get("current_ad")
        if not ad:
            await query.message.reply_text("⚠️ No active creative to save.")
            return

        # Save the actual CDN media URL so the Ads Launch Bot can download it.
        # Fallback chain: media_url → main_image_url (thumbnail) → ad_library_url
        media_url_cdn = (
            str(ad.get("media_url", "")).strip()
            or str(ad.get("main_image_url", "")).strip()
            or str(ad.get("thumbnail_url", "")).strip()
        )
        lib_url = str(ad.get("ad_library_url", "")).strip()
        ad_url = media_url_cdn or lib_url

        if not ad_url:
            await query.message.reply_text("⚠️ No media URL found for this creative.")
            return

        ok, saved_col = save_creative(sku, ad_url, tab=APPROVED_TAB)

        if ok:
            product = session["products"][session["product_idx"]]
            product[saved_col]  = ad_url
            remaining_slots     = count_empty_slots(product)

            await query.message.reply_text(
                f"✅ <b>Saved</b> to <b>{_esc(saved_col)}</b>\n"
                f"Remaining open slots: <b>{remaining_slots}</b>",
                parse_mode="HTML",
            )
            logger.info(f"[creative_hunt] SKU '{sku}' — saved to '{saved_col}'")

            if remaining_slots <= 0:
                await query.message.reply_text(
                    "🎉 All creative slots for this product are now full!\n"
                    "Moving to next product…"
                )
                await _advance_product(context.bot, session)
                return
        else:
            await query.message.reply_text(
                "⚠️ Could not save creative (slots may be full or SKU not found)."
            )

        await _show_candidate(context.bot, session)
        return

    # ── SAVE AS NEW PENDING PRODUCT (background — hunt continues immediately)
    if action == "ch_save_pending":
        ad = session.get("current_ad")
        if not ad:
            await query.message.reply_text("⚠️ No active creative to save.")
            await _show_candidate(context.bot, session)
            return

        ad_library_url   = str(ad.get("ad_library_url", "")).strip()
        landing_page_url = str(ad.get("landing_page_url", "")).strip()
        keyword          = str(session["products"][session["product_idx"]].get("KEYWORD", "")).strip()
        chat_id          = session["chat_id"]

        if not ad_library_url and not landing_page_url:
            await query.message.reply_text("⚠️ No URL found for this creative — cannot save.")
            await _show_candidate(context.bot, session)
            return

        # Acknowledge immediately — pipeline runs in background
        await query.message.reply_text(
            "📌 <b>Saving product in background…</b>\nYou'll get a notification when done.",
            parse_mode="HTML",
        )

        # Fire-and-forget background task
        asyncio.create_task(
            _bg_save_pending_product(context.bot, chat_id, ad_library_url, landing_page_url, keyword)
        )

        # Creative hunt continues immediately — show next candidate
        await _show_candidate(context.bot, session)
        return

    # ── REJECT ────────────────────────────────────────────────────────────
    if action == "ch_reject":
        await _show_candidate(context.bot, session)
        return


# ── Background: save pending product pipeline ────────────────────────────

def _load_research_bot_chat_id() -> int | None:
    """Read the research bot chat ID from the shared file it writes on /start."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_chatid.txt")
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


async def _bg_save_pending_product(
    bot,
    chat_id:          int,
    ad_library_url:   str,
    landing_page_url: str,
    keyword:          str,
) -> None:
    """
    Runs the full product-research pipeline in the background:
      1. Scrape product page (title + images)
      2. Sourcing lookup (price, URL, weight)
      3. Generate SKU and save to PENDING sheet
    Progress + result are sent to the FB Ads bot chat (ads-launch-bot).
    Falls back to creative-hunt-bot chat if fb-bot chat ID is unavailable.
    """
    from telegram import Bot as TGBot

    loop = asyncio.get_event_loop()

    # Send progress to the research bot chat
    research_token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    research_chat_id = _load_research_bot_chat_id()

    if research_token and research_chat_id:
        notify_bot     = TGBot(token=research_token)
        notify_chat_id = research_chat_id
    else:
        # Fallback: use the creative-hunt-bot chat
        notify_bot     = bot
        notify_chat_id = chat_id
        logger.warning("[bg_save_pending] research bot token/chat_id not found — notifying in creative-hunt chat")

    async def _notify(text: str):
        try:
            await notify_bot.send_message(
                chat_id=notify_chat_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as _e:
            logger.warning(f"[bg_save_pending] notify failed: {_e}")

    try:
        await _notify("📌 <b>New product — saving in progress…</b>\n⏳ Step 1/3 — Scraping product page…")

        # Step 1: scrape product page
        scraped = {}
        if landing_page_url:
            try:
                scraped = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: scrape_product_page(landing_page_url)),
                    timeout=25,
                )
            except Exception as _se:
                logger.warning(f"[bg_save_pending] scrape failed: {_se}")

        product_name = scraped.get("title", "").strip()
        image_urls   = scraped.get("image_urls", [])

        await _notify(
            f"🔍 Step 2/3 — Looking up sourcing price…\n"
            f"🛍 Product: {_esc(product_name) or '—'}\n"
            f"🖼 Images found: {len(image_urls)}"
        )

        # Step 2: sourcing
        class _MinimalCluster:
            canonical_name = product_name
            ads = [{
                "_product_images":  image_urls,
                "og_image_url":     image_urls[0] if image_urls else "",
                "landing_page_url": landing_page_url,
            }]

        sourcing_usd = sourcing_url = weight_gram = ""
        try:
            sourcing_usd, sourcing_url, weight_gram = await asyncio.wait_for(
                get_sourcing_for_cluster(_MinimalCluster()),
                timeout=90,
            )
        except Exception as _se:
            logger.warning(f"[bg_save_pending] sourcing failed: {_se}")

        await _notify("💾 Step 3/3 — Saving to PENDING sheet…")

        # Step 3: generate SKU and save
        new_sku_num = await loop.run_in_executor(None, get_next_sku_number)
        new_sku     = f"PRD-{new_sku_num:04d}"

        row = {
            "SKU":                   new_sku,
            "KEYWORD":               keyword,
            "URL PRODUCT":           landing_page_url,
            "URL LANDING PAGE":      landing_page_url,
            "ADS LIBRARY MEDIA URL": ad_library_url,
            "PRODUCT NAME":          product_name,
            "IMAGE URL":             image_urls[0] if image_urls else "",
            "SOURCING PRICE USD":    sourcing_usd,
            "SOURCING URL":          sourcing_url,
            "WEIGHT GRAM":           weight_gram,
            "STATU":                 "PENDING",
        }
        await loop.run_in_executor(None, lambda: append_cluster_rows([row]))

        sourcing_display = f"${sourcing_usd}" if sourcing_usd else "not found"
        weight_display   = f"{weight_gram}g"  if weight_gram  else "—"

        await _notify(
            f"✅ <b>New product saved to PENDING!</b>\n\n"
            f"🔖 SKU: <code>{new_sku}</code>\n"
            f"🛍 Name: {_esc(product_name) or '—'}\n"
            f"💰 Sourcing: {sourcing_display}\n"
            f"⚖️ Weight: {weight_display}\n"
            f"🔗 {_esc(landing_page_url) if landing_page_url else '—'}"
        )
        logger.info(f"[bg_save_pending] Saved {new_sku} — sourcing={sourcing_usd} url={landing_page_url}")

    except Exception as e:
        logger.error(f"[bg_save_pending] Failed: {e}", exc_info=True)
        await _notify(f"❌ Failed to save product: {e}")


# ── Application builder ───────────────────────────────────────────────────

def build_creative_hunt_application() -> Application:
    token = os.environ.get("TELEGRAM_CREATIVE_BOT_TOKEN", "")
    if not token:
        raise ValueError(
            "TELEGRAM_CREATIVE_BOT_TOKEN is not set. "
            "Create a bot via @BotFather and add the token as a secret."
        )

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("creativehunt", cmd_creativehunt))
    app.add_handler(CommandHandler("stop",         cmd_stop))
    app.add_handler(CommandHandler("scrollrounds", cmd_scrollrounds))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
