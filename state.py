from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class LedgerState:
    # Inputs
    cc_csv_path: str = ""
    checking_csv_path: str = ""
    dry_run: bool = False

    # Set by Ingestion Agent
    df_cc: Optional[pd.DataFrame] = None
    df_checking: Optional[pd.DataFrame] = None
    df_payments: Optional[pd.DataFrame] = None
    df_income: Optional[pd.DataFrame] = None
    df_all: Optional[pd.DataFrame] = None
    dedup_report: dict = field(default_factory=dict)
    df_checking_raw: Optional[pd.DataFrame] = None

    # Set by Ledger Verification Agent
    ledger_valid: bool = False
    ledger_report: dict = field(default_factory=dict)

    # Set by Categorization Agent
    categorization_report: dict = field(default_factory=dict)

    # Set by Sheets Writer Agent
    sheets_written: list = field(default_factory=list)
    sheet_url: str = ""
    write_success: bool = False

