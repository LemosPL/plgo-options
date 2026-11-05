"""One tick of the execution bot (Cloud Scheduler: POST /api/execution-bot/tick, every minute).

For every working parent order (execution/store.py):
    1) past its deadline: expired, the unfilled remainder reported, nothing crossed
    2) the kill switch on: nothing sent, the tick recorded as halted
    3) otherwise: every order book of its expiry (smile_recorder.expiry_books, one
       snapshot per expiry shared by its orders), the spline's fair value without the
       strike (smile_fit.spline_fair), the threshold decision (threshold.decide), and
       for a take, one IOC limit order through the connector, its fills recorded

Paper mode only for now: the PaperConnector fills against the same depth-10 book
the decision saw. Live orders are refused at creation until the Deribit connector
(Lucas) passes on testnet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from typing import Any

from plgo_options.agents.store import kill_switch_on
from plgo_options.market_data.smile_recorder import expiry_books
from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd
from plgo_options.pricing.smile_fit import otm_quotes, spline_fair

from . import store
from .models import BookDepth, BookTop, OrderIntent, OrderStatus
from .paper import PaperConnector, deribit_option_fee
from .threshold import QTY_STEP, ThresholdOrder, decide

LOCK, LOCK_TTL = "exec_tick", 120.0
BooksFn = Callable[[list[str]], Awaitable[tuple[dict[str, list[dict]], float, float]]]


async def _books(expiries: list[str]) -> tuple[dict[str, list[dict]], float, float]:
    return await expiry_books(expiries, depth=10)


def _depth(b: dict) -> BookDepth:
    return BookDepth(b["instrument_name"], [tuple(x) for x in b.get("bids") or []],
                     [tuple(x) for x in b.get("asks") or []], b.get("mark_price"), time.time())


def _price(o: ThresholdOrder, books: list[dict], now: float) -> tuple[dict, dict] | str:
    """(fair, book) for the order's option, or why there is none. CPU only."""
    book = next((b for b in books if b["instrument_name"] == o.instrument), None)
    if book is None:
        return f"{o.instrument} not listed"
    q = otm_quotes(o.expiry, books, dt.datetime.fromtimestamp(now, dt.timezone.utc))
    if q is None:
        return f"{o.expiry}: too few two-sided strikes to fit"
    return spline_fair(q, o.strike, o.cp), book


async def _work(o: ThresholdOrder, books: list[dict], now: float) -> dict[str, Any]:
    priced = await asyncio.to_thread(_price, o, books, now)
    if isinstance(priced, str):
        await store.add_tick(o.id, now, "no_book", detail=priced)
        return {"id": o.id, "action": "no_book", "detail": priced}
    fair, book = priced
    S = book["estimated_delivery_price"]
    depth = _depth(book)
    d = decide(o, now, fair, S, depth)
    ctx = dict(theta_vp=d.theta_vp, theta_usd=d.theta_usd, best_cost=d.best_cost, fair_usd=fair["fair"],
               iv=fair["iv"], vega=fair["vega"], S=S)
    if d.action != "take":
        await store.add_tick(o.id, now, d.action, detail=d.detail, **ctx)
        return {"id": o.id, "action": d.action, "detail": d.detail}

    async def top(_: str) -> BookTop:
        return BookTop(o.instrument, book.get("best_bid_price"), book.get("best_ask_price"),
                       book.get("best_bid_amount"), book.get("best_ask_amount"), book.get("mark_price"), now)

    async def levels(_: str) -> BookDepth:
        return depth

    conn = PaperConnector(top, fee_fn=deribit_option_fee, clock=lambda: now, depth=levels)
    intent = OrderIntent(intent_id=f"thr{o.id}-{int(now)}", proposal_id=f"exec-{o.id}", instrument=o.instrument,
                         side=o.side, qty=d.qty, limit_price=d.limit_price, time_in_force="IOC")
    st = await conn.submit(intent)
    if st.status is OrderStatus.REJECTED:
        await store.add_tick(o.id, now, "rejected", detail=st.reject_reason or "", **ctx)
        return {"id": o.id, "action": "rejected", "detail": st.reject_reason}
    fills = []
    for f in st.fills:
        usd = f.price * S
        # a fresh paper connector each tick restarts its ids: key paper fills by the intent too
        fills.append(dict(ts=now, intent_id=f.intent_id, trade_id=f"{f.intent_id}/{f.trade_id}",
                          price_eth=f.price, price_usd=usd,
                          qty=f.qty, fee_usd=f.fee * S, mode=o.mode, fair_usd=fair["fair"],
                          cost_usd=(usd - fair["fair"] if o.side == "buy" else fair["fair"] - usd)
                          + deribit_option_fee_usd(S, usd)))
    await store.add_fills(o.id, fills)
    await store.add_tick(o.id, now, "take", qty=st.filled_qty, detail=d.detail, **ctx)
    if o.remaining - st.filled_qty < QTY_STEP:
        await store.set_status(o.id, "filled")
    return {"id": o.id, "action": "take", "qty": st.filled_qty, "cost": d.est_cost}


async def run_tick(now: float | None = None, books_fn: BooksFn = _books,
                   halted: Callable[[], Awaitable[bool]] = kill_switch_on) -> dict[str, Any]:
    now = now or time.time()
    if not await store.working_orders():  # idle: write nothing (on Cloud Run the DB sits on GCS FUSE)
        return {"ts": now, "orders": []}
    if not await store.acquire_lock(LOCK, LOCK_TTL, now):
        return {"ts": now, "skipped": "another tick is running"}
    out: dict[str, Any] = {"ts": now, "orders": []}
    try:
        orders = await store.working_orders()
        live = []
        for o in orders:
            if now >= o.deadline_ts:
                note = f"deadline passed with {o.remaining:g} of {o.qty:g} unfilled"
                await store.add_tick(o.id, now, "expired", detail=note)
                await store.set_status(o.id, "expired", note)
                out["orders"].append({"id": o.id, "action": "expired", "detail": note})
            else:
                live.append(o)
        if live and await halted():
            for o in live:
                await store.add_tick(o.id, now, "halted", detail="kill switch on")
            out["halted"] = True
            out["orders"] += [{"id": o.id, "action": "halted"} for o in live]
            live = []
        if live:
            books, t0, t1 = await books_fn(sorted({o.expiry for o in live}))
            out["snapshot_secs"] = round(t1 - t0, 2)
            for o in live:
                try:
                    out["orders"].append(await _work(o, books.get(o.expiry, []), now))
                except Exception as e:  # one bad order must not stop the others
                    await store.add_tick(o.id, now, "error", detail=f"{type(e).__name__}: {e}")
                    out["orders"].append({"id": o.id, "action": "error", "detail": str(e)})
        return out
    finally:
        await store.release_lock(LOCK, {"ts": now, "n": len(out["orders"]), "halted": out.get("halted", False)})
