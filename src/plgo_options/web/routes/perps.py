"""Perp hedge book endpoints — the ledger, its mark, and funding carry.

The optimizer proposes a Binance Futures perp trade whenever the book's net
option delta leaves its band; this is where the resulting fill gets recorded so
that (a) the next optimizer run knows the hedge is already on instead of
re-proposing it from scratch, (b) Portfolio greeks show the hedged book, and
(c) the funding paid to carry it lands in P&L.

See ``data/perp_repository.py`` for the position model and the funding sign
convention.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from plgo_options.config import SIGNALS_TOKEN
from plgo_options.data import perp_repository as repo
from plgo_options.data.database import get_db
from plgo_options.market_data import binance_client

router = APIRouter()


def _require_token(token: str | None) -> None:
    """Same shared secret the Action Radar scheduler endpoints use.

    Accruing funding writes to the DB and is driven by Cloud Scheduler on a
    public Cloud Run URL, so it carries the same guard. Read-only views don't.
    """
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")


class PerpTradeIn(BaseModel):
    asset: str = Field(..., description="Book asset — ETH or FIL")
    side: str = Field(..., description="Buy / Sell (Long / Short accepted)")
    qty: float = Field(..., gt=0, description="Token quantity, always positive")
    price: float = Field(..., ge=0, description="Fill price in USD")
    traded_at: str | None = Field(None, description="ISO timestamp; defaults to now (UTC)")
    venue: str = repo.DEFAULT_VENUE
    fee_usd: float = 0.0
    note: str = ""


async def build_summary(asset: str, venue: str = repo.DEFAULT_VENUE) -> dict:
    """Net position + live mark + funding, the payload the Perp Hedge panel draws.

    Shared with routes/portfolio.py, which folds the same numbers into the
    book's net delta and the daily MTM snapshot.
    """
    db = await get_db()
    position = await repo.get_position(db, asset, venue)
    funding = await repo.funding_summary(db, asset, venue)

    mark = None
    mark_price = 0.0
    funding_apr_pct = None
    interval_hours = None
    try:
        mark = await binance_client.get_mark(asset)
        mark_price = mark.mark_price
        history = await binance_client.get_funding_history(asset, limit=90)
        if history:
            interval_ms = binance_client.infer_interval_ms(history)
            interval_hours = interval_ms / 3_600_000
            # Trailing average over the fetched window rather than the last
            # print: a single funding period is noisy enough that the instant
            # rate annualizes to numbers that swing by tens of percent.
            avg_rate = sum(e.rate for e in history) / len(history)
            funding_apr_pct = binance_client.annualized_rate(avg_rate, interval_ms) * 100.0
    except Exception as exc:  # live feed down — the ledger is still readable
        market_error = f"{type(exc).__name__}: {exc}"
    else:
        market_error = None

    unrealized = position.unrealized_pnl_usd(mark_price) if mark_price else 0.0
    return {
        "asset": (asset or "").upper(),
        "venue": venue,
        "symbol": binance_client.symbol_for(asset),
        "net_qty": round(position.net_qty, 6),
        "avg_entry": round(position.avg_entry, 6),
        "mark_price": round(mark_price, 6),
        "notional_usd": round(position.notional_usd(mark_price), 2),
        "unrealized_pnl_usd": round(unrealized, 2),
        "realized_pnl_usd": round(position.realized_pnl_usd, 2),
        "fees_usd": round(position.fees_usd, 2),
        # Everything the hedge has cost or earned since inception: mark-to-market
        # against average entry, plus closed P&L, plus settled funding, minus fees.
        "total_pnl_usd": round(
            unrealized + position.realized_pnl_usd + funding["total_usd"] - position.fees_usd, 2
        ),
        "funding": funding,
        "funding_apr_pct": round(funding_apr_pct, 2) if funding_apr_pct is not None else None,
        "funding_interval_hours": interval_hours,
        "next_funding_time_ms": mark.next_funding_time_ms if mark else None,
        "last_funding_rate": mark.last_funding_rate if mark else None,
        "trade_count": position.trade_count,
        "last_traded_at": position.last_traded_at,
        "market_error": market_error,
    }


@router.get("/position")
async def get_position(asset: str = "ETH", venue: str = repo.DEFAULT_VENUE) -> dict:
    """Net perp position, live mark, and funding carry for one asset."""
    return await build_summary(asset, venue)


@router.get("/trades")
async def get_trades(asset: str = "ETH", venue: str = repo.DEFAULT_VENUE,
                     include_closed: bool = False) -> dict:
    db = await get_db()
    trades = await repo.list_trades(db, asset=asset, venue=venue,
                                    include_closed=include_closed)
    position = repo.summarize(trades, asset=(asset or "").upper(), venue=venue)
    return {
        "asset": (asset or "").upper(),
        "venue": venue,
        "trades": trades,
        "net_qty": round(position.net_qty, 6),
        "avg_entry": round(position.avg_entry, 6),
    }


async def _reaccrue(db, asset: str, venue: str) -> None:
    """Best-effort re-accrual after a ledger change.

    add_trade/delete_trade clear the funding rows the change invalidated, so
    without this the panel would show a gap until someone pressed "Accrue
    funding". A Binance outage must not stop a fill being recorded, though —
    the rows are simply rebuilt on the next accrual.
    """
    try:
        await repo.accrue_funding(db, asset, venue)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Post-trade funding re-accrual failed: %s", exc)


@router.post("/trades")
async def create_trade(body: PerpTradeIn) -> dict:
    db = await get_db()
    trade_id = await repo.add_trade(
        db, asset=body.asset, side=body.side, qty=body.qty, price=body.price,
        traded_at=body.traded_at, venue=body.venue, fee_usd=body.fee_usd,
        note=body.note,
    )
    await _reaccrue(db, body.asset, body.venue)
    return {"id": trade_id, **await build_summary(body.asset, body.venue)}


@router.delete("/trades/{trade_id}")
async def remove_trade(trade_id: int, asset: str = "ETH",
                       venue: str = repo.DEFAULT_VENUE) -> dict:
    db = await get_db()
    if not await repo.delete_trade(db, trade_id):
        raise HTTPException(status_code=404, detail=f"No perp trade with id {trade_id}")
    await _reaccrue(db, asset, venue)
    return await build_summary(asset, venue)


@router.get("/funding")
async def get_funding(asset: str = "ETH", venue: str = repo.DEFAULT_VENUE,
                      limit: int = 60) -> dict:
    db = await get_db()
    return {
        "asset": (asset or "").upper(),
        "venue": venue,
        "summary": await repo.funding_summary(db, asset, venue),
        "rows": await repo.funding_rows(db, asset, venue, limit=limit),
    }


class AccrueIn(BaseModel):
    assets: list[str] = Field(default_factory=lambda: ["ETH", "FIL"])
    venue: str = repo.DEFAULT_VENUE


@router.post("/accrue-funding")
async def accrue(body: AccrueIn | None = None,
                 x_signals_token: str | None = Header(default=None)) -> dict:
    """Book every funding event settled since the last run. Idempotent.

    Scheduler-friendly: run it more often than the funding interval and the
    overlapping window is absorbed by the ``perp_funding`` primary key.
    """
    _require_token(x_signals_token)
    body = body or AccrueIn()
    db = await get_db()
    results = [await repo.accrue_funding(db, asset, body.venue) for asset in body.assets]
    return {
        "results": results,
        "events": sum(r["events"] for r in results),
        "payment_usd": round(sum(r["payment_usd"] for r in results), 2),
    }
