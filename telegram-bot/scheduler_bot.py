"""
scheduler_bot.py - Daily research scheduler bot.

Collects tomorrow's research keywords at a configurable time (default 21:00),
then automatically triggers the extraction pipeline at another configurable
time (default 09:00) and sends a final report.

Timezone setup (/timezone) lets the user pick their timezone via phone
location, a list, or manual entry — then guides through setting reminder
and research times in the same flow.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from timezonefinder import TimezoneFinder

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

sys.path.insert(0, os.path.dirname(__file__))

# ── Shared trigger/results file paths (must match bot.py) ─────────────────
_BASE_DIR     = Path(os.path.dirname(__file__))
TRIGGER_FILE  = _BASE_DIR / "research_trigger.json"
RESULTS_FILE  = _BASE_DIR / "research_results.json"
PROGRESS_FILE = _BASE_DIR / "research_progress.jsonl"
_LOCK_FILE    = _BASE_DIR / ".research_running"

logger = logging.getLogger(__name__)

# ── Config paths ──────────────────────────────────────────────────────────────
BASE_DIR              = Path(__file__).parent
SCHEDULER_CONFIG_FILE = BASE_DIR / "scheduler_config.json"
DAILY_CONFIG_FILE     = BASE_DIR / "daily_config.json"

DEFAULT_SCHED_CFG: dict = {
    "timezone":            "Asia/Dubai",
    "keyword_request_time": "21:00",
    "research_run_time":    "09:00",
    "countries":           ["FR"],
    "media_type":          "both",
    "active_status":       "active",
    "chat_id":             None,
}

# ── Common timezone list shown in option 2 ────────────────────────────────────
COMMON_TIMEZONES: list[str] = [
    "Africa/Abidjan",   "Africa/Accra",     "Africa/Cairo",
    "Africa/Lagos",     "Africa/Nairobi",   "Africa/Tunis",
    "America/New_York", "America/Chicago",  "America/Los_Angeles",
    "America/Toronto",  "America/Sao_Paulo",
    "Asia/Dubai",       "Asia/Kolkata",     "Asia/Karachi",
    "Asia/Jakarta",     "Asia/Makassar",    "Asia/Bangkok",
    "Asia/Ho_Chi_Minh", "Asia/Shanghai",    "Asia/Tokyo",
    "Asia/Seoul",       "Asia/Riyadh",      "Asia/Singapore",
    "Europe/London",    "Europe/Paris",     "Europe/Berlin",
    "Europe/Moscow",    "Europe/Istanbul",
    "Pacific/Auckland", "Australia/Sydney",
]

# ── In-memory state ───────────────────────────────────────────────────────────
_g: dict = {
    # keyword / ads collection flow
    "expecting":         None,   # "keywords" | "total_ads" | tz states | None
    "reminder_count":    0,
    "pending_keywords":  [],
    "research_running":  False,
    # timezone setup flow
    "temp_timezone":     None,   # holds tz string while walking through the sub-flow
}

# ── Timezone sub-flow state labels (stored in _g["expecting"]) ────────────────
TZ_CHOICE    = "tz_choice"     # waiting for one of the 3 option buttons
TZ_LIST      = "tz_list"       # waiting for selection from timezone list
TZ_MANUAL    = "tz_manual"     # waiting for free-text IANA timezone
TZ_REMINDER  = "tz_reminder"   # waiting for reminder time HH:MM
TZ_RESEARCH  = "tz_research"   # waiting for research time HH:MM

# ── Parameters sub-flow state labels ──────────────────────────────────────────
PARAM_COUNTRIES = "param_countries"   # waiting for free-text country list


# ── Config helpers ────────────────────────────────────────────────────────────

def load_sched_cfg() -> dict:
    if SCHEDULER_CONFIG_FILE.exists():
        cfg = json.loads(SCHEDULER_CONFIG_FILE.read_text())
        for k, v in DEFAULT_SCHED_CFG.items():
            cfg.setdefault(k, v)
        return cfg
    cfg = dict(DEFAULT_SCHED_CFG)
    save_sched_cfg(cfg)
    return cfg


def save_sched_cfg(cfg: dict) -> None:
    SCHEDULER_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def load_daily_cfg() -> dict | None:
    if DAILY_CONFIG_FILE.exists():
        return json.loads(DAILY_CONFIG_FILE.read_text())
    return None


def save_daily_cfg(data: dict) -> None:
    DAILY_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Keyword format helpers ────────────────────────────────────────────────────
# daily_config.json now stores keywords as a list of dicts:
#   {"term": "kw", "ads_target": N}
# Old format was a flat list of strings. Both are supported for reading.

def _get_keyword_terms(daily: dict) -> list[str]:
    """Return flat list of keyword strings from daily config (handles both formats)."""
    kws = daily.get("keywords", [])
    if not kws:
        return []
    if isinstance(kws[0], dict):
        return [k["term"] for k in kws]
    return list(kws)


def _get_keyword_targets(daily: dict) -> list[dict]:
    """Return list of {term, ads_target} from daily config (handles both formats)."""
    kws  = daily.get("keywords", [])
    total = daily.get("total_ads_target", 0)
    if not kws:
        return []
    if isinstance(kws[0], dict):
        return kws
    # Old flat format — distribute evenly
    n    = len(kws)
    base = total // n if n else 0
    rem  = total % n  if n else 0
    return [
        {"term": kw, "ads_target": base + (1 if i < rem else 0)}
        for i, kw in enumerate(kws)
    ]


def _distribute_targets(keywords: list[str], total: int) -> list[dict]:
    """Distribute total ads evenly across keywords, spreading remainder fairly."""
    n    = len(keywords)
    if n == 0:
        return []
    base = total // n
    rem  = total % n
    return [
        {"term": kw, "ads_target": base + (1 if i < rem else 0)}
        for i, kw in enumerate(keywords)
    ]


# ── Scheduler live-update helper ──────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def _reschedule_jobs(cfg: dict) -> None:
    """Update APScheduler jobs in-place when timezone / times change."""
    if _scheduler is None or not _scheduler.running:
        return
    try:
        tz     = pytz.timezone(cfg["timezone"])
        kw_h,  kw_m  = [int(x) for x in cfg["keyword_request_time"].split(":")]
        res_h, res_m = [int(x) for x in cfg["research_run_time"].split(":")]

        _scheduler.reschedule_job(
            "ask_keywords",
            trigger=CronTrigger(hour=kw_h, minute=kw_m, timezone=tz),
        )
        _scheduler.reschedule_job(
            "run_research",
            trigger=CronTrigger(hour=res_h, minute=res_m, timezone=tz),
        )
        logger.info(
            f"[scheduler] Jobs rescheduled — "
            f"keywords={cfg['keyword_request_time']}, "
            f"research={cfg['research_run_time']} ({cfg['timezone']})"
        )
    except Exception as e:
        logger.warning(f"[scheduler] Could not reschedule jobs: {e}")


# ── Validation helpers ────────────────────────────────────────────────────────

def _valid_iana_tz(name: str) -> bool:
    try:
        pytz.timezone(name)
        return True
    except pytz.exceptions.UnknownTimeZoneError:
        return False


def _valid_hhmm(text: str) -> bool:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text.strip())
    return bool(m)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    cfg = load_sched_cfg()
    cfg["chat_id"] = chat_id
    save_sched_cfg(cfg)
    logger.info(f"[scheduler] /start registered chat_id={chat_id}")
    await update.message.reply_text(
        "✅ *Scheduler Bot is active!*\n\n"
        f"📅 Keyword request: *{cfg['keyword_request_time']}* ({cfg['timezone']})\n"
        f"🚀 Research run:    *{cfg['research_run_time']}* ({cfg['timezone']})\n"
        f"🌍 Countries: *{', '.join(cfg['countries'])}*\n\n"
        "Every day at the keyword request time I will ask you for tomorrow's "
        "keywords. At the research run time I will start extraction automatically.\n\n"
        "*Commands:*\n"
        "/timezone — set timezone and schedule times\n"
        "/settings — show current timezone and schedule\n"
        "/parameters — configure research parameters\n"
        "/status — show current config and daily keywords\n"
        "/setkeywords — enter keywords manually now\n"
        "/settarget — set ads target manually\n"
        "/runresearch — trigger research immediately\n"
        "/cancel — cancel current input",
        parse_mode="Markdown",
    )


# ── /settings ─────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_sched_cfg()
    await update.message.reply_text(
        "*⚙️ Current Settings*\n\n"
        f"Timezone: `{cfg['timezone']}`\n"
        f"Reminder time: `{cfg['keyword_request_time']}`\n"
        f"Research time: `{cfg['research_run_time']}`",
        parse_mode="Markdown",
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg   = load_sched_cfg()
    daily = load_daily_cfg()

    lines = [
        "*⚙️ Scheduler Config*",
        f"• Timezone: `{cfg['timezone']}`",
        f"• Keyword request time: `{cfg['keyword_request_time']}`",
        f"• Research run time:    `{cfg['research_run_time']}`",
        f"• Countries: `{', '.join(cfg['countries'])}`",
        f"• Media type: `{cfg['media_type']}`",
        f"• Active status: `{cfg['active_status']}`",
    ]

    if daily:
        targets = _get_keyword_targets(daily)
        kw_lines = "\n".join(
            f"  • {t['term']} → {t['ads_target']} ads" for t in targets
        ) if targets else "  _none_"
        lines += [
            "",
            f"*📋 Daily Config ({daily.get('date', '?')})*",
            f"• Total ads target: {daily.get('total_ads_target', '?')}",
            f"• Keywords:\n{kw_lines}",
        ]
    else:
        lines.append("\n_No daily config saved yet._")

    if _g.get("research_running"):
        lines.append("\n⏳ *Research is currently running.*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /cancel ───────────────────────────────────────────────────────────────────

def _reset_state() -> None:
    """Clear all in-progress input state."""
    _g["expecting"]        = None
    _g["pending_keywords"] = []
    _g["temp_timezone"]    = None
    _g.pop("temp_reminder", None)
    _g.pop("param_message_id", None)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_state()
    await update.message.reply_text(
        "❌ Input cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Shared cancel-button row ──────────────────────────────────────────────────

_CANCEL_ROW = [KeyboardButton("❌ Cancel")]


def _is_cancel(text: str) -> bool:
    return text.strip().lower().lstrip("❌ ").startswith("cancel")


# ── /timezone — entry point ───────────────────────────────────────────────────

async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _g["expecting"]     = TZ_CHOICE
    _g["temp_timezone"] = None

    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Use my phone location", request_location=True)],
            [KeyboardButton("📋 Choose from list")],
            [KeyboardButton("✏️ Enter manually")],
            _CANCEL_ROW,
        ],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "🌍 *Timezone setup*\n\nHow do you want to set your timezone?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Location handler (option 1) ───────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _g.get("expecting") not in (TZ_CHOICE, None):
        # Only handle location when we're in the timezone setup flow
        if _g.get("expecting") != TZ_CHOICE:
            return

    loc = update.message.location
    if not loc:
        return

    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=loc.latitude, lng=loc.longitude)

    if not tz_name:
        await update.message.reply_text(
            "⚠️ Could not detect timezone from your location. "
            "Please try /timezone and choose a different option.",
            reply_markup=ReplyKeyboardRemove(),
        )
        _g["expecting"] = None
        return

    _g["temp_timezone"] = tz_name
    _g["expecting"]     = TZ_REMINDER

    await update.message.reply_text(
        f"✅ Timezone detected: *{tz_name}*\n\n"
        "Now send the *daily reminder time* in HH:MM format (24-hour).\n"
        "_Example: 21:30_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── /setkeywords ──────────────────────────────────────────────────────────────

async def cmd_setkeywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _g["expecting"] = "keywords"
    await update.message.reply_text(
        "📝 Send tomorrow's research keywords separated by commas.\n\n"
        "_Example: portable blender, car vacuum, posture corrector_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── /settarget ────────────────────────────────────────────────────────────────

async def cmd_settarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    daily = load_daily_cfg()
    if not daily or not daily.get("keywords"):
        await update.message.reply_text("⚠️ No keywords set yet. Use /setkeywords first.")
        return
    _g["expecting"]        = "total_ads"
    _g["pending_keywords"] = _get_keyword_terms(daily)
    await update.message.reply_text("🔢 How many total ads do you want to extract?")


# ── /runresearch ──────────────────────────────────────────────────────────────

async def cmd_runresearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    daily = load_daily_cfg()
    if not daily or not daily.get("keywords"):
        await update.message.reply_text(
            "⚠️ No daily config found. Set keywords with /setkeywords first."
        )
        return
    if _g.get("research_running"):
        await update.message.reply_text("⚠️ Research is already running.")
        return
    chat_id = update.effective_chat.id
    targets = _get_keyword_targets(daily)
    kw_lines = "\n".join(f"  • {t['term']} → {t['ads_target']} ads" for t in targets)
    await update.message.reply_text(
        f"🚀 Starting research now...\n\n"
        f"Keywords:\n{kw_lines}\n\n"
        f"Total target: {daily['total_ads_target']} ads"
    )
    asyncio.create_task(_run_research(context.bot, chat_id))


# ── Main text router ──────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text      = update.message.text.strip()
    expecting = _g.get("expecting")

    # ── Global cancel intercept — works from any state ─────────────────────────
    # Catches "❌ Cancel" button, plain "cancel", "/cancel" typed as text, etc.
    if _is_cancel(text) and expecting is not None:
        await cmd_cancel(update, context)
        return

    # ── Timezone sub-flow ──────────────────────────────────────────────────────
    if expecting == TZ_CHOICE:
        await _handle_tz_choice(update, text)

    elif expecting == TZ_LIST:
        await _handle_tz_list_selection(update, text)

    elif expecting == TZ_MANUAL:
        await _handle_tz_manual(update, text)

    elif expecting == TZ_REMINDER:
        await _handle_tz_reminder(update, text)

    elif expecting == TZ_RESEARCH:
        await _handle_tz_research(update, text)

    # ── Keyword / ads flow ─────────────────────────────────────────────────────
    elif expecting == "keywords":
        await _handle_keywords_input(update, text)

    elif expecting == "total_ads":
        await _handle_total_ads_input(update, text)

    elif expecting == PARAM_COUNTRIES:
        await _handle_param_countries_input(update, text)

    # else: unknown state — silently ignore


# ── Timezone step 1: handle choice buttons ────────────────────────────────────

async def _handle_tz_choice(update: Update, text: str) -> None:
    if "location" in text.lower() or "phone" in text.lower():
        # User tapped the button but location sharing didn't fire
        # (shouldn't happen, but handle gracefully)
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Share location", request_location=True)]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "Please tap the button below to share your location.",
            reply_markup=keyboard,
        )

    elif "list" in text.lower():
        _g["expecting"] = TZ_LIST
        # Build 2-column keyboard with cancel at the bottom
        rows = []
        for i in range(0, len(COMMON_TIMEZONES), 2):
            row = COMMON_TIMEZONES[i : i + 2]
            rows.append([KeyboardButton(tz) for tz in row])
        rows.append(_CANCEL_ROW)
        keyboard = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "🌍 Select your timezone:",
            reply_markup=keyboard,
        )

    elif "manual" in text.lower() or "enter" in text.lower():
        _g["expecting"] = TZ_MANUAL
        await update.message.reply_text(
            "✏️ Type your timezone in IANA format.\n\n"
            "_Examples: Africa/Abidjan, Europe/Paris, America/New\\_York_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:
        await update.message.reply_text(
            "⚠️ Please choose one of the three options.",
        )


# ── Timezone step 2a: selection from list ─────────────────────────────────────

async def _handle_tz_list_selection(update: Update, text: str) -> None:
    if not _valid_iana_tz(text):
        await update.message.reply_text(
            f"⚠️ '{text}' is not a valid timezone. Please tap one from the list.",
        )
        return

    _g["temp_timezone"] = text
    _g["expecting"]     = TZ_REMINDER

    await update.message.reply_text(
        f"✅ Timezone selected: *{text}*\n\n"
        "Now send the *daily reminder time* in HH:MM format (24-hour).\n"
        "_Example: 21:30_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Timezone step 2b: manual entry ────────────────────────────────────────────

async def _handle_tz_manual(update: Update, text: str) -> None:
    if not _valid_iana_tz(text):
        await update.message.reply_text(
            f"⚠️ *'{text}'* is not a recognised IANA timezone.\n\n"
            "Examples of valid values:\n"
            "  `Africa/Abidjan`\n"
            "  `Europe/Paris`\n"
            "  `America/New_York`\n"
            "  `Asia/Dubai`\n\n"
            "Please try again:",
            parse_mode="Markdown",
        )
        return

    _g["temp_timezone"] = text
    _g["expecting"]     = TZ_REMINDER

    await update.message.reply_text(
        f"✅ Timezone accepted: *{text}*\n\n"
        "Now send the *daily reminder time* in HH:MM format (24-hour).\n"
        "_Example: 21:30_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Timezone step 3: reminder time ────────────────────────────────────────────

async def _handle_tz_reminder(update: Update, text: str) -> None:
    if not _valid_hhmm(text):
        await update.message.reply_text(
            "⚠️ Invalid format. Please send time in *HH:MM* format (24-hour).\n"
            "_Example: 21:30_",
            parse_mode="Markdown",
        )
        return

    _g["temp_reminder"] = text.strip()
    _g["expecting"]     = TZ_RESEARCH

    await update.message.reply_text(
        f"✅ Reminder time: *{text.strip()}*\n\n"
        "Now send the *daily research start time* in HH:MM format (24-hour).\n"
        "_Example: 09:00_",
        parse_mode="Markdown",
    )


# ── Timezone step 4: research time — save everything ─────────────────────────

async def _handle_tz_research(update: Update, text: str) -> None:
    if not _valid_hhmm(text):
        await update.message.reply_text(
            "⚠️ Invalid format. Please send time in *HH:MM* format (24-hour).\n"
            "_Example: 09:00_",
            parse_mode="Markdown",
        )
        return

    tz            = _g.pop("temp_timezone", None)
    reminder_time = _g.pop("temp_reminder", None)
    research_time = text.strip()
    _g["expecting"] = None

    if not tz or not reminder_time:
        await update.message.reply_text(
            "⚠️ Something went wrong. Please start over with /timezone."
        )
        return

    # Persist
    cfg = load_sched_cfg()
    cfg["timezone"]             = tz
    cfg["keyword_request_time"] = reminder_time
    cfg["research_run_time"]    = research_time
    save_sched_cfg(cfg)

    # Live-update APScheduler jobs without restart
    _reschedule_jobs(cfg)

    logger.info(
        f"[scheduler] Settings updated — tz={tz}, "
        f"reminder={reminder_time}, research={research_time}"
    )

    await update.message.reply_text(
        "✅ *Settings saved successfully.*\n\n"
        f"Timezone: `{tz}`\n"
        f"Reminder time: `{reminder_time}`\n"
        f"Research time: `{research_time}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── /parameters — inline menu ────────────────────────────────────────────────

def _params_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the parameters menu."""
    media_labels  = {"video": "🎬 Video only", "image": "🖼 Image only",  "both": "🎭 Both"}
    status_labels = {"active": "✅ Active",    "inactive": "❌ Inactive", "both": "🔀 Both"}

    mt  = cfg.get("media_type", "both")
    ast = cfg.get("active_status", "active")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 Countries",          callback_data="param:countries")],
        [
            InlineKeyboardButton(
                "🎬 Video" + (" ✓" if mt == "video" else ""),
                callback_data="param:media:video",
            ),
            InlineKeyboardButton(
                "🖼 Image" + (" ✓" if mt == "image" else ""),
                callback_data="param:media:image",
            ),
            InlineKeyboardButton(
                "🎭 Both"  + (" ✓" if mt == "both"  else ""),
                callback_data="param:media:both",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ Active"   + (" ✓" if ast == "active"   else ""),
                callback_data="param:status:active",
            ),
            InlineKeyboardButton(
                "❌ Inactive" + (" ✓" if ast == "inactive" else ""),
                callback_data="param:status:inactive",
            ),
            InlineKeyboardButton(
                "🔀 Both"    + (" ✓" if ast == "both"     else ""),
                callback_data="param:status:both",
            ),
        ],
        [InlineKeyboardButton("✅ Done", callback_data="param:done")],
    ])


