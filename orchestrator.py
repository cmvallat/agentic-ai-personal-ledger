from state import LedgerState
from agents.ingestion_agent import IngestionAgent
from agents.categorization_agent import CategorizationAgent
from agents.ledger_verification_agent import LedgerVerificationAgent
from agents.sheets_writer_agent import SheetsWriterAgent


class Orchestrator:
    def __init__(self):
        self.agents = [
            IngestionAgent(),
            CategorizationAgent(),
            LedgerVerificationAgent(),
            SheetsWriterAgent(),
        ]

    def run(self, state: LedgerState) -> LedgerState:
        print("\n" + "=" * 52)
        print("   🤖 AGENTIC AI PERSONAL LEDGER")
        print("=" * 52)

        for agent in self.agents:
            print(f"\n--- {agent.name} ---")
            state = agent.run(state)

            if agent.name == "IngestionAgent":
                if state.df_all is None or state.df_all.empty:
                    print("\n❌ No transactions to process. Exiting.")
                    return state

            # Halt before writing if ledger verification failed
            if agent.name == "LedgerVerificationAgent":
                if not state.ledger_valid:
                    print(
                        "\n❌ Ledger verification failed — "
                        "aborting before sheet write."
                    )
                    for check in state.ledger_report.get("checks", []):
                        if not check["passed"]:
                            print(f"   Failed: {check['name']}")
                            for error in check["errors"]:
                                print(f"   → {error}")
                    return state

        self._print_summary(state)
        return state

    def _print_summary(self, state: LedgerState) -> None:
        print("\n" + "=" * 52)
        print("   ✅ PIPELINE COMPLETE")
        print("=" * 52)

        dedup = state.dedup_report
        print(f"\n📥 Ingestion")
        print(f"   {dedup.get('transactions_remaining', 0)} transactions loaded")
        print(f"   {dedup.get('duplicates_skipped', 0)} duplicates skipped")

        cat = state.categorization_report
        print(f"\n🧠 Categorization")
        print(f"   Keyword map:  {cat.get('keyword_map_matched', 0)} transactions")
        print(f"   Claude:       {cat.get('claude_categorized', 0)} transactions")
        print(f"   Corrections:  {cat.get('corrected_by_reflection', 0)} by reflection")
        print(f"   Flagged:      {cat.get('flagged', 0)} for your review")

        print(f"\n🔍 Ledger Verification")
        for check in state.ledger_report.get("checks", []):
            status = "✅" if check["passed"] else "❌"
            print(f"   {status} {check['name']}")

        print(f"\n📊 Google Sheets")
        if state.dry_run:
            print(f"   DRY RUN — nothing written")
        else:
            for sheet in state.sheets_written:
                print(f"   ✅ {sheet}")
            if state.sheet_url:
                print(f"\n   🔗 {state.sheet_url}")

        print("\n" + "=" * 52 + "\n")