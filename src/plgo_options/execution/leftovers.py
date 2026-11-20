"""What the bot left unfilled at a deadline, as a Block RFQ the bot sends and accepts itself.

Replayed (scripts/execution_replay.py, 6-17 Nov 2026), a 2 h order leaves ~30% unfilled, and crossing
it at the touch then costs ~1.1 vol points, about 40% of the order's all-in cost. Market makers may
quote the remainder as one structure for less.

An order created with ``leftover_rfq`` that expires with at least one contract left goes ``pending``
(bot._pass). Each pass then (``step``):

  pending  the pending orders of one mode and expiry, together, become one structure in round units
           (rfq.ratios, UNIT contracts; what rounding leaves stays unfilled), priced now on the
           spline (rfq.price_structure). Our limit, per unit of the structure:

               fair + hard_vp x the structure's vega - the RFQ's fee

           so a fill costs at most the order's hard limit, fee included, as the bot's own takes.
           Within the day's caps (rails.room), it is sent: ``sent``. Not priceable (the spline off
           its quotes, no book) it waits, for at most PENDING_FOR, then ``lapsed``.
  sent     read each pass. Once quotes have shown for QUOTE_WAIT seconds (makers compete), the best
           one for the whole amount at our limit or better is accepted (fill-or-kill, at its own
           price): ``filled``, its trades in the cost table as Block RFQ legs (rfq_desk.record). None
           by EXPIRY_MARGIN before the RFQ ends: cancelled, ``lapsed``, with the best quote against
           our limit.

Testnet only, like the desk (rfq_desk.py): a live order's remainder is ``skipped`` until the
testnet run confirms Block RFQ end to end; a paper order's too (no RFQ to simulate against). The
kill switch stops sending and accepting; an RFQ already out then lapses on its own.

The RFQ's essentials are kept on the orders (``leftover_json``), not in this process, so a restart or
another Cloud Run instance picks it up where it was.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from plgo_options.pricing.smile_fit import fit_error, otm_quotes, spline_smile

from . import rails, rfq_desk, store
from .block_rfq import GRACE_SECONDS, Rfq
from .rfq import MIN_RFQ_CONTRACTS, price_structure, ratios
from .threshold import MAX_FIT_ERROR

MODES = ("testnet",)       # where a remainder goes out as an RFQ (the desk is testnet-only)
UNIT = 10                  # contracts: the structure's round unit; under it per leg stays unfilled
QUOTE_WAIT = 15.0          # seconds of visible quotes before accepting, so several makers can quote
EXPIRY_MARGIN = 10.0       # seconds before the RFQ ends: give up, cancel
PENDING_FOR = 600.0        # seconds a remainder waits to be priced and sent before it lapses
BY = "bot (leftovers)"

BooksFn = Callable[[list[str], list[str]], Awaitable[dict[str, list[dict]]]]


def initial(o, now: float) -> dict[str, Any]:
    """The leftover state of an order expiring now with ``o.remaining`` unfilled."""
    if o.mode not in MODES:
        why = ("paper: no Block RFQ to simulate against" if o.mode == "paper"
               else f"{o.mode}: Block RFQ is testnet-only for now")
        return {"status": "skipped", "since": now, "qty": o.remaining, "detail": why}
    return {"status": "pending", "since": now, "qty": o.remaining,
            "detail": f"{o.remaining:g} to send as a Block RFQ"}


async def _set(rows: list[dict], state: dict[str, Any]) -> None:
    for r in rows:
        await store.set_leftover(r["id"], r["leftover"] | state)


def _rfq_state(r: Rfq) -> dict[str, Any]:
    return dict(block_rfq_id=r.block_rfq_id, label=r.label, side=r.side, amount=r.amount, legs=r.legs,
                state=r.state, created=r.created, expires=r.expires, hedge=r.hedge)


async def step(now: float, books_fn: BooksFn, halted: Callable[[], Awaitable[bool]]) -> list[dict[str, Any]]:
    """One pass over the remainders on their way to an RFQ; what happened to each group."""
    rows = await store.open_leftovers()
    if not rows:
        return []
    if await halted():
        return [{"ids": [r["id"] for r in rows], "action": "halted"}]
    out = []
    sent: dict[int, list[dict]] = defaultdict(list)
    pending: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["leftover"]["status"] == "sent":
            sent[r["leftover"]["rfq"]["block_rfq_id"]].append(r)
        else:
            pending[(r["mode"], r["instrument"].split("-")[1])].append(r)
    for rid, group in sent.items():
        try:
            out.append(await _watch(rid, group, now))
        except Exception as ex:  # read again next pass; the RFQ lapses on its own if it never recovers
            out.append({"ids": [r["id"] for r in group], "action": "error", "detail": f"{type(ex).__name__}: {ex}"})
    for (mode, expiry), group in pending.items():
        try:
            out.append(await _send(mode, expiry, group, now, books_fn))
        except Exception as ex:
            detail = f"{type(ex).__name__}: {ex}"
            lapse = now - min(r["leftover"]["since"] for r in group) > PENDING_FOR
            await _set(group, {"status": "lapsed", "detail": f"not sent: {detail}", "at": now} if lapse
                       else {"detail": f"not sent yet ({detail}); trying again"})
            out.append({"ids": [r["id"] for r in group], "action": "error", "detail": detail})
    return out


async def _send(mode: str, expiry: str, group: list[dict], now: float, books_fn: BooksFn) -> dict[str, Any]:
    ids = [r["id"] for r in group]
    # one leg per instrument and side, the orders' remainders added up
    legs: dict[tuple[str, str], dict] = {}
    for r in group:
        k = (r["instrument"], r["side"])
        legs.setdefault(k, {"instrument": k[0], "side": k[1], "qty": 0, "orders": []})
        legs[k]["qty"] += int(r["leftover"]["qty"])
        legs[k]["orders"].append(r)
    ls = list(legs.values())
    r_, amount, left = ratios([x["qty"] for x in ls], UNIT)
    total = sum(x * amount for x in r_)
    if total < MIN_RFQ_CONTRACTS:
        await _set(group, {"status": "skipped", "at": now,
                           "detail": f"{total:g} contracts in round units, under Deribit's {MIN_RFQ_CONTRACTS}-contract RFQ minimum"})
        return {"ids": ids, "action": "skipped", "detail": "under the RFQ minimum"}
    books = await books_fn([expiry], sorted({x["instrument"] for x in ls}))
    q = otm_quotes(expiry, books.get(expiry, []), dt.datetime.fromtimestamp(now, dt.timezone.utc))
    smile = spline_smile(q) if q is not None else None
    if smile is None or fit_error(q, smile)[0] > MAX_FIT_ERROR:
        if now - min(r["leftover"]["since"] for r in group) > PENDING_FOR:
            await _set(group, {"status": "lapsed", "at": now, "detail": "not sent: the spline stayed off its quotes"})
            return {"ids": ids, "action": "lapsed", "detail": "no usable spline"}
        return {"ids": ids, "action": "wait", "detail": "the spline is off its quotes"}
    s = price_structure("directional", ls, r_, amount, {expiry: (q, smile)}, books, 0.0, 0.0)
    hard = min(r["hard_vp"] for r in group)
    limit_usd = s["fair_usd"] + hard * s["vega_usd"] - s["rfq_fee_usd"]
    limit_eth = limit_usd / s["S"]
    per_contract = abs(limit_usd) * amount / total if total else 0.0
    room, why = await rails.room(mode, total, per_contract, now)
    if room < total:
        await _set(group, {"status": "lapsed", "at": now, "detail": f"not sent: {why}"})
        return {"ids": ids, "action": "day_cap", "detail": why}
    send_legs = [{"instrument": x["instrument"], "side": x["side"], "qty": ri * amount} for x, ri in zip(ls, r_) if ri]
    c = rfq_desk.client()
    rfq = await rfq_desk.create(c, send_legs, f"plgo-left-{ids[0]}-{uuid.uuid4().hex[:8]}")
    per_unit = limit_eth * amount / rfq.amount  # Deribit's amount is the legs' common factor
    unsent = {f"{x['instrument']} {x['side']}": lo for x, lo in zip(ls, left) if lo}
    state = {"status": "sent", "at": now, "rfq": _rfq_state(rfq), "limit_eth": per_unit, "spot": s["S"],
             "fair_usd": s["fair_usd"] * amount / rfq.amount, "limit_usd": per_unit * s["S"],
             "screen_cost_usd": s["screen_cost_usd"], "orders": ids, "unsent": unsent,
             "detail": f"RFQ #{rfq.block_rfq_id}: {_legs(send_legs)}, our limit {_pay(per_unit * s['S'])} per unit of {rfq.amount:g}"}
    await _set(group, state)
    return {"ids": ids, "action": "sent", "rfq": rfq.block_rfq_id, "limit_usd": round(per_unit * s["S"], 2)}


def _pay(usd: float) -> str:
    return f"pay ${usd:,.2f}" if usd >= 0 else f"receive ${-usd:,.2f}"


def _legs(legs: list[dict]) -> str:
    return ", ".join(f"{x['side']} {x['qty']:g} {x['instrument']}" for x in legs)


async def _watch(rid: int, group: list[dict], now: float) -> dict[str, Any]:
    ids = [r["id"] for r in group]
    st = group[0]["leftover"]
    rfq = Rfq(**st["rfq"])
    c = rfq_desk.client()
    await c.read(rfq)
    best = rfq.best()
    ours = rfq.ours(best.price) if best else None
    spot = st["spot"]
    vs = (f"best {_pay(ours * spot)} vs our limit {_pay(st['limit_eth'] * spot)} per unit" if best
          else "no quote for the whole amount")
    if not rfq.open:
        await _set(group, {"status": "lapsed", "at": now, "detail": f"RFQ #{rid} {rfq.state}: {vs}"})
        return {"ids": ids, "action": "lapsed", "rfq": rid, "detail": vs}
    visible_for = now - (rfq.created + GRACE_SECONDS)
    if best is not None and ours <= st["limit_eth"] + 1e-12 and visible_for >= QUOTE_WAIT:
        res = await c.accept(rfq, ours)
        if res.filled:
            try:
                recorded = await rfq_desk.record(rid, res.trades, BY)
                rec = f"; in the cost table as order(s) {', '.join(f'#{x}' for x in recorded)}"
            except Exception as ex:  # the trade is done: report a failed record, never undo
                recorded, rec = [], f"; not in the cost table: {type(ex).__name__}: {ex}"
            await _set(group, {"status": "filled", "at": now, "recorded": recorded,
                               "price_usd": ours * spot, "detail": f"RFQ #{rid} filled: {_pay(ours * spot)} per unit{rec}"})
            return {"ids": ids, "action": "filled", "rfq": rid, "price_usd": round(ours * spot, 2)}
        return {"ids": ids, "action": "no_fill", "rfq": rid, "detail": res.reason}  # the quote went: try again
    if now >= rfq.expires - EXPIRY_MARGIN:
        await c.cancel(rfq)
        await _set(group, {"status": "lapsed", "at": now, "detail": f"RFQ #{rid} cancelled: {vs}"})
        return {"ids": ids, "action": "lapsed", "rfq": rid, "detail": vs}
    await _set(group, {"rfq": _rfq_state(rfq), "best_usd": None if best is None else ours * spot,
                       "detail": f"RFQ #{rid} open, {max(0, rfq.expires - now):.0f} s left: {vs}"})
    return {"ids": ids, "action": "watching", "rfq": rid, "detail": vs}
