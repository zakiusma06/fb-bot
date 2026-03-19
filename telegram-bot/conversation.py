"""
conversation.py - Telegram conversation states and wizard flow.
Defines all conversation steps and the extraction workflow runner.
"""

import asyncio
import logging
import os
import traceback
from concurrent.futures import ProcessPoolExecutor

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from ads_scraper import scrape_ads_sync, LoginWallError, _is_blocked_landing_page
from product_extractor import enrich_batch, generate_secondary_keyword
import task_registry

from physical_classifier import filter_physical_ads
from cluster_builder import build_clusters
from sheet_writer import (
    read_existing_rows,
    read_keyword_stats,
    get_next_sku_number,
    append_cluster_rows,
    save_csv_backup,
)
from deduplicator import is_cluster_duplicate
from config import FACEBOOK_COOKIES
from pricing_engine import get_sourcing_for_cluster
import fb_auth

logger = logging.getLogger(__name__)

# ── ProcessPoolExecutor for Playwright scraping ────────────────────────────
# max_workers=1: only one Facebook session at a time (single auth state).
# The executor lives for the lifetime of the process so the worker is reused
# across extractions rather than spawned fresh each time.
_scrape_executor = ProcessPoolExecutor(max_workers=1)

# ── Conversation states ────────────────────────────────────────────────────
(
    ASK_QUANTITY,
    ASK_KEYWORD_MODE,
    ASK_NICHE,
    ASK_KEYWORD_SELECTION,
    ASK_MANUAL_KEYWORDS,
    ASK_COUNTRIES,
    ASK_MEDIA_TYPE,
    ASK_ACTIVE_STATUS,
    ASK_CONFIRM,
) = range(9)


# ── /extract entry point ───────────────────────────────────────────────────
async def cmd_extract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    _has_auth = fb_auth.FB_STORAGE_STATE.exists() or fb_auth.FB_PROFILE_DIR.exists() or bool(FACEBOOK_COOKIES)
    if not _has_auth:
        await update.message.reply_text(
            "⚠️ *Facebook authentication is not configured.*\n\n"
            "Without it, Meta Ads Library only shows political ads.\n"
            "Run <code>python fb_login.py</code> in the Shell to seed auth, "
            "or set the <code>FACEBOOK_COOKIES</code> secret and run it again. Continuing anyway…",
            parse_mode="HTML",
        )
    await update.message.reply_text(
        "Let's set up your extraction!\n\n"
        "How many *unique product clusters* do you want to extract?",
        parse_mode="Markdown",
    )
    return ASK_QUANTITY


