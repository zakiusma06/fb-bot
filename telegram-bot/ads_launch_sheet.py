"""
ads_launch_sheet.py - Google Sheets operations for the Ads Launch bot.

Handles: READY TO ADS, ADS RUNNING, WINNER, LOSER, ADS ERROR tabs.
"""

import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SHEET_NAME, get_google_credentials_dict, SHEET_COLUMNS

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TAB_READY   = "READY FOR ADS"
TAB_RUNNING = "ADS RUNNING"
TAB_WINNER  = "WINNER"
TAB_LOSER   = "LOSER"
TAB_ERROR   = "ADS ERROR"

# READY FOR ADS uses the exact same columns as the APPROVED tab
READY_COLS = SHEET_COLUMNS

# Core product columns for ADS RUNNING / WINNER / LOSER / ADS ERROR.
# Column order is kept stable so existing rows are not misaligned.
BASE_COLS = [
    "SKU", "KEYWORD", "URL PRODUCT",
    "ADS LIBRARY MEDIA URL", "ADS LIBRARY MEDIA URL 2", "ADS LIBRARY MEDIA URL 3",
    "ADS LIBRARY MEDIA URL 4", "ADS LIBRARY MEDIA URL 5",
    "PRICE", "COMPARE AT PRICE", "URL LANDING PAGE", "PRODUCT NAME",
    "STATU", "NOTE", "SOURCING PRICE USD", "SOURCING URL", "WEIGHT GRAM", "HAS VARIANTS",
]

OPS_COLS = [
    "CAMPAIGN NAME", "ADSET NAME", "AD NAME",
    "META CAMPAIGN ID", "META ADSET ID", "META AD ID",
    "AD TYPE", "SELECTED CREATIVES", "SELECTED PRIMARY TEXT", "SELECTED HEADLINE",
    "PUBLISH MODE", "SCHEDULED TIME", "PUBLISHED AT", "EFFECTIVE START TIME",
    "LAST METRICS SYNC", "RESULTS", "SPEND",
    "1 DAY COST PER RZLT", "2 DAY COST PER RZLT", "TOTAL COST PER RZLT",
    "RULE TRIGGERED", "ERROR MESSAGE",
    "MANUAL DECISION", "MANUAL NOTE", "OVERRIDE ACTIVE", "OVERRIDE UNTIL",
    "STOPPED AT", "STOP REASON",
]

# Extra media columns appended after OPS_COLS so existing row positions do not shift
EXTRA_MEDIA_COLS = [
    "ADS LIBRARY MEDIA URL 6", "ADS LIBRARY MEDIA URL 7", "ADS LIBRARY MEDIA URL 8",
    "ADS LIBRARY MEDIA URL 9", "ADS LIBRARY MEDIA URL 10",
    "IMAGE URL",
    "UPLOADED ASSET IDS",
]

RUNNING_COLS = BASE_COLS + OPS_COLS + EXTRA_MEDIA_COLS
RESULT_COLS  = BASE_COLS + OPS_COLS + EXTRA_MEDIA_COLS

WINNER_LOSER_COLS = [
    "PUBLISHED AT",
    "KEYWORD", "SKU", "PRODUCT NAME", "STATU", "URL PRODUCT",
    "PRICE", "SOURCING PRICE USD", "WEIGHT GRAM", "SOURCING URL",
    "NOTE", "MANUAL NOTE",
    "RESULTS", "SPEND",
    "1 DAY COST PER RZLT", "2 DAY COST PER RZLT", "TOTAL COST PER RZLT",
    "ADS LIBRARY MEDIA URL", "ADS LIBRARY MEDIA URL 2", "ADS LIBRARY MEDIA URL 3",
    "ADS LIBRARY MEDIA URL 4", "ADS LIBRARY MEDIA URL 5",
    "URL LANDING PAGE", "HAS VARIANTS",
    "CAMPAIGN NAME", "ADSET NAME", "AD NAME",
    "META CAMPAIGN ID", "META ADSET ID", "META AD ID",
    "AD TYPE", "SELECTED CREATIVES", "SELECTED PRIMARY TEXT", "SELECTED HEADLINE",
    "PUBLISH MODE", "SCHEDULED TIME", "EFFECTIVE START TIME",
    "LAST METRICS SYNC",
    "RULE TRIGGERED", "ERROR MESSAGE",
    "MANUAL DECISION", "OVERRIDE ACTIVE", "OVERRIDE UNTIL",
    "STOPPED AT", "STOP REASON",
    "ADS LIBRARY MEDIA URL 6", "ADS LIBRARY MEDIA URL 7", "ADS LIBRARY MEDIA URL 8",
    "ADS LIBRARY MEDIA URL 9", "ADS LIBRARY MEDIA URL 10",
    "IMAGE URL", "UPLOADED ASSET IDS",
    "COMPARE AT PRICE",
]

