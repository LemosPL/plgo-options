from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