# ── Step 1: quantity ──────────────────────────────────────────────────────
async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("Please enter a valid number (e.g. 10).")
        return ASK_QUANTITY
    context.user_data["quantity"] = int(text)
    keyboard = [["Keyword Suggestions", "Manual Keywords"], ["Cancel"]]
    await update.message.reply_text(
        f"Great! I'll extract *{text}* unique product clusters.\n\n"
        "How do you want to choose keywords?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_KEYWORD_MODE


# ── Step 2: keyword mode ──────────────────────────────────────────────────
async def ask_keyword_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if "cancel" in text:
        return await cmd_cancel(update, context)
    if "suggestion" in text:
        return await ask_keyword_suggestions(update, context)
    else:
        await update.message.reply_text(
            "Enter one or more keywords separated by commas.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_MANUAL_KEYWORDS


# ── Step 3a: keyword suggestions from historical sheet data ───────────────
async def _safe_reply(update, thinking_msg, text: str) -> None:
    """Edit thinking_msg, or send a new message if edit fails."""
    try:
        await thinking_msg.edit_text(text)
    except Exception:
        try:
            await update.message.reply_text(text)
        except Exception:
            pass


async def ask_keyword_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.warning(">>>>>> [TRACE] ask_keyword_suggestions() ENTERED <<<<<<")
    thinking_msg = await update.message.reply_text(
        "⏳ Loading keyword performance from your sheet…",
        reply_markup=ReplyKeyboardRemove(),
    )
    stats = []
    try:
        logger.info("[conv] ask_keyword_suggestions: fetching keyword stats…")
        stats = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, read_keyword_stats),
            timeout=12,
        )
        logger.info(f"[conv] ask_keyword_suggestions: got {len(stats)} keywords")
    except Exception as e:
        logger.warning(f"[conv] ask_keyword_suggestions: failed ({type(e).__name__}: {e})")
        await _safe_reply(
            update, thinking_msg,
            "⚠️ Could not load keyword suggestions (sheet unavailable).\n\n"
            "Type one or more keywords manually, separated by commas:",
        )
        return ASK_MANUAL_KEYWORDS

    if not stats:
        await _safe_reply(
            update, thinking_msg,
            "No keyword data found yet in your sheet.\n\n"
            "Type one or more keywords manually, separated by commas or new lines:",
        )
        return ASK_MANUAL_KEYWORDS

    top = stats[:10]
    context.user_data["ai_keywords"] = [s["keyword"] for s in top]
    context.user_data["ai_keyword_pcts"] = [round(s["approval_rate"] * 100) for s in top]
    context.user_data["kw_selected"] = set()

    keyboard = _build_kw_keyboard(top, set())
    msg_text = "*Top Keyword Suggestions*\nTap to select, then tap ✅ Confirm:"
    try:
        await thinking_msg.edit_text(
            msg_text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await update.message.reply_text(
            msg_text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return ASK_KEYWORD_SELECTION


def _build_kw_keyboard(stats_list: list, selected: set) -> list:
    """Build inline keyboard rows for keyword selection."""
    rows = []
    for i, s in enumerate(stats_list):
        pct = s["approval_rate"] if isinstance(s["approval_rate"], int) else round(s["approval_rate"] * 100)
        check = "✅" if i in selected else "☐"
        label = f"{check} {s['keyword']} ({pct}%)"
        rows.append([InlineKeyboardButton(label, callback_data=f"kw_toggle:{i}")])
    count = len(selected)
    confirm_label = f"✅ Confirm ({count} selected)" if count else "✅ Confirm"
    rows.append([InlineKeyboardButton(confirm_label, callback_data="kw_confirm")])
    return rows


# ── Step 3b: inline keyword toggle ────────────────────────────────────────
async def cb_keyword_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    selected: set = ctx.user_data.get("kw_selected", set())
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    ctx.user_data["kw_selected"] = selected

    kws = ctx.user_data.get("ai_keywords", [])
    pcts = ctx.user_data.get("ai_keyword_pcts", [])
    stats_list = [{"keyword": kw, "approval_rate": pct} for kw, pct in zip(kws, pcts)]
    keyboard = _build_kw_keyboard(stats_list, selected)
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_KEYWORD_SELECTION


# ── Step 3b: inline keyword confirm ───────────────────────────────────────
async def cb_keyword_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    selected: set = ctx.user_data.get("kw_selected", set())
    if not selected:
        await query.answer("Select at least one keyword first!", show_alert=True)
        return ASK_KEYWORD_SELECTION
    await query.answer()
    ai_keywords = ctx.user_data.get("ai_keywords", [])
    chosen = [ai_keywords[i] for i in sorted(selected)]
    ctx.user_data["keywords"] = chosen
    try:
        await query.edit_message_text(f"✅ Keywords selected: {', '.join(chosen)}")
    except Exception:
        pass
    return await _proceed_to_countries(update, ctx)


# ── Step 3b: manual text fallback (user types their own keywords) ─────────
async def ask_keyword_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    normalized = text.replace(";", ",").replace("\n", ",")
    keywords = [k.strip() for k in normalized.split(",") if k.strip()]
    if not keywords:
        await update.message.reply_text(
            "No keywords found. Type one or more keywords separated by commas.",
        )
        return ASK_KEYWORD_SELECTION
    context.user_data["keywords"] = keywords
    return await _proceed_to_countries(update, context)


# ── Step 3c: manual keywords ──────────────────────────────────────────────
async def ask_manual_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    # Support commas, semicolons, or newlines as separators
    normalized = text.replace(";", ",").replace("\n", ",")
    keywords = [k.strip() for k in normalized.split(",") if k.strip()]
    if not keywords:
        await update.message.reply_text("Please enter at least one keyword.")
        return ASK_MANUAL_KEYWORDS
    context.user_data["keywords"] = keywords
    return await _proceed_to_countries(update, context)


# ── Step 3: countries ──────────────────────────────────────────────────────
async def _proceed_to_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    source_display = f"Keywords: *{', '.join(d.get('keywords', []))}*"
    await update.message.reply_text(
        f"{source_display}\n\n"
        "Which countries should I search?\n"
        "Examples: `France`, `US, Germany`, `ALL`",
        parse_mode="Markdown",
    )
    return ASK_COUNTRIES


async def ask_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    countries = [c.strip() for c in text.replace(";", ",").split(",") if c.strip()]
    if not countries:
        await update.message.reply_text("Please enter at least one country.")
        return ASK_COUNTRIES
    context.user_data["countries"] = countries
    keyboard = [["Video Only", "Image Only", "Both"]]
    await update.message.reply_text(
        "What *media type* should I search for?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_MEDIA_TYPE


# ── Step 5: media type ────────────────────────────────────────────────────
async def ask_media_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if "video" in text:
        context.user_data["media_type"] = "video"
        label = "Video only"
    elif "image" in text:
        context.user_data["media_type"] = "image"
        label = "Image only"
    else:
        context.user_data["media_type"] = "both"
        label = "Both (video + image)"
    keyboard = [["Active Only", "Inactive Only", "Both"]]
    await update.message.reply_text(
        f"Media type: *{label}*\n\nWhat *active status* should I filter for?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_ACTIVE_STATUS


# ── Step 6: active status ──────────────────────────────────────────────────
async def ask_active_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if "inactive" in text:
        context.user_data["active_status"] = "inactive"
        label = "Inactive only"
    elif "active" in text and "inactive" not in text:
        context.user_data["active_status"] = "active"
        label = "Active only"
    else:
        context.user_data["active_status"] = "both"
        label = "Both (active + inactive)"
    d = context.user_data
    source_line = f"• Keywords: *{', '.join(d.get('keywords', []))}*\n"
    summary = (
        "📋 *Extraction Summary*\n\n"
        f"• Product clusters requested: *{d.get('quantity', '?')}*\n"
        f"{source_line}"
        f"• Countries: *{', '.join(d.get('countries', []))}*\n"
        f"• Media type: *{d.get('media_type', '?')}*\n"
        f"• Active status: *{label}*\n\n"
        "Each product cluster → 1 row with 1 product URL and 1 creative.\n\n"
        "Shall I start the extraction?"
    )
    keyboard = [["Confirm ✅", "Cancel ❌"]]
    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return ASK_CONFIRM


# ── Step 7: confirmation & run ─────────────────────────────────────────────
async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if "cancel" in text or "❌" in text:
        return await cmd_cancel(update, context)
    await update.message.reply_text(
        "Starting extraction… I'll update you as I go! ⏳",
        reply_markup=ReplyKeyboardRemove(),
    )
    task = asyncio.create_task(_run_extraction(update, context))
    context.user_data["_extraction_task"] = task
    chat_id = update.effective_chat.id
    task_registry.register(chat_id, task)
    return ConversationHandler.END


# ── Extraction workflow ────────────────────────────────────────────────────
async def _run_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Full pipeline:
      1. Scrape ads by keyword
      2. Enrich ads with landing page data (titles, keywords)
      3. Validate physical products
      4. Build product clusters (1 URL + 1 creative per cluster)
      5. Write one row per cluster to Google Sheets
    """
    d = context.user_data
    quantity = d.get("quantity", 10)
    keywords = d.get("keywords", [])
    countries = d.get("countries", [])
    media_type = d.get("media_type", "both")
    active_status = d.get("active_status", "both")
    chat_id = update.effective_chat.id
    bot = context.bot

    async def progress(msg: str):
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception:
            pass

    try:
        await _do_extraction(
            update, context, quantity, keywords, countries,
            media_type, active_status, chat_id, bot, progress,
        )
    except asyncio.CancelledError:
        logger.info(f"[conv] Extraction cancelled by user (chat {chat_id})")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="⛔ *Extraction stopped.* Products saved so far are in your sheet.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"[conv] Unhandled error in extraction (chat {chat_id}): {exc}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ *Extraction crashed with an unexpected error:*\n"
                    f"`{type(exc).__name__}: {str(exc)[:300]}`\n\n"
                    "Products saved before the crash are still in your sheet.\n"
                    "Please send /extract to start a new extraction."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    finally:
        task_registry.unregister(chat_id)


async def _do_extraction(
    update, context, quantity, keywords, countries,
    media_type, active_status, chat_id, bot, progress,
):

    # ── Load sheet state once (needed for dedup across all rounds) ────────
    start_sku = 1
    sheet_error_msg = ""
    existing_rows: list[dict] = []
    try:
        start_sku = get_next_sku_number()
        existing_rows = read_existing_rows()
        logger.info(f"[conv] Loaded {len(existing_rows)} existing sheet rows for dedup check")
    except Exception as e:
        sheet_error_msg = str(e)
        logger.warning(f"[conv] Could not read sheet for dedup: {e}")

    # ── Iterative scrape → process → save loop (Phases 1-4 per round) ────────
    #
    # Each round: scrape → enrich → cluster → dedup → source → SAVE TO SHEET.
    # Saving happens product-by-product inside the sourcing loop.
    #
    # Scan depth grows each round so later rounds scroll deeper into Meta's
    # Ads Library, surfacing ads that were below the fold in earlier rounds.
    #
    # Stops when:
    #   • we have AT LEAST `quantity` valid clusters AND the current batch is done, OR
    #   • 2 consecutive rounds find zero new unseen ads (truly exhausted), OR
    #   • we exceed MAX_ROUNDS
    #
    # `quantity` is a MINIMUM target — valid products discovered in the current
    # batch are NEVER discarded just because the target has already been reached.
    #
    MAX_ROUNDS = 5
    valid_clusters: list = []            # accumulates all accepted clusters (for dedup + count)
    seen_ad_ids: set[str] = set()        # global ad dedup key
    skipped_duplicates: list[str] = []   # names skipped as hard sheet dupes
    possible_duplicates: list[str] = []  # names flagged as possible dupes (still saved)
    sku_offset = 0                       # how many SKUs already assigned
    ads_scanned = 0                      # total raw ads collected (for final summary)
    total_saved = 0                      # total rows written across all rounds
    last_csv_path = None                 # path of last CSV backup (fallback)
    consecutive_empty = 0               # rounds in a row with zero new unseen ads
    consecutive_no_lp = 0              # rounds in a row where all new ads had no landing page
    login_wall_hit = False
    round_num = 0

    await progress(
        f"🔍 *Phase 1–3:* Searching, filtering and clustering products…\n"
        f"_Minimum target: *{quantity}* valid products. "
        f"All valid products found in each batch will be saved._"
    )

    while len(valid_clusters) < quantity and round_num < MAX_ROUNDS and consecutive_empty < 2 and consecutive_no_lp < 2:
        round_num += 1
        needed = quantity - len(valid_clusters)

        # ── Dynamic scan depth scaling (grows each round to scroll deeper) ────
        # Minimum base ensures we always scroll enough even for small requests.
        # With scroll_rounds=_max//20 and 1.5s sleep: 500→25 scrolls(37.5s),
        # Early exit at 30 ads found. Timeout: 600s for France/slow regions.
        # Round 1: max(quantity×5, 150) capped at 500
        # Round 2: max(quantity×7, 250) capped at 600
        # Round 3+: max(quantity×10, 350) capped at 800
        if round_num == 1:
            scan_limit = min(max(quantity * 5, 150), 500)
        elif round_num == 2:
            scan_limit = min(max(quantity * 7, 250), 600)
        else:
            scan_limit = min(max(quantity * 10, 350), 800)

        if round_num == 1:
            logger.info(
                f"[conv] min target: {quantity} | round 1 scan depth: {scan_limit}"
            )
        else:
            logger.info(
                f"[conv] Round {round_num}: need {needed} more to reach min target, "
                f"scan depth: {scan_limit}"
            )
            await progress(
                f"  🔄 Round {round_num}: *{needed}* more needed to reach minimum target "
                f"of {quantity} — scanning deeper (up to {scan_limit} ads)…"
            )

        # ── Phase 1 + 2-4: scrape → enrich → cluster → source → save (per keyword) ──
        # IMPORTANT: Process and save each keyword's ads immediately before moving to next
        for keyword in keywords:
            if login_wall_hit:
                break
            
            keyword_ads: list[dict] = []
            for country in countries:
                if login_wall_hit:
                    break
                try:
                    _scroll_rounds_override = (context.bot_data or {}).get("scroll_rounds", 0) if context else 0
                    _loop = asyncio.get_event_loop()
                    raw = await asyncio.wait_for(
                        _loop.run_in_executor(
                            _scrape_executor,
                            scrape_ads_sync,
                            keyword, country, media_type, active_status,
                            scan_limit, _scroll_rounds_override,
                        ),
                        timeout=600,
                    )
                    ads_scanned += len(raw)
                    newly_added = 0
                    for ad in raw:
                        key = ad.get("ad_library_url", "") or ad.get("landing_page_url", "")
                        if key and key not in seen_ad_ids:
                            seen_ad_ids.add(key)
                            keyword_ads.append(ad)
                            newly_added += 1
                    resolved = sum(1 for a in raw if a.get("landing_page_url"))
                    await progress(
                        f"  Found *{len(raw)}* ads for '{keyword}' / {country} "
                        f"({resolved} with landing page, *{newly_added}* new)"
                    )
                except LoginWallError:
                    login_wall_hit = True
                    await progress(
                        "🔐 <b>Facebook session expired</b> — login wall detected.\n\n"
                        "The saved auth state is no longer valid.\n\n"
                        "<b>To fix:</b> Refresh your <code>FACEBOOK_COOKIES</code> secret, "
                        "then run <code>python fb_login.py</code> in the Shell to re-seed "
                        "the persistent auth state. Restart the bot afterwards."
                    )
                    break
                except Exception as e:
                    logger.error(
                        f"[conv] scrape_ads error for '{keyword}' / {country}: {e}\n"
                        + traceback.format_exc()
                    )
                    await progress(f"⚠️ Error for '{keyword}' / {country}: {e}")

            if login_wall_hit:
                break

            # ── Process this keyword's ads immediately ──────────────────────
            if not keyword_ads:
                # No new ads for this keyword, skip processing
                continue
            
            # ── Process this keyword's ads immediately before moving to next ──
            round_new_ads = keyword_ads
            consecutive_empty = 0  # found ads for this keyword

            # ── Filter funnel tracking ─────────────────────────────────────
            funnel_start = len(round_new_ads)

            # ── Filter: track ads without landing page URL (but keep them) ──
            # Some ads (video-only, no CTA) may lack landing pages; enrichment
            # will fetch product data from the ad images/text instead.
            no_lp = [a for a in round_new_ads if not (a.get("landing_page_url") or "").strip()]
            
            # ── Filter: reject known junk/non-product landing page domains ─
            junk_lp = [a for a in round_new_ads if (a.get("landing_page_url") or "").strip() and _is_blocked_landing_page(a.get("landing_page_url", ""))]
            round_new_ads = [a for a in round_new_ads if not ((a.get("landing_page_url") or "").strip() and _is_blocked_landing_page(a.get("landing_page_url", "")))]
            if junk_lp:
                logger.info(
                    f"[conv] Rejected {len(junk_lp)} ad(s) with junk landing page domains "
                    f"(e.g. metastatus.com)"
                )

            if not round_new_ads:
                logger.info(f"[conv] '{keyword}': all {funnel_start} ads filtered out — next keyword")
                await progress(
                    f"  ⚠️ '{keyword}': all ads filtered (junk domains, etc.) — next keyword"
                )
                continue

            # ── Phase 2: enrich ───────────────────────────────────────────
            await progress(f"📄 Fetching product data for *{len(round_new_ads)}* ads…")
            try:
                round_new_ads = await asyncio.wait_for(
                    enrich_batch(round_new_ads, concurrency=4), timeout=120
                )
            except asyncio.TimeoutError:
                await progress("  ⚠️ Enrichment timed out after 2 min — continuing with partial results")
                logger.warning("[conv] enrich_batch timed out")

            # Filter: homepage / non-product redirects
            non_product = [a for a in round_new_ads if a.get("_skip_non_product")]
            round_new_ads = [a for a in round_new_ads if not a.get("_skip_non_product")]

            # Filter: collection / category pages
            collection = [a for a in round_new_ads if a.get("_skip_collection")]
            round_new_ads = [a for a in round_new_ads if not a.get("_skip_collection")]

            # ── Phase 2.5: physical product validation ────────────────────
            physical_ads, digital_count, unclear_count = filter_physical_ads(round_new_ads)
            round_new_ads = physical_ads

            # ── Filter funnel report ──────────────────────────────────────
            funnel_parts  = [f"*{funnel_start}* new ads"]
            if no_lp:
                funnel_parts.append(f"*{len(no_lp)}* no landing page")
            if junk_lp:
                funnel_parts.append(f"*{len(junk_lp)}* junk URL")
            if non_product:
                funnel_parts.append(f"*{len(non_product)}* homepage/non-product")
            if collection:
                funnel_parts.append(f"*{len(collection)}* collection page")
            if digital_count:
                funnel_parts.append(f"*{digital_count}* digital")
            if unclear_count:
                funnel_parts.append(f"*{unclear_count}* unclear")
            funnel_parts.append(f"*{len(round_new_ads)}* physical products remain")
            await progress("  🔬 Filter funnel: " + " → ".join(funnel_parts))

            if not round_new_ads:
                logger.info(f"[conv] '{keyword}': no physical ads after filtering — next keyword")
                continue

            # ── Phase 3: cluster ──────────────────────────────────────────
            await progress(f"🧩 Clustering *{len(round_new_ads)}* ads into product groups…")
            round_clusters = build_clusters(round_new_ads, start_sku=start_sku + sku_offset)

            # Filter: clusters with no extractable product URL
            no_url = [c for c in round_clusters if not c.product_urls]
            round_clusters = [c for c in round_clusters if c.product_urls]
            if no_url:
                await progress(f"  ⏭ Skipped *{len(no_url)}* cluster(s) — no product URL")

            # ── Pre-dedup debug preview (top clusters before dedup) ───────
            if round_clusters:
                preview_lines = []
                for _c in round_clusters[:10]:
                    _url = _c.product_urls[0][:60] if _c.product_urls else "NO URL"
                    preview_lines.append(f"  {_c.sku}: {_c.canonical_name[:35] or '(no name)'} | {_url}")
                logger.info(
                    f"[conv] '{keyword}': {len(round_clusters)} clusters before dedup:\n"
                    + "\n".join(preview_lines)
                )

            # ── Dedup against existing sheet rows ─────────────────────────
            round_valid: list = []
            dup_count_this_round = 0
            for cluster in round_clusters:
                is_dup, hard_reason, possible_reason = is_cluster_duplicate(cluster, existing_rows)
                if is_dup:
                    skipped_duplicates.append(
                        f"• *{cluster.canonical_name[:45] or cluster.sku}* — {hard_reason}"
                    )
                    logger.info(
                        f"[conv] HARD DUPLICATE skipped: "
                        f"{cluster.canonical_name[:50] or cluster.sku} ({hard_reason})"
                    )
                    dup_count_this_round += 1
                else:
                    round_valid.append(cluster)
                    sku_offset += 1
                    if possible_reason:
                        possible_duplicates.append(
                            f"• *{cluster.canonical_name[:45] or cluster.sku}* — {possible_reason}"
                        )
                        logger.info(
                            f"[conv] POSSIBLE DUPLICATE saved anyway: "
                            f"{cluster.canonical_name[:50] or cluster.sku} ({possible_reason})"
                        )

            total_after_kw = len(valid_clusters) + len(round_valid)
            if total_after_kw >= quantity:
                target_note = (
                    f" ✅ target {quantity} reached"
                    if total_after_kw == quantity
                    else f" ✅ target {quantity} exceeded (saving all {total_after_kw})"
                )
            else:
                target_note = f" — continuing to reach target of {quantity}"

            await progress(
                f"  📦 '{keyword}': *{len(round_clusters)}* clusters → "
                f"*{dup_count_this_round}* duplicates → "
                f"*{len(round_valid)}* new to save{target_note}"
            )
            logger.info(
                f"[conv] '{keyword}': {len(round_clusters)} clusters, "
                f"{len(round_valid)} valid, {dup_count_this_round} dupes — "
                f"total so far: {total_after_kw}/{quantity}"
            )

            if not round_valid:
                continue

            # ── Phase 3.5 + 4: source & save each product immediately ─────
            await progress(
                f"🔍 *Phase 3.5 → 4:* Sourcing & saving *{len(round_valid)}* product(s) "
                f"one by one…"
            )
            round_saved = 0
            for i, cluster in enumerate(round_valid):
                row = cluster.to_row()
                product_name = cluster.canonical_name or row.get("KEYWORD", "")

                # ── Source ────────────────────────────────────────────────
                try:
                    result = await asyncio.wait_for(
                        get_sourcing_for_cluster(cluster), timeout=90
                    )
                    sourcing_usd, sourcing_url, weight_gram = result
                except (asyncio.TimeoutError, Exception) as _se:
                    logger.warning(
                        f"[conv] sourcing failed/timed-out for '{product_name[:40]}': {_se}"
                    )
                    sourcing_usd, sourcing_url, weight_gram = "", "", ""

                has_variants = (
                    "YES"
                    if any(ad.get("has_variants") == "YES" for ad in cluster.ads)
                    else "NO"
                )

                row["SOURCING PRICE USD"] = sourcing_usd
                row["SOURCING URL"]       = sourcing_url
                row["WEIGHT GRAM"]        = weight_gram
                row["HAS VARIANTS"]       = has_variants

                sourcing_display = f"${sourcing_usd}" if sourcing_usd else "not found"
                weight_display   = f"{weight_gram}g"  if weight_gram  else "—"

                # ── Save immediately to sheet ──────────────────────────────
                save_status = ""
                try:
                    saved = append_cluster_rows([row])
                    total_saved += saved
                    round_saved += saved
                    save_status = "✅ saved"
                except Exception as e:
                    err = str(e)
                    if "drive.googleapis.com" in err or "Drive API" in err.lower():
                        save_status = "⚠️ Drive API not enabled"
                    elif "sheets.googleapis.com" in err or "Sheets API" in err.lower():
                        save_status = "⚠️ Sheets API not enabled"
                    else:
                        save_status = f"⚠️ sheet error: {err[:80]}"
                    logger.warning(f"[conv] sheet write failed for '{product_name[:40]}': {err[:120]}")
                    try:
                        last_csv_path = save_csv_backup([row])
                    except Exception:
                        pass

                await progress(
                    f"  [{i+1}/{len(round_valid)}] *{product_name[:40]}*\n"
                    f"    Sourcing: {sourcing_display} | Weight: {weight_display} | "
                    f"Variants: {has_variants} | {save_status}"
                )

            # Accumulate into valid_clusters for overall count tracking
            valid_clusters.extend(round_valid)
            await progress(
                f"  ✅ '{keyword}' done — "
                f"*{len(valid_clusters)}* / {quantity} valid products so far"
            )
        # end for keyword

        if login_wall_hit:
            break

        # If no keyword in this entire round produced any new ads, increment empty counter
        round_had_new_ads = consecutive_empty == 0
        if not round_had_new_ads:
            consecutive_empty += 1
            logger.info(
                f"[conv] Round {round_num}: no new ads from any keyword "
                f"({consecutive_empty}/2 consecutive empty rounds)"
            )
            if consecutive_empty >= 2:
                await progress("  ⚠️ No new ads found in 2 consecutive rounds — stopping.")
                break
            await progress(
                f"  ⚠️ Round {round_num}: no new ads yet — scanning deeper next round…"
            )

    # ── End of loop — final summary ────────────────────────────────────────
    if login_wall_hit:
        return

    dedup_msg = ""
    if skipped_duplicates:
        dedup_msg = (
            f"\n  ⏭ Skipped *{len(skipped_duplicates)}* exact duplicate(s) already in your sheet:\n"
            + "\n".join(skipped_duplicates[:5])
            + ("\n  …and more" if len(skipped_duplicates) > 5 else "")
        )
    if possible_duplicates:
        dedup_msg += (
            f"\n  ⚠️ *{len(possible_duplicates)}* possible duplicate(s) "
            f"(saved anyway — verify manually):\n"
            + "\n".join(possible_duplicates[:3])
            + ("\n  …and more" if len(possible_duplicates) > 3 else "")
        )

    shortfall_msg = ""
    if len(valid_clusters) < quantity:
        if consecutive_empty >= 2:
            reason = (
                "Meta Ads Library returned no more unseen ads for these keywords/countries "
                "after 2 consecutive attempts — try adding more keywords or countries"
            )
        elif round_num >= MAX_ROUNDS:
            reason = (
                f"reached the {MAX_ROUNDS}-round search limit — add more keywords/countries "
                "or request a smaller quantity"
            )
        else:
            reason = (
                "most ads were filtered out (no landing page, collection page, digital product, "
                "or already in your sheet)"
            )
        shortfall_msg = (
            f"\n  ⚠️ Could only find *{len(valid_clusters)}* / {quantity} minimum target "
            f"({reason})."
        )

    if not valid_clusters:
        if consecutive_empty >= 2:
            await progress(
                "⚠️ No products found — Meta returned no new ads for these keywords/countries.\n"
                "Try adding more keywords or different countries."
            )
        elif skipped_duplicates:
            dup_detail = "\n".join(skipped_duplicates[:5])
            if len(skipped_duplicates) > 5:
                dup_detail += f"\n  …and {len(skipped_duplicates) - 5} more"
            await progress(
                f"⚠️ All *{len(skipped_duplicates)}* discovered cluster(s) were exact "
                f"URL matches already in your sheet — nothing new to add.\n\n"
                f"*Skipped:*\n{dup_detail}\n\n"
                f"Try different keywords or countries to find new products."
            )
        else:
            # Ads were found and clustered but filtered out before reaching dedup
            await progress(
                "⚠️ No new product clusters passed all filters — nothing saved.\n"
                "Possible reasons: no product URLs found, all ads were collection/homepage "
                "pages, or all products were digital.\n"
                "Try different keywords or relax your filters."
            )
        return

    if total_saved:
        sheet_status = "✅ Sheet updated successfully"
    elif last_csv_path:
        sheet_status = "⚠️ Sheet unavailable — CSV backup saved"
    else:
        sheet_status = "⚠️ Nothing written"

    preview_lines = []
    for c in valid_clusters[:5]:
        has_url = "✓" if c.product_urls else "✗"
        has_creative = "✓" if c.media_urls else "✗"
        preview_lines.append(f"  *{c.sku}* — URL:{has_url} Creative:{has_creative}")
    preview = "\n".join(preview_lines)
    if len(valid_clusters) > 5:
        preview += f"\n  …and {len(valid_clusters) - 5} more"

    final_msg = (
        "🎉 *Extraction finished!*\n\n"
        f"• Ads scanned: *{ads_scanned}*\n"
        f"• Product clusters found: *{len(valid_clusters)}*\n"
        f"• Clusters saved: *{total_saved}*\n"
        f"• Sheet status: {sheet_status}"
        f"{dedup_msg}{shortfall_msg}\n\n"
        f"*Clusters:*\n{preview}"
    )
    await progress(final_msg)

    if last_csv_path and not total_saved:
        try:
            if os.path.exists(last_csv_path):
                with open(last_csv_path, "rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=os.path.basename(last_csv_path),
                        caption=(
                            f"📊 {len(valid_clusters)} product cluster(s) — "
                            "CSV backup (import to Google Sheets manually)"
                        ),
                    )
        except Exception as e:
            logger.warning(f"Could not send CSV file: {e}")


# ── Cancel & fallback ─────────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    # Check both local user_data and the global registry (covers scheduler-triggered runs)
    task: asyncio.Task = context.user_data.get("_extraction_task") or task_registry.get(chat_id)

    if task and not task.done():
        task.cancel()
        task_registry.unregister(chat_id)
        await update.message.reply_text(
            "⛔ Stopping extraction… any products already saved to the sheet are kept.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text(
            "No active extraction to cancel. Send /extract to start one.",
            reply_markup=ReplyKeyboardRemove(),
        )
    context.user_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    """Build and return the ConversationHandler for /extract."""
    return ConversationHandler(
        entry_points=[CommandHandler("extract", cmd_extract)],
        states={
            ASK_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_quantity)],
            ASK_KEYWORD_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_keyword_mode)],
            ASK_KEYWORD_SELECTION: [
                CallbackQueryHandler(cb_keyword_toggle,  pattern="^kw_toggle:"),
                CallbackQueryHandler(cb_keyword_confirm, pattern="^kw_confirm$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_keyword_selection),
            ],
            ASK_MANUAL_KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_manual_keywords)],
            ASK_COUNTRIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_countries)],
            ASK_MEDIA_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_media_type)],
            ASK_ACTIVE_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_active_status)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
