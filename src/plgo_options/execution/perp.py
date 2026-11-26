"""The perp hedge that comes with an optimizer solution (ETH-PERPETUAL), worked by the bot.

The optimizer's solution can carry a perp trade next to its option trades. Sent to the bot with
them, it becomes one more parent order (qty in ETH, as the optimizer gives it), and keeps pace with
the option orders created with it (``hedge_of``): each pass it brings its own fill up to the
options' share filled, contracts over contracts. Half the options filled, half the perp. Sent alone,
it goes all at once.

Each step is one IOC on the perp, ``order_type="market"``: the venue's touch, at most
``SLIPPAGE_BPS`` from its mid (on testnet, testnet's own book). Steps under ``MIN_USD`` wait for more
option fills. It finishes when its options have: filled if they all did, otherwise expired at the
share they reached, the rest reported. Its deadline is the options' plus ``GRACE``, so it can catch
up on their last fills.

Its cost is against the production mid at the step, fee included, per ETH. Deribit sizes the perp in
USD: each step's ETH is turned into USD at the mid, each fill's USD into ETH at its price. The venue
modes go through the same halt, health and position checks as the options (rails.py); the option
caps (contracts, premium) do not apply to it.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from . import rails, store
from .models import BookTop, OrderIntent, OrderStatus
from .deribit import ConnectorError
from .paper import PaperConnector
from .threshold import ThresholdOrder

INSTRUMENT = "ETH-PERPETUAL"
SLIPPAGE_BPS = 10.0   # the most a step may pay over the venue's mid
FEE_RATE = 0.0005     # Deribit perp taker fee, of the notional
MIN_USD = 10.0        # a step under this waits for more option fills
GRACE = 600.0         # seconds past the options' deadline, to catch up on their last fills
EPS_ETH = 0.01        # filled within this of the target: done
API = "https://www.deribit.com/api/v2/public"

PerpFn = Callable[[], Awaitable[dict[str, Any]]]


async def production_book() -> dict[str, Any]:
    """ETH-PERPETUAL's top of book on production: best_bid_price, best_ask_price, their amounts, mark, index."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/get_order_book", params={"instrument_name": INSTRUMENT, "depth": 1})
        r.raise_for_status()
    return r.json()["result"]


def perp_fee(_instrument: str, qty_usd: float, price: float, _maker: bool) -> float:
    """Deribit's perp taker fee in ETH: FEE_RATE of the USD notional, at the fill price."""
    return FEE_RATE * qty_usd / price


def target(o: ThresholdOrder, options: list[dict[str, Any]]) -> tuple[float, bool, str]:
    """(ETH the hedge should have filled by now, whether its options are done, in words)."""
    if not o.hedge_of:
        return o.qty, True, "sent without options: all at once"
    qty = sum(x["qty"] for x in options)
    filled = sum(x["filled"] for x in options)
    share = filled / qty if qty else 1.0
    done = len(options) == len(o.hedge_of) and all(x["status"] in store.FINISHED for x in options)
    ids = ", ".join(f"#{i}" for i in o.hedge_of)
    return o.qty * share, done, f"options {ids} {100 * share:.0f}% filled ({filled:g} of {qty:g})"


