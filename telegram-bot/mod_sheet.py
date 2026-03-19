"""
mod_sheet.py - Google Sheets operations for the moderation bot.

Provides:
  load_pending_rows()        → list of row dicts from the PENDING tab
  move_row(sku, target_tab, new_status)
                             → move row from PENDING to another tab
  update_statistics_tab()    → refresh the STATISTICS tab with live counts
"""

import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials

from config import (
    GOOGLE_SHEET_NAME,
    PENDING_COLUMNS,
    SHEET_COLUMNS,
    DISAPPROVED_COLUMNS,
    ADS_COLUMNS,
    get_google_credentials_dict,
)
from utils import safe_str

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

STATUS_TABS = [
    "PENDING",
    "PREAPPROVED",
    "APPROVED",
    "DISAPROVED",
    "READY FOR ADS",
    "ADS RUNNING",
    "WINNER",
    "LOSER",
]

# Per-tab column definitions
TAB_COLUMNS = {
    "PENDING":       PENDING_COLUMNS,
    "PREAPPROVED":   ADS_COLUMNS,
    "APPROVED":      SHEET_COLUMNS,
    "DISAPROVED":    DISAPPROVED_COLUMNS,
    "READY FOR ADS": ADS_COLUMNS,
    "ADS RUNNING":   ADS_COLUMNS,
    "WINNER":        ADS_COLUMNS,
    "LOSER":         ADS_COLUMNS,
}

STATS_TAB = "STATISTICS"


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        get_google_credentials_dict(), scopes=SCOPES
    )
    return gspread.authorize(creds)


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    return client.open(GOOGLE_SHEET_NAME)


def _ensure_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """Return the worksheet, creating it with the correct per-tab header if it doesn't exist."""
    cols = TAB_COLUMNS.get(tab_name, SHEET_COLUMNS)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=tab_name, rows=1000, cols=len(cols)
        )
        ws.append_row(cols, value_input_option="USER_ENTERED")
        logger.info(f"[mod_sheet] Created tab '{tab_name}'")
        return ws

    existing = ws.row_values(1)
    if not existing:
        ws.append_row(cols, value_input_option="USER_ENTERED")
    elif existing != cols:
        # Header is outdated — read records with the OLD header so that
        # column name → value mapping stays correct, then rewrite everything.
        try:
            existing_records = ws.get_all_records(default_blank="")
        except Exception:
            existing_records = []
        ws.clear()
        new_rows = [cols]
        for rec in existing_records:
            new_rows.append([safe_str(rec.get(col, "")) for col in cols])
        if ws.col_count < len(cols):
            ws.resize(cols=len(cols))
        ws.update("A1", new_rows, value_input_option="USER_ENTERED")
        logger.info(
            f"[mod_sheet] Migrated tab '{tab_name}' to new columns "
            f"({len(existing_records)} data rows preserved)"
        )
    return ws


def _count_data_rows(spreadsheet: gspread.Spreadsheet, tab_name: str) -> int:
    """Return the number of data rows (excluding header) in a tab, or 0 if missing."""
    try:
        ws = spreadsheet.worksheet(tab_name)
        all_vals = ws.get_all_values()
        if not all_vals:
            return 0
        return max(0, len(all_vals) - 1)  # subtract header row
    except gspread.WorksheetNotFound:
        return 0
    except Exception as e:
        logger.warning(f"[mod_sheet] Could not count rows in '{tab_name}': {e}")
        return 0