TAB_COLUMNS = {
    TAB_READY:   READY_COLS,
    TAB_RUNNING: RUNNING_COLS,
    TAB_WINNER:  WINNER_LOSER_COLS,
    TAB_LOSER:   WINNER_LOSER_COLS,
    TAB_ERROR:   RESULT_COLS,
}


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        get_google_credentials_dict(), scopes=SCOPES
    )
    return gspread.authorize(creds)


def _open_sheet(client: gspread.Client) -> gspread.Spreadsheet:
    return client.open(GOOGLE_SHEET_NAME)


def _ensure_tab(ss: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """
    Return the worksheet for tab_name, creating it if absent.
    If the header row doesn't match the expected column list, migrate all
    existing data rows by column name (not position) so no values are lost.
    """
    cols = TAB_COLUMNS.get(tab_name, BASE_COLS)
    try:
        ws = ss.worksheet(tab_name)
        existing = ws.row_values(1)
        # Strip trailing empty strings — gspread returns extra empty cells
        while existing and existing[-1] == "":
            existing.pop()
        if existing != cols:
            all_values = ws.get_all_values()
            if len(all_values) > 1:
                old_header = existing or cols
                data_rows  = all_values[1:]
                new_rows   = []
                for row in data_rows:
                    if not any(c.strip() for c in row):
                        continue
                    old_dict = {old_header[i]: row[i]
                                for i in range(min(len(old_header), len(row)))}
                    new_rows.append([old_dict.get(col, "") for col in cols])
                if len(cols) > ws.col_count:
                    ws.resize(rows=max(ws.row_count, len(new_rows) + 1),
                              cols=len(cols))
                ws.clear()
                ws.update("A1", [cols] + new_rows, value_input_option="RAW")
                logger.info(f"[ads_sheet] Migrated header+data for tab '{tab_name}' "
                            f"({len(new_rows)} rows)")
            else:
                if len(cols) > ws.col_count:
                    ws.resize(rows=ws.row_count, cols=len(cols))
                ws.update([cols], "A1")
                logger.info(f"[ads_sheet] Fixed header for tab '{tab_name}'")
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=1000, cols=len(cols))
        ws.append_row(cols, value_input_option="RAW")
        logger.info(f"[ads_sheet] Created tab '{tab_name}'")
    return ws


def _row_to_dict(header: list, row: list) -> dict:
    return {col: (row[i] if i < len(row) else "") for i, col in enumerate(header)}


def migrate_winner_loser_tabs() -> None:
    """
    One-time migration: reorder WINNER and LOSER sheets to WINNER_LOSER_COLS.
    Existing rows are remapped by column name — no data is lost.
    Safe to call repeatedly; a no-op if headers already match.
    """
    client = _get_client()
    ss     = _open_sheet(client)
    for tab in (TAB_WINNER, TAB_LOSER):
        _ensure_tab(ss, tab)
        logger.info(f"[ads_sheet] migrate_winner_loser_tabs: '{tab}' done")


