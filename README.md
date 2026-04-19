# agentic-ai-personal-ledger

A multi-agent AI system that automates personal financial tracking.
Reads credit card and checking account transaction CSVs, categorizes
spending using Claude, detects duplicates, and writes the results to
a Google Sheet organized by month.

Built as a learning project to explore agentic AI design patterns —
orchestration, shared state, tool use, and reflection.

---

## What it does

- Loads and normalizes transactions from credit card and checking CSVs
- Detects and skips transactions already written to the sheet
- Categorizes transactions using a keyword map, then uses Claude for
  anything the map doesn't recognize
- Reflection pass: Claude reviews its own categorizations and flags
  anything it isn't confident about
- Writes results to Google Sheets, creating new monthly tabs as needed
- Supports `--dry-run` to preview output without touching the sheet

---

## Agent architecture

main.py → Orchestrator
│
├── IngestionAgent
│ Load CSVs → normalize → separate payments/income
│ → deduplicate against existing sheet
│
├── CategorizationAgent
│ Keyword map → Claude for unknowns → reflection pass
│
└── SheetsWriterAgent
Create/update monthly tabs → write transactions
→ summary section → category totals → formatting

Each agent receives a shared `LedgerState` object, does its work,
and returns the updated state to the orchestrator.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/agentic-ai-personal-ledger.git
cd agentic-ai-personal-ledger
```

### 2. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Google Sheets credentials

- Go to [console.cloud.google.com](https://console.cloud.google.com)
- Create a project and enable the **Google Sheets API** and **Google Drive API**
- Create a **Service Account** and download the credentials JSON
- Place the file at the path specified in `config/config.json`
- Share your Google Sheet with the service account email (Editor access)

### 4. Anthropic API key

- Create an account at [console.anthropic.com](https://console.anthropic.com)
- Generate an API key
- Create a `.env` file in the project root:

ANTHROPIC_API_KEY=sk-ant-...

### 5. Config

Create `config/config.json`:

```json
{
  "spreadsheet_key": "your_google_sheet_id_here",
  "creds_path": "../credentials.json",
  "cc_file_path": "../data/cc.csv",
  "checking_file_path": "../data/checking.csv",
  "starting_balance": 0.0
}
```

The spreadsheet key is the long ID in your Google Sheet URL:
`https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit`

---

## Usage

```bash
# Use CSV paths from config.json
python3 main.py

# Override with specific files
python3 main.py --cc path/to/cc.csv --checking path/to/checking.csv

# Preview without writing to Google Sheets
python3 main.py --dry-run
```

---

## Example output

====================================================
🤖 AGENTIC AI PERSONAL LEDGER
--- IngestionAgent ---
[IngestionAgent] Loading CSVs...
[IngestionAgent] Loaded 48 CC rows, 14 checking rows.
[IngestionAgent] Separating payments and income...
[IngestionAgent] Found 2 payments, 3 income transactions.
[IngestionAgent] Combining and sorting transactions...
[IngestionAgent] Combined total: 51 transactions.
[IngestionAgent] Checking for duplicates in existing sheet...
[IngestionAgent] 11 duplicates skipped, 40 transactions remaining.
--- CategorizationAgent ---
[CategorizationAgent] Keyword map: 28 matched, 12 unknown.
[CategorizationAgent] Sending 12 unknowns to Claude...
[CategorizationAgent] Reflecting on 12 categorizations...
[CategorizationAgent] ⚠️ Flagged: 'WHOLESOME MKTPLACE' → Shopping (62%)
--- SheetsWriterAgent ---
[SheetsWriterAgent] Writing March 2026...
[SheetsWriterAgent] ✅ March 2026 complete.
[SheetsWriterAgent] Writing April 2026...
[SheetsWriterAgent] ✅ April 2026 complete.
====================================================
✅ PIPELINE COMPLETE
📥 Ingestion
40 transactions loaded
11 duplicates skipped
🧠 Categorization
Keyword map: 28 transactions
Claude: 10 transactions
Corrections: 1 by reflection
Flagged: 1 for your review
📊 Google Sheets
✅ March 2026
✅ April 2026
🔗 https://docs.google.com/spreadsheets/d/...
====================================================

---

## Project structure

agentic-ai-personal-ledger/
├── main.py # Entry point, CLI arguments
├── orchestrator.py # Coordinates agents, manages flow
├── state.py # Shared LedgerState dataclass
├── mappings.py # Keyword maps for categories and descriptions
├── agents/
│ ├── ingestion_agent.py # Load, normalize, deduplicate
│ ├── categorization_agent.py # Keyword map + Claude + reflection
│ └── sheets_writer_agent.py # Write results to Google Sheets
├── utils/
│ ├── sheets.py # Google Sheets auth and helpers
│ └── formatting.py # Sheet formatting utilities
├── config/
│ └── config.json # Credentials paths and settings (not committed)
├── requirements.txt
└── .env # Anthropic API key (not committed)

---

## Agentic AI patterns demonstrated

**Orchestrator pattern** — a central coordinator manages agent execution
order, handles errors between steps, and owns the pipeline flow.

**Shared state** — agents communicate through a single `LedgerState`
object rather than calling each other directly. Each agent reads what
it needs and writes its results back.

**Tool use** — agents use external tools (Google Sheets API, Anthropic
API) to do work they couldn't do alone.

**Reflection** — the Categorization Agent makes a second Claude call
to review its own work, flag uncertainty, and correct mistakes before
they reach the sheet.

**Deterministic + AI hybrid** — AI is used only where human-like
judgment is needed (categorizing ambiguous merchants). Everything else
— loading data, deduplication, writing to sheets, math — is
deterministic Python.

---

## Roadmap for future development

- [ ] Ledger verification agent — mathematically verify running balance
- [ ] Refund reconciliation — match refunds to original charges
- [ ] Cross-month batch assignment — handle payments spanning month boundaries
- [ ] Eval loop — track Claude categorization accuracy over time
- [ ] Plaid integration — eliminate manual CSV download step
