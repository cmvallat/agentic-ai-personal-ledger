import json
import os
import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from state import LedgerState
from mappings import category_map

load_dotenv()

CATEGORIES = [
    "Groceries",
    "Gym"
    "Dining Out",
    "Parking and Transportation",
    "Rent",
    "Loans",
    "Wardrobe"
    "Renewals",
    "Fun",
    "Haircut",
    "Car Insurance"
    "Income",
    "Misc",
]

SYSTEM_PROMPT = f"""You are a personal finance categorization assistant.
You will be given a list of bank transaction descriptions and must categorize
each one into exactly one of the following categories:

{chr(10).join(f"- {c}" for c in CATEGORIES)}

Rules:
- Return ONLY a valid JSON array of strings, one category per transaction,
  in the same order as the input.
- Every category in your response must be from the list above exactly as written.
- Do not include any explanation, markdown, or extra text — just the JSON array.
- If you are genuinely unsure, use "Misc".

Example input:  ["NETFLIX.COM", "WHOLEFDS #1234", "UBER *TRIP"]
Example output: ["Subscriptions", "Groceries", "Transport"]"""

REFLECTION_SYSTEM_PROMPT = f"""You are a personal finance categorization reviewer.
You will be given a list of bank transactions that have already been categorized,
and your job is to review each one and assess whether the category is correct
and how confident you are.

Valid categories are:
{chr(10).join(f"- {c}" for c in CATEGORIES)}

Return ONLY a valid JSON array of objects, one per transaction, in the same order.
Each object must have exactly these fields:
- "description": the original transaction description (string)
- "category": the assigned category — keep it or correct it (string, must be from the list)
- "confidence": your confidence that this is the right category (integer 1-100)
- "flag": true if you are less than 80 percent confident, false otherwise (boolean)
- "note": a brief reason if flagged, empty string if not flagged (string)

Do not include any explanation, markdown, or extra text — just the JSON array."""

CONFIDENCE_THRESHOLD = 80
BATCH_SIZE = 50           # for categorization calls
REFLECTION_BATCH_SIZE = 25  # for reflection calls — more verbose output


def categorize(desc: str) -> str:
    """Categorize a transaction description using the keyword map."""
    desc_lower = desc.lower()
    for keyword, category in category_map.items():
        if keyword in desc_lower:
            return category
    return "Misc"