def migrate_ready_for_ads() -> None:
    """
    One-time fix: remap READY FOR ADS rows that were written in the old
    ADS_COLUMNS format (where column 15 was CREATIVE COUNT, a small integer 1-10)
    to the current SHEET_COLUMNS format.

    Safe to call repeatedly — rows already in SHEET_COLUMNS format are left as-is.
    """
    from config import ADS_COLUMNS
    client = _get_client()
    ss     = _open_sheet(client)
    try:
        ws = ss.worksheet(TAB_READY)
    except gspread.WorksheetNotFound:
        return

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return

    header    = all_values[0]
    data_rows = all_values[1:]

    migrated = 0
    new_all_rows = [READY_COLS]
    for row in data_rows:
        if not any(c.strip() for c in row):
            continue
        # Detect old ADS_COLUMNS format:
        # index 15 was CREATIVE COUNT (integer 1-10) in ADS_COLUMNS,
        # but is COMPARE AT PRICE (large GNF number or empty) in SHEET_COLUMNS.
        val15 = row[15].strip() if len(row) > 15 else ""
        if val15.isdigit() and 1 <= int(val15) <= 10:
            # Old format — remap by ADS_COLUMNS position
            old_dict = {ADS_COLUMNS[i]: row[i]
                        for i in range(min(len(ADS_COLUMNS), len(row)))}
            new_row = [old_dict.get(col, "") for col in READY_COLS]
            migrated += 1
        else:
            # Already in SHEET_COLUMNS (or unknown) — remap by current header
            curr_dict = {header[i]: row[i]
                         for i in range(min(len(header), len(row)))}
            new_row = [curr_dict.get(col, "") for col in READY_COLS]
        new_all_rows.append(new_row)

    if migrated > 0:
        if len(READY_COLS) > ws.col_count:
            ws.resize(rows=max(ws.row_count, len(new_all_rows)), cols=len(READY_COLS))
        ws.clear()
        ws.update("A1", new_all_rows, value_input_option="RAW")
        logger.info(f"[ads_sheet] migrate_ready_for_ads: fixed {migrated} row(s) "
                    f"(ADS_COLUMNS → SHEET_COLUMNS)")
    else:
        logger.info("[ads_sheet] migrate_ready_for_ads: all rows already in correct format")


def ensure_all_tabs() -> None:
    """Sync headers (and migrate data rows) for every managed tab at bot startup."""
    client = _get_client()
    ss     = _open_sheet(client)
    for tab in TAB_COLUMNS:
        _ensure_tab(ss, tab)
    logger.info("[ads_sheet] All tab headers verified/synced")


def _retry(fn, retries: int = 3, base_delay: float = 1.0):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
            else:
                raise


# ── Public API ────────────────────────────────────────────────────────────────

def load_ready_to_ads() -> list[dict]:
    """Load all launchable rows from READY FOR ADS tab."""
    client = _get_client()
    ss = _open_sheet(client)
    ws = _ensure_tab(ss, TAB_READY)
    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        return []
    header = all_rows[0]
    skip_statuses = {"ADS RUNNING", "WINNER", "LOSER", "ADS ERROR"}
    result = []
    for row in all_rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = _row_to_dict(header, row)
        if d.get("SKU") and d.get("STATU", "").strip() not in skip_statuses:
            result.append(d)
    return result


def load_ads_error() -> list[dict]:
    """Load all rows from ADS ERROR tab that can be retried."""
    client = _get_client()
    ss = _open_sheet(client)
    ws = _ensure_tab(ss, TAB_ERROR)
    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        return []
    header = all_rows[0]
    result = []
    for row in all_rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = _row_to_dict(header, row)
        if d.get("SKU"):
            result.append(d)
    return result


def move_product(
    product: dict,
    target_tab: str,
    extra_fields: dict | None = None,
    source_tab: str | None = None,
) -> bool:
    """
    Move a product row from source_tab (default: READY FOR ADS) to target_tab.
    extra_fields: additional operational fields to set on the destination row.
    """
    src_tab = source_tab or TAB_READY
    try:
        client = _get_client()
        ss = _open_sheet(client)
        src_ws = _ensure_tab(ss, src_tab)
        dst_ws = _ensure_tab(ss, target_tab)

        merged = dict(product)
        if extra_fields:
            merged.update(extra_fields)

        dst_cols = TAB_COLUMNS.get(target_tab, BASE_COLS)
        dst_row  = [merged.get(col, "") for col in dst_cols]

        _retry(lambda: dst_ws.append_row(dst_row, value_input_option="RAW"))

        sku = product.get("SKU", "")
        if sku:
            src_data = src_ws.get_all_values()
            if src_data and "SKU" in src_data[0]:
                sku_col = src_data[0].index("SKU")
                for i, row in enumerate(src_data[1:], start=2):
                    if len(row) > sku_col and row[sku_col] == sku:
                        _retry(lambda ri=i: src_ws.delete_rows(ri))
                        break

        logger.info(f"[ads_sheet] Moved {sku} → {target_tab}")
        return True
    except Exception as e:
        logger.error(f"[ads_sheet] move_product failed: {e}")
        return False


