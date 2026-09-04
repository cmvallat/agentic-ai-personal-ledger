import json
import os
import calendar
import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from state import LedgerState
from utils.sheets import authorize_google_sheets

load_dotenv()

PROXIMITY_DAYS = 3  # days either side of a holiday counted as holiday spending

SUGGESTIONS_PROMPT = """You are a personal finance advisor reviewing a spending summary.
Based on the data below, give 3-5 concise, actionable suggestions to reduce spending and save more money.
Be specific — reference actual categories and dollar amounts from the data.
If trend data is present, note whether any categories are increasing month-over-month.
Each suggestion should be a single sentence starting with an action verb.
Return ONLY a valid JSON array of strings, one suggestion per element. No markdown, no explanation."""


class SpendingAnalysisAgent:
    name = "SpendingAnalysisAgent"

    def __init__(self):
        self.client = Anthropic()
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        with open(config_path) as f:
            self.config = json.load(f)

    def run(self, state: LedgerState) -> LedgerState:
        print(f"[{self.name}] Running...")

        if state.df_all is None or state.df_all.empty:
            print(f"[{self.name}] No transactions to analyze. Skipping.")
            return state

        df_current = state.df_all.copy()
        df_current["Date"] = pd.to_datetime(df_current["Date"])

        # Fetch all previously written months from Google Sheets
        df_history = self._fetch_history()
        if not df_history.empty:
            month_count = df_history["Date"].dt.to_period("M").nunique()
            print(f"[{self.name}] Loaded {len(df_history)} historical transactions across {month_count} month(s).")
            df_all = pd.concat([df_history, df_current], ignore_index=True)
        else:
            print(f"[{self.name}] No historical data found — analyzing current transactions only.")
            df_all = df_current

        analysis = {}
        analysis["category_breakdown"] = self._category_breakdown(df_all)
        analysis["day_of_week"]        = self._day_of_week_pattern(df_all)
        analysis["time_of_month"]      = self._time_of_month_pattern(df_all)
        analysis["top_merchants"]      = self._top_merchants(df_all)
        analysis["holiday_spending"]   = self._holiday_proximity(df_all)
        analysis["monthly_trends"]     = self._monthly_trends(df_all)

        print(f"[{self.name}] Generating suggestions via Claude...")
        analysis["suggestions"] = self._get_suggestions(analysis)

        state.analysis_report = analysis
        print(
            f"[{self.name}] Done. "
            f"{len(analysis['category_breakdown'])} categories, "
            f"{len(analysis['monthly_trends'])} months, "
            f"{len(analysis['suggestions'])} suggestions."
        )
        return state

    # ------------------------------------------------------------------ #
    #  History fetching                                                   #
    # ------------------------------------------------------------------ #

    def _fetch_history(self) -> pd.DataFrame:
        """
        Read transaction rows from every monthly tab already in the
        spreadsheet. Returns an empty DataFrame if the sheet is
        unreachable or has no month tabs yet.
        """
        try:
            client = authorize_google_sheets(self.config["creds_path"])
            spreadsheet = client.open_by_key(self.config["spreadsheet_key"])
        except Exception as e:
            print(f"[{self.name}] Could not connect to Sheets for history: {e}")
            return pd.DataFrame()

        all_dfs = []
        for ws in spreadsheet.worksheets():
            # Only process tabs whose title parses as "Month YYYY"
            try:
                pd.to_datetime(ws.title, format="%B %Y")
            except ValueError:
                continue

            values = ws.get_all_values()
            if len(values) < 2:
                continue

            rows = []
            for row in values[1:]:          # skip header
                if not row or not row[0].strip():
                    break                   # blank row = end of transactions
                if len(row) > 1 and row[1].strip() in ("Income", "Payment"):
                    break                   # hit income/payment section
                if len(row) >= 4:
                    rows.append({
                        "Date":        row[0],
                        "Description": row[1],
                        "Net":         row[2],
                        "Category":    row[3],
                    })

            if not rows:
                continue

            df = pd.DataFrame(rows)
            df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%y", errors="coerce")
            df["Net"]  = pd.to_numeric(df["Net"], errors="coerce").fillna(0)
            df = df.dropna(subset=["Date"])

            if not df.empty:
                all_dfs.append(df)
                print(f"[{self.name}]   {ws.title}: {len(df)} transactions")

        return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

    # ------------------------------------------------------------------ #
    #  Phase 1 — deterministic analysis                                   #
    # ------------------------------------------------------------------ #

    def _category_breakdown(self, df: pd.DataFrame) -> list[dict]:
        total = df["Net"].sum()
        grouped = (
            df.groupby("Category")["Net"]
            .sum()
            .sort_values(ascending=False)
            .reset_index()
        )
        return [
            {
                "category": row["Category"],
                "total": round(float(row["Net"]), 2),
                "pct": round(float(row["Net"]) / total * 100, 1) if total else 0,
            }
            for _, row in grouped.iterrows()
        ]

    def _day_of_week_pattern(self, df: pd.DataFrame) -> list[dict]:
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        df = df.copy()
        df["DayOfWeek"] = df["Date"].dt.dayofweek
        grouped = (
            df.groupby("DayOfWeek")["Net"]
            .agg(["sum", "mean", "count"])
            .reset_index()
        )
        return [
            {
                "day":         day_names[int(row["DayOfWeek"])],
                "total":       round(float(row["sum"]), 2),
                "avg_per_txn": round(float(row["mean"]), 2),
                "txn_count":   int(row["count"]),
            }
            for _, row in grouped.iterrows()
        ]

    def _time_of_month_pattern(self, df: pd.DataFrame) -> list[dict]:
        df = df.copy()
        df["DayOfMonth"] = df["Date"].dt.day
        bins   = [0, 10, 20, 31]
        labels = ["Early (1–10)", "Mid (11–20)", "Late (21–31)"]
        df["Period"] = pd.cut(df["DayOfMonth"], bins=bins, labels=labels)
        grouped = df.groupby("Period", observed=True)["Net"].agg(["sum", "count"]).reset_index()
        total = df["Net"].sum()
        return [
            {
                "period":    str(row["Period"]),
                "total":     round(float(row["sum"]), 2),
                "txn_count": int(row["count"]),
                "pct":       round(float(row["sum"]) / total * 100, 1) if total else 0,
            }
            for _, row in grouped.iterrows()
        ]

    def _top_merchants(self, df: pd.DataFrame, n: int = 5) -> list[dict]:
        grouped = (
            df.groupby("Description")["Net"]
            .agg(["sum", "count"])
            .sort_values("sum", ascending=False)
            .head(n)
            .reset_index()
        )
        return [
            {
                "merchant": row["Description"],
                "total":    round(float(row["sum"]), 2),
                "visits":   int(row["count"]),
            }
            for _, row in grouped.iterrows()
        ]

    def _holiday_proximity(self, df: pd.DataFrame) -> list[dict]:
        years    = df["Date"].dt.year.unique().tolist()
        holidays = self._us_holidays(years)
        results  = []
        for holiday_date, holiday_name in sorted(holidays.items()):
            window_start = holiday_date - pd.Timedelta(days=PROXIMITY_DAYS)
            window_end   = holiday_date + pd.Timedelta(days=PROXIMITY_DAYS)
            nearby = df[(df["Date"] >= window_start) & (df["Date"] <= window_end)]
            if not nearby.empty:
                results.append({
                    "holiday":   holiday_name,
                    "date":      holiday_date.strftime("%m/%d/%Y"),
                    "txn_count": len(nearby),
                    "total":     round(float(nearby["Net"].sum()), 2),
                })
        return results

    def _monthly_trends(self, df: pd.DataFrame) -> list[dict]:
        """Total spend and per-category breakdown for each month, sorted oldest-first."""
        df = df.copy()
        df["MonthKey"] = df["Date"].dt.to_period("M")
        months = sorted(df["MonthKey"].unique())

        monthly_total  = df.groupby("MonthKey")["Net"].sum()
        monthly_by_cat = df.groupby(["MonthKey", "Category"])["Net"].sum()

        results = []
        for month in months:
            by_cat = (
                monthly_by_cat[month].sort_values(ascending=False)
                if month in monthly_by_cat.index.get_level_values(0)
                else pd.Series(dtype=float)
            )
            results.append({
                "month":       month.strftime("%B %Y"),
                "total":       round(float(monthly_total[month]), 2),
                "by_category": {cat: round(float(amt), 2) for cat, amt in by_cat.items()},
            })
        return results

    def _us_holidays(self, years: list[int]) -> dict:
        holidays = {}
        for year in years:
            holidays[pd.Timestamp(year, 1, 1)]   = "New Year's Day"
            holidays[pd.Timestamp(year, 7, 4)]   = "Independence Day"
            holidays[pd.Timestamp(year, 12, 25)] = "Christmas"
            holidays[pd.Timestamp(year, 12, 31)] = "New Year's Eve"
            holidays[self._nth_weekday(year, 5, 0, -1)] = "Memorial Day"
            holidays[self._nth_weekday(year, 9, 0, 1)]  = "Labor Day"
            holidays[self._nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
        return holidays

    def _nth_weekday(self, year: int, month: int, weekday: int, n: int) -> pd.Timestamp:
        """weekday: 0=Mon…6=Sun. n: positive = nth from start, -1 = last."""
        if n > 0:
            first = pd.Timestamp(year, month, 1)
            diff  = (weekday - first.weekday()) % 7
            return first + pd.Timedelta(days=diff + 7 * (n - 1))
        else:
            last = pd.Timestamp(year, month, calendar.monthrange(year, month)[1])
            diff = (last.weekday() - weekday) % 7
            return last - pd.Timedelta(days=diff)

    # ------------------------------------------------------------------ #
    #  Phase 2 — Claude suggestions                                       #
    # ------------------------------------------------------------------ #

    def _get_suggestions(self, analysis: dict) -> list[str]:
        lines = ["Category spending (highest to lowest, all months combined):"]
        for item in analysis["category_breakdown"]:
            lines.append(f"  {item['category']}: ${item['total']:,.2f} ({item['pct']}%)")

        lines.append("\nSpending by day of week:")
        for item in sorted(analysis["day_of_week"], key=lambda x: -x["total"]):
            lines.append(
                f"  {item['day']}: ${item['total']:,.2f} total, "
                f"{item['txn_count']} transactions"
            )

        lines.append("\nSpending by time of month:")
        for item in analysis["time_of_month"]:
            lines.append(f"  {item['period']}: ${item['total']:,.2f} ({item['pct']}%)")

        lines.append("\nTop merchants:")
        for item in analysis["top_merchants"]:
            lines.append(f"  {item['merchant']}: ${item['total']:,.2f} ({item['visits']} visits)")

        if analysis["holiday_spending"]:
            lines.append("\nHoliday-adjacent spending:")
            for item in analysis["holiday_spending"]:
                lines.append(
                    f"  {item['holiday']} ({item['date']}): "
                    f"${item['total']:,.2f} across {item['txn_count']} transactions"
                )

        if len(analysis["monthly_trends"]) > 1:
            lines.append("\nMonth-over-month totals:")
            for item in analysis["monthly_trends"]:
                lines.append(f"  {item['month']}: ${item['total']:,.2f}")

        prompt = SUGGESTIONS_PROMPT + "\n\n" + "\n".join(lines)

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = "\n".join(
                    line for line in raw.split("\n")
                    if not line.startswith("```")
                ).strip()
            return json.loads(raw)
        except Exception as e:
            print(f"[{self.name}] Warning: could not get suggestions from Claude: {e}")
            return []