async def work(o: ThresholdOrder, now: float, record: bool, book_fn: PerpFn,
               connectors: dict[str, Callable[[], Any]], tick: Callable[..., Awaitable[None]],
               blocking: Callable[[ThresholdOrder, str], str | None]) -> dict[str, Any]:
    want, done, pace = target(o, await store.orders_by_id(o.hedge_of))
    need = want - o.filled
    try:
        b = await book_fn()
    except (httpx.HTTPError, KeyError) as ex:
        detail = f"{INSTRUMENT} book: {type(ex).__name__}: {ex}"
        await tick(o, now, "no_book", record, detail=detail)
        return {"id": o.id, "action": "no_book", "detail": detail}
    bid, ask = b.get("best_bid_price") or None, b.get("best_ask_price") or None
    if not bid or not ask:
        await tick(o, now, "no_book", record, detail=f"{INSTRUMENT}: no two-sided book")
        return {"id": o.id, "action": "no_book"}
    mid, S = (bid + ask) / 2, b.get("index_price") or (bid + ask) / 2
    ctx = dict(fair_usd=mid, S=S)
    usd = math.floor(need * mid) if need > 0 else 0
    if usd < MIN_USD:
        if done:
            if o.filled >= o.qty - EPS_ETH:
                await store.set_status(o.id, "filled")
                return {"id": o.id, "action": "filled"}
            note = f"{pace}, finished: the hedge stopped at the same share, {o.filled:.2f} of {o.qty:g} ETH"
            await store.add_tick(o.id, now, "expired", detail=note)
            await store.set_status(o.id, "expired", note)
            return {"id": o.id, "action": "expired", "detail": note}
        detail = f"in step: {o.filled:.2f} of {o.qty:g} ETH, {pace}"
        await tick(o, now, "wait", record, detail=detail, **ctx)
        return {"id": o.id, "action": "wait", "detail": detail}

    before = 0.0
    if o.mode == "paper":
        async def top(_: str) -> BookTop:
            return BookTop(INSTRUMENT, bid, ask, b.get("best_bid_amount"), b.get("best_ask_amount"), b.get("mark_price"), now)

        conn = PaperConnector(top, fee_fn=perp_fee, clock=lambda: now)
    else:
        if halt := (await store.halts()).get(o.mode):
            detail = f"{o.mode} halted by a failed position check: {halt['reason']}; clear it on the page"
            await tick(o, now, "halted", record, detail=detail, **ctx)
            return {"id": o.id, "action": "halted", "detail": detail}
        try:
            conn = connectors[o.mode]()
            ok = await conn.healthy()
            before = await rails.position(conn, INSTRUMENT) if ok else 0.0
        except ConnectorError as ex:
            ok, why = False, str(ex)
        else:
            why = "unhealthy"
        if not ok:
            await tick(o, now, "connector_down", record, detail=f"{o.mode} connector {why}: nothing sent", **ctx)
            return {"id": o.id, "action": "connector_down"}
    intent = OrderIntent(intent_id=f"perp{o.id}-{int(now * 1000)}", proposal_id=f"exec-{o.id}", instrument=INSTRUMENT,
                         side=o.side, qty=float(usd), order_type="market", max_slippage_bps=SLIPPAGE_BPS,
                         time_in_force="IOC")
    st = await conn.submit(intent)
    if st.status is OrderStatus.REJECTED and not st.fills:
        reason = st.reject_reason or ""
        if why := blocking(o, reason):
            detail = f"{why}; paused, nothing more sent: fix it, then resume (Deribit: {reason})"
            await store.add_tick(o.id, now, "blocked", detail=detail, **ctx)
            await store.set_status(o.id, store.PAUSED, detail)
            return {"id": o.id, "action": "blocked", "detail": detail}
        if "no liquidity" in reason:  # the touch is past SLIPPAGE_BPS from the mid: the next pass looks again
            detail = f"{INSTRUMENT}: the touch is over {SLIPPAGE_BPS:g} bps from the mid on {o.mode}: nothing sent"
            await tick(o, now, "wait", record, detail=detail, **ctx)
            return {"id": o.id, "action": "wait", "detail": detail}
        await tick(o, now, "rejected", record, detail=reason, **ctx)
        return {"id": o.id, "action": "rejected", "detail": reason}
    sign = 1.0 if o.side == "buy" else -1.0
    if not st.fills:
        detail = f"IOC {o.side} ${usd:,} {INSTRUMENT} on {o.mode}: nothing within {SLIPPAGE_BPS:g} bps of the mid"
        if o.mode != "paper" and (bad := await rails.reconcile(conn, INSTRUMENT, before, 0.0)):
            await store.set_halt(o.mode, bad, now)
            await store.add_tick(o.id, now, "mismatch", detail=f"{bad}: {o.mode} halted")
            return {"id": o.id, "action": "mismatch", "detail": bad}
        await tick(o, now, "no_fill", record, detail=detail, **ctx)
        return {"id": o.id, "action": "no_fill", "detail": detail}
    fills = []
    for f in st.fills:
        eth = f.qty / f.price
        fee_usd = f.fee * f.price
        fills.append(dict(ts=now if o.mode == "paper" else (f.ts or now), intent_id=f.intent_id,
                          trade_id=f"{f.intent_id}/{f.trade_id}" if o.mode == "paper" else f.trade_id,
                          price_eth=f.price / S, price_usd=f.price, qty=eth, fee_usd=fee_usd, mode=o.mode,
                          fair_usd=mid, mark_usd=b.get("mark_price"), mid_usd=mid,
                          cost_usd=sign * (f.price - mid) + fee_usd / eth))   # per ETH; no vega: no markouts
    await store.add_fills(o.id, fills)
    got = sum(x["qty"] for x in fills)
    await tick(o, now, "take", record, qty=got, detail=f"${st.filled_qty:,.0f} ({got:.2f} ETH): {pace}", **ctx)
    if o.mode != "paper":
        if bad := await rails.reconcile(conn, INSTRUMENT, before, sign * st.filled_qty):
            await store.set_halt(o.mode, bad, now)
            await store.add_tick(o.id, now + 0.001, "mismatch", detail=f"{bad}: {o.mode} halted")
    if o.filled + got >= o.qty - EPS_ETH:
        await store.set_status(o.id, "filled")
    return {"id": o.id, "action": "take", "qty": got}
