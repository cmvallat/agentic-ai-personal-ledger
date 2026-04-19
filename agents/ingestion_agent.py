import json
import os
import pandas as pd
import gspread
from state import LedgerState
from utils.sheets import authorize_google_sheets
from mappings import category_map, description_map


def categorize(desc: str) -> str:
    """Categorize a transaction description using the keyword map."""
    desc_lower = desc.lower()
    for keyword, category in category_map.items():
        if keyword in desc_lower:
            return category
    return "Misc"


def update_description(desc: str) -> str:
    """Clean up a raw bank description using the description map."""
    desc_lower = desc.lower()
    for keyword, title in description_map.items():
        if keyword in desc_lower:
            return title
    return desc


class IngestionAgent:
    name = "IngestionAgent"

    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        with open(config_path) as f:
            self.config = json.load(f)

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        # Step 1: Load CSVs
        state = self._load_and_normalize(state)

        # Step 2: Separate payments, income, and CC payment rows
        state = self._separate_transaction_types(state)

        # Step 3: Clean descriptions and assign categories
        state = self._apply_mappings(state)

        # Step 4: Combine into df_all
        state = self._combine(state)

        # Step 5: Deduplicate against existing sheet
        state = self._deduplicate(state)

        total = len(state.df_all)
        skipped = state.dedup_report.get("duplicates_skipped", 0)
        print(f"[{self.name}] Done. {total} new transactions loaded, {skipped} duplicates skipped.")
        return state

    # ------------------------------------------------------------------ #
    #  Private methods                                                     #
    # ------------------------------------------------------------------ #

    def _load_and_normalize(self, state: LedgerState) -> LedgerState:
        """Load both CSVs and normalize column names, types, and structure."""
        print(f"[{self.name}] Loading CSVs...")

        df_cc = pd.read_csv(state.cc_csv_path)
        df_checking = pd.read_csv(state.checking_csv_path)

        # Normalize column names to match what the rest of the pipeline expects
        df_cc = df_cc.rename(columns={"Posted Date": "Date"})
        df_checking = df_checking.rename(columns={"Post Date": "Date"})

        # Keep only the columns we need
        df_cc = df_cc[["Description", "Debit", "Credit", "Category", "Date"]]
        df_checking = df_checking[["Description", "Debit", "Credit", "Date", "Balance"]]

        # Store the complete checking data before any filtering happens —
        # the Ledger Verification Agent needs all rows including payments
        # and income to verify the running balance chain. Those rows get
        # removed from df_checking later in _separate_transaction_types.
        state.df_checking_raw = df_checking.copy()

        # Tag each transaction with its payment method
        df_cc["Method"] = "C1"
        df_checking["Method"] = "D"

        # Checking transactions have no category from the bank — default to Misc
        df_checking["Category"] = "Misc"

        # Parse dates — CC and checking use different formats from their respective banks
        df_cc["Date"] = pd.to_datetime(
            df_cc["Date"], format="%Y-%m-%d", errors="coerce"
        )
        df_checking["Date"] = pd.to_datetime(
            df_checking["Date"], format="%m/%d/%Y", errors="coerce"
        )

        # Fill blanks with 0 so arithmetic doesn't produce NaN
        for df in [df_cc, df_checking]:
            df["Debit"] = df["Debit"].fillna(0)
            df["Credit"] = df["Credit"].fillna(0)

        state.df_cc = df_cc
        state.df_checking = df_checking

        print(f"[{self.name}] Loaded {len(df_cc)} CC rows, {len(df_checking)} checking rows.")
        return state

    def _separate_transaction_types(self, state: LedgerState) -> LedgerState:
        """
        Pull out payments and income into their own dataframes.
        These are not expenses and should not be categorized or summed
        alongside regular transactions.
        """
        print(f"[{self.name}] Separating payments and income...")
        df_cc = state.df_cc
        df_checking = state.df_checking

        # --- Credit card payments ---
        # A payment is a credit on the CC statement where the description
        # matches the autopay label exactly. We pull these out so they
        # don't appear as income in the transaction list.
        payment_mask = (
            df_cc["Description"].str.upper() == "CAPITAL ONE AUTOPAY PYMT"
        ) & (df_cc["Credit"] > 0)

        df_payments = df_cc[payment_mask][["Date", "Description", "Credit"]].copy()
        df_payments.rename(columns={"Credit": "Amount"}, inplace=True)
        df_payments["Description"] = "C1 Payment"
        df_payments["Date"] = pd.to_datetime(df_payments["Date"])
        df_payments = df_payments.sort_values("Date").reset_index(drop=True)

        # Remove payments from CC transactions
        df_cc = df_cc[~payment_mask].copy()

        # Net = Debit - Credit gives a single signed amount per transaction.
        # Positive = money left your account (expense).
        # Negative = money came back (refund).
        df_cc["Net"] = df_cc["Debit"].fillna(0) - df_cc["Credit"].fillna(0)
        df_cc["OriginalNet"] = df_cc["Net"]  # preserved for payment batch verification

        # --- Income / paychecks from checking ---
        # Any credit in checking is income (paycheck, transfer in, etc.)
        income_mask = df_checking["Credit"] > 0
        df_income = df_checking[income_mask][["Date", "Description", "Credit"]].copy()
        df_income.rename(columns={"Credit": "Amount"}, inplace=True)
        df_income["Description"] = df_income["Description"].apply(update_description)

        # Remove income rows from checking transactions
        df_checking = df_checking[~income_mask].copy()

        # Remove the CC payment row from checking so paying off the credit
        # card doesn't show up as a checking expense
        cc_payment_mask = df_checking["Description"].str.contains(
            "CAPITAL ONE CRCARDPMT", case=False, na=False
        )
        df_checking = df_checking[~cc_payment_mask].copy()
        df_checking["Net"] = df_checking["Debit"].fillna(0) - df_checking["Credit"].fillna(0)

        state.df_cc = df_cc
        state.df_checking = df_checking
        state.df_payments = df_payments
        state.df_income = df_income

        print(f"[{self.name}] Found {len(df_payments)} payments, {len(df_income)} income transactions.")
        return state

    def _apply_mappings(self, state: LedgerState) -> LedgerState:
        """
        Apply description cleanup and initialize categories.
        Note: categorize() runs in the Categorization Agent, but it must
        run against raw descriptions — so we store the raw description
        in a separate column before cleaning it up.
        """
        print(f"[{self.name}] Applying description mappings...")
        df_cc = state.df_cc.copy()
        df_checking = state.df_checking.copy()

        # Store the raw bank description before cleaning it up.
        # The Categorization Agent will use this column for keyword matching
        # so that "jewel osco" still matches even after the display name
        # has been changed to "JO", for example.
        df_cc["RawDescription"] = df_cc["Description"]
        df_checking["RawDescription"] = df_checking["Description"]

        # Now clean up descriptions for human readability
        df_cc["Description"] = df_cc["Description"].apply(update_description)
        df_checking["Description"] = df_checking["Description"].apply(update_description)

        # Initialize Category as Misc — Categorization Agent fills this in
        df_cc["Category"] = "Misc"
        df_checking["Category"] = "Misc"

        state.df_cc = df_cc
        state.df_checking = df_checking
        return state

    def _combine(self, state: LedgerState) -> LedgerState:
        """
        Merge CC and checking transactions into one sorted dataframe.
        OrigIndex preserves each row's position in its source dataframe,
        which is needed later by the batch assignment logic to update
        the correct row in df_all.
        """
        print(f"[{self.name}] Combining and sorting transactions...")
        df_cc = state.df_cc
        df_checking = state.df_checking

        df_cc["OrigIndex"] = df_cc.index
        df_checking["OrigIndex"] = df_checking.index

        df_all = pd.concat([df_cc, df_checking])
        df_all = df_all.sort_values("Date").reset_index(drop=True)
        df_all["Date"] = pd.to_datetime(df_all["Date"])

        state.df_all = df_all
        print(f"[{self.name}] Combined total: {len(df_all)} transactions.")
        return state

    def _deduplicate(self, state: LedgerState) -> LedgerState:
        """
        Check the existing Google Sheet for transactions that are already
        written. Remove any incoming transactions that match an existing
        row on date + description + amount to prevent duplicates when
        running the script over an overlapping date range.
        """
        print(f"[{self.name}] Checking for duplicates in existing sheet...")

        try:
            client = authorize_google_sheets(self.config["creds_path"])
            spreadsheet = client.open_by_key(self.config["spreadsheet_key"])
        except Exception as e:
            print(f"[{self.name}] Could not connect to Google Sheets: {e}. Skipping deduplication.")
            state.dedup_report = {"duplicates_skipped": 0, "note": "Sheets connection failed"}
            return state

        df_all = state.df_all
        existing_keys = set()

        # Build a set of (date_str, description, amount) tuples from
        # every monthly sheet that exists. We check all sheets rather
        # than just the current month because a date range can span months.
        for worksheet in spreadsheet.worksheets():
            try:
                rows = worksheet.get_all_values()
                if len(rows) <= 1:
                    continue

                # Row format in the sheet: Date, Description, Amount, Category, Method
                for row in rows[1:]:  # skip header
                    if len(row) >= 3:
                        date_str = row[0].strip()
                        desc_str = row[1].strip()
                        amt_str  = row[2].strip().replace(",", "")
                        if date_str and desc_str and amt_str:
                            try:
                                amt = round(float(amt_str), 2)
                                existing_keys.add((date_str, desc_str, amt))
                            except ValueError:
                                continue
            except Exception:
                continue

        if not existing_keys:
            print(f"[{self.name}] No existing transactions found. Nothing to deduplicate.")
            state.dedup_report = {"duplicates_skipped": 0}
            return state

        # Build the same (date_str, description, amount) key for each
        # incoming transaction so we can compare apples to apples
        def make_key(row):
            date_str = pd.to_datetime(row["Date"]).strftime("%m/%d/%y")
            desc     = str(row["Description"]).strip()
            amt      = round(float(row["Net"]), 2)
            return (date_str, desc, amt)

        df_all["_dedup_key"] = df_all.apply(make_key, axis=1)
        duplicate_mask = df_all["_dedup_key"].isin(existing_keys)
        duplicates_skipped = int(duplicate_mask.sum())

        df_all = df_all[~duplicate_mask].drop(columns=["_dedup_key"]).reset_index(drop=True)

        state.df_all = df_all
        state.dedup_report = {
            "duplicates_skipped": duplicates_skipped,
            "transactions_remaining": len(df_all)
        }

        print(f"[{self.name}] {duplicates_skipped} duplicates skipped, {len(df_all)} transactions remaining.")
        return state