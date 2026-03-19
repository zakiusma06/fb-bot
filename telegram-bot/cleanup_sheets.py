"""
cleanup_sheets.py - Remove obsolete sheet tabs from the Google Spreadsheet.

Keeps: PENDING, APPROVED, DISAPPROVED, READY FOR ADS, ADS RUNNING, WINNER, LOSER, ADS ERROR
Deletes: any other tab (PREAPPROVED, MODERATION, PRICING, CREATIVE_QUEUE, TESTING, etc.)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import gspread
from google.oauth2.service_account import Credentials
from config import GOOGLE_SHEET_NAME, get_google_credentials_dict

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REQUIRED_TABS = {
    "PENDING",
    "APPROVED",
    "DISAPPROVED",
    "READY FOR ADS",
    "ADS RUNNING",
    "WINNER",
    "LOSER",
    "ADS ERROR",
}

# Also keep the exact spelling used in mod_sheet.py for DISAPPROVED
REQUIRED_TABS.add("DISAPROVED")


def run_cleanup():
    creds = Credentials.from_service_account_info(
        get_google_credentials_dict(), scopes=SCOPES
    )
    client      = gspread.authorize(creds)
    spreadsheet = client.open(GOOGLE_SHEET_NAME)

    all_worksheets = spreadsheet.worksheets()
    all_titles     = [ws.title for ws in all_worksheets]

    print(f"\nSpreadsheet: {GOOGLE_SHEET_NAME}")
    print(f"All existing tabs: {all_titles}\n")

    to_delete = [ws for ws in all_worksheets if ws.title not in REQUIRED_TABS]

    if not to_delete:
        print("Nothing to delete — all tabs are already in the required list.")
        return

    print(f"Tabs to DELETE: {[ws.title for ws in to_delete]}")
    print(f"Tabs to KEEP:   {[ws.title for ws in all_worksheets if ws.title in REQUIRED_TABS]}\n")

    for ws in to_delete:
        print(f"  Deleting '{ws.title}'…", end=" ")
        spreadsheet.del_worksheet(ws)
        print("done.")

    remaining = [ws.title for ws in spreadsheet.worksheets()]
    print(f"\nFinal tab list: {remaining}")


if __name__ == "__main__":
    run_cleanup()
