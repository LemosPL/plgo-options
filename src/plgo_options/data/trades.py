"""Read trades from the ETH/FIL dashboard Excel files."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, date

import openpyxl

# Expected columns from the 'Trades' sheet
TRADE_COLUMNS = [
    "Counterparty",
    "ID",
    "Initial Trade Date",
    "Buy / Sell / Unwind",
    "Option Type",
    "Trade_ID",
    "Option Expiry Date",
    "Days Remaining to Expiry",
    "Strike",
    "Ref. Spot Price",
    "% OTM",
    "ETH Options",
    "$ Notional (mm)",
    "Premium per Contract",
    "Premium USD",
]

# --- ETH paths ---
# Primary: bundled data/ directory (works in Docker and local)
_PROJECT_DATA_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "PLGO_Trades_2026-03-17.xlsx"
)

# Fallback: user's Downloads folder (local dev)
_DOWNLOADS_PATH = (
    Path.home() / "Downloads" / "PLGO_Trades_2026-03-17.xlsx"
)

# --- FIL paths ---
_FIL_PROJECT_DATA_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "FIL - Dashboard Risk+PnL Improvement Proposal.xlsx"
)

_FIL_DOWNLOADS_PATH = (
    Path.home() / "Downloads" / "FIL - Dashboard Risk+PnL Improvement Proposal (2).xlsx"
)


def _safe_float(v) -> float:
    """Coerce a value to float, returning 0.0 on failure."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_eth_trades(file_path: Path | None = None) -> list[dict]:
    """Return a list of trade dicts from the 'Trades' tab.

    Each dict is keyed by the exact column headers found in the sheet.
    Dates are normalised to ISO-8601 strings for JSON serialisation.
    """
    fp = file_path
    if fp is None:
        # Try bundled data/ first, then Downloads
        if _PROJECT_DATA_PATH.exists():
            fp = _PROJECT_DATA_PATH
        else:
            fp = _DOWNLOADS_PATH

    try:
        wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    except (FileNotFoundError, PermissionError, OSError):
        # Last resort: try the other path
        alt = _PROJECT_DATA_PATH if fp != _PROJECT_DATA_PATH else _DOWNLOADS_PATH
        if alt.exists():
            fp = alt
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        else:
            raise

    ws = wb["Trades"]
    rows = ws.iter_rows(values_only=True)

    # Scan for the header row that starts with "Counterparty"
    headers: list[str] | None = None
    for row in rows:
        if row and str(row[0]).strip().lower() == "counterparty":
            headers = [
                str(h).strip() if h is not None else f"col_{i}"
                for i, h in enumerate(row)
            ]
            break

    if headers is None:
        wb.close()
        raise ValueError(
            "Could not find header row starting with 'Counterparty' "
            "in the 'Trades' sheet"
        )

    trades: list[dict] = []
    for row in rows:
        if all(cell is None for cell in row):
            continue
        record = dict(zip(headers, row))
        # Normalise dates/datetimes → ISO strings
        for k, v in record.items():
            if isinstance(v, datetime):
                record[k] = v.isoformat()
            elif isinstance(v, date):
                record[k] = v.isoformat()
        trades.append(record)

    wb.close()
    return trades


def read_fil_trades(file_path: Path | None = None) -> list[dict]:
    """Return a list of trade dicts from the FIL 'Trades' tab.

    Same structure as read_eth_trades but reads from the FIL spreadsheet.
    The FIL spreadsheet reuses 'ETH Options' as the qty column header.
    """
    fp = file_path
    if fp is None:
        if _FIL_PROJECT_DATA_PATH.exists():
            fp = _FIL_PROJECT_DATA_PATH
        else:
            fp = _FIL_DOWNLOADS_PATH

    try:
        wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    except (FileNotFoundError, PermissionError, OSError):
        alt = _FIL_PROJECT_DATA_PATH if fp != _FIL_PROJECT_DATA_PATH else _FIL_DOWNLOADS_PATH
        if alt.exists():
            fp = alt
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        else:
            raise

    ws = wb["Trades"]
    rows = ws.iter_rows(values_only=True)

    # Scan for the header row that starts with "Counterparty"
    headers: list[str] | None = None
    for row in rows:
        if row and str(row[0]).strip().lower() == "counterparty":
            headers = [
                str(h).strip() if h is not None else f"col_{i}"
                for i, h in enumerate(row)
            ]
            break

    if headers is None:
        wb.close()
        raise ValueError(
            "Could not find header row starting with 'Counterparty' "
            "in the FIL 'Trades' sheet"
        )

    trades: list[dict] = []
    for row in rows:
        if all(cell is None for cell in row):
            continue
        record = dict(zip(headers, row))
        for k, v in record.items():
            if isinstance(v, datetime):
                record[k] = v.isoformat()
            elif isinstance(v, date):
                record[k] = v.isoformat()
        trades.append(record)

    wb.close()
    return trades


