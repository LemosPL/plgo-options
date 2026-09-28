"""B1: Monday's reference price and the six rows around it.

"Look at the price, find the row, do what it says." A row fires once per week;
between two rows nothing happens. Monday (UK) resets the week.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")


@dataclass
class Row:
    key: str            # "+10", "-20", ...
    step: int           # 1, 2, 3 (distance in steps from reference)
    direction: int      # +1 up, -1 down
    pct: float
    price: float
    action: str
    instrument: str     # "perp" | "options" | "none"
    who: str            # "lucas" | "lucas_tell_chris" | "chris"


ETH_ACTIONS = {
    1: ("PERP OR FORWARD - sell to bring exposure back to target. No option trade.",
        "Buy back part of the forward or perp hedge. No option trade.", "perp", "lucas"),
    2: ("Sell a quarter of the winning calls; spend it on a put spread; move the floor up.",
        "Take profit on the puts that gained; spend it on longer-dated calls.",
        "options", "lucas_tell_chris"),
    3: ("Stop. Call Chris before trading anything.",
        "Stop. Call Chris before trading anything.", "none", "chris"),
}
FIL_ACTIONS = {
    1: ("PERP OR FORWARD - sell to bring exposure back to target.",
        "Buy back part of the hedge.", "perp", "lucas"),
    2: ("Sell forward or perp to reduce exposure. Buy protection only if it prices fairly vs model.",
        "Take profit on protection that gained. Buy longer-dated upside only if it prices fairly.",
        "perp", "lucas_tell_chris"),
    3: ("Stop. Call Chris.",
        "Stop. Call Chris. Do not unwind option structures into a thin market.", "none", "chris"),
}


def build_rows(asset: str, reference: float, steps_pct: list[float]) -> list[Row]:
    table = FIL_ACTIONS if asset.upper() == "FIL" else ETH_ACTIONS
    rows: list[Row] = []
    for i, pct in enumerate(steps_pct[:3], start=1):
        up_txt, dn_txt, instr, who = table[i]
        rows.append(Row(f"+{pct:g}", i, +1, pct, reference * (1 + pct / 100), up_txt, instr, who))
        rows.append(Row(f"-{pct:g}", i, -1, pct, reference * (1 - pct / 100), dn_txt, instr, who))
    return sorted(rows, key=lambda r: r.price)


def crossed_rows(rows: list[Row], reference: float, spot: float) -> list[Row]:
    """Rows spot has reached, furthest first (a gap through two rows fires the outer)."""
    hit = [r for r in rows
           if (r.direction > 0 and spot >= r.price) or (r.direction < 0 and spot <= r.price)]
    return sorted(hit, key=lambda r: -r.step)


def next_rows(rows: list[Row], spot: float) -> tuple[Row | None, Row | None]:
    below = [r for r in rows if r.price < spot]
    above = [r for r in rows if r.price > spot]
    return (max(below, key=lambda r: r.price) if below else None,
            min(above, key=lambda r: r.price) if above else None)


def week_key(now: datetime | None = None) -> str:
    """ISO date of this week's Monday, UK time — the row-fire reset boundary."""
    d = (now or datetime.now(UK)).astimezone(UK).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UK)).astimezone(UK).strftime("%Y-%m")


def today_uk(now: datetime | None = None) -> date:
    return (now or datetime.now(UK)).astimezone(UK).date()
