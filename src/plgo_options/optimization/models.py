from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl

from plgo_options.optimization.optimizer_utils import _safe_int, _safe_float


@dataclass
class Position:
    id: int
    instrument: str
    opt: str
    counterparty: str | None
    side: str
    strike: float
    expiry: str
    days_remaining: int
    net_qty: float
    iv_pct: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    mark_price_usd: float
    current_mtm: float
    payoff_by_horizon: dict[str, list[float]]
    mtm_by_horizon: list[float]

    @property
    def expiry_date(self) -> date:
        return date.fromisoformat(self.expiry.split("T")[0])


@dataclass
class Candidate:
    """A tradeable instrument from the vol surface."""
    expiry_code: str
    expiry_date: str
    dte: int
    strike: float
    opt: str  # "C" or "P"
    counterparty: str
    iv_pct: float
    delta: float
    gamma: float
    theta: float
    vega: float
    bs_price_usd: float


POSITIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "positions"
def load_positions_from_latest_xlsx(positions_dir: Path = POSITIONS_DIR) -> list[Position]:
    """Load positions from the most recent .xlsx file in data/positions."""
    if not positions_dir.exists() or not positions_dir.is_dir():
        return []

    xlsx_files = sorted(
        positions_dir.glob("*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not xlsx_files:
        return []

    latest_file = xlsx_files[0]
    wb = openpyxl.load_workbook(latest_file, read_only=True, data_only=True)

    # Use the first sheet unless you want to pin this to a specific name.
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)

    headers: list[str] | None = None
    positions: list[Position] = []

    for row in rows:
        if row and any(cell is not None for cell in row):
            headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(row)]
            break

    if headers is None:
        wb.close()
        return []

    for row in rows:
        if not row or all(cell is None for cell in row):
            continue

        record = dict(zip(headers, row))

        instrument = str(record.get("Instrument") or "").strip()
        if not instrument:
            continue

        opt = str(record.get("Type") or "").strip().upper()
        if opt in {"CALL", "C"}:
            opt = "C"
        elif opt in {"PUT", "P"}:
            opt = "P"

        side = str(record.get("Side") or "").strip()
        counterparty = str(record.get("Counterparty") or "brokerage").strip() or "brokerage"
        expiry = str(record.get("Expiry") or "").strip()

        # Build a stable synthetic ID if needed
        raw_id = record.get("ID")
        pos_id = _safe_int(raw_id, default=len(positions) + 1)

        positions.append(
            Position(
                id=pos_id,
                instrument=instrument,
                opt=opt,
                counterparty=counterparty,
                side=side,
                strike=_safe_float(record.get("Strike")),
                expiry=expiry,
                days_remaining=_safe_int(record.get("DTE")),
                net_qty=_safe_float(record.get("Qty")),
                iv_pct=_safe_float(record.get("IV%")),
                delta=record.get("Delta"),
                gamma=record.get("Gamma"),
                theta=record.get("Theta"),
                vega=record.get("Vega"),
                mark_price_usd=_safe_float(record.get("Mark Price")),
                current_mtm=_safe_float(record.get("MTM")),
                payoff_by_horizon={},
                mtm_by_horizon=[],
            )
        )

    wb.close()
    return positions