def update_statistics_tab(spreadsheet: gspread.Spreadsheet | None = None) -> None:
    """
    Rebuild the STATISTICS tab with live counts from all STATUS_TABS.

    Layout:
      Row 1 : Header  — Metric | Value
      Row 2 : Pending
      Row 3 : Approved
      Row 4 : Disapproved
      Row 5 : Ready For Ads
      Row 6 : Ads Running
      Row 7 : Winner
      Row 8 : Loser
      Row 9 : (blank)
      Row 10: Approval Rate  (APPROVED / (APPROVED + DISAPPROVED))
    """
    try:
        if spreadsheet is None:
            client = _get_client()
            spreadsheet = _open_spreadsheet(client)

        counts = {tab: _count_data_rows(spreadsheet, tab) for tab in STATUS_TABS}

        approved    = counts.get("APPROVED", 0)
        disapproved = counts.get("DISAPROVED", 0)
        reviewed    = approved + disapproved
        if reviewed > 0:
            rate = f"{round(approved / reviewed * 100, 1)}%"
        else:
            rate = "N/A"

        # Build the rows to write
        display_names = {
            "PENDING":       "Pending",
            "PREAPPROVED":   "Pre-Approved",
            "APPROVED":      "Approved",
            "DISAPROVED":    "Disapproved",
            "READY FOR ADS": "Ready For Ads",
            "ADS RUNNING":   "Ads Running",
            "WINNER":        "Winner",
            "LOSER":         "Loser",
        }

        data = [["Metric", "Value"]]
        for tab in STATUS_TABS:
            data.append([display_names[tab], counts[tab]])
        data.append(["", ""])                        # blank row
        data.append(["Approval Rate", rate])

        # Get or create the STATISTICS worksheet
        try:
            stats_ws = spreadsheet.worksheet(STATS_TAB)
        except gspread.WorksheetNotFound:
            stats_ws = spreadsheet.add_worksheet(
                title=STATS_TAB, rows=20, cols=2
            )
            logger.info(f"[mod_sheet] Created '{STATS_TAB}' tab")

        # Clear and rewrite
        stats_ws.clear()
        stats_ws.update("A1", data, value_input_option="USER_ENTERED")

        logger.info(f"[mod_sheet] STATISTICS tab updated — {counts}")

    except Exception as e:
        logger.error(f"[mod_sheet] update_statistics_tab failed: {e}")


def get_statistics() -> dict:
    """
    Read current row counts from all STATUS_TABS and return a dict:
      { tab_name: count, ..., "approval_rate": "62.5%" }
    Also triggers a refresh of the STATISTICS tab.
    """
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)
        counts = {tab: _count_data_rows(spreadsheet, tab) for tab in STATUS_TABS}
        approved    = counts.get("APPROVED", 0)
        disapproved = counts.get("DISAPROVED", 0)
        reviewed    = approved + disapproved
        counts["approval_rate"] = (
            f"{round(approved / reviewed * 100, 1)}%" if reviewed > 0 else "N/A"
        )
        # Refresh stats tab in the background (best-effort)
        try:
            update_statistics_tab(spreadsheet)
        except Exception:
            pass
        return counts
    except Exception as e:
        logger.error(f"[mod_sheet] get_statistics failed: {e}")
        return {}


def load_pending_rows(
    price_range: str = "",
    keyword: str = "",
    variants: str = "",
) -> list[dict]:
    """
    Load rows from the PENDING tab with optional filters (all combinable).

    price_range: one of "" | "under5" | "5to10" | "10to20" | "20plus"
    keyword:     exact keyword string, or "" for all
    variants:    "" | "yes" | "no"

    Returns a list of row dicts. Empty / header-only sheets return [].
    """
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)
        ws = _ensure_tab(spreadsheet, "PENDING")
        rows = ws.get_all_records()
        logger.info(f"[mod_sheet] Loaded {len(rows)} pending row(s) (raw)")

        if price_range:
            filtered = []
            for row in rows:
                raw = str(row.get("SOURCING PRICE USD", "") or "").strip()
                try:
                    price = float(raw)
                except ValueError:
                    continue
                if price_range == "under5"  and price < 5:
                    filtered.append(row)
                elif price_range == "5to10" and 5 <= price < 10:
                    filtered.append(row)
                elif price_range == "10to20" and 10 <= price < 20:
                    filtered.append(row)
                elif price_range == "20plus" and price >= 20:
                    filtered.append(row)
            rows = filtered

        if keyword:
            rows = [r for r in rows if (r.get("KEYWORD") or "").strip() == keyword]

        if variants == "yes":
            rows = [r for r in rows if (r.get("HAS VARIANTS") or "").strip().upper() == "YES"]
        elif variants == "no":
            rows = [r for r in rows if (r.get("HAS VARIANTS") or "").strip().upper() == "NO"]

        logger.info(
            f"[mod_sheet] After filters (price={price_range!r} "
            f"kw={keyword!r} variants={variants!r}) → {len(rows)} row(s)"
        )
        return rows
    except Exception as e:
        logger.error(f"[mod_sheet] load_pending_rows failed: {e}")
        return []