def _params_text(cfg: dict) -> str:
    """Build the parameters summary message text."""
    media_labels  = {"video": "Video only", "image": "Image only", "both": "Both"}
    status_labels = {"active": "Active only", "inactive": "Inactive only", "both": "Both"}
    return (
        "📊 *Research Parameters*\n\n"
        f"• Countries: `{', '.join(cfg.get('countries', ['FR']))}`\n"
        f"• Media type: `{media_labels.get(cfg.get('media_type', 'both'), cfg.get('media_type', 'both'))}`\n"
        f"• Ad status: `{status_labels.get(cfg.get('active_status', 'active'), cfg.get('active_status', 'active'))}`\n\n"
        "_Tap a button below to change a value. Tap ✅ Done when finished._"
    )


async def cmd_parameters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_sched_cfg()
    await update.message.reply_text(
        _params_text(cfg),
        parse_mode="Markdown",
        reply_markup=_params_keyboard(cfg),
    )


async def _param_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "param:media:video"

    if not data.startswith("param:"):
        return

    parts = data.split(":")
    action = parts[1]

    cfg = load_sched_cfg()

    if action == "done":
        await query.edit_message_text(
            "✅ *Parameters saved.*\n\n" + _params_text(cfg).split("\n\n_")[0],
            parse_mode="Markdown",
        )
        return

    if action == "countries":
        _g["expecting"] = PARAM_COUNTRIES
        await query.message.reply_text(
            "🌍 Send the country codes separated by commas.\n\n"
            "_Examples: `FR` · `US, DE` · `FR, US, GB`_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if action == "media" and len(parts) >= 3:
        cfg["media_type"] = parts[2]
        save_sched_cfg(cfg)
        await query.edit_message_text(
            _params_text(cfg), parse_mode="Markdown",
            reply_markup=_params_keyboard(cfg),
        )
        return

    if action == "status" and len(parts) >= 3:
        cfg["active_status"] = parts[2]
        save_sched_cfg(cfg)
        await query.edit_message_text(
            _params_text(cfg), parse_mode="Markdown",
            reply_markup=_params_keyboard(cfg),
        )
        return


async def _handle_param_countries_input(update: Update, text: str) -> None:
    """Handle free-text country codes input from the /parameters flow."""
    raw_countries = [c.strip() for c in text.replace(";", ",").split(",") if c.strip()]
    if not raw_countries:
        await update.message.reply_text(
            "⚠️ No countries found. Send at least one country code.\n"
            "_Example: `FR` or `FR, US`_",
            parse_mode="Markdown",
        )
        return

    cfg = load_sched_cfg()
    cfg["countries"] = raw_countries
    save_sched_cfg(cfg)
    _g["expecting"] = None

    await update.message.reply_text(
        f"✅ Countries updated: `{', '.join(raw_countries)}`\n\n"
        + _params_text(cfg),
        parse_mode="Markdown",
        reply_markup=_params_keyboard(cfg),
    )
    logger.info(f"[scheduler] Countries updated: {raw_countries}")


# ── Keyword input handler ─────────────────────────────────────────────────────

async def _handle_keywords_input(update: Update, text: str) -> None:
    parts = [k.strip() for k in text.split(",")]
    seen: set[str] = set()
    keywords: list[str] = []
    for kw in parts:
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            keywords.append(kw)

    if not keywords:
        await update.message.reply_text(
            "⚠️ No valid keywords found. Please separate keywords with commas.\n\n"
            "_Example: portable blender, car vacuum, posture corrector_",
            parse_mode="Markdown",
        )
        return

    _g["pending_keywords"] = keywords
    _g["expecting"]        = "total_ads"
    _g["reminder_count"]   = 99  # Signal reminder job to stop

    await update.message.reply_text(
        "✅ Keywords received:\n"
        + "\n".join(f"  • {k}" for k in keywords)
        + "\n\n🔢 How many total ads do you want to extract?"
    )
    logger.info(f"[scheduler] Keywords received: {keywords}")


# ── Total ads input handler ───────────────────────────────────────────────────

async def _handle_total_ads_input(update: Update, text: str) -> None:
    try:
        total = int(text.strip())
        if total <= 0:
            raise ValueError("must be positive")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please enter a valid positive number.\n_Example: 40_",
            parse_mode="Markdown",
        )
        return

    keywords = _g.get("pending_keywords", [])
    if not keywords:
        await update.message.reply_text(
            "⚠️ Keywords were lost. Please start over with /setkeywords."
        )
        _g["expecting"] = None
        return

    # Distribute total evenly, spreading remainder across first N keywords
    targets = _distribute_targets(keywords, total)

    today = datetime.now().strftime("%Y-%m-%d")
    save_daily_cfg({
        "date":             today,
        "keywords":         targets,
        "total_ads_target": total,
    })
    _g["expecting"]        = None
    _g["pending_keywords"] = []

    kw_lines = "\n".join(f"  • {t['term']} → {t['ads_target']}" for t in targets)
    await update.message.reply_text(
        f"✅ *Tomorrow's research configuration saved!*\n\n"
        f"Keywords:\n{kw_lines}\n\n"
        f"Total ads target: *{total}*",
        parse_mode="Markdown",
    )
    logger.info(f"[scheduler] Config saved — keywords={keywords}, target={total}")


