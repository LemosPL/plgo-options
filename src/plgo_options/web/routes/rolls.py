"""Calendar roll endpoints — reads from Calendar rolls.xlsx."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from plgo_options.data.trades import read_calendar_rolls

router = APIRouter()


@router.get("/sheets")
async def get_roll_sheets():
    """Return list of sheet names in the calendar rolls workbook."""
    try:
        data = read_calendar_rolls()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"sheets": list(data.keys())}


@router.get("/data")
async def get_roll_data(sheet: str | None = None):
    """Return calendar roll data.

    If `sheet` query param is given, return only that sheet.
    Otherwise return all sheets.
    """
    try:
        data = read_calendar_rolls()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if sheet:
        if sheet not in data:
            raise HTTPException(status_code=404, detail=f"Sheet '{sheet}' not found")
        return {"sheet": sheet, "rows": data[sheet]}

    return {"sheets": {name: rows for name, rows in data.items()}}