def move_running_product(sku: str, target_tab: str, extra_fields: dict | None = None) -> bool:
    """Move a row from ADS RUNNING to WINNER, LOSER, or ADS ERROR."""
    try:
        client = _get_client()
        ss = _open_sheet(client)
        src_ws = _ensure_tab(ss, TAB_RUNNING)
        dst_ws = _ensure_tab(ss, target_tab)

        src_data = src_ws.get_all_values()
        if not src_data or len(src_data) < 2:
            return False
        header = src_data[0]
        sku_col = header.index("SKU") if "SKU" in header else 0

        for i, row in enumerate(src_data[1:], start=2):
            if len(row) > sku_col and row[sku_col] == sku:
                product = _row_to_dict(header, row)
                if extra_fields:
                    product.update(extra_fields)
                dst_cols = TAB_COLUMNS.get(target_tab, RESULT_COLS)
                dst_row  = [product.get(col, "") for col in dst_cols]
                _retry(lambda: dst_ws.append_row(dst_row, value_input_option="RAW"))
                _retry(lambda ri=i: src_ws.delete_rows(ri))
                logger.info(f"[ads_sheet] Moved running {sku} → {target_tab}")
                return True
        logger.warning(f"[ads_sheet] SKU {sku} not found in ADS RUNNING")
        return False
    except Exception as e:
        logger.error(f"[ads_sheet] move_running_product failed: {e}")
        return False


def update_running_row(sku: str, fields: dict) -> bool:
    """
    Update specific fields on a row in ADS RUNNING.
    Uses a single batch_update call instead of one update_cell per field
    to avoid hitting Google Sheets API rate limits (60 writes/min).
    """
    try:
        client = _get_client()
        ss = _open_sheet(client)
        ws = _ensure_tab(ss, TAB_RUNNING)
        data = ws.get_all_values()
        if not data:
            return False
        header = data[0]
        for i, row in enumerate(data[1:], start=2):
            d = _row_to_dict(header, row)
            if d.get("SKU") == sku:
                updates = []
                for field, value in fields.items():
                    if field in header:
                        col_idx = header.index(field) + 1
                        cell    = gspread.utils.rowcol_to_a1(i, col_idx)
                        updates.append({"range": cell, "values": [[str(value)]]})
                if updates:
                    _retry(lambda u=updates: ws.batch_update(u, value_input_option="RAW"))
                return True
        return False
    except Exception as e:
        logger.error(f"[ads_sheet] update_running_row failed: {e}")
        return False


def update_row_in_tab(sku: str, tab: str, fields: dict) -> bool:
    """Update specific fields on a row in any tab (identified by SKU)."""
    try:
        client = _get_client()
        ss     = _open_sheet(client)
        ws     = _ensure_tab(ss, tab)
        data   = ws.get_all_values()
        if not data:
            return False
        header = data[0]
        for i, row in enumerate(data[1:], start=2):
            d = _row_to_dict(header, row)
            if d.get("SKU") == sku:
                for field, value in fields.items():
                    if field in header:
                        col_idx = header.index(field) + 1
                        ws.update_cell(i, col_idx, str(value))
                return True
        logger.warning(f"[ads_sheet] update_row_in_tab: SKU {sku} not found in {tab}")
        return False
    except Exception as e:
        logger.error(f"[ads_sheet] update_row_in_tab failed: {e}")
        return False


def load_running_rows() -> list[dict]:
    """Load all rows from ADS RUNNING tab."""
    try:
        client = _get_client()
        ss = _open_sheet(client)
        ws = _ensure_tab(ss, TAB_RUNNING)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return []
        header = rows[0]
        return [_row_to_dict(header, r) for r in rows[1:] if any(c.strip() for c in r)]
    except Exception as e:
        logger.error(f"[ads_sheet] load_running_rows failed: {e}")
        return []
