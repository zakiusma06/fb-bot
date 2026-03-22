"""
bot.py - Telegram bot setup and command registration.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, FACEBOOK_COOKIES
from conversation import build_conversation_handler, cmd_cancel, cmd_extract
import task_registry

logger = logging.getLogger(__name__)

# ── Trigger / results file paths (shared with scheduler_bot.py) ───────────
_BASE            = Path(__file__).parent
TRIGGER_FILE     = _BASE / "research_trigger.json"
RESULTS_FILE     = _BASE / "research_results.json"
PROGRESS_FILE    = _BASE / "research_progress.jsonl"
_LOCK_FILE       = _BASE / ".research_running"

# ── Persistent main menu shown after /start ────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🔍 Extract Products", "📊 Status"],
        ["🔧 Setup",            "📖 Help"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Text labels for main menu buttons ─────────────────────────────────────
_BTN_EXTRACT = "🔍 Extract Products"
_BTN_STATUS  = "📊 Status"
_BTN_SETUP   = "🔧 Setup"
_BTN_HELP    = "📖 Help"


# ── Trigger-file monitor (called by scheduler_bot) ────────────────────────

async def _trigger_monitor_loop(application: Application) -> None:
    """
    Background coroutine that watches for research_trigger.json.
    When found, runs _do_extraction per-keyword and writes research_results.json.
    This is the ONLY place where extraction is executed for scheduled runs.
    """
    from conversation import _do_extraction

    while True:
        await asyncio.sleep(10)

        if _LOCK_FILE.exists():
            continue

        if not TRIGGER_FILE.exists():
            continue

        # ── Atomically claim the trigger ──────────────────────────────────
        try:
            payload = json.loads(TRIGGER_FILE.read_text())
            TRIGGER_FILE.unlink()
        except Exception as exc:
            logger.warning(f"[trigger] Could not read/delete trigger file: {exc}")
            await asyncio.sleep(30)
            continue

        # ── Mark as running ───────────────────────────────────────────────
        _LOCK_FILE.write_text(datetime.now().isoformat())
        PROGRESS_FILE.write_text("")
        # Clear any stale results from a previous run
        if RESULTS_FILE.exists():
            RESULTS_FILE.unlink()

        keyword_targets = payload.get("keywords", [])
        params          = payload.get("parameters", {})
        countries       = params.get("countries", ["FR"])
        media_type      = params.get("media_type", "both")
        active_status   = params.get("active_status", "active")
        triggered_at    = payload.get("triggered_at", datetime.now().isoformat())
        chat_id         = payload.get("chat_id")

        logger.info(
            f"[trigger] Research triggered — "
            f"keywords={[k['term'] for k in keyword_targets]}, "
            f"countries={countries}, media={media_type}, status={active_status}"
        )

        per_kw_results: dict = {}
        start_ts = time.time()

        # ── Notify the user directly from this bot that research has started ──
        if chat_id:
            try:
                kw_names = ", ".join(k["term"] for k in keyword_targets)
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔍 *Research Bot started extraction*\n\n"
                        f"*Keywords:* {kw_names}\n"
                        f"*Total target:* {sum(k['ads_target'] for k in keyword_targets)} ads\n\n"
                        f"I'll send you live progress updates and notify you when done.\n"
                        f"Send /cancel at any time to stop."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"[trigger] Failed to send startup message to chat_id={chat_id}: {e}")

        async def _run_trigger_keywords():
            for kw_cfg in keyword_targets:
                term   = kw_cfg["term"]
                target = kw_cfg["ads_target"]

                def _make_progress(t: str = term, cid: int = chat_id):
                    async def _progress(msg: str) -> None:
                        if not cid:
                            return
                        try:
                            await application.bot.send_message(
                                chat_id=cid,
                                text=f"[{t}] {msg}",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.warning(f"[trigger] send_message to {cid} failed: {e}")
                    return _progress

                logger.info(f"[trigger] Extracting '{term}' — target {target}")
                try:
                    await _do_extraction(
                        None, None,
                        target, [term], countries,
                        media_type, active_status,
                        chat_id, application.bot, _make_progress(),
                    )
                    per_kw_results[term] = {"target": target, "status": "ok"}
                except asyncio.CancelledError:
                    logger.info(f"[trigger] Extraction cancelled by user for keyword '{term}'")
                    per_kw_results[term] = {"target": target, "status": "cancelled"}
                    raise
                except Exception as exc:
                    logger.exception(f"[trigger] Extraction failed for '{term}': {exc}")
                    per_kw_results[term] = {"target": target, "status": "error", "error": str(exc)}

        extraction_task = asyncio.create_task(_run_trigger_keywords())
        if chat_id:
            task_registry.register(chat_id, extraction_task)

        cancelled = False
        try:
            await extraction_task
        except asyncio.CancelledError:
            cancelled = True
            logger.info("[trigger] Trigger extraction cancelled by user")
            if chat_id:
                try:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text="⛔ *Extraction stopped.* Products saved so far are in your sheet.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        finally:
            if chat_id:
                task_registry.unregister(chat_id)

        elapsed = int(time.time() - start_ts)
        any_failed = any(v.get("status") == "error" for v in per_kw_results.values())

        results = {
            "status":          "partial_failure" if any_failed else "completed",
            "triggered_at":    triggered_at,
            "completed_at":    datetime.now().isoformat(),
            "elapsed_seconds": elapsed,
            "per_keyword":     per_kw_results,
            "parameters": {
                "countries":     countries,
                "media_type":    media_type,
                "active_status": active_status,
            },
        }

        try:
            RESULTS_FILE.write_text(json.dumps(results, indent=2))
            logger.info(f"[trigger] Research complete in {elapsed}s — results written")
        except Exception as exc:
            logger.error(f"[trigger] Could not write results file: {exc}")

        # ── Send final summary directly to the user from the Research Bot ──
        if chat_id:
            try:
                elapsed_str = f"{elapsed // 60} min {elapsed % 60} sec"
                kw_summary  = "\n".join(
                    f"  • {term}: {v.get('status', '?')}"
                    for term, v in per_kw_results.items()
                )
                status_icon = "✅" if not any_failed else "⚠️"
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{status_icon} *Research complete!* ({elapsed_str})\n\n"
                        f"*Results:*\n{kw_summary}\n\n"
                        f"New products have been saved to the Google Sheet for review."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        # ── Always remove the lock ─────────────────────────────────────────
        try:
            _LOCK_FILE.unlink()
        except Exception:
            pass


# ── Register clickable slash-command menu with Telegram ───────────────────
async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([
            BotCommand("start",        "Start the bot / show menu"),
            BotCommand("extract",      "Start a new extraction"),
            BotCommand("status",       "Check bot status"),
            BotCommand("scrollrounds", "Set manual scroll rounds (0 = auto)"),
            BotCommand("setup",        "Facebook cookie setup guide"),
            BotCommand("setcookies",   "Update Facebook session cookies"),
            BotCommand("help",         "How to use this bot"),
            BotCommand("cancel",       "Cancel current operation"),
        ])
        logger.info("[bot] Telegram command menu registered")
    except Exception as e:
        logger.warning(f"[bot] Could not register command menu (non-fatal): {e}")

    # Start the trigger-file monitor in the background
    asyncio.create_task(_trigger_monitor_loop(application))
    logger.info("[bot] Research trigger monitor started")


# ── Simple commands ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb_ok = "✅" if FACEBOOK_COOKIES else "❌"
    await update.message.reply_text(
        "👋 Welcome to the *Meta Ads Research Bot*!\n\n"
        "I extract unique products from Meta Ads Library and save them to Google Sheets.\n\n"
        "*Setup status:*\n"
        f"{fb_ok} Facebook cookies — "
        f"{'configured' if FACEBOOK_COOKIES else 'missing — tap 🔧 Setup'}\n\n"
        "Use the menu below or the commands list (☰ button) to navigate.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )

async def cmd_setcookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_cookies"] = True
    await update.message.reply_text("🍪 <b>Update Facebook Cookies</b>\n\nPaste your cookies JSON array (from Cookie-Editor) and send it.", parse_mode="HTML")



async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use this bot*\n\n"
        "1. Tap *🔧 Setup* (one-time: add your Facebook cookies)\n"
        "2. Tap *🔍 Extract Products* to start the wizard\n"
        "3. Choose how many products you want\n"
        "4. Pick keywords (suggestions from your data or manual)\n"
        "5. Select countries, media type, and active status\n"
        "6. Confirm — the bot scrapes Meta Ads Library!\n"
        "7. Results are clustered and saved to your Google Sheet\n\n"
        "The bot skips duplicate products automatically.\n\n"
        "Send /cancel at any time to stop the current extraction.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies_status = (
        "✅ *Facebook cookies are already configured!*\n\nYou can start an extraction now."
        if FACEBOOK_COOKIES
        else "❌ *Facebook cookies are NOT set yet.*"
    )
    await update.message.reply_text(
        f"🔧 *Setup Guide — Facebook Cookies*\n\n"
        f"{cookies_status}\n\n"
        "Meta Ads Library requires you to be logged in to search commercial ads. "
        "The bot uses your browser session cookies to authenticate.\n\n"
        "*Step-by-step instructions:*\n\n"
        "1️⃣ Open Chrome/Firefox → `https://www.facebook.com/ads/library`\n\n"
        "2️⃣ Make sure you're *logged in* to Facebook\n\n"
        "3️⃣ Press *F12* → *Network* tab → Refresh (F5)\n\n"
        "4️⃣ Click any request to `www.facebook.com`\n\n"
        "5️⃣ Find the `cookie:` header in Request Headers — copy the full value\n\n"
        "6️⃣ In Replit go to *Secrets* (🔒) → add:\n"
        "   Key: `FACEBOOK_COOKIES`\n"
        "   Value: paste the cookie string\n\n"
        "7️⃣ Restart the bot workflow → tap /start to verify\n\n"
        "⚠️ Cookies expire after ~90 days. Repeat if scraping stops working.",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def cmd_scrollrounds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.bot_data.get("scroll_rounds", 0)
    auto_note = "auto" if current == 0 else str(current)
    context.user_data["awaiting_scroll_rounds"] = True
    await update.message.reply_text(
        f"🔄 *Scroll Rounds*\n\n"
        f"Current setting: *{auto_note}*\n\n"
        f"Enter the number of scroll rounds per keyword:\n"
        f"_(type `0` to reset to auto mode)_",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    running = context.user_data.get("running", False)
    fb_ok   = "✅ Configured" if FACEBOOK_COOKIES else "❌ Missing (tap 🔧 Setup)"
    status  = "🔄 Extraction in progress…" if running else "✅ Ready"
    await update.message.reply_text(
        f"*Bot Status*\n\n"
        f"User: {user.first_name}\n"
        f"Extraction: {status}\n"
        f"Facebook cookies: {fb_ok}",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# ── Main menu button handler ───────────────────────────────────────────────
# Catches persistent reply-keyboard button presses when NOT inside a
# ConversationHandler state (the ConversationHandler takes priority while
# a conversation is active).
    if context.user_data.get("awaiting_cookies"):
        import json as _json, pathlib as _pl
        accumulated = context.user_data.get("pending_cookies_text", "") + text
        context.user_data["pending_cookies_text"] = accumulated
        try:
            raw = _json.loads(accumulated)
            if not isinstance(raw, list): raise ValueError("Expected a JSON array")
            smap = {"no_restriction":"None","lax":"Lax","strict":"Strict"}
            pw = []
            for c in raw:
                if c.get("domain","").lstrip(".") not in ("facebook.com","www.facebook.com"): continue
                ss = smap.get((c.get("sameSite") or "no_restriction").lower(),"None")
                ck = {"name":c["name"],"value":c["value"],"domain":c["domain"],"path":c.get("path","/"),"secure":c.get("secure",True),"httpOnly":c.get("httpOnly",False),"sameSite":ss}
                if c.get("expirationDate"): ck["expires"]=int(c["expirationDate"])
                pw.append(ck)
            if not pw: raise ValueError("No facebook.com cookies found")
            sp = _pl.Path(__file__).parent / "fb_auth_state.json"
            sp.write_text(_json.dumps({"cookies":pw,"origins":[]},indent=2))
            context.user_data.pop("awaiting_cookies")
            context.user_data.pop("pending_cookies_text", None)
            await update.message.reply_text(f"✅ {len(pw)} cookies saved. New session active.")
        except _json.JSONDecodeError:
            await update.message.reply_text("📨 Got part 1, send the rest…")
        except Exception as e:
            context.user_data.pop("awaiting_cookies")
            context.user_data.pop("pending_cookies_text", None)
            await update.message.reply_text(f"❌ Failed: {e}")
        return


async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # ── Capture scroll rounds reply ────────────────────────────────────────
    if context.user_data.get("awaiting_scroll_rounds"):
        context.user_data.pop("awaiting_scroll_rounds")
        try:
            value = int(text)
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid number. Please send `/scrollrounds` again and enter a valid number.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
            return
        context.bot_data["scroll_rounds"] = value
        if value == 0:
            msg = "✅ *Confirmed!* Scroll rounds reset to *auto* mode (scan depth ÷ 20)."
        else:
            secs = value * 1.5
            msg = (
                f"✅ *Confirmed!* Scroll rounds set to *{value}* per keyword "
                f"(~{secs:.0f}s scroll time per keyword).\n\n"
                f"This applies to all future extractions until you change it."
            )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=MAIN_MENU)
        return

    if text == _BTN_EXTRACT:
        await cmd_extract(update, context)
    elif text == _BTN_STATUS:
        await cmd_status(update, context)
    elif text == _BTN_SETUP:
        await cmd_setup(update, context)
    elif text == _BTN_HELP:
        await cmd_help(update, context)


# ── Application builder ────────────────────────────────────────────────────

def build_application() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # ConversationHandler must be first so it takes priority during a session
    app.add_handler(build_conversation_handler())

    # Cookie handler runs in group -1 (before ConversationHandler) so it always works
    async def _cookie_intercept(update, context):
        if not context.user_data.get("awaiting_cookies"):
            return
        import json as _json, pathlib as _pl
        text = (update.message.text or "").strip()
        accumulated = context.user_data.get("pending_cookies_text", "") + text
        context.user_data["pending_cookies_text"] = accumulated
        try:
            raw = _json.loads(accumulated)
            if not isinstance(raw, list): raise ValueError("Expected a JSON array")
            smap = {"no_restriction":"None","lax":"Lax","strict":"Strict"}
            pw = []
            for c in raw:
                if c.get("domain","").lstrip(".") not in ("facebook.com","www.facebook.com"): continue
                ss = smap.get((c.get("sameSite") or "no_restriction").lower(),"None")
                ck = {"name":c["name"],"value":c["value"],"domain":c["domain"],"path":c.get("path","/"),"secure":c.get("secure",True),"httpOnly":c.get("httpOnly",False),"sameSite":ss}
                if c.get("expirationDate"): ck["expires"]=int(c["expirationDate"])
                pw.append(ck)
            if not pw: raise ValueError("No facebook.com cookies found")
            sp = _pl.Path(__file__).parent / "fb_auth_state.json"
            sp.write_text(_json.dumps({"cookies":pw,"origins":[]},indent=2))
            context.user_data.pop("awaiting_cookies")
            context.user_data.pop("pending_cookies_text", None)
            await update.message.reply_text(f"✅ {len(pw)} cookies saved. New session active.")
        except _json.JSONDecodeError:
            await update.message.reply_text("📨 Got part 1, send the rest…")
        except Exception as e:
            context.user_data.pop("awaiting_cookies", None)
            context.user_data.pop("pending_cookies_text", None)
            await update.message.reply_text(f"❌ Failed: {e}")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _cookie_intercept), group=-1)

    # Simple commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("setup",        cmd_setup))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("scrollrounds", cmd_scrollrounds))
    app.add_handler(CommandHandler("cancel",       cmd_cancel))
    app.add_handler(CommandHandler("setcookies",    cmd_setcookies))

    # Menu button presses (only fires when NOT in an active conversation)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_main_menu,
        )
    )

    return app