# ── Scheduled: daily keyword request ─────────────────────────────────────────

async def scheduled_ask_keywords(bot: Bot) -> None:
    cfg     = load_sched_cfg()
    chat_id = cfg.get("chat_id")
    if not chat_id:
        logger.warning("[scheduler] No chat_id — keyword request skipped (send /start first)")
        return

    _g["expecting"]      = "keywords"
    _g["reminder_count"] = 0
    logger.info("[scheduler] Sending daily keyword request")

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "📝 *Send tomorrow's research keywords separated by commas.*\n\n"
            "_Example: portable blender, car vacuum, posture corrector_"
        ),
        parse_mode="Markdown",
    )


# ── Scheduled: reminders every 5 min ─────────────────────────────────────────

async def scheduled_reminder(bot: Bot) -> None:
    cfg     = load_sched_cfg()
    chat_id = cfg.get("chat_id")
    if not chat_id or _g.get("expecting") != "keywords":
        return

    count = _g.get("reminder_count", 0)

    if count >= 5:
        _g["expecting"]      = None
        _g["reminder_count"] = 0
        daily = load_daily_cfg()
        if daily and daily.get("keywords"):
            logger.info("[scheduler] No response after 5 reminders — using yesterday's config")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏰ *No response received. Using yesterday's configuration.*\n\n"
                    f"Keywords: {', '.join(daily['keywords'])}\n"
                    f"Ads target: {daily['total_ads_target']}"
                ),
                parse_mode="Markdown",
            )
        else:
            logger.warning("[scheduler] No response and no yesterday config — research skipped")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏰ No response received and no previous configuration available.\n"
                    "Research will be skipped today."
                ),
            )
        return

    _g["reminder_count"] = count + 1
    logger.info(f"[scheduler] Sending reminder {count + 1}/5")
    await bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *Reminder {count + 1}/5:* Please send tomorrow's research keywords.",
        parse_mode="Markdown",
    )