def load_pending_keywords() -> list[str]:
    """
    Return keywords from PENDING sorted by approval rate (highest first).
    Approval rate = approved count / (approved + pending count) per keyword.
    Falls back to alphabetical sort if APPROVED tab is unavailable.
    """
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)

        pending_ws = _ensure_tab(spreadsheet, "PENDING")
        pending_rows = pending_ws.get_all_records()

        approved_ws = _ensure_tab(spreadsheet, "APPROVED")
        approved_rows = approved_ws.get_all_records()

        pending_counts: dict[str, int] = {}
        for row in pending_rows:
            kw = (row.get("KEYWORD") or "").strip()
            if kw:
                pending_counts[kw] = pending_counts.get(kw, 0) + 1

        approved_counts: dict[str, int] = {}
        for row in approved_rows:
            kw = (row.get("KEYWORD") or "").strip()
            if kw:
                approved_counts[kw] = approved_counts.get(kw, 0) + 1

        all_kws = list(pending_counts.keys())

        def _approval_rate(kw: str) -> float:
            p = pending_counts.get(kw, 0)
            a = approved_counts.get(kw, 0)
            total = a + p
            return a / total if total > 0 else 0.0

        all_kws.sort(key=lambda k: (-_approval_rate(k), k))
        return all_kws
    except Exception as e:
        logger.error(f"[mod_sheet] load_pending_keywords failed: {e}")
        return []


def _col_letter(col_idx: int) -> str:
    """Convert 0-based column index to A1-notation letter (handles >26 cols)."""
    result = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def update_pending_fields(sku: str, **fields) -> bool:
    """
    Update any set of columns in the PENDING tab for the row matching SKU.
    Keyword arguments must match column names exactly (e.g. PRICE="99000").
    Only non-None values are written. Returns True on success.
    """
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)
        ws = _ensure_tab(spreadsheet, "PENDING")

        all_values = ws.get_all_values()
        if not all_values:
            logger.warning(f"[mod_sheet] PENDING is empty — cannot update fields for {sku}")
            return False

        header = all_values[0]
        sku_col = header.index("SKU") if "SKU" in header else None
        if sku_col is None:
            logger.error("[mod_sheet] SKU column missing from PENDING header")
            return False

        row_num = None
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) > sku_col and row[sku_col] == sku:
                row_num = i
                break

        if row_num is None:
            logger.warning(f"[mod_sheet] SKU '{sku}' not found in PENDING for field update")
            return False

        for field_name, value in fields.items():
            if value is None:
                continue
            if field_name not in header:
                logger.warning(f"[mod_sheet] Column '{field_name}' not in PENDING header — skipping")
                continue
            col_idx = header.index(field_name)
            cell = f"{_col_letter(col_idx)}{row_num}"
            ws.update(cell, [[str(value)]], value_input_option="USER_ENTERED")

        logger.info(f"[mod_sheet] update_pending_fields for '{sku}': {list(fields.keys())}")
        return True

    except Exception as e:
        logger.error(f"[mod_sheet] update_pending_fields failed for '{sku}': {e}")
        return False


