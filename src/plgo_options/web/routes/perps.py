"""Perp hedge book endpoints — the ledger, its mark, and funding carry.

The optimizer proposes perp trades — one per counterparty, booked with that
counterparty — whenever the book's net option delta leaves its band; this is where the resulting fill gets recorded so
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
from plgo_options.market_data import perp_feed

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


async def _market(asset: str) -> dict:
    """Live mark and trailing funding for ``asset`` — one fetch, shared by every
    venue's summary. The hedge's funding is priced off the exchange perp's rate
    whatever the venue: OTC counterparties quote perps off that same rate, and
    there is no per-counterparty feed to read instead."""
    out = {"mark": None, "mark_venue": None, "mark_price": 0.0,
           "funding_apr_pct": None, "interval_hours": None, "market_error": None}
    try:
        vm = await perp_feed.get_mark_with_venue(asset)
        out["mark"], out["mark_venue"] = vm.mark, vm.venue
        out["mark_price"] = vm.mark.mark_price
        history = await perp_feed.get_funding_history(asset, limit=90)
        if history:
            interval_ms = perp_feed.infer_interval_ms(history)
            out["interval_hours"] = interval_ms / 3_600_000
            # Trailing average over the fetched window rather than the last
            # print: a single funding period is noisy enough that the instant
            # rate annualizes to numbers that swing by tens of percent.
            avg_rate = sum(e.rate for e in history) / len(history)
            out["funding_apr_pct"] = perp_feed.annualized_rate(avg_rate, interval_ms) * 100.0
    except Exception as exc:  # live feed down — the ledger is still readable
        out["market_error"] = f"{type(exc).__name__}: {exc}"
    return out


async def _venue_summary(db, asset: str, venue: str, market: dict) -> dict:
    position = await repo.get_position(db, asset, venue)
    funding = await repo.funding_summary(db, asset, venue)
    mark = market["mark"]
    mark_price = market["mark_price"]
    funding_apr_pct = market["funding_apr_pct"]
    unrealized = position.unrealized_pnl_usd(mark_price) if mark_price else 0.0
    return {
        "asset": (asset or "").upper(),
        "venue": venue,
        "is_exchange": repo.is_exchange_venue(venue),
        "symbol": perp_feed.symbol_for(asset),
        # Which venue actually answered: Binance is 451 from Cloud Run, so a
        # brief quoting a mark should be able to say where it came from.
        "mark_venue": market["mark_venue"],
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
        "funding_interval_hours": market["interval_hours"],
        "next_funding_time_ms": mark.next_funding_time_ms if mark else None,
        "last_funding_rate": mark.last_funding_rate if mark else None,
        "trade_count": position.trade_count,
        "last_traded_at": position.last_traded_at,
        "market_error": market["market_error"],
    }


def _sum_funding(summaries: list[dict]) -> dict:
    """Add up repo.funding_summary payloads from several venues."""
    return {
        "total_usd": round(sum(f["total_usd"] for f in summaries), 2),
        "last_30d_usd": round(sum(f["last_30d_usd"] for f in summaries), 2),
        "events": sum(int(f["events"]) for f in summaries),
        "last_funding_at": max((f["last_funding_at"] or "" for f in summaries), default=""),
    }


async def build_summary(asset: str, venue: str | None = None) -> dict:
    """Net position + live mark + funding, the payload the Perp Hedge panel draws.

    With ``venue`` set, that one venue's book. Without it, the whole hedge for
    the asset summed across every venue holding fills (the exchange plus each
    OTC counterparty the rehedge booked with), with the per-venue books under
    ``by_venue``. Shared with routes/portfolio.py (net delta, daily snapshot),
    routes/optimization.py (existing hedge per counterparty) and
    routes/collateral.py (perp MtM netted per counterparty).
    """
    db = await get_db()
    market = await _market(asset)
    if venue:
        return await _venue_summary(db, asset, venue, market)

    venues = await repo.list_venues(db, asset) or [repo.DEFAULT_VENUE]
    by_venue = [await _venue_summary(db, asset, v, market) for v in venues]
    total = dict(by_venue[0])
    for key in ("net_qty", "notional_usd", "unrealized_pnl_usd", "realized_pnl_usd",
                "fees_usd", "total_pnl_usd", "trade_count"):
        total[key] = round(sum(v[key] for v in by_venue), 6 if key == "net_qty" else 2)
    total["trade_count"] = int(total["trade_count"])
    total["funding"] = _sum_funding([v["funding"] for v in by_venue])
    # Average entry of the net, i.e. the price at which the summed position's
    # unrealised P&L is what the venues' unrealised P&Ls add up to. Undefined
    # when the venues net to flat (a long at one, an equal short at another).
    net = sum(v["net_qty"] for v in by_venue)
    total["avg_entry"] = (round(sum(v["net_qty"] * v["avg_entry"] for v in by_venue) / net, 6)
                          if abs(net) > 1e-9 else 0.0)
    total["last_traded_at"] = max((v["last_traded_at"] or "" for v in by_venue), default="")
    open_venues = [v["venue"] for v in by_venue if abs(v["net_qty"]) > 1e-9]
    total["venue"] = ", ".join(open_venues) if open_venues else by_venue[0]["venue"]
    total["is_exchange"] = all(v["is_exchange"] for v in by_venue)
    total["by_venue"] = by_venue
    return total


@router.get("/position")
async def get_position(asset: str = "ETH", venue: str | None = None) -> dict:
    """Net perp position, live mark, and funding carry for one asset — summed
    across venues unless ``venue`` names one."""
    return await build_summary(asset, venue)


@router.get("/trades")
async def get_trades(asset: str = "ETH", venue: str | None = None,
                     include_closed: bool = False) -> dict:
    db = await get_db()
    trades = await repo.list_trades(db, asset=asset, venue=venue,
                                    include_closed=include_closed)
    # Net/avg entry only mean something within one venue's ledger; the
    # all-venues view gets its net from /position, which sums per venue.
    position = repo.summarize(trades, asset=(asset or "").upper(), venue=venue or "")
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
    return {"id": trade_id, **await build_summary(body.asset)}


@router.delete("/trades/{trade_id}")
async def remove_trade(trade_id: int) -> dict:
    db = await get_db()
    # The fill itself says which (asset, venue) book to re-accrue.
    row = await repo.get_trade(db, trade_id)
    if row is None or not await repo.delete_trade(db, trade_id):
        raise HTTPException(status_code=404, detail=f"No perp trade with id {trade_id}")
    await _reaccrue(db, row["asset"], row["venue"])
    return await build_summary(row["asset"])


@router.get("/funding")
async def get_funding(asset: str = "ETH", venue: str | None = None,
                      limit: int = 60) -> dict:
    db = await get_db()
    if not venue:
        venues = await repo.list_venues(db, asset) or [repo.DEFAULT_VENUE]
        rows = []
        for v in venues:
            rows += [{**r, "venue": v} for r in await repo.funding_rows(db, asset, v, limit=limit)]
        rows.sort(key=lambda r: str(r["funding_time"]), reverse=True)
        summary = _sum_funding([await repo.funding_summary(db, asset, v) for v in venues])
        return {"asset": (asset or "").upper(), "venue": None,
                "summary": summary, "rows": rows[:limit]}
    return {
        "asset": (asset or "").upper(),
        "venue": venue,
        "summary": await repo.funding_summary(db, asset, venue),
        "rows": await repo.funding_rows(db, asset, venue, limit=limit),
    }


class AccrueIn(BaseModel):
    assets: list[str] = Field(default_factory=lambda: ["ETH", "FIL"])
    # None = every venue holding fills for each asset (exchange + OTC).
    venue: str | None = None


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
    results = []
    for asset in body.assets:
        venues = [body.venue] if body.venue else (await repo.list_venues(db, asset) or [repo.DEFAULT_VENUE])
        results += [await repo.accrue_funding(db, asset, v) for v in venues]
    return {
        "results": results,
        "events": sum(r["events"] for r in results),
        "payment_usd": round(sum(r["payment_usd"] for r in results), 2),
    }
