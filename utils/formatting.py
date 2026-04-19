import gspread
from gspread_formatting import (
    CellFormat,
    TextFormat,
    NumberFormat,
    Color,
    format_cell_range,
    ConditionalFormatRule,
    BooleanRule,
    BooleanCondition,
    GridRange,
    get_conditional_format_rules,
)

NEGATIVE_COLOR = Color(red=0.956, green=0.8, blue=0.8)
POSITIVE_COLOR = Color(red=182/255, green=215/255, blue=168/255)


def format_sheet(sh: gspread.Worksheet) -> None:
    print("Formatting sheet...")
    range_str = "A1:Z200"

    center_fmt = CellFormat(horizontalAlignment="LEFT")
    format_cell_range(sh, range_str, center_fmt)

    financial_format = CellFormat(
        numberFormat=NumberFormat(type="NUMBER", pattern="#,##0.00")
    )
    format_cell_range(sh, range_str, financial_format)

    header_fmt = CellFormat(
        horizontalAlignment="LEFT",
        textFormat=TextFormat(bold=True)
    )
    format_cell_range(sh, "A1:Z1", header_fmt)


def apply_conditional_formatting(
    ou_budget_cell: str,
    sheet: gspread.Worksheet
) -> None:
    red_rule = ConditionalFormatRule(
        ranges=[GridRange.from_a1_range(ou_budget_cell, sheet)],
        booleanRule=BooleanRule(
            condition=BooleanCondition("NUMBER_LESS", ["0"]),
            format=CellFormat(
                backgroundColor=NEGATIVE_COLOR,
                textFormat=TextFormat(bold=False)
            )
        )
    )
    green_rule = ConditionalFormatRule(
        ranges=[GridRange.from_a1_range(ou_budget_cell, sheet)],
        booleanRule=BooleanRule(
            condition=BooleanCondition("NUMBER_GREATER", ["0"]),
            format=CellFormat(
                backgroundColor=POSITIVE_COLOR,
                textFormat=TextFormat(bold=False)
            )
        )
    )

    rules = get_conditional_format_rules(sheet)
    rules.clear()
    rules.append(red_rule)
    rules.append(green_rule)
    rules.save()

def auto_resize_column(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet, col_index: int) -> None:
    """
    Auto-resize a column to fit its longest content, equivalent to
    double-clicking the column border in Google Sheets.
    col_index is 0-based (so column A = 0, column F = 5).
    """
    sheet_id = worksheet._properties["sheetId"]
    body = {
        "requests": [
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_index,
                        "endIndex": col_index + 1
                    }
                }
            }
        ]
    }
    spreadsheet.batch_update(body)

def bold_row(worksheet: gspread.Worksheet, row: int) -> None:
    """Bold an entire row by row number (1-based)."""
    header_fmt = CellFormat(textFormat=TextFormat(bold=True))
    format_cell_range(worksheet, f"A{row}:E{row}", header_fmt)