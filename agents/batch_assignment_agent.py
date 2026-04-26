import json
import os
import re
import pandas as pd
import gspread
from anthropic import Anthropic
from dotenv import load_dotenv
from state import LedgerState
from utils.sheets import authorize_google_sheets

load_dotenv()

MAX_RETRIES = 3


class BatchAssignmentAgent:
    name = "BatchAssignmentAgent"

    def __init__(self):
        self.client = Anthropic()
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        with open(config_path) as f:
            self.config = json.load(f)

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        if state.df_all is None or state.df_all.empty:
            print(f"[{self.name}] No transactions to process. Skipping.")
            return state

        if state.df_payments is None or state.df_payments.empty:
            print(f"[{self.name}] No payments found. Marking all CC transactions as pending.")
            state = self._mark_all_pending(state)
            return state

        # Step 1 — Read previously pending transactions from sheets
        df_pending = self._read_pending_from_sheets()

        # Step 2 — Build the full unassigned pool
        unassigned = self._build_unassigned_pool(state, df_pending)
        print(f"[{self.name}] Unassigned pool: {len(unassigned)} transactions "
              f"({len(df_pending)} carried over, "
              f"{len(unassigned) - len(df_pending)} from this run).")

        # Step 3 — Process each payment one at a time
        payments = state.df_payments.sort_values("Date").reset_index(drop=True)
        batch_details = []

        for _, payment in payments.iterrows():
            payment_amount = round(float(payment["Amount"]), 2)
            payment_date = pd.to_datetime(payment["Date"])
            payment_date_str = payment_date.strftime("%m/%d/%Y")

            print(f"\n[{self.name}] Processing payment: "
                f"${payment_amount:.2f} on {payment_date_str}")
            print(f"[{self.name}] Unassigned transactions available: {len(unassigned)}")

            if not unassigned:
                print(f"[{self.name}] No unassigned transactions remaining.")
                break

            # Step 3a — Call Claude to assign transactions
            result, attempts = self._assign_with_retry(
                unassigned, payment_amount, payment_date_str
            )

            # Guard 1 — Claude failed after all retries
            if result is None:
                print(f"[{self.name}] ❌ Could not assign payment "
                    f"${payment_amount:.2f} after {MAX_RETRIES} attempts. "
                    f"Flagging for review.")
                batch_details.append({
                    "payment_date": payment_date_str,
                    "payment_amount": payment_amount,
                    "status": "flagged",
                    "attempts": attempts
                })
                continue

            # Guard 2 — Claude returned empty indices (previous period payment)
            if not result["assigned_indices"]:
                print(f"[{self.name}] ⚠️  Payment ${payment_amount:.2f} on "
                    f"{payment_date_str} has no matching transactions — "
                    f"likely a previous period payment. Skipping.")
                batch_details.append({
                    "payment_date": payment_date_str,
                    "payment_amount": payment_amount,
                    "status": "skipped - no matching transactions",
                    "attempts": attempts
                })
                continue

            # Step 3b — Apply the assignment
            assigned_indices = result["assigned_indices"]
            cross_month = result.get("cross_month", False)
            notes = result.get("notes", "")
            reasoning = result.get("reasoning", "")

            # Build the method label for this payment
            method_label = f"C1 - payment of ${payment_amount:.2f} on {payment_date_str}"
            if cross_month:
                method_label += f" (cross-month: {notes})"

            # Update df_all for current-run transactions
            state = self._apply_assignment_to_df(
                state, assigned_indices, unassigned, method_label
            )

            # Update previously-pending rows directly in Google Sheets
            self._update_pending_in_sheets(
                assigned_indices, unassigned, method_label
            )

            # Remove assigned transactions from the unassigned pool
            unassigned = [
                t for t in unassigned
                if t["index"] not in assigned_indices
            ]

            batch_details.append({
                "payment_date": payment_date_str,
                "payment_amount": payment_amount,
                "assigned_count": len(assigned_indices),
                "status": "confirmed",
                "attempts": attempts,
                "cross_month": cross_month,
                "notes": notes,
                "reasoning": reasoning
            })

            print(f"[{self.name}] ✅ Payment ${payment_amount:.2f} confirmed "
                  f"({len(assigned_indices)} transactions, {attempts} attempt(s)).")

        # Step 4 — Mark remaining unassigned transactions as pending
        remaining_indices = [t["index"] for t in unassigned]
        state = self._mark_pending(state, remaining_indices, unassigned)
        print(f"\n[{self.name}] {len(unassigned)} transactions marked as pending.")

        # Step 5 — Write batch report to state
        confirmed = len([d for d in batch_details if d["status"] == "confirmed"])
        flagged = len([d for d in batch_details if d["status"] == "flagged"])

        state.batch_report = {
            "payments_processed": len(batch_details),
            "payments_confirmed": confirmed,
            "payments_flagged": flagged,
            "transactions_pending": len(unassigned),
            "details": batch_details
        }

        print(f"[{self.name}] Done. "
              f"{confirmed} payments confirmed, "
              f"{flagged} flagged, "
              f"{len(unassigned)} transactions pending.")
        return state

    # ------------------------------------------------------------------ #
    #  Step 1 — Read pending transactions from existing sheets            #
    # ------------------------------------------------------------------ #

    def _read_pending_from_sheets(self) -> list[dict]:
        """
        Scan all monthly sheets for rows where Method contains
        'Pending - next payment'. Return them as a list of dicts
        with enough info to update them later.
        """
        pending = []
        try:
            client = authorize_google_sheets(self.config["creds_path"])
            spreadsheet = client.open_by_key(self.config["spreadsheet_key"])
        except Exception as e:
            print(f"[{self.name}] Could not connect to Sheets "
                  f"for pending read: {e}")
            return pending

        for worksheet in spreadsheet.worksheets():
            try:
                rows = worksheet.get_all_values()
                if len(rows) <= 1:
                    continue

                headers = [h.strip().lower() for h in rows[0]]

                # Find which column is Method and Amount
                try:
                    method_col = headers.index("method")
                    desc_col = headers.index("expense")
                    amount_col = headers.index("amount")
                    date_col = headers.index("date")
                except ValueError:
                    continue

                for row_num, row in enumerate(rows[1:], start=2):
                    if len(row) <= method_col:
                        continue
                    method_val = row[method_col].strip()
                    if "Pending" not in method_val:
                        continue

                    try:
                        amount = round(float(
                            row[amount_col].replace(",", "").strip()
                        ), 2)
                    except (ValueError, IndexError):
                        continue

                    pending.append({
                        "index": f"P{len(pending)}",
                        "date": row[date_col].strip(),
                        "description": row[desc_col].strip(),
                        "amount": amount,
                        "source": "pending",
                        "sheet_name": worksheet.title,
                        "row_number": row_num,
                        "worksheet": worksheet
                    })

            except Exception as e:
                print(f"[{self.name}] Error reading sheet "
                      f"'{worksheet.title}': {e}")
                continue

        print(f"[{self.name}] Found {len(pending)} previously pending "
              f"transactions in existing sheets.")
        return pending

    # ------------------------------------------------------------------ #
    #  Step 2 — Build the unassigned pool                                 #
    # ------------------------------------------------------------------ #

    def _build_unassigned_pool(
        self,
        state: LedgerState,
        df_pending: list[dict]
    ) -> list[dict]:
        """
        Combine previously pending transactions from sheets with
        new CC transactions from this run into one unified list.
        Only CC transactions (Method == "C1") are included —
        checking transactions are not assigned to CC payments.
        """
        pool = list(df_pending)  # start with carried-over pending

        df_all = state.df_all.copy()
        df_all["Date"] = pd.to_datetime(df_all["Date"])

        cc_txns = df_all[df_all["Method"] == "C1"].copy()

        for idx, row in cc_txns.iterrows():
            pool.append({
                "index": str(idx),
                "date": pd.to_datetime(row["Date"]).strftime("%m/%d/%Y"),
                "description": str(row["Description"]),
                "amount": round(float(row["OriginalNet"]), 2),
                "source": "current",
                "df_index": idx
            })

        return pool

    # ------------------------------------------------------------------ #
    #  Step 3 — Assign with retry loop                                    #
    # ------------------------------------------------------------------ #

    def _assign_with_retry(
        self,
        unassigned: list[dict],
        payment_amount: float,
        payment_date_str: str
    ) -> tuple[dict | None, int]:
        """
        Call Claude to assign transactions to a payment.
        Retry up to MAX_RETRIES times if the eval fails.
        Returns (result_dict, attempts) or (None, attempts) if all fail.
        """
        previous_assignment = None
        discrepancy_info = None

        for attempt in range(1, MAX_RETRIES + 1):
            print(f"[{self.name}]   Attempt {attempt}/{MAX_RETRIES}...")

            prompt = self._build_prompt(
                unassigned,
                payment_amount,
                payment_date_str,
                previous_assignment=previous_assignment,
                discrepancy_info=discrepancy_info
            )

            try:
                result = self._call_claude(prompt)
            except Exception as e:
                print(f"[{self.name}]   Claude call failed: {e}")
                continue

            assigned_indices = result.get("assigned_indices", [])

            # If Claude returns empty indices, the payment has no matching
            # transactions in the current pool — likely a previous period
            # payment where the June transactions aren't in this CSV
            if not assigned_indices:
                print(f"[{self.name}]   Claude returned empty assignment — "
                    f"payment may be for a previous period not in this run.")
                return {
                    "assigned_indices": [],
                    "sum_check": 0.00,
                    "cross_month": False,
                    "reasoning": result.get("reasoning", "No matching transactions found."),
                    "notes": ""
                }, attempt

            # Eval — deterministic math check
            computed_sum = self._compute_sum(assigned_indices, unassigned)
            difference = round(abs(computed_sum - payment_amount), 2)

            print(f"[{self.name}]   Claude sum: ${computed_sum:.2f} "
                f"vs payment: ${payment_amount:.2f} "
                f"(difference: ${difference:.2f})")

            if difference == 0.00:
                result["_all_unassigned"] = unassigned
                return result, attempt

            # Eval failed — prepare retry context
            direction = "SHORT" if computed_sum < payment_amount else "OVER"
            discrepancy_info = {
                "previous_indices": assigned_indices,
                "computed_sum": computed_sum,
                "difference": difference,
                "direction": direction
            }
            previous_assignment = assigned_indices
            print(f"[{self.name}]   Eval failed — "
                f"{direction} by ${difference:.2f}. Retrying...")

        return None, MAX_RETRIES

    def _build_prompt(
        self,
        unassigned: list[dict],
        payment_amount: float,
        payment_date_str: str,
        previous_assignment: list[str] | None = None,
        discrepancy_info: dict | None = None
    ) -> str:
        """
        Build the Claude prompt for a single payment.
        If this is a retry, include the discrepancy context.
        """
        # Build the numbered transaction list
        txn_lines = []
        for t in unassigned:
            amount_str = f"${t['amount']:.2f}"
            if t["amount"] < 0:
                amount_str = f"-${abs(t['amount']):.2f}  (refund)"
            carried = "  (carried over from previous run)" \
                if t["source"] == "pending" else ""
            txn_lines.append(
                f"[{t['index']}] {t['date']} | "
                f"{t['description']:<20} | "
                f"{amount_str}{carried}"
            )
        transaction_list = "\n".join(txn_lines)

        base_prompt = f"""You are a financial transaction batch assignment specialist.

Your job is to figure out which credit card transactions were included
in a specific credit card payment. This is like solving a puzzle —
you need to find the combination of transactions that adds up to
exactly the payment amount.

CONTEXT:
- These are real personal finance transactions
- The credit card is paid in full whenever a payment is made
- Payments do NOT follow a fixed date window — a payment can include
  any unaccounted-for transactions regardless of date
- Some transactions may be from a previous month that were not yet
  assigned to a payment — these are marked "carried over"
- Refund credits (negative amounts) reduce the payment total
- There is ZERO tolerance for rounding — the assigned transactions
  must sum to EXACTLY the payment amount to the cent
- Every transaction will eventually be assigned to exactly one payment
- If you cannot find an exact match, get as close as possible and
  explain the discrepancy in your reasoning
- If none of the available transactions logically belong to this payment
(e.g. it is a cross-month payment for a previous month not represented
in this transaction list), return an empty assigned_indices list and
explain in your reasoning

PAYMENT TO MATCH:
Amount: ${payment_amount:.2f}
Date: {payment_date_str}

UNASSIGNED TRANSACTIONS:
{transaction_list}

TASK:
Find which of the above transactions were included in this payment.
They must sum to exactly ${payment_amount:.2f}.

CRITICAL: Your response must be a single valid JSON object and nothing else.
Do not write any explanation before or after the JSON.
Do not use markdown formatting.
Start your response with {{ and end with }}.

{{
    "assigned_indices": ["P0", "1", "3"],
    "sum_check": {payment_amount:.2f},
    "cross_month": true,
    "reasoning": "brief explanation of your logic",
    "notes": "cross-month split or refund details if applicable, empty string if not. If you list a cross-month split, please include the amounts for each month, but do NOT list the indices of the transactions included in the split."
}}

Where:
- "assigned_indices" is the list of index labels from the transaction list above
- "sum_check" is what YOU calculate the assigned transactions sum to
- "cross_month" is true if any assigned transaction is from a different
  month than the payment date
- "reasoning" is a brief explanation of how you arrived at this assignment
- "notes" contains cross-month split details e.g.
  "$50.00 from June, $72.00 from July" or refund notes,
  empty string if neither applies"""

        # If this is a retry, append the discrepancy context
        if discrepancy_info:
            prev_str = ", ".join(discrepancy_info["previous_indices"])
            base_prompt += f"""

RETRY CONTEXT:
Your previous assignment [{prev_str}] summed to \
${discrepancy_info['computed_sum']:.2f} but the payment \
was ${payment_amount:.2f}.
You are {discrepancy_info['direction']} by \
${discrepancy_info['difference']:.2f}.
Please try again with a different combination."""

        return base_prompt

    def _call_claude(self, prompt: str) -> dict:
        """Send the prompt to Claude and parse the JSON response."""
        response = self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2048,
            system="""You are a financial batch assignment engine.
    You ALWAYS respond with valid JSON only.
    No prose, no markdown, no explanation outside the JSON object.
    No bold text, no bullet points, no headers.
    Your entire response must be parseable by json.loads().""",
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        print(f"[{self.name}]   Claude raw response: {raw[:300]}...")

        if not raw:
            raise ValueError("Claude returned an empty response")

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        # Try direct parse first
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Fallback — extract the first JSON object found in the response.
            # This handles cases where Claude adds prose before or after the JSON.
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                extracted = match.group(0)
                print(f"[{self.name}]   Extracted JSON from response: "
                    f"{extracted[:200]}...")
                return json.loads(extracted)
            raise ValueError(
                f"Could not extract valid JSON from Claude response: {raw[:200]}"
            )

    def _compute_sum(
        self,
        assigned_indices: list[str],
        unassigned: list[dict]
    ) -> float:
        """
        Deterministic math check — sum the OriginalNet values
        of the assigned transactions. Zero tolerance enforced here.
        """
        index_map = {t["index"]: t for t in unassigned}
        total = 0.0
        for idx in assigned_indices:
            if idx in index_map:
                total += index_map[idx]["amount"]
        return round(total, 2)

    # ------------------------------------------------------------------ #
    #  Apply assignments and update state/sheets                          #
    # ------------------------------------------------------------------ #

    def _apply_assignment_to_df(
        self,
        state: LedgerState,
        assigned_indices: list[str],
        unassigned: list[dict],
        method_label: str
    ) -> LedgerState:
        """
        Update the Method column in df_all for current-run transactions
        that were assigned to this payment.
        """
        df_all = state.df_all.copy()
        index_map = {t["index"]: t for t in unassigned}

        for idx_str in assigned_indices:
            t = index_map.get(idx_str)
            if t is None or t["source"] != "current":
                continue
            df_idx = t.get("df_index")
            if df_idx is None:
                continue
            df_all.at[df_idx, "Method"] = method_label

        state.df_all = df_all
        return state

    def _update_pending_in_sheets(
        self,
        assigned_indices: list[str],
        unassigned: list[dict],
        method_label: str
    ) -> None:
        """
        For previously-pending transactions that got assigned this run,
        update their Method cell directly in the existing Google Sheet.
        """
        index_map = {t["index"]: t for t in unassigned}

        for idx_str in assigned_indices:
            t = index_map.get(idx_str)
            if t is None or t["source"] != "pending":
                continue

            try:
                worksheet = t["worksheet"]
                row_num = t["row_number"]

                # Find the Method column index from the header row
                headers = [
                    h.strip().lower()
                    for h in worksheet.row_values(1)
                ]
                method_col = headers.index("method") + 1  # 1-based for gspread

                worksheet.update_cell(row_num, method_col, method_label)
                print(f"[{self.name}]   Updated pending row "
                      f"'{t['description']}' in {t['sheet_name']}")
            except Exception as e:
                print(f"[{self.name}]   Could not update pending row "
                      f"'{t.get('description', '?')}': {e}")

    def _mark_pending(
        self,
        state: LedgerState,
        remaining_indices: list[str],
        unassigned: list[dict]
    ) -> LedgerState:
        """
        Mark all remaining unassigned current-run CC transactions
        as pending in df_all so the Sheets Writer writes them
        with the correct Method value.
        """
        df_all = state.df_all.copy()
        index_map = {t["index"]: t for t in unassigned}

        for idx_str in remaining_indices:
            t = index_map.get(idx_str)
            if t is None or t["source"] != "current":
                continue
            df_idx = t.get("df_index")
            if df_idx is None:
                continue
            df_all.at[df_idx, "Method"] = "Pending - next payment"

        state.df_all = df_all
        return state

    def _mark_all_pending(self, state: LedgerState) -> LedgerState:
        """Mark all CC transactions as pending when there are no payments."""
        df_all = state.df_all.copy()
        cc_mask = df_all["Method"] == "C1"
        df_all.loc[cc_mask, "Method"] = "Pending - next payment"
        state.df_all = df_all
        return state

    def _compute_cross_month_split(
        self,
        assigned_indices: list[str],
        unassigned_before: list[dict]
    ) -> tuple[float, float]:
        """
        After assignment, compute how much of the payment came from
        each month. Used for cross-month notes on both sheets.
        Returns (prev_month_total, curr_month_total).
        """
        index_map = {t["index"]: t for t in unassigned_before}
        prev_total = 0.0
        curr_total = 0.0

        for idx_str in assigned_indices:
            t = index_map.get(idx_str)
            if t is None:
                continue
            if t["source"] == "pending":
                prev_total += t["amount"]
            else:
                curr_total += t["amount"]

        return round(prev_total, 2), round(curr_total, 2)