def update_sourcing_data(
    sku: str,
    price_usd: str = "",
    weight_gram: str = "",
    sourcing_url: str = "",
) -> bool:
    """
    Update SOURCING PRICE USD, WEIGHT GRAM, and/or SOURCING URL for a row in the PENDING tab.
    Only fields with non-empty values are updated.
    Returns True on success.
    """
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)
        ws = _ensure_tab(spreadsheet, "PENDING")

        all_values = ws.get_all_values()
        if not all_values:
            logger.warning(f"[mod_sheet] PENDING is empty — cannot update sourcing for {sku}")
            return False

        header = all_values[0]

        sku_col = header.index("SKU") if "SKU" in header else None
        if sku_col is None:
            logger.error("[mod_sheet] SKU column missing from PENDING header")
            return False

        price_col       = header.index("SOURCING PRICE USD") if "SOURCING PRICE USD" in header else None
        weight_col      = header.index("WEIGHT GRAM")        if "WEIGHT GRAM"        in header else None
        sourcing_url_col = header.index("SOURCING URL")      if "SOURCING URL"       in header else None

        row_num = None
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) > sku_col and row[sku_col] == sku:
                row_num = i
                break

        if row_num is None:
            logger.warning(f"[mod_sheet] SKU '{sku}' not found in PENDING for sourcing update")
            return False

        updates = []
        if price_usd and price_col is not None:
            col_letter = chr(ord("A") + price_col)
            updates.append((f"{col_letter}{row_num}", price_usd))

        if weight_gram and weight_col is not None:
            col_letter = chr(ord("A") + weight_col)
            updates.append((f"{col_letter}{row_num}", weight_gram))

        if sourcing_url and sourcing_url_col is not None:
            col_letter = chr(ord("A") + sourcing_url_col)
            updates.append((f"{col_letter}{row_num}", sourcing_url))

        for cell_addr, value in updates:
            ws.update(cell_addr, [[value]], value_input_option="USER_ENTERED")

        logger.info(
            f"[mod_sheet] Updated sourcing for SKU '{sku}': "
            f"price_usd={price_usd!r} weight_gram={weight_gram!r}"
        )
        return True

    except Exception as e:
        logger.error(f"[mod_sheet] update_sourcing_data failed for '{sku}': {e}")
        return False


def move_row(sku: str, target_tab: str, new_status: str) -> bool:
    """
    Find the row in PENDING with matching SKU, update its STATU,
    append it to target_tab, then delete it from PENDING.
    Automatically refreshes the STATISTICS tab afterwards.

    Retries up to 3 times with exponential backoff on API errors.
    Returns True on success, False if the SKU was not found or all retries failed.
    """
    for attempt in range(3):
        try:
            client = _get_client()
            spreadsheet = _open_spreadsheet(client)

            pending_ws = _ensure_tab(spreadsheet, "PENDING")
            target_ws  = _ensure_tab(spreadsheet, target_tab)

            # Find the row in PENDING (row 1 is header → data starts at row 2)
            all_values = pending_ws.get_all_values()
            if not all_values:
                logger.warning(f"[mod_sheet] PENDING tab is empty — SKU {sku} not found")
                return False

            header      = all_values[0]
            sku_col_idx = header.index("SKU") if "SKU" in header else None

            if sku_col_idx is None:
                logger.error("[mod_sheet] SKU column not found in PENDING header")
                return False

            row_num    = None
            row_values = None
            for i, row in enumerate(all_values[1:], start=2):
                if len(row) > sku_col_idx and row[sku_col_idx] == sku:
                    row_num    = i
                    row_values = list(row)
                    break

            if row_num is None:
                logger.warning(f"[mod_sheet] SKU '{sku}' not found in PENDING")
                return False

            # Pad row to source header width
            while len(row_values) < len(header):
                row_values.append("")

            # Build a dict keyed by the PENDING header
            row_dict = {header[i]: row_values[i] for i in range(len(header))}

            # Override STATU with the new status value
            row_dict["STATU"] = new_status

            # Build the target row using the target tab's column definition
            target_cols = TAB_COLUMNS.get(target_tab, SHEET_COLUMNS)
            target_row  = [row_dict.get(col, "") for col in target_cols]

            # Append to target tab (do this BEFORE delete so no data is lost on error)
            target_ws.append_row(target_row, value_input_option="USER_ENTERED")
            logger.info(f"[mod_sheet] Appended SKU '{sku}' to '{target_tab}'")

            # Delete from PENDING
            pending_ws.delete_rows(row_num)
            logger.info(f"[mod_sheet] Deleted row {row_num} (SKU '{sku}') from PENDING")

            # Refresh statistics — best-effort, never blocks success
            update_statistics_tab(spreadsheet)

            return True

        except Exception as e:
            if attempt < 2:
                wait = 2 ** attempt  # 1s then 2s
                logger.warning(
                    f"[mod_sheet] move_row attempt {attempt + 1} failed for '{sku}': {e} "
                    f"— retrying in {wait}s…"
                )
                time.sleep(wait)
            else:
                logger.error(f"[mod_sheet] move_row failed for SKU '{sku}' after 3 attempts: {e}")
                return False
