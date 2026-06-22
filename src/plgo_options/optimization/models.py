from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

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
    existing_qty: float = 0.0
    unwind_only: bool = False

@dataclass(frozen=True)
class SpreadCandidate:
    kind: str  # "CALL_SPREAD" or "PUT_SPREAD"
    long_leg: Candidate
    short_leg: Candidate

    @property
    def expiry_code(self) -> str:
        return self.long_leg.expiry_code

    @property
    def expiry_date(self):
        return self.long_leg.expiry_date

    @property
    def dte(self) -> int:
        return self.long_leg.dte

    @property
    def counterparty(self) -> str:
        return self.long_leg.counterparty

    @property
    def opt(self) -> str:
        return self.long_leg.opt

    @property
    def strike(self) -> float:
        return self.long_leg.strike

    @property
    def width(self) -> float:
        return abs(float(self.short_leg.strike) - float(self.long_leg.strike))

    @property
    def bs_price_usd(self) -> float:
        return float(self.long_leg.bs_price_usd or 0.0) - float(self.short_leg.bs_price_usd or 0.0)

    @property
    def vega(self) -> float:
        return float(self.long_leg.vega or 0.0) - float(self.short_leg.vega or 0.0)

    @property
    def delta(self) -> float:
        return float(self.long_leg.delta or 0.0) - float(self.short_leg.delta or 0.0)

    @property
    def gamma(self) -> float:
        return float(self.long_leg.gamma or 0.0) - float(self.short_leg.gamma or 0.0)

    @property
    def iv_pct(self) -> float:
        return 0.5 * (
            float(self.long_leg.iv_pct or 0.0)
            + float(self.short_leg.iv_pct or 0.0)
        )


@dataclass(frozen=True)
class StraddleCandidate:
    kind: str  # "STRADDLE"
    call_leg: Candidate
    put_leg: Candidate

    @property
    def expiry_code(self) -> str:
        return self.call_leg.expiry_code

    @property
    def expiry_date(self):
        return self.call_leg.expiry_date

    @property
    def dte(self) -> int:
        return self.call_leg.dte

    @property
    def counterparty(self) -> str:
        return self.call_leg.counterparty

    @property
    def opt(self) -> str:
        return "STRADDLE"

    @property
    def strike(self) -> float:
        return self.call_leg.strike

    @property
    def bs_price_usd(self) -> float:
        return float(self.call_leg.bs_price_usd or 0.0) + float(self.put_leg.bs_price_usd or 0.0)

    @property
    def vega(self) -> float:
        return float(self.call_leg.vega or 0.0) + float(self.put_leg.vega or 0.0)

    @property
    def delta(self) -> float:
        return float(self.call_leg.delta or 0.0) + float(self.put_leg.delta or 0.0)

    @property
    def gamma(self) -> float:
        return float(self.call_leg.gamma or 0.0) + float(self.put_leg.gamma or 0.0)

    @property
    def theta(self) -> float:
        return float(self.call_leg.theta or 0.0) + float(self.put_leg.theta or 0.0)

    @property
    def iv_pct(self) -> float:
        return 0.5 * (
            float(self.call_leg.iv_pct or 0.0)
            + float(self.put_leg.iv_pct or 0.0)
        )


