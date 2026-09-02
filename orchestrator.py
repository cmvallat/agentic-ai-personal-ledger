from state import LedgerState
from agents.ingestion_agent import IngestionAgent
from agents.categorization_agent import CategorizationAgent
from agents.ledger_verification_agent import LedgerVerificationAgent
from agents.sheets_writer_agent import SheetsWriterAgent
from agents.batch_assignment_agent import BatchAssignmentAgent


class Orchestrator:
    def __init__(self):
        self.pipeline_agents = [
            IngestionAgent(),
            CategorizationAgent(),
            LedgerVerificationAgent(),
            BatchAssignmentAgent(),
        ]
        self.sheets_writer = SheetsWriterAgent()

    def run(self, state: LedgerState) -> LedgerState:
        print("\n" + "=" * 52)
        print("   🤖 AGENTIC AI PERSONAL LEDGER")
        print("=" * 52)

        features = state.features

        for agent in self.pipeline_agents:

            # Feature flag checks
            if agent.name == "BatchAssignmentAgent" and \
                    not features.get("batch_assignment", True):
                print(f"\n--- {agent.name} --- [SKIPPED — feature flag off]")
                continue

            if agent.name == "LedgerVerificationAgent" and \
                    not features.get("ledger_verification", True):
                print(f"\n--- {agent.name} --- [SKIPPED — feature flag off]")
                continue

            print(f"\n--- {agent.name} ---")
            state = agent.run(state)

            if agent.name == "IngestionAgent":
                if state.df_all is None or state.df_all.empty:
                    print("\n❌ No transactions to process. Exiting.")
                    return state

        # Ask user before writing regardless of check results
        state = self._confirm_and_write(state)

        self._print_summary(state)
        return state

    def _confirm_and_write(self, state: LedgerState) -> LedgerState:
        """
        Ask the user whether to proceed with writing to Google Sheets.
        Always asks regardless of check results so the user can choose
        to write partial or flagged data if they want to.
        Respects the sheets_writer feature flag and dry_run flag.
        """
        if not state.features.get("sheets_writer", True):
            print("\n[Orchestrator] Sheets writer skipped — feature flag off.")
            return state

        if state.dry_run:
            print("\n[Orchestrator] DRY RUN — skipping write.")
            state = self.sheets_writer.run(state)
            return state

        # Show pre-write summary of any issues
        checks = state.ledger_report.get("checks", [])
        failed_checks = [c for c in checks if not c["passed"]]
        flagged_payments = state.batch_report.get("payments_flagged", 0)

        print("\n" + "=" * 52)
        print("   📋 PRE-WRITE SUMMARY")
        print("=" * 52)

        if failed_checks:
            print(f"\n⚠️  {len(failed_checks)} ledger check(s) failed:")
            for check in failed_checks:
                print(f"   ❌ {check['name']}")
                for error in check["errors"]:
                    print(f"      → {error}")
        else:
            print("\n✅ All ledger checks passed.")

        if flagged_payments > 0:
            print(f"\n⚠️  {flagged_payments} payment batch(es) flagged for manual review.")
        elif state.batch_report:
            print("✅ All payment batches confirmed.")

        print(f"\nWrite results to Google Sheets? (y/n): ", end="")
        user_input = input().strip().lower()

        if user_input in ("y", "yes"):
            print("\n[Orchestrator] Writing to Google Sheets...")
            state = self.sheets_writer.run(state)
        else:
            print("\n[Orchestrator] Write cancelled by user.")
            state.write_success = False

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

        if state.ledger_report.get("checks"):
            print(f"\n🔍 Ledger Verification")
            for check in state.ledger_report.get("checks", []):
                status = "✅" if check["passed"] else "❌"
                print(f"   {status} {check['name']}")

        batch = state.batch_report
        if batch:
            print(f"\n📦 Batch Assignment")
            print(f"   Payments confirmed: {batch.get('payments_confirmed', 0)}")
            print(f"   Payments flagged:   {batch.get('payments_flagged', 0)}")
            print(f"   Pending:            {batch.get('transactions_pending', 0)} transactions")

        print(f"\n📊 Google Sheets")
        if state.dry_run:
            print(f"   DRY RUN — nothing written")
        elif not state.write_success:
            print(f"   Write cancelled or failed.")
        else:
            for sheet in state.sheets_written:
                print(f"   ✅ {sheet}")
            if state.sheet_url:
                print(f"\n   🔗 {state.sheet_url}")

        print("\n" + "=" * 52 + "\n")