def aggregate_positions(trades: list[dict]) -> list[dict]:
    """Aggregate raw trades into net positions grouped by
    (Option Type, Strike, Option Expiry Date).

    Returns a list of position dicts with net quantities, average prices,
    and summed notionals.
    """
    positions: dict[str, dict] = {}

    for t in trades:
        opt_type = str(t.get("Option Type") or "").strip()
        strike = _safe_float(t.get("Strike"))
        expiry = str(t.get("Option Expiry Date") or "").strip()
        key = f"{opt_type}|{strike}|{expiry}"

        side_raw = str(t.get("Buy / Sell / Unwind") or "").strip().lower()
        qty = _safe_float(t.get("ETH Options"))
        premium_per = _safe_float(t.get("Premium per Contract"))
        premium_usd = _safe_float(t.get("Premium USD"))
        notional_mm = _safe_float(t.get("$ Notional (mm)"))
        ref_spot = _safe_float(t.get("Ref. Spot Price"))
        days_remaining = _safe_float(t.get("Days Remaining to Expiry"))
        pct_otm = _safe_float(t.get("% OTM"))

        if side_raw in ("buy", "long"):
            sign = 1.0
        elif side_raw in ("sell", "short"):
            sign = -1.0
        elif side_raw == "unwind":
            sign = -1.0  # unwind reduces the position
        else:
            sign = 1.0

        if key not in positions:
            positions[key] = {
                "option_type": opt_type,
                "strike": strike,
                "expiry": expiry,
                "days_remaining": days_remaining,
                "pct_otm": pct_otm,
                "ref_spot": ref_spot,
                "net_qty": 0.0,
                "total_premium_usd": 0.0,
                "total_notional_mm": 0.0,
                "trade_count": 0,
                "counterparties": set(),
            }

        pos = positions[key]
        pos["net_qty"] += sign * qty
        pos["total_premium_usd"] += sign * premium_usd
        pos["total_notional_mm"] += sign * notional_mm
        pos["trade_count"] += 1
        pos["days_remaining"] = max(pos["days_remaining"], days_remaining)
        cp = str(t.get("Counterparty") or "").strip()
        if cp:
            pos["counterparties"].add(cp)

    # Compute derived fields and serialise sets
    result: list[dict] = []
    for pos in positions.values():
        net = pos["net_qty"]
        pos["avg_premium_per_contract"] = (
            pos["total_premium_usd"] / net if net != 0 else 0.0
        )
        pos["counterparties"] = sorted(pos["counterparties"])
        pos["side"] = "Long" if net > 0 else ("Short" if net < 0 else "Flat")
        result.append(pos)

    # Sort by expiry then strike
    result.sort(key=lambda p: (p["expiry"], p["strike"]))
    return result


def read_calendar_rolls(file_path: Path | None = None) -> dict:
    """Read calendar rolls from 'Calendar rolls.xlsx'.

    Auto-detects sheet names and header rows (first row with data in each sheet).
    Returns a dict: { sheet_name: [row_dict, ...], ... }
    """
    fp = file_path or (
        Path(__file__).resolve().parents[3]
        / "data"
        / "Calendar rolls.xlsx"
    )

    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    result: dict[str, list[dict]] = {}

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)

        # Find the first non-empty row as the header
        headers: list[str] | None = None
        for row in rows_iter:
            if row and any(cell is not None for cell in row):
                headers = [
                    str(h).strip() if h is not None else f"col_{i}"
                    for i, h in enumerate(row)
                ]
                break

        if headers is None:
            result[sheet_name] = []
            continue

        records: list[dict] = []
        for row in rows_iter:
            if all(cell is None for cell in row):
                continue
            record = dict(zip(headers, row))
            # Normalise dates → ISO strings
            for k, v in record.items():
                if isinstance(v, datetime):
                    record[k] = v.isoformat()
                elif isinstance(v, date):
                    record[k] = v.isoformat()
            records.append(record)

        result[sheet_name] = records

    wb.close()
    return result