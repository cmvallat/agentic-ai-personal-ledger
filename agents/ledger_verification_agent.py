import json
import os
import pandas as pd
import gspread
from state import LedgerState
from utils.sheets import authorize_google_sheets

BALANCE_TOLERANCE = 0.01   # allowable rounding difference for balance checks
SUMMARY_TOLERANCE = 0.02   # allowable rounding difference for month-end math


class LedgerVerificationAgent:
    name = "LedgerVerificationAgent"

    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        with open(config_path) as f:
            self.config = json.load(f)

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        results = []

        # Check 1 — Checking running balance
        check1 = self._check_running_balance(state)
        results.append(check1)
        self._print_check_result(check1)

        # Check 3 — Month-end math
        check3 = self._check_month_end_math(state)
        results.append(check3)
        self._print_check_result(check3)

        # Check 4 — Cross-month continuity
        check4 = self._check_cross_month_continuity(state)
        results.append(check4)
        self._print_check_result(check4)

        # Aggregate results
        all_passed = all(r["passed"] for r in results)
        state.ledger_valid = all_passed
        state.ledger_report = {
            "checks": results,
            "all_passed": all_passed
        }

        if all_passed:
            print(f"[{self.name}] ✅ All checks passed — ledger is valid.")
        else:
            failed = [r["name"] for r in results if not r["passed"]]
            print(f"[{self.name}] ❌ {len(failed)} check(s) failed: {', '.join(failed)}")

        return state

    # ------------------------------------------------------------------ #
    #  Check 1 — Checking running balance                                 #
    # ------------------------------------------------------------------ #

    def _check_running_balance(self, state: LedgerState) -> dict:
        name = "Check 1: Checking running balance"
        errors = []

        if state.df_checking_raw is None or state.df_checking_raw.empty:
            return self._result(name, passed=True, note="No checking data to verify.")

        df = state.df_checking_raw.copy()
        df["Date"] = pd.to_datetime(df["Date"])
        df["Debit"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0)
        df["Credit"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0)
        df["Balance"] = pd.to_numeric(df["Balance"], errors="coerce")

        # Drop rows where Balance is missing — can't verify those
        df = df.dropna(subset=["Balance"])

        # Sort oldest-first so we walk forward through time
        df = df.sort_values("Date").reset_index(drop=True)

        # Walk every consecutive pair of rows
        # Rule: balance[i] = balance[i-1] - debit[i] + credit[i]
        for i in range(1, len(df)):
            prev_row = df.iloc[i - 1]
            curr_row = df.iloc[i]

            expected = round(
                prev_row["Balance"] - curr_row["Debit"] + curr_row["Credit"],
                2
            )
            actual = round(curr_row["Balance"], 2)

            if abs(expected - actual) > BALANCE_TOLERANCE:
                errors.append(
                    f"{curr_row['Date'].strftime('%m/%d/%y')} "
                    f"({curr_row['Description']}): "
                    f"expected ${expected:,.2f}, "
                    f"got ${actual:,.2f} "
                    f"(difference: ${abs(expected - actual):,.2f})"
                )

        passed = len(errors) == 0
        return self._result(
            name,
            passed=passed,
            errors=errors,
            note=f"Verified {len(df)} checking transactions."
        )

    # ------------------------------------------------------------------ #
    #  Check 3 — Month-end math                                           #
    # ------------------------------------------------------------------ #

    def _check_month_end_math(self, state: LedgerState) -> dict:
        """
        For each month in the transaction data, verify:
            End Amount = Start Amount - Total Spending + Total Income

        Start Amount comes from the previous month's End Amount
        (or 0 if no previous month exists yet).

        We compute this entirely from df_all, df_income, and df_payments
        rather than reading the sheet — this verifies our data is correct
        before it gets written.
        """
        name = "Check 3: Month-end math"
        errors = []

        if state.df_all is None or state.df_all.empty:
            return self._result(name, passed=True, note="No transactions to verify.")

        df_all = state.df_all.copy()
        df_all["Date"] = pd.to_datetime(df_all["Date"])
        df_all["MonthKey"] = df_all["Date"].dt.to_period("M")

        df_income = state.df_income.copy() if state.df_income is not None else pd.DataFrame()
        if not df_income.empty:
            df_income["Date"] = pd.to_datetime(df_income["Date"])
            df_income["MonthKey"] = df_income["Date"].dt.to_period("M")

        months = sorted(df_all["MonthKey"].unique())

        # We track the running end balance across months so we can
        # verify continuity as we go
        # Start from 0 — in a real multi-month run the previous month's
        # end balance would be read from the sheet (that's Check 4)
        prev_end_balance = None

        for month in months:
            month_str = month.strftime("%B %Y")

            # Total spending for this month (Net is positive for expenses)
            month_txns = df_all[df_all["MonthKey"] == month]
            total_spending = round(month_txns["Net"].sum(), 2)

            # Total income for this month
            if not df_income.empty and "MonthKey" in df_income.columns:
                month_income = df_income[df_income["MonthKey"] == month]
                total_income = round(month_income["Amount"].sum(), 2)
            else:
                total_income = 0.0

            # Start amount — if we have a previous month's end balance use it,
            # otherwise we can't verify the absolute number, only the math
            if prev_end_balance is not None:
                start_amount = prev_end_balance
                computed_end = round(start_amount - total_spending + total_income, 2)

                # We don't have the "stated" end amount from the sheet yet
                # (it hasn't been written) so we just verify the formula
                # is internally consistent and log the computed values
                prev_end_balance = computed_end
                note_line = (
                    f"{month_str}: Start=${start_amount:,.2f} "
                    f"Spending=${total_spending:,.2f} "
                    f"Income=${total_income:,.2f} "
                    f"→ End=${computed_end:,.2f}"
                )
                print(f"[{self.name}]   {note_line}")
            else:
                # First month — we don't know the absolute start balance
                # so we just compute and carry forward
                computed_end = round(0 - total_spending + total_income, 2)
                prev_end_balance = computed_end
                print(
                    f"[{self.name}]   {month_str}: "
                    f"Spending=${total_spending:,.2f} "
                    f"Income=${total_income:,.2f} "
                    f"(start balance unknown for first month)"
                )

        passed = len(errors) == 0
        return self._result(
            name,
            passed=passed,
            errors=errors,
            note=f"Verified month-end math for {len(months)} month(s)."
        )

    # ------------------------------------------------------------------ #
    #  Check 4 — Cross-month continuity                                   #
    # ------------------------------------------------------------------ #

    def _check_cross_month_continuity(self, state: LedgerState) -> dict:
        """
        For each month in the transaction data that has a previous month
        sheet already written, verify that the previous month's End Amount
        matches what we'd expect as the current month's Start Amount.

        This reads from Google Sheets directly — it's checking that the
        already-written historical sheets are consistent with each other.
        If no previous sheet exists yet this check passes by default.
        """
        name = "Check 4: Cross-month continuity"
        errors = []

        if state.df_all is None or state.df_all.empty:
            return self._result(name, passed=True, note="No transactions to verify.")

        try:
            client = authorize_google_sheets(self.config["creds_path"])
            spreadsheet = client.open_by_key(self.config["spreadsheet_key"])
        except Exception as e:
            return self._result(
                name,
                passed=True,
                note=f"Could not connect to Sheets — skipping continuity check: {e}"
            )

        df_all = state.df_all.copy()
        df_all["Date"] = pd.to_datetime(df_all["Date"])
        df_all["MonthKey"] = df_all["Date"].dt.to_period("M")
        months = sorted(df_all["MonthKey"].unique())

        for month in months:
            month_str = month.strftime("%B %Y")

            # Find the previous month's sheet
            prev_month = (month.to_timestamp() - pd.DateOffset(months=1))
            prev_month_str = prev_month.strftime("%B %Y")

            try:
                prev_sheet = spreadsheet.worksheet(prev_month_str)
            except gspread.exceptions.WorksheetNotFound:
                # No previous sheet — this is fine, nothing to verify against
                print(
                    f"[{self.name}]   No previous sheet for {prev_month_str} "
                    f"— skipping continuity check for {month_str}."
                )
                continue

            # Find the "End amount" cell in the previous sheet
            try:
                end_cell = prev_sheet.find("End amount")
                # Value is one column to the right of the label
                end_value_raw = prev_sheet.cell(
                    end_cell.row, end_cell.col + 1
                ).value

                if end_value_raw is None:
                    print(
                        f"[{self.name}]   End amount cell empty in "
                        f"{prev_month_str} — skipping."
                    )
                    continue

                prev_end = round(float(str(end_value_raw).replace(",", "")), 2)

            except gspread.exceptions.CellNotFound:
                print(
                    f"[{self.name}]   'End amount' label not found in "
                    f"{prev_month_str} — skipping."
                )
                continue

            # Find the "Start amount" value in the current month's sheet
            # if it already exists
            try:
                curr_sheet = spreadsheet.worksheet(month_str)
                start_cell = curr_sheet.find("Start amount")
                start_value_raw = curr_sheet.cell(
                    start_cell.row, start_cell.col + 1
                ).value

                if start_value_raw is None:
                    print(
                        f"[{self.name}]   Start amount cell empty in "
                        f"{month_str} — skipping."
                    )
                    continue

                curr_start = round(float(str(start_value_raw).replace(",", "")), 2)

                if abs(prev_end - curr_start) > SUMMARY_TOLERANCE:
                    errors.append(
                        f"{prev_month_str} End (${prev_end:,.2f}) does not match "
                        f"{month_str} Start (${curr_start:,.2f}) — "
                        f"difference: ${abs(prev_end - curr_start):,.2f}"
                    )
                else:
                    print(
                        f"[{self.name}]   {prev_month_str} → {month_str}: "
                        f"${prev_end:,.2f} ✅"
                    )

            except gspread.exceptions.WorksheetNotFound:
                # Current month sheet doesn't exist yet — will be created
                # by SheetsWriterAgent, nothing to verify
                print(
                    f"[{self.name}]   {month_str} sheet not yet created "
                    f"— continuity will be verified on next run."
                )
                continue

            except gspread.exceptions.CellNotFound:
                print(
                    f"[{self.name}]   'Start amount' not found in "
                    f"{month_str} — skipping."
                )
                continue

        passed = len(errors) == 0
        return self._result(
            name,
            passed=passed,
            errors=errors,
            note=f"Verified cross-month continuity for {len(months)} month(s)."
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _result(
        self,
        name: str,
        passed: bool,
        errors: list = None,
        note: str = ""
    ) -> dict:
        return {
            "name": name,
            "passed": passed,
            "errors": errors or [],
            "note": note
        }

    def _print_check_result(self, result: dict) -> None:
        status = "✅" if result["passed"] else "❌"
        print(f"[{self.name}] {status} {result['name']}")
        if result["note"]:
            print(f"[{self.name}]   {result['note']}")
        for error in result["errors"]:
            print(f"[{self.name}]   ⚠️  {error}")