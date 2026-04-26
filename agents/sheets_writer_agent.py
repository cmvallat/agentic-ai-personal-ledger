import json
import os
import time
import pandas as pd
import gspread
from gspread_formatting import CellFormat, TextFormat, format_cell_range
from state import LedgerState
from utils.sheets import authorize_google_sheets, sheets_api_call_with_retry
from utils.formatting import bold_row, format_sheet, apply_conditional_formatting


class SheetsWriterAgent:
    name = "SheetsWriterAgent"

    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        with open(config_path) as f:
            self.config = json.load(f)

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        # Respect dry-run flag — report what would happen but don't write
        if state.dry_run:
            print(f"[{self.name}] DRY RUN — skipping all sheet writes.")
            months = state.df_all["Date"].dt.to_period("M").unique()
            for month in sorted(months):
                count = len(state.df_all[state.df_all["Date"].dt.to_period("M") == month])
                print(f"[{self.name}]   Would write {count} transactions to {month.strftime('%B %Y')}")
            return state

        # Guard — nothing to write if ingestion produced no transactions
        if state.df_all is None or state.df_all.empty:
            print(f"[{self.name}] No transactions to write. Skipping.")
            return state

        try:
            client = authorize_google_sheets(self.config["creds_path"])
            spreadsheet = client.open_by_key(self.config["spreadsheet_key"])
        except Exception as e:
            print(f"[{self.name}] Could not connect to Google Sheets: {e}")
            state.write_success = False
            return state

        state = self._write_monthly_sheets(state, spreadsheet)

        print(f"[{self.name}] Done. Sheets written: {', '.join(state.sheets_written)}")
        return state

    # ------------------------------------------------------------------ #
    #  Private methods                                                     #
    # ------------------------------------------------------------------ #

    def _get_summary_start_col(self, month_txns: pd.DataFrame) -> int:
        """
        Calculate which column the summary section should start in.
        Leaves a 2-column gap after the last transaction column.
        """
        base_col_count = 5  # Date, Expense, Amount, Category, Method
        extra_cols = 1 if "Flagged" in month_txns.columns else 0
        return base_col_count + extra_cols + 1

    def _write_monthly_sheets(
        self,
        state: LedgerState,
        spreadsheet: gspread.Spreadsheet
    ) -> LedgerState:
        """
        Group transactions by month and write each group to its own
        sheet tab. Creates the tab if it does not already exist.
        """
        df_all = state.df_all.copy()
        df_income = state.df_income.copy() if state.df_income is not None else None
        df_payments = state.df_payments.copy() if state.df_payments is not None else None

        # Ensure dates are datetime so we can group by month
        df_all["Date"] = pd.to_datetime(df_all["Date"])
        df_all["MonthKey"] = df_all["Date"].dt.to_period("M")

        if df_income is not None:
            df_income["Date"] = pd.to_datetime(df_income["Date"])

        if df_payments is not None:
            df_payments["Date"] = pd.to_datetime(df_payments["Date"])

        months = sorted(df_all["MonthKey"].unique())

        for month in months:
            month_str = month.strftime("%B %Y")  # e.g. "March 2026"
            print(f"[{self.name}] Writing {month_str}...")

            # Open existing sheet tab or create a new one
            try:
                sh = spreadsheet.worksheet(month_str)
            except gspread.exceptions.WorksheetNotFound:
                sh = spreadsheet.add_worksheet(
                    title=month_str, rows="200", cols="20"
                )
                print(f"[{self.name}] Created new sheet: {month_str}")

            # Filter to just this month's transactions
            month_txns = df_all[df_all["MonthKey"] == month].copy()
            curr_month_date = month_txns["Date"].min()

            # Format dates as strings for display in the sheet
            month_txns["Date"] = month_txns["Date"].dt.strftime("%m/%d/%y")

            # Clear the sheet before writing so we don't stack on old data
            sheets_api_call_with_retry(sh.clear)

            self._write_transactions(sh, spreadsheet, month_txns)
            self._write_summary(sh, spreadsheet, month_txns, curr_month_date, df_income, month)
            self._write_category_totals(sh, month_txns)
            self._write_income_and_payments(sh, df_income, df_payments, month)

            format_sheet(sh)

            state.sheets_written.append(month_str)
            print(f"[{self.name}] ✅ {month_str} complete.")

        # Brief pause between months to stay under rate limits
        # when writing multiple sheets in one run
        if len(months) > 1:
            print(f"[{self.name}] Pausing briefly to respect API rate limits...")
            time.sleep(3)

        state.sheet_url = (
            f"https://docs.google.com/spreadsheets/d/{self.config['spreadsheet_key']}"
        )
        state.write_success = True
        return state

    def _write_transactions(
        self,
        sh: gspread.Worksheet,
        spreadsheet: gspread.Spreadsheet,
        month_txns: pd.DataFrame
    ) -> None:
        from utils.formatting import auto_resize_columns

        base_columns = ["Date", "Description", "Net", "Category", "Method"]
        headers = ["Date", "Expense", "Amount", "Category", "Method"]
        review_note_col_index = None

        if "Flagged" in month_txns.columns:
            month_txns = month_txns.copy()
            month_txns["Review Note"] = month_txns.apply(
                lambda row: f"⚠️ {row['Flag_Note']}" if row["Flagged"] else "",
                axis=1
            )
            base_columns += ["Review Note"]
            headers += ["Review Note"]
            # Review Note is the 6th column, index 5 (0-based)
            review_note_col_index = len(base_columns) - 1

        sheets_api_call_with_retry(sh.append_row, headers)
        transactions = month_txns[base_columns].values.tolist()
        if transactions:
            sheets_api_call_with_retry(sh.append_rows, transactions)

        # Auto-resize all columns in one API call
        # end_col_index is exclusive so len(headers) covers all written columns
        auto_resize_columns(
            spreadsheet,
            sh,
            start_col_index=0,
            end_col_index=len(headers)
        )

    def _write_summary(
        self,
        sh: gspread.Worksheet,
        spreadsheet: gspread.Spreadsheet,
        month_txns: pd.DataFrame,
        curr_month_date: pd.Timestamp,
        df_income: pd.DataFrame,
        month: pd.Period
    ) -> None:
        start_col = self._get_summary_start_col(month_txns)
        summary_row = 1
        summary_col_letter = chr(64 + start_col + 1)

        # Calculate total income for this month
        total_income = 0.0
        if df_income is not None:
            month_income = df_income[
                df_income["Date"].dt.to_period("M") == month
            ].copy()
            if not month_income.empty:
                total_income = month_income["Amount"].sum()

        total_spending = month_txns["Net"].sum()

        # Build all summary rows as a 2D array and write in one API call
        # Column start_col = labels, column start_col+1 = values
        end_formula = (
            f"={summary_col_letter}{summary_row + 1}"
            f"-{summary_col_letter}{summary_row + 2}"
            f"+{summary_col_letter}{summary_row + 3}"
        )
        ou_formula = (
            f"={summary_col_letter}{summary_row + 4}"
            f"-{summary_col_letter}{summary_row + 1}"
        )

        summary_data = [
            ["Summary", ""],
            ["Start amount", ""],        # value filled by link_dynamic below
            ["Total spending", round(total_spending, 2)],
            ["Total income", round(total_income, 2)],
            ["End amount", end_formula],
            ["", ""],
            ["O/U budget", ou_formula],
        ]

        # Convert start_col to A1 notation for the range
        start_col_letter = chr(64 + start_col)
        end_col_letter = chr(64 + start_col + 1)
        summary_range = (
            f"{start_col_letter}{summary_row}"
            f":{end_col_letter}{summary_row + len(summary_data) - 1}"
        )

        sh.update(summary_range, summary_data, value_input_option="USER_ENTERED")

        # Link start amount to previous month's end balance —
        # done after the batch write so it overwrites the empty string
        from utils.sheets import link_dynamic_previous_month_balance
        link_dynamic_previous_month_balance(
            spreadsheet,
            curr_month_date,
            f"{summary_col_letter}{summary_row + 1}"
        )

        # Conditional formatting on O/U budget cell
        ou_budget_cell = f"{summary_col_letter}{summary_row + 6}"
        apply_conditional_formatting(ou_budget_cell, sh)

    def _write_category_totals(
        self,
        sh: gspread.Worksheet,
        month_txns: pd.DataFrame
    ) -> None:
        start_col = self._get_summary_start_col(month_txns)
        summary_row = 1
        category_row = summary_row + 9

        # Calculate the exact last row of transaction data
        # Row 1 is the header, rows 2 onwards are transactions
        last_txn_row = len(month_txns) + 1  # +1 for the header row

        sh.update_cell(category_row, start_col, "Category")
        sh.update_cell(category_row, start_col + 1, "Total")

        # Use a bounded range C2:C{last_txn_row} so that income and
        # payment rows written below the transactions are excluded
        formula = (
            '={SORT({'
            f'FILTER(UNIQUE(D2:D{last_txn_row}), LEN(UNIQUE(D2:D{last_txn_row}))), '
            f'ARRAYFORMULA(SUMIF(D2:D{last_txn_row}, FILTER(UNIQUE(D2:D{last_txn_row}), LEN(UNIQUE(D2:D{last_txn_row}))), C2:C{last_txn_row}))'
            '}, 2, FALSE); '
            '{"",""}; '
            f'{{"Total", SUM(C2:C{last_txn_row})}}}}'
        )
        sh.update_cell(category_row + 1, start_col, formula)

        header_range = (
            f"{chr(64 + start_col)}{category_row}"
            f":{chr(64 + start_col + 1)}{category_row}"
        )
        header_fmt = CellFormat(textFormat=TextFormat(bold=True))
        format_cell_range(sh, header_range, header_fmt)

    def _write_income_and_payments(
        self,
        sh: gspread.Worksheet,
        df_income: pd.DataFrame,
        df_payments: pd.DataFrame,
        month: pd.Period
    ) -> None:
        """
        Append income and payment rows below the transactions with
        clearly labeled section headers and spacing between them.
        """
        # --- Income section ---
        if df_income is not None:
            month_income = df_income[
                df_income["Date"].dt.to_period("M") == month
            ].copy()
            month_income["Date"] = month_income["Date"].dt.strftime("%m/%d/%y")

            if not month_income.empty:
                # Two blank rows for breathing room
                sheets_api_call_with_retry(sh.append_row, [])
                sheets_api_call_with_retry(sh.append_row, [])

                # Section header row — same columns as transactions
                # Read current row count so we know which row to bold
                current_row = len(sh.get_all_values()) + 1
                sheets_api_call_with_retry(sh.append_row, ["Date", "Income", "Amount", "", ""])
                bold_row(sh, current_row)

                # Income rows
                sheets_api_call_with_retry(sh.append_rows, 
                    month_income[["Date", "Description", "Amount"]].values.tolist()
                )

        # --- Payments section ---
        if df_payments is not None:
            month_payments = df_payments[
                df_payments["Date"].dt.to_period("M") == month
            ].copy()
            month_payments["Date"] = month_payments["Date"].dt.strftime("%m/%d/%y")

            if not month_payments.empty:
                # Two blank rows for breathing room
                sheets_api_call_with_retry(sh.append_row, [])
                sheets_api_call_with_retry(sh.append_row, [])

                # Section header row
                current_row = len(sh.get_all_values()) + 1
                sheets_api_call_with_retry(sh.append_row, ["Date", "Payment", "Amount", "", ""])
                bold_row(sh, current_row)

                # Payment rows
                sheets_api_call_with_retry(sh.append_rows, 
                    month_payments[["Date", "Description", "Amount"]].values.tolist()
                )