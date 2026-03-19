"""
sheet_writer.py - Read and write product cluster rows to Google Sheets.

Tab structure:
  PENDING | APPROVED | DISAPROVED | READY FOR ADS | ADS RUNNING | WINNER | LOSER

Rules:
  - All tabs share the same full header (SHEET_COLUMNS).
  - Newly extracted rows are always written to the PENDING tab.
  - STATU is always set to "PENDING" for new rows.
  - Only SKU, KEYWORD, URL PRODUCT, ADS LIBRARY MEDIA URL,
    SOURCING PRICE USD, SOURCING URL, STATU are filled; all others are left blank.
  - SKU numbering scans every tab to find the global max.
  - Dedup check reads every tab.
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

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

# Ordered list of all status tabs — created automatically if missing.
STATUS_TABS = [
    "PENDING",
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
    "APPROVED":      SHEET_COLUMNS,
    "DISAPROVED":    DISAPPROVED_COLUMNS,
    "READY FOR ADS": ADS_COLUMNS,
    "ADS RUNNING":   ADS_COLUMNS,
    "WINNER":        ADS_COLUMNS,
    "LOSER":         ADS_COLUMNS,
}

# Columns filled automatically for new rows (all others stay blank).
FILLED_COLUMNS = {
    "SKU", "KEYWORD",
    "URL PRODUCT", "ADS LIBRARY MEDIA URL",
    "SOURCING PRICE USD", "SOURCING URL",
    "WEIGHT GRAM", "HAS VARIANTS",
    "STATU",
    "IMAGE URL",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    creds_dict = get_google_credentials_dict()
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Tab management ────────────────────────────────────────────────────────────

def _ensure_tab(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """
    Return the worksheet for tab_name, creating it with the correct header row
    if it does not yet exist. Each tab uses its own column definition.
    """
    cols = TAB_COLUMNS.get(tab_name, SHEET_COLUMNS)

    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        logger.info(f"[sheet] Tab '{tab_name}' not found — creating it…")
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(cols))

    # Ensure the header row is correct
    existing = ws.row_values(1)
    if existing == cols:
        pass  # already correct
    elif not existing:
        ws.append_row(cols, value_input_option="USER_ENTERED")
        logger.info(f"[sheet] Wrote header row to tab '{tab_name}'")
    else:
        # Header is outdated — migrate data rows to the new column layout.
        # Read all records NOW (while the old header is still in place) so
        # that column name → value mapping is correct, then rewrite everything
        # under the new header, keeping only the columns that exist in cols.
        try:
            existing_records = ws.get_all_records(default_blank="")
        except Exception:
            existing_records = []

        # Clear the entire sheet and rewrite with new header + remapped rows
        ws.clear()
        new_rows = [cols]
        for rec in existing_records:
            new_rows.append([safe_str(rec.get(col, "")) for col in cols])
        if ws.col_count < len(cols):
            ws.resize(cols=len(cols))
        ws.update("A1", new_rows, value_input_option="USER_ENTERED")
        logger.info(
            f"[sheet] Migrated tab '{tab_name}' to new columns "
            f"({len(existing_records)} data rows preserved)"
        )

    return ws


def _get_or_create_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    """Open (or create) the spreadsheet and ensure all status tabs exist."""
    try:
        spreadsheet = client.open(GOOGLE_SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        logger.info(f"[sheet] Spreadsheet '{GOOGLE_SHEET_NAME}' not found — creating it…")
        spreadsheet = client.create(GOOGLE_SHEET_NAME)
        try:
            creds = get_google_credentials_dict()
            owner_email = creds.get("client_email", "")
            if owner_email:
                spreadsheet.share(owner_email, perm_type="user", role="writer")
        except Exception:
            pass

    # Ensure every status tab exists (creates missing ones in order)
    for tab_name in STATUS_TABS:
        _ensure_tab(spreadsheet, tab_name)

    return spreadsheet


# ── Public API ────────────────────────────────────────────────────────────────

def get_next_sku_number() -> int:
    """
    Scan all status tabs and return the next available SKU number globally.
    Returns 1 if no SKUs exist anywhere.
    """
    try:
        client = _get_client()
        spreadsheet = _get_or_create_spreadsheet(client)
        max_num = 0
        for tab_name in STATUS_TABS:
            try:
                ws = spreadsheet.worksheet(tab_name)
                rows = ws.get_all_records()
                for row in rows:
                    sku = str(row.get("SKU", ""))
                    m = re.match(r"PRD-(\d+)", sku, re.IGNORECASE)
                    if m:
                        max_num = max(max_num, int(m.group(1)))
            except Exception:
                pass
        return max_num + 1
    except gspread.exceptions.APIError as e:
        _handle_api_error(e)
        return 1
    except Exception as e:
        logger.warning(f"[sheet] Could not read existing SKUs: {e}")
        return 1


def read_existing_rows() -> list[dict]:
    """
    Read all rows from every status tab (for deduplication checks).
    Returns a flat list of row dicts, each with an extra '_tab' key
    indicating which tab the row came from (used for dedup logging).
    """
    results: list[dict] = []
    try:
        client = _get_client()
        spreadsheet = _get_or_create_spreadsheet(client)
        for tab_name in STATUS_TABS:
            try:
                ws = spreadsheet.worksheet(tab_name)
                tab_rows = ws.get_all_records()
                for row in tab_rows:
                    row["_tab"] = tab_name
                results.extend(tab_rows)
                logger.debug(f"[sheet] read_existing_rows: {len(tab_rows)} rows from '{tab_name}'")
            except Exception as e:
                logger.debug(f"[sheet] read_existing_rows: could not read '{tab_name}': {e}")
        logger.info(f"[sheet] read_existing_rows: {len(results)} total rows loaded for dedup")
        return results
    except gspread.exceptions.APIError as e:
        _handle_api_error(e)
        return []
    except Exception as e:
        logger.warning(f"[sheet] Could not read existing rows: {e}")
        return []


def read_keyword_stats() -> list[dict]:
    """
    Read APPROVED and DISAPROVED tabs and return keyword approval stats.

    Each returned dict has:
        keyword       – the keyword string
        approved      – number of approved products
        disapproved   – number of disapproved products
        total         – approved + disapproved
        approval_rate – float 0.0–1.0

    All keywords with at least 1 reviewed occurrence are included.
    Sorted by approval_rate descending.

    NOTE: intentionally skips _get_or_create_spreadsheet() to avoid the
    7-tab validation loop (7+ API round-trips). Only the two needed tabs
    are fetched here.
    """
    import socket
    from collections import defaultdict

    logger.warning(">>>>>> [TRACE] read_keyword_stats() ENTERED <<<<<<")

    # Hard socket timeout — forces every network call in this thread to fail
    # after 8s regardless of gspread version, requests version, or SSL state.
    # Saved and restored so other threads are not affected after we return.
    _old_socket_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    logger.info("[sheet] read_keyword_stats: socket timeout set to 8s")

    approved_count: dict[str, int] = defaultdict(int)
    disapproved_count: dict[str, int] = defaultdict(int)

    try:
        logger.info("[sheet] read_keyword_stats: authenticating…")
        client = _get_client()
        logger.info("[sheet] read_keyword_stats: opening spreadsheet…")
        spreadsheet = client.open(GOOGLE_SHEET_NAME)

        for tab_name, counter in (("APPROVED", approved_count), ("DISAPROVED", disapproved_count)):
            try:
                logger.info(f"[sheet] read_keyword_stats: reading tab '{tab_name}'…")
                ws = spreadsheet.worksheet(tab_name)
                rows = ws.get_all_records()
                logger.info(f"[sheet] read_keyword_stats: {len(rows)} rows in '{tab_name}'")
                for row in rows:
                    kw = str(row.get("KEYWORD", "") or "").strip().lower()
                    if kw:
                        counter[kw] += 1
            except Exception as e:
                logger.warning(f"[sheet] read_keyword_stats: could not read '{tab_name}': {e}")

    except Exception as e:
        logger.warning(f"[sheet] read_keyword_stats: failed: {e}")
        return []

    finally:
        socket.setdefaulttimeout(_old_socket_timeout)
        logger.info("[sheet] read_keyword_stats: socket timeout restored")

    all_keywords = set(approved_count) | set(disapproved_count)
    results = []
    for kw in all_keywords:
        appr = approved_count.get(kw, 0)
        disp = disapproved_count.get(kw, 0)
        total = appr + disp
        if total < 1:
            continue
        results.append({
            "keyword": kw,
            "approved": appr,
            "disapproved": disp,
            "total": total,
            "approval_rate": appr / total,
        })

    results.sort(key=lambda x: x["approval_rate"], reverse=True)
    logger.info(f"[sheet] read_keyword_stats: returning {len(results)} keywords")
    return results


def append_cluster_rows(cluster_rows: list[dict]) -> int:
    """
    Write new cluster rows to the PENDING tab.

    Only fills FILLED_COLUMNS; all other columns are written as empty strings.
    STATU is always forced to "PENDING".

    Returns the number of rows written.
    """
    if not cluster_rows:
        return 0
    try:
        client = _get_client()
        spreadsheet = _get_or_create_spreadsheet(client)
        pending_ws = spreadsheet.worksheet("PENDING")

        data = []
        for row in cluster_rows:
            # Force STATU = PENDING; only write the allowed columns
            clean_row = {col: "" for col in PENDING_COLUMNS}
            for col in FILLED_COLUMNS:
                if col in clean_row:
                    clean_row[col] = safe_str(row.get(col, ""))
            clean_row["STATU"] = "PENDING"
            data.append([clean_row[col] for col in PENDING_COLUMNS])

        pending_ws.append_rows(data, value_input_option="USER_ENTERED")
        logger.info(
            f"[sheet] Appended {len(cluster_rows)} row(s) to "
            f"'{GOOGLE_SHEET_NAME}' → PENDING tab"
        )
        return len(cluster_rows)
    except gspread.exceptions.APIError as e:
        _handle_api_error(e)
        raise
    except Exception as e:
        logger.error(f"[sheet] Failed to write cluster rows: {e}")
        raise


def save_csv_backup(cluster_rows: list[dict], prefix: str = "clusters_backup") -> str | None:
    """Save a local CSV backup of cluster rows. Returns file path or None."""
    if not cluster_rows:
        return None
    try:
        Path("backups").mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = f"backups/{prefix}_{ts}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
            writer.writeheader()
            for row in cluster_rows:
                # Mirror the same blank-all-except-filled-columns rule
                clean = {col: "" for col in SHEET_COLUMNS}
                for col in FILLED_COLUMNS:
                    clean[col] = safe_str(row.get(col, ""))
                clean["STATU"] = "PENDING"
                writer.writerow(clean)
        logger.info(f"[sheet] CSV backup saved to {path}")
        return path
    except Exception as e:
        logger.warning(f"[sheet] CSV backup failed: {e}")
        return None


# ── Error handling ────────────────────────────────────────────────────────────

def _handle_api_error(e: gspread.exceptions.APIError):
    msg = str(e)
    if "drive.googleapis.com" in msg or "DRIVE" in msg.upper():
        logger.error(
            "Google Drive API is not enabled. "
            "Enable it at: https://console.cloud.google.com/apis/api/drive.googleapis.com/"
        )
    elif "sheets.googleapis.com" in msg or "SHEETS" in msg.upper():
        logger.error(
            "Google Sheets API is not enabled. "
            "Enable it at: https://console.cloud.google.com/apis/api/sheets.googleapis.com/"
        )
    else:
        logger.error(f"Google API error: {e}")
