from state import LedgerState
from agents.ingestion_agent import IngestionAgent
from agents.categorization_agent import CategorizationAgent
from agents.sheets_writer_agent import SheetsWriterAgent


class Orchestrator:
    def __init__(self):
        self.agents = [
            IngestionAgent(),
            CategorizationAgent(),
            SheetsWriterAgent(),
        ]

    def run(self, state: LedgerState) -> LedgerState:
        print("\n" + "=" * 52)
        print("   🤖 AGENTIC AI PERSONAL LEDGER")
        print("=" * 52)

        for agent in self.agents:
            print(f"\n--- {agent.name} ---")
            state = agent.run(state)

            # Stop the pipeline early if ingestion produced nothing
            if agent.name == "IngestionAgent":
                if state.df_all is None or state.df_all.empty:
                    print("\n❌ No transactions to process. Exiting.")
                    return state

        self._print_summary(state)
        return state

    def _print_summary(self, state: LedgerState) -> None:
        print("\n" + "=" * 52)
        print("   ✅ PIPELINE COMPLETE")
        print("=" * 52)

        # Ingestion
        dedup = state.dedup_report
        print(f"\n📥 Ingestion")
        print(f"   {dedup.get('transactions_remaining', 0)} transactions loaded")
        print(f"   {dedup.get('duplicates_skipped', 0)} duplicates skipped")

        # Categorization
        cat = state.categorization_report
        print(f"\n🧠 Categorization")
        print(f"   Keyword map:  {cat.get('keyword_map_matched', 0)} transactions")
        print(f"   Claude:       {cat.get('claude_categorized', 0)} transactions")
        print(f"   Corrections:  {cat.get('corrected_by_reflection', 0)} by reflection")
        print(f"   Flagged:      {cat.get('flagged', 0)} for your review")

        # Sheets
        print(f"\n📊 Google Sheets")
        if state.dry_run:
            print(f"   DRY RUN — nothing written")
        else:
            for sheet in state.sheets_written:
                print(f"   ✅ {sheet}")
            if state.sheet_url:
                print(f"\n   🔗 {state.sheet_url}")

        print("\n" + "=" * 52 + "\n")