class CategorizationAgent:
    name = "CategorizationAgent"

    def __init__(self):
        self.client = Anthropic()

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        if state.df_all is None or state.df_all.empty:
            print(f"[{self.name}] No transactions to categorize. Skipping.")
            return state

        # Step 1: Keyword map pass
        state = self._keyword_map_pass(state)

        # Step 2: Claude pass for unknowns
        state = self._claude_pass(state)

        # Step 3: Reflection pass
        state = self._reflection_pass(state)

        known = len(state.df_all[state.df_all["Category"] != "Misc"])
        unknown = len(state.df_all[state.df_all["Category"] == "Misc"])
        flagged = len(state.df_all[state.df_all.get("Flagged", False) == True]) if "Flagged" in state.df_all.columns else 0
        print(
            f"[{self.name}] Done. {known} categorized, "
            f"{unknown} Misc, {flagged} flagged for review."
        )
        return state

    # ------------------------------------------------------------------ #
    #  Private methods                                                     #
    # ------------------------------------------------------------------ #

    def _keyword_map_pass(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running keyword map pass...")
        df_all = state.df_all.copy()

        if "RawDescription" in df_all.columns:
            df_all["Category"] = df_all["RawDescription"].apply(categorize)
        else:
            df_all["Category"] = df_all["Description"].apply(categorize)

        known = len(df_all[df_all["Category"] != "Misc"])
        unknown = len(df_all[df_all["Category"] == "Misc"])
        print(f"[{self.name}] Keyword map: {known} matched, {unknown} unknown.")

        # Store for the final summary report
        state.categorization_report["keyword_map_matched"] = known

        state.df_all = df_all
        return state

    def _claude_pass(self, state: LedgerState) -> LedgerState:
        """
        Second pass: send all remaining Misc transactions to Claude
        in batches. Claude returns a category for each one.
        """
        df_all = state.df_all.copy()
        misc_mask = df_all["Category"] == "Misc"
        misc_indices = df_all[misc_mask].index.tolist()

        # Track these so the reflection pass knows exactly what Claude touched - not key mappings
        state.categorization_report["claude_indices"] = misc_indices

        if not misc_indices:
            print(f"[{self.name}] No unknowns — skipping Claude pass.")
            return state

        print(f"[{self.name}] Sending {len(misc_indices)} unknowns to Claude...")

        descriptions = df_all.loc[misc_indices, "Description"].tolist()
        all_categories = []
        total_batches = (len(descriptions) + BATCH_SIZE - 1) // BATCH_SIZE

        for batch_num, i in enumerate(range(0, len(descriptions), BATCH_SIZE), 1):
            batch = descriptions[i : i + BATCH_SIZE]
            print(f"[{self.name}] Batch {batch_num}/{total_batches} ({len(batch)} transactions)...")

            batch_categories = self._call_claude_categorize(batch)

            if len(batch_categories) != len(batch):
                print(
                    f"[{self.name}] Warning: Claude returned {len(batch_categories)} "
                    f"categories for {len(batch)} transactions. "
                    f"Falling back to Misc for this batch."
                )
                batch_categories = ["Misc"] * len(batch)

            all_categories.extend(batch_categories)

        for idx, category in zip(misc_indices, all_categories):
            if category in CATEGORIES:
                df_all.at[idx, "Category"] = category
            else:
                print(f"[{self.name}] Warning: unknown category '{category}' — keeping Misc.")

        state.df_all = df_all

        newly_categorized = df_all.loc[misc_indices]
        state.categorization_report["claude_categorized"] = len(
            newly_categorized[newly_categorized["Category"] != "Misc"]
        )
        state.categorization_report["still_misc_after_claude"] = len(
            newly_categorized[newly_categorized["Category"] == "Misc"]
        )

        print(
            f"[{self.name}] Claude categorized "
            f"{state.categorization_report['claude_categorized']} transactions. "
            f"{state.categorization_report['still_misc_after_claude']} still Misc."
        )
        return state

    def _reflection_pass(self, state: LedgerState) -> LedgerState:
        """
        Third pass: Claude reviews only the transactions it just
        categorized and assigns a confidence score to each one.
        Anything below the confidence threshold gets flagged so you
        can spot it easily in the sheet. Claude can also correct a
        category it now thinks was wrong on second look.
        """
        df_all = state.df_all.copy()

        # Only reflect on what Claude categorized this run — never keyword map results
        claude_indices = state.categorization_report.get("claude_indices", [])

        if not claude_indices:
            print(f"[{self.name}] Nothing to reflect on — skipping reflection pass.")
            return state

        print(f"[{self.name}] Reflecting on {len(claude_indices)} categorizations...")

        # Add columns to track reflection results
        df_all["Confidence"] = 100  # default — keyword map results stay at 100
        df_all["Flagged"] = False
        df_all["Flag_Note"] = ""

        # Build the input for the reflection call
        transactions_to_review = [
            {
                "description": str(df_all.at[idx, "Description"]),
                "category": str(df_all.at[idx, "Category"])
            }
            for idx in claude_indices
        ]

        # Send in batches
        all_results = []
        total_batches = (len(transactions_to_review) + REFLECTION_BATCH_SIZE - 1) // REFLECTION_BATCH_SIZE        

        for batch_num, i in enumerate(range(0, len(transactions_to_review), REFLECTION_BATCH_SIZE), 1):
            batch = transactions_to_review[i : i + REFLECTION_BATCH_SIZE]
            print(f"[{self.name}] Reflection batch {batch_num}/{total_batches}...")

            batch_results = self._call_claude_reflect(batch)

            # If reflection call fails or returns wrong length, skip
            # updating those rows rather than misaligning
            if len(batch_results) != len(batch):
                print(
                    f"[{self.name}] Warning: reflection returned unexpected "
                    f"number of results for batch {batch_num}. Skipping batch."
                )
                # Fill with safe defaults so zip alignment stays correct
                batch_results = [
                    {
                        "description": t["description"],
                        "category": t["category"],
                        "confidence": 100,
                        "flag": False,
                        "note": ""
                    }
                    for t in batch
                ]

            all_results.extend(batch_results)

        # Write reflection results back to df_all
        flagged_count = 0
        corrected_count = 0

        for idx, result in zip(claude_indices, all_results):
            confidence = int(result.get("confidence", 100))
            flagged = bool(result.get("flag", False))
            note = str(result.get("note", ""))
            corrected_category = result.get("category", df_all.at[idx, "Category"])

            # Apply any category corrections Claude made during reflection
            if (
                corrected_category in CATEGORIES
                and corrected_category != df_all.at[idx, "Category"]
            ):
                original = df_all.at[idx, "Category"]
                df_all.at[idx, "Category"] = corrected_category
                corrected_count += 1
                print(
                    f"[{self.name}] Corrected: '{df_all.at[idx, 'Description']}' "
                    f"{original} → {corrected_category}"
                )

            df_all.at[idx, "Confidence"] = confidence
            df_all.at[idx, "Flagged"] = flagged
            df_all.at[idx, "Flag_Note"] = note

            if flagged:
                flagged_count += 1
                print(
                    f"[{self.name}] ⚠️  Flagged: '{df_all.at[idx, 'Description']}' "
                    f"→ {df_all.at[idx, 'Category']} "
                    f"(confidence: {confidence}%) — {note}"
                )

        state.df_all = df_all
        state.categorization_report["flagged"] = flagged_count
        state.categorization_report["corrected_by_reflection"] = corrected_count

        print(
            f"[{self.name}] Reflection complete. "
            f"{flagged_count} flagged for review, "
            f"{corrected_count} categories corrected."
        )
        return state

    def _call_claude_categorize(self, descriptions: list[str]) -> list[str]:
        """
        Send a batch of transaction descriptions to Claude and get
        back a list of category strings in the same order.
        """
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(descriptions)
                }
            ]
        )

        raw = response.content[0].text.strip()

        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        return json.loads(raw)

    def _call_claude_reflect(self, transactions: list[dict]) -> list[dict]:
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=REFLECTION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(transactions)
                }
            ]
        )

        raw = response.content[0].text.strip()

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
            # Fallback — extract the first JSON array found in the response
            import re
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                extracted = match.group(0)
                return json.loads(extracted)
            raise ValueError(
                f"Could not extract valid JSON array from reflection response: "
                f"{raw[:200]}"
            )