# ── Scheduled: run research ───────────────────────────────────────────────────

async def scheduled_run_research(bot: Bot) -> None:
    cfg     = load_sched_cfg()
    chat_id = cfg.get("chat_id")
    if not chat_id:
        logger.warning("[scheduler] No chat_id — research skipped")
        return
    if _g.get("research_running"):
        logger.warning("[scheduler] Research already running — duplicate trigger ignored")
        return

    daily = load_daily_cfg()
    if not daily or not daily.get("keywords"):
        logger.warning("[scheduler] No daily config — research skipped")
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ No daily configuration found. Research was skipped today.",
        )
        return

    await _run_research(bot, chat_id)


# ── Core research runner ──────────────────────────────────────────────────────

async def _run_research(bot: Bot, chat_id: int) -> None:
    """
    Trigger the original Product Research Bot and wait for results.
    Does NOT run any extraction logic itself — writes a trigger file and polls
    for research_results.json written by bot.py's _trigger_monitor_loop.
    """
    if _g.get("research_running"):
        await bot.send_message(chat_id=chat_id, text="⚠️ Research is already running.")
        return

    # Safety: clear a stale lock from a previous crashed run
    if _LOCK_FILE.exists():
        try:
            lock_age = time.time() - _LOCK_FILE.stat().st_mtime
            if lock_age > 7200:   # 2-hour stale lock → clear it
                _LOCK_FILE.unlink()
                logger.warning("[scheduler] Stale research lock cleared (>2h old)")
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Research is already running in the main bot. Please wait.",
                )
                return
        except Exception:
            pass

    _g["research_running"] = True

    cfg   = load_sched_cfg()
    daily = load_daily_cfg()

    keyword_targets = _get_keyword_targets(daily)
    total_target    = daily.get("total_ads_target", sum(t["ads_target"] for t in keyword_targets))
    countries       = cfg.get("countries", ["FR"])
    media_type      = cfg.get("media_type", "both")
    active_status   = cfg.get("active_status", "active")

    kw_lines = "\n".join(
        f"  • {t['term']} → {t['ads_target']} ads" for t in keyword_targets
    )

    # ── Build and write the trigger payload ───────────────────────────────────
    trigger_payload = {
        "date":             datetime.now().strftime("%Y-%m-%d"),
        "triggered_at":     datetime.now().isoformat(),
        "chat_id":          chat_id,
        "keywords":         keyword_targets,
        "total_ads_target": total_target,
        "parameters": {
            "countries":     countries,
            "media_type":    media_type,
            "active_status": active_status,
            "source":        "facebook_ads_library",
        },
    }

    # Clear any stale results file before writing trigger
    if RESULTS_FILE.exists():
        try:
            RESULTS_FILE.unlink()
        except Exception:
            pass

    try:
        TRIGGER_FILE.write_text(json.dumps(trigger_payload, indent=2))
    except Exception as exc:
        logger.error(f"[scheduler] Could not write trigger file: {exc}")
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ *Could not start research:* could not write trigger file.\n`{exc}`",
            parse_mode="Markdown",
        )
        _g["research_running"] = False
        return

    logger.info(
        f"[scheduler] Research trigger written — "
        f"keywords={[t['term'] for t in keyword_targets]}, total={total_target}"
    )

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 *Research Bot triggered!*\n\n"
            f"*Keywords:*\n{kw_lines}\n\n"
            f"*Total target:* {total_target} ads\n"
            f"*Countries:* {', '.join(str(c) for c in countries)}\n"
            f"*Media type:* {media_type} | *Ad status:* {active_status}\n\n"
            f"✅ The Research Bot is now running the extraction.\n"
            f"You will receive live updates and a final report *directly from the Research Bot*."
        ),
        parse_mode="Markdown",
    )

    # ── Wait for the Research Bot to finish (it sends progress directly) ─────
    POLL_INTERVAL = 30      # seconds between each check
    TIMEOUT       = 5400    # 90-minute hard timeout
    start_ts      = time.time()

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = time.time() - start_ts

        # ── Check for results ────────────────────────────────────────────
        if RESULTS_FILE.exists():
            break

        # ── Timeout guard ────────────────────────────────────────────────
        if elapsed > TIMEOUT:
            logger.error("[scheduler] Research timed out after 90 min")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏱ *Research timed out* after 90 minutes.\n"
                    "The Research Bot may still be running. "
                    "Check it directly for status."
                ),
                parse_mode="Markdown",
            )
            _g["research_running"] = False
            return

    # ── Read and report results ────────────────────────────────────────────────
    try:
        results = json.loads(RESULTS_FILE.read_text())
    except Exception as exc:
        logger.error(f"[scheduler] Could not read results file: {exc}")
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Research finished but results file could not be read.",
        )
        _g["research_running"] = False
        return

    per_kw    = results.get("per_keyword", {})
    status    = results.get("status", "unknown")
    elapsed_s = results.get("elapsed_seconds", int(time.time() - start_ts))
    elapsed_str = f"{elapsed_s // 60} min {elapsed_s % 60} sec"

    results_lines = "\n".join(
        f"  {term} → {v.get('status', '?')}"
        for term, v in per_kw.items()
    ) if per_kw else "  (no results)"

    status_str = "✅ Completed" if status == "completed" else "⚠️ Partially failed"

    report = (
        "📊 *Daily Research Report*\n\n"
        f"*Keywords & targets:*\n{kw_lines}\n\n"
        f"*Parameters:*\n"
        f"  Countries: {', '.join(str(c) for c in countries)} | "
        f"Media: {media_type} | Status: {active_status}\n\n"
        f"*Results:*\n{results_lines}\n\n"
        f"*Execution time:* {elapsed_str}\n"
        f"*Status:* {status_str}"
    )

    await bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
    logger.info(
        f"[scheduler] Research report sent — "
        f"status={status}, time={elapsed_str}"
    )

    _g["research_running"] = False


