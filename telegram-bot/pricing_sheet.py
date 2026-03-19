"""
pricing_sheet.py - Google Sheets operations for the Pricing Bot.

Provides:
  load_unpriced_rows()           → rows from APPROVED where PRICE or COMPARE AT PRICE is empty
  update_pricing(sku, price, compare_at_price)
                                 → update those two columns in the APPROVED tab
  get_pricing_stats()            → dict with totals for the /stats command
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SHEET_NAME, SHEET_COLUMNS, ADS_COLUMNS, get_google_credentials_dict

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

APPROVED_TAB  = "APPROVED"
STATS_TAB     = "STATISTICS"

# Column indices (0-based) derived from SHEET_COLUMNS
_COL         = {name: i for i, name in enumerate(SHEET_COLUMNS)}
SKU_IDX      = _COL["SKU"]
PRICE_IDX    = _COL["PRICE"]
CMP_IDX      = _COL["COMPARE AT PRICE"]


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        get_google_credentials_dict(), scopes=SCOPES
    )
    return gspread.authorize(creds)


def _open_sheet(client: gspread.Client) -> gspread.Spreadsheet:
    return client.open(GOOGLE_SHEET_NAME)


_TAB_COLUMNS = {
    "APPROVED":      SHEET_COLUMNS,
    "READY FOR ADS": ADS_COLUMNS,
}


def _get_or_create_tab(
    spreadsheet: gspread.Spreadsheet, tab_name: str
) -> gspread.Worksheet:
    cols = _TAB_COLUMNS.get(tab_name, SHEET_COLUMNS)
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=tab_name, rows=1000, cols=len(cols)
        )
        ws.append_row(cols, value_input_option="USER_ENTERED")
        logger.info(f"[pricing_sheet] Created tab '{tab_name}'")
        return ws


def load_unpriced_rows() -> list[dict]:
    """
    Return all rows from the APPROVED tab where PRICE or COMPARE AT PRICE is empty.
    Each row is returned as a dict keyed by SHEET_COLUMNS.
    """
    try:
        client      = _get_client()
        spreadsheet = _open_sheet(client)
        ws          = _get_or_create_tab(spreadsheet, APPROVED_TAB)
        all_rows    = ws.get_all_records()

        unpriced = [
            row for row in all_rows
            if not str(row.get("PRICE", "")).strip()
            or not str(row.get("COMPARE AT PRICE", "")).strip()
        ]
        logger.info(
            f"[pricing_sheet] APPROVED: {len(all_rows)} total, "
            f"{len(unpriced)} unpriced"
        )
        return unpriced
    except Exception as e:
        logger.error(f"[pricing_sheet] load_unpriced_rows failed: {e}")
        return []


def update_pricing(sku: str, price: str, compare_at_price: str) -> bool:
    """
    Find the row in APPROVED with matching SKU and update PRICE + COMPARE AT PRICE.
    All other columns are preserved.
    Returns True on success, False if SKU not found.
    """
    try:
        client      = _get_client()
        spreadsheet = _open_sheet(client)
        ws          = _get_or_create_tab(spreadsheet, APPROVED_TAB)
        all_values  = ws.get_all_values()

        if not all_values:
            logger.warning("[pricing_sheet] APPROVED tab is empty")
            return False

        header = all_values[0]
        try:
            sku_col   = header.index("SKU")
            price_col = header.index("PRICE")
            cmp_col   = header.index("COMPARE AT PRICE")
        except ValueError as e:
            logger.error(f"[pricing_sheet] Missing column in header: {e}")
            return False

        row_num = None
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) > sku_col and row[sku_col] == sku:
                row_num = i
                break

        if row_num is None:
            logger.warning(f"[pricing_sheet] SKU '{sku}' not found in APPROVED")
            return False

        # Update only PRICE and COMPARE AT PRICE cells (1-based col index)
        ws.update_cell(row_num, price_col + 1, price)
        ws.update_cell(row_num, cmp_col + 1, compare_at_price)
        logger.info(
            f"[pricing_sheet] Updated SKU '{sku}': "
            f"PRICE={price}, COMPARE AT PRICE={compare_at_price}"
        )

        # Refresh STATISTICS tab
        _refresh_stats(spreadsheet)
        return True

    except Exception as e:
        logger.error(f"[pricing_sheet] update_pricing failed for SKU '{sku}': {e}")
        return False


def get_pricing_stats() -> dict:
    """
    Return a dict with:
      approved_total, priced, unpriced, completion_pct (string)
    Also refreshes the STATISTICS tab.
    """
    try:
        client      = _get_client()
        spreadsheet = _open_sheet(client)
        ws          = _get_or_create_tab(spreadsheet, APPROVED_TAB)
        all_rows    = ws.get_all_records()

        total   = len(all_rows)
        priced  = sum(
            1 for r in all_rows
            if str(r.get("PRICE", "")).strip()
            and str(r.get("COMPARE AT PRICE", "")).strip()
        )
        unpriced = total - priced
        pct      = f"{round(priced / total * 100, 1)}%" if total > 0 else "N/A"

        _refresh_stats(spreadsheet)
        return {
            "approved_total": total,
            "priced":         priced,
            "unpriced":       unpriced,
            "completion_pct": pct,
        }
    except Exception as e:
        logger.error(f"[pricing_sheet] get_pricing_stats failed: {e}")
        return {}


READY_TAB = "READY FOR ADS"


def publish_approved_row(sku: str, product_name: str, url_landing_page: str) -> bool:
    """
    Move the row matching SKU from APPROVED to READY FOR ADS:
      1. Find the row in APPROVED
      2. Update PRODUCT NAME, URL LANDING PAGE, STATU = READY FOR ADS on the row data
      3. Append the full updated row to READY FOR ADS tab
      4. Delete the original row from APPROVED
    Returns True on success.
    """
    try:
        client      = _get_client()
        spreadsheet = _open_sheet(client)
        approved_ws = _get_or_create_tab(spreadsheet, APPROVED_TAB)
        all_values  = approved_ws.get_all_values()

        if not all_values:
            logger.warning("[pricing_sheet] APPROVED tab is empty — nothing to move")
            return False

        header = all_values[0]
        try:
            sku_col  = header.index("SKU")
            name_col = header.index("PRODUCT NAME")
            url_col  = header.index("URL LANDING PAGE")
            stat_col = header.index("STATU")
        except ValueError as e:
            logger.error(f"[pricing_sheet] publish_approved_row missing column: {e}")
            return False

        row_num = None
        row_data = None
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) > sku_col and row[sku_col] == sku:
                row_num = i
                row_data = list(row)
                break

        if row_num is None or row_data is None:
            logger.warning(f"[pricing_sheet] SKU '{sku}' not found in APPROVED for move")
            return False

        # Pad row to source header length in case trailing cells are missing
        while len(row_data) < len(header):
            row_data.append("")

        # Build a dict from the APPROVED row (keyed by that tab's header)
        row_dict = {header[i]: row_data[i] for i in range(len(header))}

        # Apply the three updated fields
        row_dict["PRODUCT NAME"]     = product_name
        row_dict["URL LANDING PAGE"] = url_landing_page
        row_dict["STATU"]            = "READY FOR ADS"

        logger.info(f"[pricing_sheet] Moving SKU '{sku}' from APPROVED to READY FOR ADS")

        # 1. Build the READY FOR ADS row using ADS_COLUMNS (extra creative cols default to "")
        ready_row = [row_dict.get(col, "") for col in ADS_COLUMNS]

        ready_ws = _get_or_create_tab(spreadsheet, READY_TAB)
        ready_ws.append_row(ready_row, value_input_option="USER_ENTERED")
        logger.info(f"[pricing_sheet] Row for SKU '{sku}' appended to READY FOR ADS ✅")

        # 2. Delete original row from APPROVED (row_num is 1-based, sheet rows are 1-based)
        approved_ws.delete_rows(row_num)
        logger.info(f"[pricing_sheet] Original row for SKU '{sku}' deleted from APPROVED ✅")

        return True

    except Exception as e:
        logger.error(f"[pricing_sheet] publish_approved_row failed for SKU '{sku}': {e}")
        return False


def _refresh_stats(spreadsheet: gspread.Spreadsheet) -> None:
    """
    Add / update a PRICING section at the bottom of the STATISTICS tab.
    Creates the tab if it doesn't exist.
    """
    try:
        try:
            stats_ws = spreadsheet.worksheet(STATS_TAB)
        except gspread.WorksheetNotFound:
            stats_ws = spreadsheet.add_worksheet(title=STATS_TAB, rows=30, cols=2)
            logger.info(f"[pricing_sheet] Created '{STATS_TAB}' tab")

        # Read existing content so we can append below it
        existing = stats_ws.get_all_values()

        # Find or create the "PRICING" section header row
        pricing_start = None
        for i, row in enumerate(existing):
            if row and str(row[0]).strip().upper() == "PRICING":
                pricing_start = i + 1  # 1-based
                break

        # Calculate stats
        approved_ws = _get_or_create_tab(spreadsheet, APPROVED_TAB)
        all_rows    = approved_ws.get_all_records()
        total   = len(all_rows)
        priced  = sum(
            1 for r in all_rows
            if str(r.get("PRICE", "")).strip()
            and str(r.get("COMPARE AT PRICE", "")).strip()
        )
        unpriced = total - priced
        pct      = f"{round(priced / total * 100, 1)}%" if total > 0 else "N/A"

        pricing_rows = [
            ["PRICING",               ""],
            ["Approved Total",         total],
            ["Priced",                 priced],
            ["Not Yet Priced",         unpriced],
            ["Pricing Completion",     pct],
        ]

        if pricing_start is not None:
            # Overwrite in place
            cell_range = f"A{pricing_start}:B{pricing_start + len(pricing_rows) - 1}"
            stats_ws.update(cell_range, pricing_rows, value_input_option="USER_ENTERED")
        else:
            # Append a blank separator then the pricing block
            blank_row_num = len(existing) + 1
            stats_ws.update(
                f"A{blank_row_num}:B{blank_row_num + len(pricing_rows)}",
                [["", ""]] + pricing_rows,
                value_input_option="USER_ENTERED",
            )

        logger.info("[pricing_sheet] STATISTICS tab refreshed with pricing section")
    except Exception as e:
        logger.warning(f"[pricing_sheet] _refresh_stats failed: {e}")