@dataclass(frozen=True)
class IronCondorCandidate:
    kind: str  # "IRON_CONDOR"
    put_low_leg: Candidate    # long put wing
    put_high_leg: Candidate   # short put body
    call_low_leg: Candidate   # short call body
    call_high_leg: Candidate  # long call wing

    @property
    def expiry_code(self) -> str:
        return self.put_low_leg.expiry_code

    @property
    def expiry_date(self):
        return self.put_low_leg.expiry_date

    @property
    def dte(self) -> int:
        return self.put_low_leg.dte

    @property
    def counterparty(self) -> str:
        return self.put_low_leg.counterparty

    @property
    def opt(self) -> str:
        return "IRON_CONDOR"

    @property
    def strike(self) -> float:
        return 0.5 * (
            float(self.put_high_leg.strike or 0.0)
            + float(self.call_low_leg.strike or 0.0)
        )

    @property
    def bs_price_usd(self) -> float:
        return (
            float(self.put_low_leg.bs_price_usd or 0.0)
            - float(self.put_high_leg.bs_price_usd or 0.0)
            - float(self.call_low_leg.bs_price_usd or 0.0)
            + float(self.call_high_leg.bs_price_usd or 0.0)
        )

    @property
    def vega(self) -> float:
        return (
            float(self.put_low_leg.vega or 0.0)
            - float(self.put_high_leg.vega or 0.0)
            - float(self.call_low_leg.vega or 0.0)
            + float(self.call_high_leg.vega or 0.0)
        )

    @property
    def delta(self) -> float:
        return (
            float(self.put_low_leg.delta or 0.0)
            - float(self.put_high_leg.delta or 0.0)
            - float(self.call_low_leg.delta or 0.0)
            + float(self.call_high_leg.delta or 0.0)
        )

    @property
    def gamma(self) -> float:
        return (
            float(self.put_low_leg.gamma or 0.0)
            - float(self.put_high_leg.gamma or 0.0)
            - float(self.call_low_leg.gamma or 0.0)
            + float(self.call_high_leg.gamma or 0.0)
        )

    @property
    def theta(self) -> float:
        return (
            float(self.put_low_leg.theta or 0.0)
            - float(self.put_high_leg.theta or 0.0)
            - float(self.call_low_leg.theta or 0.0)
            + float(self.call_high_leg.theta or 0.0)
        )

    @property
    def iv_pct(self) -> float:
        return 0.25 * (
            float(self.put_low_leg.iv_pct or 0.0)
            + float(self.put_high_leg.iv_pct or 0.0)
            + float(self.call_low_leg.iv_pct or 0.0)
            + float(self.call_high_leg.iv_pct or 0.0)
        )

def _xlsx_column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str], namespace: dict[str, str]):
    cell_type = cell.attrib.get("t")
    value_node = cell.find("main:v", namespace)

    if cell_type == "inlineStr":
        text_node = cell.find("main:is/main:t", namespace)
        return text_node.text if text_node is not None else None

    if value_node is None or value_node.text is None:
        return None

    raw_value = value_node.text

    if cell_type == "s":
        index = int(raw_value)
        return shared_strings[index] if 0 <= index < len(shared_strings) else raw_value

    if cell_type == "b":
        return raw_value == "1"

    try:
        number = float(raw_value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw_value


def _read_first_xlsx_sheet_rows(path: Path) -> list[list[object]]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", namespace):
                parts = [
                    node.text or ""
                    for node in item.findall(".//main:t", namespace)
                ]
                shared_strings.append("".join(parts))

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        first_sheet = workbook_root.find("main:sheets/main:sheet", namespace)
        if first_sheet is None:
            return []

        relationship_id = first_sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]

        relationships_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_namespace = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
        sheet_target = None

        for relationship in relationships_root.findall("rel:Relationship", rel_namespace):
            if relationship.attrib.get("Id") == relationship_id:
                sheet_target = relationship.attrib["Target"]
                break

        if sheet_target is None:
            return []

        sheet_path = "xl/" + sheet_target.lstrip("/")
        sheet_root = ET.fromstring(archive.read(sheet_path))

        rows: list[list[object]] = []
        for row_node in sheet_root.findall(".//main:sheetData/main:row", namespace):
            row_values: list[object] = []

            for cell in row_node.findall("main:c", namespace):
                cell_ref = cell.attrib.get("r", "")
                column_index = _xlsx_column_index(cell_ref)

                while len(row_values) <= column_index:
                    row_values.append(None)

                row_values[column_index] = _xlsx_cell_value(cell, shared_strings, namespace)

            rows.append(row_values)

        return rows


def load_positions_from_latest_xlsx(token) -> list[Position]:
    # positions_dir: Path = POSITIONS_DIR
    if token == 'ETH':
        positions_dir = Path(__file__).resolve().parent.parent.parent.parent / "data" / "positions"
    else:
        token_positions_dir = token + "_positions"
        positions_dir = Path(__file__).resolve().parent.parent.parent.parent / "data" / token_positions_dir
    #POSITIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "positions"

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
    rows = iter(_read_first_xlsx_sheet_rows(latest_file))

    headers: list[str] | None = None
    positions: list[Position] = []

    for row in rows:
        if row and any(cell is not None for cell in row):
            headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(row)]
            break

    if headers is None:
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

    return positions