import json
import os
import pandas as pd
import gspread
from gspread_formatting import CellFormat, TextFormat, format_cell_range
from state import LedgerState
from utils.sheets import authorize_google_sheets
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

        if state.analysis_report:
            print(f"[{self.name}] Writing Insights tab...")
            self._write_insights_tab(state, spreadsheet)

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
        return base_col_count + extra_cols + 2

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
            sh.clear()

            self._write_transactions(sh, spreadsheet, month_txns)
            self._write_summary(sh, spreadsheet, month_txns, curr_month_date, df_income, month)
            self._write_category_totals(sh, month_txns)
            self._write_income_and_payments(sh, df_income, df_payments, month)

            format_sheet(sh)

            state.sheets_written.append(month_str)
            print(f"[{self.name}] ✅ {month_str} complete.")

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
        from utils.formatting import auto_resize_column

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

        sh.append_row(headers)
        transactions = month_txns[base_columns].values.tolist()
        if transactions:
            sh.append_rows(transactions)

        # Auto-resize the Review Note column if it was written
        if review_note_col_index is not None:
            auto_resize_column(spreadsheet, sh, review_note_col_index)

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

        from utils.sheets import link_dynamic_previous_month_balance
        link_dynamic_previous_month_balance(
            spreadsheet,
            curr_month_date,
            f"{summary_col_letter}{summary_row + 1}"
        )

        sh.update_cell(1, start_col, "Summary")
        sh.update_cell(summary_row + 1, start_col, "Start amount")

        total_spending = month_txns["Net"].sum()
        sh.update_cell(summary_row + 2, start_col, "Total spending")
        sh.update_cell(summary_row + 2, start_col + 1, total_spending)

        if df_income is not None:
            month_income = df_income[
                df_income["Date"].dt.to_period("M") == month
            ].copy()
            if not month_income.empty:
                total_income = month_income["Amount"].sum()
                sh.update_cell(summary_row + 3, start_col, "Total income")
                sh.update_cell(summary_row + 3, start_col + 1, total_income)

        sh.update_cell(summary_row + 4, start_col, "End amount")
        sh.update_cell(
            summary_row + 4, start_col + 1,
            f"={summary_col_letter}{summary_row + 1}"
            f"-{summary_col_letter}{summary_row + 2}"
            f"+{summary_col_letter}{summary_row + 3}"
        )

        sh.update_cell(summary_row + 5, start_col, "")
        sh.update_cell(summary_row + 5, start_col + 1, "")

        sh.update_cell(summary_row + 6, start_col, "O/U budget")
        sh.update_cell(
            summary_row + 6, start_col + 1,
            f"={summary_col_letter}{summary_row + 4}"
            f"-{summary_col_letter}{summary_row + 1}"
        )

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

        sh.update_cell(category_row, start_col, "Category")
        sh.update_cell(category_row, start_col + 1, "Total")

        formula = (
            '={SORT({'
            'FILTER(UNIQUE(D2:D), LEN(UNIQUE(D2:D))), '
            'ARRAYFORMULA(SUMIF(D2:D, FILTER(UNIQUE(D2:D), LEN(UNIQUE(D2:D))), C2:C))'
            '}, 2, FALSE); '
            '{"",""}; '
            '{"Total", SUM(C2:C)}}'
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
                sh.append_row([])
                sh.append_row([])

                # Section header row — same columns as transactions
                # Read current row count so we know which row to bold
                current_row = len(sh.get_all_values()) + 1
                sh.append_row(["Date", "Income", "Amount", "", ""])
                bold_row(sh, current_row)

                # Income rows
                sh.append_rows(
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
                sh.append_row([])
                sh.append_row([])

                # Section header row
                current_row = len(sh.get_all_values()) + 1
                sh.append_row(["Date", "Payment", "Amount", "", ""])
                bold_row(sh, current_row)

                # Payment rows
                sh.append_rows(
                    month_payments[["Date", "Description", "Amount"]].values.tolist()
                )

    def _write_insights_tab(
        self,
        state: LedgerState,
        spreadsheet: gspread.Spreadsheet
    ) -> None:
        analysis = state.analysis_report

        try:
            sh = spreadsheet.worksheet("Insights")
        except gspread.exceptions.WorksheetNotFound:
            sh = spreadsheet.add_worksheet(title="Insights", rows="200", cols="10")

        sh.clear()

        # Build all rows in memory so we can write in one batch
        rows = []
        bold_rows = []  # 1-based row indices to bold after writing

        def header(text):
            bold_rows.append(len(rows) + 1)
            rows.append([text])

        def col_headers(*labels):
            bold_rows.append(len(rows) + 1)
            rows.append(list(labels))

        def data(*values):
            rows.append(list(values))

        def blank():
            rows.append([])

        header("Spending Insights")
        blank()

        # 1. AI Suggestions
        suggestions = analysis.get("suggestions", [])
        if suggestions:
            header("AI Suggestions")
            for s in suggestions:
                data(f"• {s}")
            blank()

        # 2. Monthly Spending Totals
        monthly_trends = analysis.get("monthly_trends", [])
        if len(monthly_trends) > 1:
            month_labels = [item["month"] for item in monthly_trends]

            header("Monthly Spending Totals")
            col_headers("Month", "Total ($)")
            for item in monthly_trends:
                data(item["month"], item["total"])
            blank()

        # 3. Top Merchants
        header("Top Merchants")
        col_headers("Merchant", "Total ($)", "Visits")
        for item in analysis.get("top_merchants", []):
            data(item["merchant"], item["total"], item["visits"])
        blank()

        # 4. Category Breakdown
        header("Category Breakdown")
        col_headers("Category", "Total ($)", "% of Spend")
        for item in analysis.get("category_breakdown", []):
            data(item["category"], item["total"], item["pct"])
        blank()

        # 5. Spending by Category per Month (immediately after Category Breakdown)
        if len(monthly_trends) > 1:
            all_categories = sorted(
                {cat for item in monthly_trends for cat in item["by_category"]}
            )
            header("Spending by Category per Month")
            col_headers("Category", *month_labels)
            for cat in all_categories:
                row_values = [item["by_category"].get(cat, 0.0) for item in monthly_trends]
                data(cat, *row_values)
            blank()

        # 6. Spending by Day of Week
        header("Spending by Day of Week")
        col_headers("Day", "Total ($)", "Transactions", "Avg per Transaction ($)")
        for item in sorted(analysis.get("day_of_week", []), key=lambda x: -x["total"]):
            data(item["day"], item["total"], item["txn_count"], item["avg_per_txn"])
        blank()

        # 7. Spending by Time of Month
        header("Spending by Time of Month")
        col_headers("Period", "Total ($)", "Transactions", "% of Spend")
        for item in analysis.get("time_of_month", []):
            data(item["period"], item["total"], item["txn_count"], item["pct"])
        blank()

        # 8. Holiday-Adjacent Spending
        holiday_data = analysis.get("holiday_spending", [])
        if holiday_data:
            header("Holiday-Adjacent Spending")
            col_headers("Holiday", "Date", "Transactions", "Total ($)")
            for item in holiday_data:
                data(item["holiday"], item["date"], item["txn_count"], item["total"])
            blank()

        # Single batch write
        if rows:
            sh.update("A1", rows)

        # Bold all section/column header rows
        fmt = CellFormat(textFormat=TextFormat(bold=True))
        for row_num in bold_rows:
            format_cell_range(sh, f"A{row_num}:E{row_num}", fmt)

        state.sheets_written.append("Insights")
        print(f"[{self.name}] ✅ Insights tab written.")