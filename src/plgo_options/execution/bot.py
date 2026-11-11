"""The execution bot (Cloud Scheduler: POST /api/execution-bot/tick, every minute).

Each scheduler call holds the lease and runs a pass every 5 seconds for 55 seconds
(run_minute). A pass, for every working parent order (execution/store.py):
    1) past its deadline: expired, the unfilled remainder reported, nothing crossed
    2) the kill switch on: nothing sent, the pass recorded as halted
    3) a fill on the same option in the last 2 minutes: it rests (cooldown), so the
       book can refill and our taking does not chase it
    4) otherwise: the expiry's smile, every strike's top of book with its size from a
       WebSocket quote feed (market_data/deribit_quotes.py, one per scheduler call) and
       the forward, index and marks from one book summary call; the spline's fair value
       (smile_fit: one spline per expiry through every strike, the traded one included,
       weighted by spread and the thinner side's size), the
       threshold decision (threshold.decide), and for a take, one IOC limit order
       at the top of the book through the connector, its fills recorded

Without the feed (it failed to start, or dropped), a pass reads the worked options' own
books over HTTP and the summary's prices for the rest, which have no sizes, so the
spline falls back to spread-only weights.

Takes, fills, errors and expiries are recorded on the pass they happen; the routine
outcomes (wait, cooldown, halted, ...) once a minute, on its first pass, so the DB
(on GCS FUSE on Cloud Run) is not written every 5 seconds.

Paper mode only for now: the PaperConnector fills against the same book the
decision saw. Live orders are refused at creation until the Deribit connector
(Lucas) passes on testnet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from plgo_options.agents.store import kill_switch_on
from plgo_options.market_data.deribit_quotes import QuoteFeed
from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd
from plgo_options.pricing.smile_fit import fit_error, otm_quotes, spline_fair, spline_smile

from . import store
from .models import BookDepth, BookTop, OrderIntent, OrderStatus
from .paper import PaperConnector, deribit_option_fee
from .threshold import QTY_STEP, ThresholdOrder, decide

LOCK, LOCK_TTL = "exec_tick", 120.0
PERIOD, SPAN = 5.0, 55.0   # a pass every 5 s, for 55 s of each scheduler minute
# After a fill the option rests 2 minutes. Replayed on 7-9 Nov 2026 quotes
# (scripts/execution_replay.py --grid), 120 s against 300 s finished ~1.5 h sooner and
# ~0.01 vol points cheaper; shorter rests gain little more, and live makers may requote
# worse after being hit, which no replay shows.
# RMS half-spreads (smile_fit.fit_error): above it the expiry's spline is not trusted and its
# orders wait. Replayed on 3-11 Nov 2026 (2,161 snapshots): median 0.14, 99th percentile 0.85;
# Deribit's 08:00 UTC expiry thins the books daily (1.4 to 5.0); 10 Nov's rich call wing peaked
# at 1.13 with the spline still sane, so it keeps trading.
MAX_FIT_ERROR = 1.25
COOLDOWN = 120.0
ROUTINE = {"wait", "outside", "no_book", "cooldown", "halted"}  # recorded once a minute
API = "https://www.deribit.com/api/v2/public"
# books_fn(expiries, instruments) -> {expiry: books}: every option of the expiries, and
# the instruments' own books with their levels
BooksFn = Callable[[list[str], list[str]], Awaitable[dict[str, list[dict]]]]


def _summary_book(s: dict) -> dict:
    """A get_book_summary_by_currency row in the shape of an order book (top only)."""
    S = s["estimated_delivery_price"]
    return {"instrument_name": s["instrument_name"], "bids": None, "asks": None,
            "best_bid_price": s.get("bid_price"), "best_ask_price": s.get("ask_price"),
            "best_bid_amount": None, "best_ask_amount": None, "mark_price": s["mark_price"],
            "mark_iv": s["mark_iv"], "underlying_price": s["underlying_price"], "index_price": S,
            "estimated_delivery_price": S, "open_interest": s.get("open_interest"),
            "stats": {"volume": s.get("volume")}}


def _with_quote(b: dict, q: dict) -> dict:
    """A book with its top of book (prices and sizes) from the quote feed."""
    bid, ask = q.get("best_bid_price") or None, q.get("best_ask_price") or None
    ba, aa = q.get("best_bid_amount") or 0.0, q.get("best_ask_amount") or 0.0
    return {**b, "best_bid_price": bid, "best_ask_price": ask, "best_bid_amount": ba, "best_ask_amount": aa,
            "bids": [[bid, ba]] if bid and ba else [], "asks": [[ask, aa]] if ask and aa else []}


async def _get(c: httpx.AsyncClient, method: str, **params) -> Any:
    r = await c.get(f"{API}/{method}", params=params)
    r.raise_for_status()
    return r.json()["result"]


async def _names(expiries: list[str]) -> list[str]:
    """Every listed option of the expiries."""
    async with httpx.AsyncClient(timeout=10) as c:
        summary = await _get(c, "get_book_summary_by_currency", currency="ETH", kind="option")
    return [s["instrument_name"] for s in summary if s["instrument_name"].split("-")[1] in expiries]


async def _books(expiries: list[str], instruments: list[str], feed: QuoteFeed | None = None) -> dict[str, list[dict]]:
    """Every option of the expiries from one book summary call (forward, index, marks,
    and top of book prices fresh to the second, but no sizes), each with its live top
    of book and sizes from the feed when it is alive. Instruments the feed does not
    cover are read over HTTP, top of book with its size."""
    live = feed.quotes if feed is not None and feed.alive else {}
    need = [n for n in instruments if n not in live]
    async with httpx.AsyncClient(timeout=10) as c:
        async def book(name: str) -> dict | None:
            try:
                return await _get(c, "get_order_book", instrument_name=name, depth=1)
            except httpx.HTTPStatusError:
                return None

        summary, *own = await asyncio.gather(_get(c, "get_book_summary_by_currency", currency="ETH", kind="option"),
                                             *(book(n) for n in need))
    own_by = {b["instrument_name"]: b for b in own if b}
    out: dict[str, list[dict]] = {e: [] for e in expiries}
    for s in summary:
        name = s["instrument_name"]
        e = name.split("-")[1]
        if e in out:
            b = _summary_book(s)
            out[e].append(_with_quote(b, live[name]) if name in live else own_by.get(name) or b)
    return out


def _depth(b: dict) -> BookDepth:
    return BookDepth(b["instrument_name"], [tuple(x) for x in b.get("bids") or []],
                     [tuple(x) for x in b.get("asks") or []], b.get("mark_price"), time.time())


def _price(expiry: str, books: list[dict], orders: list[ThresholdOrder], now: float) -> dict[int, tuple | str]:
    """{order id: (fair, book) or why there is none}: one spline for the expiry. CPU only."""
    by_name = {b["instrument_name"]: b for b in books}
    q = otm_quotes(expiry, books, dt.datetime.fromtimestamp(now, dt.timezone.utc))
    smile = spline_smile(q) if q is not None else None
    bad = None
    if smile is not None:
        rms, worst = fit_error(q, smile)
        if rms > MAX_FIT_ERROR:
            bad = (f"{expiry}: the spline is off its quotes ({rms:.2f} half-spreads, worst strike {worst:.1f}): "
                   f"thin books, waiting for them to refill")
    out: dict[int, tuple | str] = {}
    for o in orders:
        book = by_name.get(o.instrument)
        if book is None or book.get("bids") is None:
            out[o.id] = f"{o.instrument}: no order book"
        elif q is None:
            out[o.id] = f"{expiry}: too few two-sided strikes to fit"
        elif bad:
            out[o.id] = bad
        else:
            out[o.id] = (spline_fair(q, o.strike, o.cp, smile), book)
    return out


async def _tick(o: ThresholdOrder, now: float, action: str, record: bool, **kw) -> None:
    if record or action not in ROUTINE:
        await store.add_tick(o.id, now, action, **kw)


async def _work(o: ThresholdOrder, priced: tuple | str, now: float, record: bool) -> dict[str, Any]:
    if isinstance(priced, str):
        await _tick(o, now, "no_book", record, detail=priced)
        return {"id": o.id, "action": "no_book", "detail": priced}
    fair, book = priced
    S = book["estimated_delivery_price"]
    depth = _depth(book)
    d = decide(o, now, fair, S, depth)
    ctx = dict(theta_vp=d.theta_vp, theta_usd=d.theta_usd, best_cost=d.best_cost, fair_usd=fair["fair"],
               iv=fair["iv"], vega=fair["vega"], S=S)
    if d.action != "take":
        await _tick(o, now, d.action, record, detail=d.detail, **ctx)
        return {"id": o.id, "action": d.action, "detail": d.detail}

    async def top(_: str) -> BookTop:
        return BookTop(o.instrument, book.get("best_bid_price"), book.get("best_ask_price"),
                       book.get("best_bid_amount"), book.get("best_ask_amount"), book.get("mark_price"), now)

    async def levels(_: str) -> BookDepth:
        return depth

    conn = PaperConnector(top, fee_fn=deribit_option_fee, clock=lambda: now, depth=levels)
    intent = OrderIntent(intent_id=f"thr{o.id}-{int(now * 1000)}", proposal_id=f"exec-{o.id}",
                         instrument=o.instrument, side=o.side, qty=d.qty, limit_price=d.limit_price,
                         time_in_force="IOC")
    st = await conn.submit(intent)
    if st.status is OrderStatus.REJECTED:
        await _tick(o, now, "rejected", record, detail=st.reject_reason or "", **ctx)
        return {"id": o.id, "action": "rejected", "detail": st.reject_reason}
    fills = []
    mark = book.get("mark_price")
    bid, ask = book.get("best_bid_price"), book.get("best_ask_price")
    mid = (bid + ask) / 2 if bid and ask else None
    for f in st.fills:
        usd = f.price * S
        # a fresh paper connector each pass restarts its ids: key paper fills by the intent too
        fills.append(dict(ts=now, intent_id=f.intent_id, trade_id=f"{f.intent_id}/{f.trade_id}",
                          price_eth=f.price, price_usd=usd,
                          qty=f.qty, fee_usd=f.fee * S, mode=o.mode, fair_usd=fair["fair"],
                          mark_usd=mark * S if mark is not None else None,
                          mid_usd=mid * S if mid is not None else None,
                          cost_usd=(usd - fair["fair"] if o.side == "buy" else fair["fair"] - usd)
                          + deribit_option_fee_usd(S, usd)))
    await store.add_fills(o.id, fills)
    await _tick(o, now, "take", record, qty=st.filled_qty, detail=d.detail, **ctx)
    if o.remaining - st.filled_qty < QTY_STEP:
        await store.set_status(o.id, "filled")
    return {"id": o.id, "action": "take", "qty": st.filled_qty, "cost": d.est_cost}


def _hms(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S")


async def _pass(now: float, books_fn: BooksFn, halted: Callable[[], Awaitable[bool]],
                record: bool) -> dict[str, Any]:
    """One pass over the working orders; the caller holds the lease."""
    out: dict[str, Any] = {"ts": now, "orders": []}
    live = []
    for o in await store.working_orders():
        if now >= o.deadline_ts:
            note = f"deadline passed with {o.remaining:g} of {o.qty:g} unfilled"
            await store.add_tick(o.id, now, "expired", detail=note)
            await store.set_status(o.id, "expired", note)
            out["orders"].append({"id": o.id, "action": "expired", "detail": note})
        else:
            live.append(o)
    if live and await halted():
        for o in live:
            await _tick(o, now, "halted", record, detail="kill switch on")
        out["halted"] = True
        out["orders"] += [{"id": o.id, "action": "halted"} for o in live]
        return out
    rested = await store.last_fill_ts([o.instrument for o in live])
    active = []
    for o in live:
        t = rested.get(o.instrument)
        if t is not None and now - t < COOLDOWN:
            detail = f"resting after the fill at {_hms(t)} UTC, until {_hms(t + COOLDOWN)}"
            await _tick(o, now, "cooldown", record, detail=detail)
            out["orders"].append({"id": o.id, "action": "cooldown", "detail": detail})
        else:
            active.append(o)
    if not active:
        return out
    by_exp: dict[str, list[ThresholdOrder]] = defaultdict(list)
    for o in active:
        by_exp[o.expiry].append(o)
    t0 = time.time()
    try:
        books = await books_fn(sorted(by_exp), sorted({o.instrument for o in active}))
    except Exception as ex:  # Deribit hiccup (a 5xx, a timeout): this pass trades nothing, the next tries again
        out["error"] = detail = f"Deribit: {type(ex).__name__}: {ex}"
        for o in active:
            await _tick(o, now, "no_book", record, detail=detail)
            out["orders"].append({"id": o.id, "action": "no_book", "detail": detail})
        return out
    out["books_secs"] = round(time.time() - t0, 2)
    for e, orders in by_exp.items():
        try:
            priced = await asyncio.to_thread(_price, e, books.get(e, []), orders, now)
        except Exception as ex:  # the expiry cannot be priced: say so on each order, go on
            priced = {o.id: f"{e}: {type(ex).__name__}: {ex}" for o in orders}
        for o in orders:
            if now - rested.get(o.instrument, -COOLDOWN) < COOLDOWN:  # another order took it this pass
                await _tick(o, now, "cooldown", record, detail="resting after a fill on the same option")
                out["orders"].append({"id": o.id, "action": "cooldown"})
                continue
            try:
                r = await _work(o, priced[o.id], now, record)
            except Exception as ex:  # one bad order must not stop the others
                await store.add_tick(o.id, now, "error", detail=f"{type(ex).__name__}: {ex}")
                r = {"id": o.id, "action": "error", "detail": str(ex)}
            if r["action"] == "take" and r["qty"] > 0:
                rested[o.instrument] = now
            out["orders"].append(r)
    return out


async def run_tick(now: float | None = None, books_fn: BooksFn = _books,
                   halted: Callable[[], Awaitable[bool]] = kill_switch_on) -> dict[str, Any]:
    """A single pass under the lease, everything recorded."""
    now = now or time.time()
    if not await store.working_orders():  # idle: write nothing (on Cloud Run the DB sits on GCS FUSE)
        return {"ts": now, "orders": []}
    if not await store.acquire_lock(LOCK, LOCK_TTL, now):
        return {"ts": now, "skipped": "another tick is running"}
    out: dict[str, Any] = {"ts": now, "orders": []}
    try:
        out = await _pass(now, books_fn, halted, record=True)
        return out
    finally:
        await store.release_lock(LOCK, {"ts": now, "passes": 1, "n": len(out["orders"]),
                                        "halted": out.get("halted", False)})


async def run_minute(books_fn: BooksFn | None = None, halted: Callable[[], Awaitable[bool]] = kill_switch_on,
                     period: float = PERIOD, span: float = SPAN, clock: Callable[[], float] = time.time,
                     sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                     feed_fn: Callable[[], QuoteFeed] = QuoteFeed,
                     names_fn: Callable[[list[str]], Awaitable[list[str]]] = _names) -> dict[str, Any]:
    """What one scheduler call does: a pass every ``period`` seconds for ``span``
    seconds under one lease, stopping early when no order is left working. Unless
    ``books_fn`` is given, the passes read a quote feed held for the call over every
    option of the working orders' expiries."""
    start = clock()
    if not await store.working_orders():
        return {"ts": start, "passes": 0, "orders": []}
    while not await store.acquire_lock(LOCK, LOCK_TTL, clock()):
        if clock() - start > 2 * period:  # the previous call should have finished by now
            return {"ts": start, "skipped": "another tick is running"}
        await sleep(1.0)  # the previous call's last pass is finishing
    start = clock()
    passes, takes, last, feed, errors = 0, [], {"orders": []}, None, []
    try:
        if books_fn is None:
            feed = feed_fn()
            try:
                await feed.start(await names_fn(sorted({o.expiry for o in await store.working_orders()})))
            except Exception as e:  # no feed: the passes read over HTTP
                feed.error = feed.error or f"{type(e).__name__}: {e}"
            books_fn = functools.partial(_books, feed=feed)
        while True:
            try:
                last = await _pass(clock(), books_fn, halted, record=passes == 0)
            except Exception as ex:  # one failed pass must not cost the rest of the minute
                last = {"orders": [], "error": f"{type(ex).__name__}: {ex}"}
            if last.get("error"):
                errors.append(last["error"])
            passes += 1
            takes += [r for r in last["orders"] if r["action"] == "take"]
            nxt = start + passes * period
            if nxt > start + span or not await store.working_orders():
                break
            await sleep(max(0.0, nxt - clock()))
        return {"ts": start, "passes": passes, "takes": takes, "orders": last["orders"],
                "halted": last.get("halted", False), "quotes": _feed_state(feed),
                "failed": len(errors), "error": errors[-1] if errors else None}
    finally:
        state = _feed_state(feed)
        if feed is not None:
            await feed.close()
        await store.release_lock(LOCK, {"ts": start, "passes": passes, "takes": len(takes),
                                        "n": len(last["orders"]), "halted": last.get("halted", False),
                                        "quotes": state, "failed": len(errors),
                                        "error": errors[-1] if errors else None})


def _feed_state(feed: QuoteFeed | None) -> str | None:
    if feed is None:
        return None
    return "live" if feed.alive else f"HTTP ({feed.error or 'feed not started'})"
