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

# No longer used, but keeping just in case. This is to resize a single column; currently we are resizing all columns
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

def auto_resize_columns(
    spreadsheet: gspread.Spreadsheet,
    worksheet: gspread.Worksheet,
    start_col_index: int = 0,
    end_col_index: int = 10,
    padding_pixels: int = 20
) -> None:
    """
    Auto-resize a range of columns to fit their longest content,
    then add padding pixels to each column for breathing room.
    col indices are 0-based (column A = 0, column B = 1, etc).
    """
    sheet_id = worksheet._properties["sheetId"]

    # Step 1 — auto-resize to fit content
    spreadsheet.batch_update({
        "requests": [
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": start_col_index,
                        "endIndex": end_col_index
                    }
                }
            }
        ]
    })

    # Step 2 — fetch current column widths via the Sheets API directly
    # gspread exposes the underlying Google API client via spreadsheet.client
    sheets_service = spreadsheet.client.batch_update
    spreadsheet_id = spreadsheet.id

    metadata = spreadsheet.client.request(
        "get",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
        params={
            "includeGridData": False,
            "fields": "sheets.properties.sheetId,sheets.data.columnMetadata"
        }
    ).json()

    # Find column metadata for this worksheet
    col_metadata = []
    for s in metadata.get("sheets", []):
        if s["properties"]["sheetId"] == sheet_id:
            col_metadata = (
                s.get("data", [{}])[0].get("columnMetadata", [])
            )
            break

    # Step 3 — build padding requests using current widths
    padding_requests = []
    for col_index in range(start_col_index, end_col_index):
        if col_index < len(col_metadata):
            current_width = col_metadata[col_index].get("pixelSize", 100)
        else:
            current_width = 100

        padding_requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col_index,
                    "endIndex": col_index + 1
                },
                "properties": {
                    "pixelSize": current_width + padding_pixels
                },
                "fields": "pixelSize"
            }
        })

    if padding_requests:
        spreadsheet.batch_update({"requests": padding_requests})

def bold_row(worksheet: gspread.Worksheet, row: int) -> None:
    """Bold an entire row by row number (1-based)."""
    header_fmt = CellFormat(textFormat=TextFormat(bold=True))
    format_cell_range(worksheet, f"A{row}:E{row}", header_fmt)