# ── APScheduler builder ───────────────────────────────────────────────────────

def _build_scheduler(bot: Bot) -> AsyncIOScheduler:
    cfg    = load_sched_cfg()
    tz     = pytz.timezone(cfg.get("timezone", "Asia/Dubai"))
    kw_h,  kw_m  = [int(x) for x in cfg["keyword_request_time"].split(":")]
    res_h, res_m = [int(x) for x in cfg["research_run_time"].split(":")]

    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(
        scheduled_ask_keywords,
        CronTrigger(hour=kw_h, minute=kw_m, timezone=tz),
        args=[bot], id="ask_keywords", replace_existing=True,
    )
    sched.add_job(
        scheduled_reminder,
        "interval", minutes=5,
        args=[bot], id="reminder", replace_existing=True,
    )
    sched.add_job(
        scheduled_run_research,
        CronTrigger(hour=res_h, minute=res_m, timezone=tz),
        args=[bot], id="run_research", replace_existing=True,
    )
    return sched


# ── post_init / post_shutdown ─────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    global _scheduler
    _scheduler = _build_scheduler(application.bot)
    _scheduler.start()

    cfg = load_sched_cfg()
    try:
        await application.bot.set_my_commands([
            ("start",       "Register this chat and show help"),
            ("timezone",    "Set timezone and schedule times"),
            ("settings",    "Show timezone and schedule settings"),
            ("parameters",  "Configure research parameters"),
            ("status",      "Show config and daily keywords"),
            ("setkeywords", "Set research keywords manually"),
            ("settarget",   "Set ads extraction target"),
            ("runresearch", "Trigger research immediately"),
            ("cancel",      "Cancel current input"),
        ])
    except Exception as e:
        logger.warning(f"[scheduler] Could not register command menu (non-fatal): {e}")
    logger.info(
        f"[scheduler] APScheduler started — "
        f"keyword_request={cfg['keyword_request_time']}, "
        f"research_run={cfg['research_run_time']} ({cfg['timezone']})"
    )


async def _post_shutdown(application: Application) -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    logger.info("[scheduler] APScheduler stopped")


# ── Application builder ───────────────────────────────────────────────────────

def build_scheduler_application(token: str) -> Application:
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("timezone",    cmd_timezone))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("parameters",  cmd_parameters))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("setkeywords", cmd_setkeywords))
    app.add_handler(CommandHandler("settarget",   cmd_settarget))
    app.add_handler(CommandHandler("runresearch", cmd_runresearch))

    # Inline button callbacks (for /parameters menu)
    app.add_handler(CallbackQueryHandler(_param_callback, pattern=r"^param:"))

    # Location handler must come before the generic text handler
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
