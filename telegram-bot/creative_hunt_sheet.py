"""
creative_hunt_sheet.py - Google Sheets operations for the Creative Hunt Bot.

Source tab  : APPROVED  (products waiting for creatives)
Destination : READY FOR ADS  (products with creatives, ready for ads)

Flow:
- Load products from APPROVED
- Save creative URLs into slots 2-5 in APPROVED
- Finalize: move row from APPROVED → READY FOR ADS (with chosen creative count)
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SHEET_NAME, ADS_COLUMNS, SHEET_COLUMNS, get_google_credentials_dict

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

APPROVED_TAB = "APPROVED"
READY_TAB    = "READY FOR ADS"

# All creative URL columns in order (slots 1-10)
CREATIVE_COLS = [
    "ADS LIBRARY MEDIA URL",
    "ADS LIBRARY MEDIA URL 2",
    "ADS LIBRARY MEDIA URL 3",
    "ADS LIBRARY MEDIA URL 4",
    "ADS LIBRARY MEDIA URL 5",
    "ADS LIBRARY MEDIA URL 6",
    "ADS LIBRARY MEDIA URL 7",
    "ADS LIBRARY MEDIA URL 8",
    "ADS LIBRARY MEDIA URL 9",
    "ADS LIBRARY MEDIA URL 10",
]

# The extra columns we manage (2-10); slot 1 already comes from PENDING/research
EXTRA_CREATIVE_COLS = CREATIVE_COLS[1:]


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        get_google_credentials_dict(), scopes=SCOPES
    )
    return gspread.authorize(creds)


def _open_sheet(client: gspread.Client) -> gspread.Spreadsheet:
    return client.open(GOOGLE_SHEET_NAME)


def _ensure_approved_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """
    Return the APPROVED worksheet.
    If the header doesn't match SHEET_COLUMNS exactly, migrate data rows by
    column name so no values are lost when new columns are added.
    """
    try:
        ws = spreadsheet.worksheet(APPROVED_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=APPROVED_TAB, rows=1000, cols=len(SHEET_COLUMNS)
        )
        ws.append_row(SHEET_COLUMNS, value_input_option="USER_ENTERED")
        logger.info(f"[creative_sheet] Created '{APPROVED_TAB}' tab")
        return ws

    header = ws.row_values(1)
    if not header:
        ws.append_row(SHEET_COLUMNS, value_input_option="USER_ENTERED")
        return ws

    if header != SHEET_COLUMNS:
        try:
            existing_records = ws.get_all_records(default_blank="")
        except Exception:
            existing_records = []
        ws.clear()
        new_rows = [SHEET_COLUMNS]
        for rec in existing_records:
            new_rows.append([str(rec.get(col, "")) for col in SHEET_COLUMNS])
        if ws.col_count < len(SHEET_COLUMNS):
            ws.resize(cols=len(SHEET_COLUMNS))
        ws.update("A1", new_rows, value_input_option="USER_ENTERED")
        logger.info(
            f"[creative_sheet] Migrated APPROVED header to {len(SHEET_COLUMNS)} cols "
            f"({len(existing_records)} data rows preserved)"
        )

    return ws


def _ensure_ready_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """
    Return the READY FOR ADS worksheet with the same columns as APPROVED (SHEET_COLUMNS).
    Creates the tab if missing; overwrites the header if it doesn't match exactly.
    """
    try:
        ws = spreadsheet.worksheet(READY_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=READY_TAB, rows=1000, cols=len(SHEET_COLUMNS)
        )
        ws.update([SHEET_COLUMNS], "A1")
        logger.info(f"[creative_sheet] Created '{READY_TAB}' tab with SHEET_COLUMNS header")
        return ws

    existing = ws.row_values(1)
    if existing != SHEET_COLUMNS:
        if ws.col_count < len(SHEET_COLUMNS):
            ws.resize(cols=len(SHEET_COLUMNS))
        ws.update([SHEET_COLUMNS], "A1")
        logger.info(f"[creative_sheet] Updated '{READY_TAB}' header to match APPROVED columns")

    return ws


# ── Load products ──────────────────────────────────────────────────────────

def load_approved_products() -> list[dict]:
    """
    Load all rows from the APPROVED tab.
    Returns a list of row dicts (all products — no slot filter, since
    the user may finalize even if all creative slots are already filled).
    """
    try:
        client      = _get_client()
        spreadsheet = _open_sheet(client)

        ws         = _ensure_approved_tab(spreadsheet)
        all_values = ws.get_all_values()

        if len(all_values) <= 1:
            return []

        header = all_values[0]

        products = []
        for row_values in all_values[1:]:
            while len(row_values) < len(header):
                row_values.append("")
            row = {header[i]: row_values[i] for i in range(len(header))}

            if not str(row.get("SKU", "")).strip():
                continue

            products.append(row)

        logger.info(
            f"[creative_sheet] {len(products)} product(s) loaded from APPROVED"
        )
        return products

    except Exception as e:
        logger.error(f"[creative_sheet] load_approved_products failed: {e}")
        return []


# ── Save creative to APPROVED ──────────────────────────────────────────────

def save_creative(sku: str, creative_url: str, tab: str = APPROVED_TAB) -> tuple[bool, str]:
    """
    Write creative_url to the next empty ADS LIBRARY MEDIA URL 2/3/4/5
    slot for the row matching SKU in the given tab (default: APPROVED).

    Returns (success, saved_to_column_name).
    """
    import time as _time
    max_attempts = 3
    for _attempt in range(max_attempts):
        try:
            return _save_creative_once(sku, creative_url, tab)
        except Exception as e:
            if "429" in str(e) and _attempt < max_attempts - 1:
                wait = 15 * (_attempt + 1)
                logger.warning(
                    f"[creative_sheet] Google Sheets rate limit (429) for SKU '{sku}' "
                    f"— retrying in {wait}s (attempt {_attempt + 1}/{max_attempts})"
                )
                _time.sleep(wait)
            else:
                logger.error(f"[creative_sheet] save_creative failed for SKU '{sku}' in '{tab}': {e}")
                return False, ""
    return False, ""


def _save_creative_once(sku: str, creative_url: str, tab: str) -> tuple[bool, str]:
    # No try/except — exceptions propagate to save_creative's retry loop
    client      = _get_client()
    spreadsheet = _open_sheet(client)

    if tab == APPROVED_TAB:
        ws = _ensure_approved_tab(spreadsheet)
    else:
        ws = spreadsheet.worksheet(tab)

    header = ws.row_values(1)
    if not header:
        return False, ""

    all_values = ws.get_all_values()

    if len(all_values) <= 1:
        return False, ""

    try:
        sku_col = header.index("SKU")
    except ValueError:
        logger.error(f"[creative_sheet] SKU column not found in '{tab}'")
        return False, ""

    row_num  = None
    row_data = None
    for i, row in enumerate(all_values[1:], start=2):
        while len(row) < len(header):
            row.append("")
        if row[sku_col] == sku:
            row_num  = i
            row_data = row
            break

    if row_num is None:
        logger.warning(f"[creative_sheet] SKU '{sku}' not found in '{tab}'")
        return False, ""

    for col_name in EXTRA_CREATIVE_COLS:
        if col_name in header:
            col_idx = header.index(col_name)
            if not _is_slot_filled(str(row_data[col_idx])):
                ws.update_cell(row_num, col_idx + 1, creative_url)
                logger.info(
                    f"[creative_sheet] Saved creative for SKU '{sku}' → '{col_name}' in '{tab}' ✅"
                )
                return True, col_name

    logger.warning(f"[creative_sheet] All creative slots full for SKU '{sku}' in '{tab}'")
    return False, ""


# ── Finalize: APPROVED → READY FOR ADS ───────────────────────────────────

def finalize_to_ready_for_ads(sku: str, num_creatives: int) -> bool:
    """
    Finalize a product:
      1. Find the row in APPROVED matching SKU.
      2. Clear creative URL slots beyond num_creatives.
      3. Set STATU = READY FOR ADS.
      4. Append to READY FOR ADS tab (using ADS_COLUMNS).
      5. Delete from APPROVED.

    Returns True on success.
    Retries up to 3 times with exponential backoff on API errors.
    """
    for attempt in range(3):
        try:
            client      = _get_client()
            spreadsheet = _open_sheet(client)

            src_ws = _ensure_approved_tab(spreadsheet)
            dst_ws = _ensure_ready_tab(spreadsheet)

            all_values = src_ws.get_all_values()
            if not all_values:
                logger.warning(f"[creative_sheet] APPROVED tab is empty — SKU {sku} not found")
                return False

            header = all_values[0]

            try:
                sku_col_idx = header.index("SKU")
            except ValueError:
                logger.error("[creative_sheet] SKU column not found in APPROVED header")
                return False

            row_num    = None
            row_values = None
            for i, row in enumerate(all_values[1:], start=2):
                while len(row) < len(header):
                    row.append("")
                if row[sku_col_idx] == sku:
                    row_num    = i
                    row_values = list(row)
                    break

            if row_num is None:
                logger.warning(f"[creative_sheet] SKU '{sku}' not found in APPROVED")
                return False

            row_dict = {header[i]: row_values[i] for i in range(len(header))}

            row_dict["STATU"] = "READY FOR ADS"

            # Clear creative slots beyond num_creatives
            for i, col_name in enumerate(CREATIVE_COLS):
                if i >= num_creatives:
                    row_dict[col_name] = ""

            # Build the READY FOR ADS row using the same columns as APPROVED
            target_row = [row_dict.get(col, "") for col in SHEET_COLUMNS]

            # Append to READY FOR ADS (copy before delete)
            dst_ws.append_row(target_row, value_input_option="USER_ENTERED")
            logger.info(
                f"[creative_sheet] Appended SKU '{sku}' to READY FOR ADS "
                f"(with {num_creatives} creative(s))"
            )

            src_ws.delete_rows(row_num)
            logger.info(
                f"[creative_sheet] Deleted row {row_num} (SKU '{sku}') from APPROVED"
            )

            return True

        except Exception as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(
                    f"[creative_sheet] finalize_to_ready_for_ads attempt {attempt + 1} "
                    f"failed for '{sku}': {e} — retrying in {wait}s…"
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"[creative_sheet] finalize_to_ready_for_ads failed for SKU '{sku}' "
                    f"after 3 attempts: {e}"
                )
                return False


# ── Creative helpers ───────────────────────────────────────────────────────

def _is_ad_creative_url(val: str) -> bool:
    """
    Return True only if val looks like a Meta Ads Library creative URL.
    This guards against misaligned data (NOTE, PRICE, etc.) being counted
    as creative slots when rows pre-date the expanded APPROVED header.
    """
    v = val.strip().lower()
    if not v:
        return False
    return (
        "facebook.com/ads/library" in v
        or "fb.watch" in v
        or "facebook.com/reel" in v
        or "facebook.com/watch" in v
    )


def _is_slot_filled(val: str) -> bool:
    """
    Return True if a creative slot contains any URL (http/https).
    Used to check occupancy — accepts both Ads Library URLs and manually-entered URLs
    while rejecting misaligned non-URL data (prices, text, etc.).
    """
    return val.strip().lower().startswith("http")


def get_existing_creatives(row: dict) -> list[str]:
    """Return all filled creative URLs stored for the given row dict (all 10 slots)."""
    return [str(row.get(col, "")).strip() for col in CREATIVE_COLS
            if _is_slot_filled(str(row.get(col, "")))]


def count_all_creatives(row: dict) -> int:
    """Return the number of filled creative slots in this row (slots 1-10)."""
    return sum(1 for col in CREATIVE_COLS if _is_slot_filled(str(row.get(col, ""))))


def count_empty_slots(row: dict) -> int:
    """Return the number of empty creative slots (2-10) remaining for this row."""
    return sum(1 for col in EXTRA_CREATIVE_COLS
               if not _is_slot_filled(str(row.get(col, ""))))
