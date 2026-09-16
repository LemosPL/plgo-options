"""Binance USDT-M futures market data — mark price and settled funding rates.

Public endpoints only, no API key: the desk's perp hedge sits on Binance
Futures (``PERP_COUNTERPARTY`` in the optimizer), and what the app needs from
it is a mark to value the leg and the settled funding history to accrue its
carry. Nothing here places or reads orders.

The synchronous equivalents of these calls live in
``scripts/funding_arb_monitor.py``; this module is the async, app-side version.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import httpx

FUTURES_BASE = "https://fapi.binance.com"
REQUEST_TIMEOUT = 10.0
DAY_MS = 86_400_000

# Binance's historical default. Only used when a symbol has too little funding
# history to infer the real spacing — several symbols have since moved to 4h.
DEFAULT_FUNDING_INTERVAL_MS = 8 * 3_600_000

# The book trades ETH and FIL; both have USDT-margined perps.
SYMBOL_BY_ASSET = {
    "ETH": "ETHUSDT",
    "FIL": "FILUSDT",
    "BTC": "BTCUSDT",
}


def symbol_for(asset: str) -> str:
    """Binance USDT-M symbol for a book asset (``FIL`` -> ``FILUSDT``)."""
    key = (asset or "").strip().upper()
    return SYMBOL_BY_ASSET.get(key, f"{key}USDT")


@dataclass
class FundingEvent:
    """One settled funding payment window on a perp."""
    funding_time_ms: int
    rate: float
    mark_price: float


@dataclass
class PerpMark:
    symbol: str
    mark_price: float
    index_price: float
    last_funding_rate: float
    next_funding_time_ms: int


async def _get(path: str, params: dict) -> object:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{FUTURES_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def get_mark(asset: str) -> PerpMark:
    """Current mark/index price and the funding rate about to settle."""
    symbol = symbol_for(asset)
    data = await _get("/fapi/v1/premiumIndex", {"symbol": symbol})
    return PerpMark(
        symbol=symbol,
        mark_price=float(data.get("markPrice") or 0.0),
        index_price=float(data.get("indexPrice") or 0.0),
        last_funding_rate=float(data.get("lastFundingRate") or 0.0),
        next_funding_time_ms=int(data.get("nextFundingTime") or 0),
    )


async def get_funding_history(
    asset: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 1000,
) -> list[FundingEvent]:
    """Settled funding events, oldest first.

    ``startTime`` is inclusive on Binance's side, so callers accruing forward
    from the last stored event should pass ``last_ms + 1`` to avoid re-reading
    a window they have already booked (the PK on ``perp_funding`` would ignore
    the duplicate anyway, but there is no reason to fetch it).

    Each event carries the mark price that funding settled against, so no
    second call is needed to value the payment.
    """
    params: dict[str, object] = {"symbol": symbol_for(asset), "limit": min(int(limit), 1000)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    raw = await _get("/fapi/v1/fundingRate", params)
    events = [
        FundingEvent(
            funding_time_ms=int(row["fundingTime"]),
            rate=float(row["fundingRate"]),
            mark_price=float(row.get("markPrice") or 0.0),
        )
        for row in (raw or [])
    ]
    events.sort(key=lambda e: e.funding_time_ms)
    return events


def infer_interval_ms(events: list[FundingEvent]) -> int:
    """Funding period inferred from the spacing of recent events.

    Read off the data rather than hardcoded: Binance has moved some symbols off
    the 8h default to 4h, and a stale constant would misstate every annualized
    figure derived from a rate by a factor of two.
    """
    times = sorted(e.funding_time_ms for e in events)
    diffs = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not diffs:
        return DEFAULT_FUNDING_INTERVAL_MS
    return int(statistics.median(diffs))


def annualized_rate(rate_per_period: float, interval_ms: int) -> float:
    """Per-period funding rate -> annualized fraction (0.12 = 12%/yr)."""
    if interval_ms <= 0:
        return 0.0
    return rate_per_period * (DAY_MS / interval_ms) * 365.0
