"""Perp mark and funding, with a venue fallback chain.

Binance answers ``451 Unavailable For Legal Reasons`` to requests from Cloud
Run, so in production every perp call failed and the desk's morning brief
reported ``Perp 0.0 ($0), funding 30d $0`` — a feed outage that reads exactly
like a flat position. The spot path already routes around Binance for the same
reason; this does it for the perp leg.

Order is Binance, then Bybit, then OKX: Binance stays first because it is the
venue the hedge actually sits on (``PERP_COUNTERPARTY`` in the optimizer), so
its mark is the right one whenever it is reachable — from a laptop it is. The
others are reference venues, used only to keep the numbers alive when it is
not, and every reading carries the venue it came from so a brief can say so.

Funding history is the awkward one. A settled payment is
``-qty x mark x rate``, so each event needs the mark *at settlement*, and
neither Bybit nor OKX returns it alongside the rate. Bybit does publish
``mark-price-kline`` on the same timestamp grid as its funding history, so the
Bybit path pairs the two. OKX has no equivalent that lines up, so it serves
marks only and is not used to accrue carry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from plgo_options.market_data.binance_client import (  # noqa: F401  (re-exported)
    DAY_MS,
    DEFAULT_FUNDING_INTERVAL_MS,
    FundingEvent,
    PerpMark,
    annualized_rate,
    infer_interval_ms,
    symbol_for,
)
from plgo_options.market_data import binance_client

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0

BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"

# Bybit uses the same USDT-M symbols as Binance; OKX uses instrument ids.
def _okx_inst(asset: str) -> str:
    return f"{(asset or '').strip().upper()}-USDT-SWAP"


@dataclass
class VenueMark:
    """A PerpMark plus which venue answered, so callers can label it."""
    mark: PerpMark
    venue: str


async def _json(url: str, params: dict | None = None) -> object:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.get(url, params=params or {}, headers={"User-Agent": "plgo-options"})
        r.raise_for_status()
        return r.json()


# ── marks ────────────────────────────────────────────────────────────────────

async def _binance_mark(asset: str) -> PerpMark:
    return await binance_client.get_mark(asset)


async def _bybit_mark(asset: str) -> PerpMark:
    sym = symbol_for(asset)
    d = await _json(f"{BYBIT_BASE}/v5/market/tickers",
                    {"category": "linear", "symbol": sym})
    row = (((d or {}).get("result") or {}).get("list") or [{}])[0]
    if not row.get("markPrice"):
        raise ValueError(f"Bybit returned no mark for {sym}")
    return PerpMark(
        symbol=sym,
        mark_price=float(row["markPrice"]),
        index_price=float(row.get("indexPrice") or 0.0),
        last_funding_rate=float(row.get("fundingRate") or 0.0),
        next_funding_time_ms=int(row.get("nextFundingTime") or 0),
    )


async def _okx_mark(asset: str) -> PerpMark:
    inst = _okx_inst(asset)
    m = ((await _json(f"{OKX_BASE}/api/v5/public/mark-price",
                      {"instId": inst})) or {}).get("data") or [{}]
    f = ((await _json(f"{OKX_BASE}/api/v5/public/funding-rate",
                      {"instId": inst})) or {}).get("data") or [{}]
    if not m[0].get("markPx"):
        raise ValueError(f"OKX returned no mark for {inst}")
    return PerpMark(
        symbol=symbol_for(asset),
        mark_price=float(m[0]["markPx"]),
        index_price=float(m[0].get("idxPx") or 0.0),
        last_funding_rate=float(f[0].get("fundingRate") or 0.0),
        next_funding_time_ms=int(f[0].get("nextFundingTime") or 0),
    )


MARK_VENUES: list[tuple[str, object]] = [
    ("binance", _binance_mark),
    ("bybit", _bybit_mark),
    ("okx", _okx_mark),
]


async def get_mark_with_venue(asset: str) -> VenueMark:
    """First venue that answers. Raises only if every one of them failed."""
    errors = []
    for name, fn in MARK_VENUES:
        try:
            return VenueMark(mark=await fn(asset), venue=name)
        except Exception as e:                       # noqa: BLE001 - try the next venue
            detail = f"{type(e).__name__}"
            if isinstance(e, httpx.HTTPStatusError):
                detail += f" {e.response.status_code}"
            errors.append(f"{name}: {detail}")
            log.warning("perp mark for %s failed on %s (%s)", asset, name, detail)
    raise RuntimeError(f"No perp venue could price {asset} - " + "; ".join(errors))


async def get_mark(asset: str) -> PerpMark:
    return (await get_mark_with_venue(asset)).mark


# ── funding history ──────────────────────────────────────────────────────────

async def _bybit_funding_history(asset: str, start_ms: int | None,
                                 end_ms: int | None, limit: int) -> list[FundingEvent]:
    """Bybit rates paired with its mark-price klines on the same grid.

    A payment is -qty x mark x rate, so the mark has to be the one at
    settlement; today's mark would misstate every historical payment. Bybit's
    hourly mark klines are keyed by the same epoch ms as its funding stamps,
    so the join is exact rather than a nearest-time guess.
    """
    sym = symbol_for(asset)
    params: dict[str, object] = {"category": "linear", "symbol": sym,
                                 "limit": min(int(limit), 200)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    d = await _json(f"{BYBIT_BASE}/v5/market/funding/history", params)
    rows = ((d or {}).get("result") or {}).get("list") or []
    if not rows:
        return []

    stamps = sorted(int(r["fundingRateTimestamp"]) for r in rows)
    kp: dict[str, object] = {"category": "linear", "symbol": sym, "interval": "60",
                             "start": stamps[0], "end": stamps[-1] + 3_600_000,
                             "limit": 1000}
    marks: dict[int, float] = {}
    try:
        k = await _json(f"{BYBIT_BASE}/v5/market/mark-price-kline", kp)
        for c in (((k or {}).get("result") or {}).get("list") or []):
            marks[int(c[0])] = float(c[4])           # [start, open, high, low, close]
    except Exception as e:                           # noqa: BLE001
        log.warning("Bybit mark klines for %s unavailable (%s); "
                    "funding events will carry mark 0", sym, type(e).__name__)

    events = [
        FundingEvent(
            funding_time_ms=int(r["fundingRateTimestamp"]),
            rate=float(r["fundingRate"]),
            mark_price=marks.get(int(r["fundingRateTimestamp"]), 0.0),
        )
        for r in rows
    ]
    events.sort(key=lambda e: e.funding_time_ms)
    return events


async def get_funding_history(asset: str, start_ms: int | None = None,
                              end_ms: int | None = None,
                              limit: int = 1000) -> list[FundingEvent]:
    """Settled funding events, oldest first, from the first venue that answers.

    OKX is deliberately absent: it publishes rates but nothing that gives the
    mark at each settlement, and booking carry against a wrong mark is worse
    than booking none.
    """
    try:
        return await binance_client.get_funding_history(
            asset, start_ms=start_ms, end_ms=end_ms, limit=limit)
    except Exception as e:                           # noqa: BLE001
        detail = f"{type(e).__name__}"
        if isinstance(e, httpx.HTTPStatusError):
            detail += f" {e.response.status_code}"
        log.warning("Binance funding history for %s failed (%s); trying Bybit", asset, detail)
    return await _bybit_funding_history(asset, start_ms, end_ms, limit)
