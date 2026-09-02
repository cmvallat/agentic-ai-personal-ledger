import argparse
import json
import os
from orchestrator import Orchestrator
from state import LedgerState


def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config", "config.json")
    with open(config_path) as f:
        return json.load(f)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Agentic AI Personal Ledger"
    )
    parser.add_argument(
        "--cc",
        help="Path to credit card transactions CSV (overrides config.json)"
    )
    parser.add_argument(
        "--checking",
        help="Path to checking account transactions CSV (overrides config.json)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all agents but skip writing to Google Sheets"
    )
    return parser.parse_args()


if __name__ == "__main__":
    config = load_config()
    args = parse_args()

    # CLI args take priority, fall back to config.json
    cc_path = args.cc or config["cc_file_path"]
    checking_path = args.checking or config["checking_file_path"]

    state = LedgerState(
        cc_csv_path=cc_path,
        checking_csv_path=checking_path,
        dry_run=args.dry_run,
        features=config.get("features", {})
    )

    orchestrator = Orchestrator()
    orchestrator.run(state)