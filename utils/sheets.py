from datetime import timedelta
import gspread
from google.oauth2.service_account import Credentials


def authorize_google_sheets(credentials_path: str) -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    return gspread.authorize(creds)


def get_previous_month_ending_balance(
    spreadsheet: gspread.Spreadsheet,
    current_month_date,
    col_letter: str = "H",
    default: float = 0
) -> float:
    """
    Read the ending balance from the previous month's sheet tab.
    Returns default if the sheet or cell is not found.
    """
    prev_month_date = current_month_date.replace(day=1) - timedelta(days=1)
    prev_month_name = prev_month_date.strftime("%B %Y")

    try:
        prev_sheet = spreadsheet.worksheet(prev_month_name)
    except gspread.exceptions.WorksheetNotFound:
        return default

    try:
        end_amount_cell = prev_sheet.find("End amount")
        row = end_amount_cell.row
        cell_value = prev_sheet.acell(f"{col_letter}{row}").value
        if cell_value:
            return float(cell_value.replace(",", ""))
        return default
    except gspread.exceptions.CellNotFound:
        return default


def link_dynamic_previous_month_balance(
    spreadsheet: gspread.Spreadsheet,
    current_month_date,
    start_cell: str = "H2"
) -> str:
    """
    Write a live formula into the current month's sheet that points
    to the previous month's ending balance cell. This means if the
    previous month's end balance changes, the current month's start
    balance updates automatically.

    Returns the formula string written, or "0" if no previous sheet exists.
    """
    prev_month_date = current_month_date.replace(day=1) - timedelta(days=1)
    prev_month_name = prev_month_date.strftime("%B %Y")
    curr_month_name = current_month_date.strftime("%B %Y")

    try:
        prev_sheet = spreadsheet.worksheet(prev_month_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"No previous sheet found for {prev_month_name}, using 0 as starting balance")
        spreadsheet.worksheet(curr_month_name).update_acell(start_cell, 0)
        return "0"

    try:
        end_amount_cell = prev_sheet.find("End amount")
        row = end_amount_cell.row
        col = end_amount_cell.col + 1
        col_letter = chr(64 + col)

        formula = f"='{prev_month_name}'!{col_letter}{row}"
        curr_sheet = spreadsheet.worksheet(curr_month_name)
        curr_sheet.update_acell(start_cell, formula)
        print(f"Linked {curr_month_name} start balance → {prev_month_name} {col_letter}{row}")
        return formula

    except gspread.exceptions.CellNotFound:
        print(f"'End amount' not found in {prev_month_name}, using 0 as starting balance")
        spreadsheet.worksheet(curr_month_name).update_acell(start_cell, 0)
        return "0"