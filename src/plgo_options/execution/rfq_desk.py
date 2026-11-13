"""The Execution Bot page's Block RFQ desk: send a previewed structure, watch the quotes, accept or cancel.

Testnet only, by construction: the client is built on a testnet connector with the testnet
key, whatever DERIBIT_TESTNET says, until the testnet run confirms the price sign and the
ratios (tests/test_block_rfq_testnet.py). A person sends and a person accepts; nothing here
acts on its own.

An accepted RFQ's option trades go into the cost table (``record``), one filled order per leg (via
'rfq'), next to the bot's orders, with their markouts.

Each RFQ sent is remembered in this process with our limit (the preview's, per unit of
Deribit's amount) and the preview's spot, so its quotes show as what we would pay. An RFQ
lives a few minutes, so a restart forgets nothing worth keeping: it expires on its own.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from plgo_options.pricing.smile_fit import fit_error, otm_quotes, spline_fair, spline_smile

from . import store
from .block_rfq import GRACE_SECONDS, BlockRfqClient, Rfq
from .deribit import ConnectorError, testnet_connector
from .threshold import MAX_FIT_ERROR

OPTION = re.compile(r"ETH-\d{1,2}[A-Z]{3}\d{2}-\d+-[CP]")

NETWORK = "testnet"


@dataclass
class Sent:
    rfq: Rfq
    kind: str
    limit_eth: float  # what we pay at most per unit of the RFQ's amount, ETH (negative: receive at least)
    spot: float  # USD per ETH when previewed, to show dollars
    by: str
    sent_at: float
    acceptance: dict[str, Any] | None = None


_client: BlockRfqClient | None = None
_sent: dict[int, Sent] = {}


def client() -> BlockRfqClient:
    global _client
    if _client is None:
        _client = BlockRfqClient(testnet_connector())
    return _client


async def send(legs: list[dict[str, Any]], kind: str, limit_eth: float, amount: float, spot: float,
               by: str = "", hedge: dict[str, Any] | None = None) -> dict[str, Any]:
    """Open an RFQ for a previewed structure. ``limit_eth`` is the preview's, per unit of ``amount``."""
    c = client()
    rfq = await c.create(legs, f"plgo-{kind}-{uuid.uuid4().hex[:10]}", hedge=hedge)
    # Deribit's amount is the legs' common factor; the preview's should equal it, but scale to be sure.
    per_unit = limit_eth * amount / rfq.amount
    _sent[rfq.block_rfq_id] = Sent(rfq, kind, per_unit, spot, by, time.time())
    return view(_sent[rfq.block_rfq_id])


def _get(rid: int) -> Sent:
    s = _sent.get(rid)
    if s is None:
        raise KeyError(f"RFQ {rid} was not sent from this server (or it restarted)")
    return s


async def read(rid: int) -> dict[str, Any]:
    s = _get(rid)
    await client().read(s.rfq)
    return view(s)


async def accept(rid: int) -> dict[str, Any]:
    s = _get(rid)
    c = client()
    await c.read(s.rfq)
    if not s.rfq.open:
        raise ConnectorError(f"RFQ {rid} is {s.rfq.state}")
    best = s.rfq.best()
    if best is None or s.rfq.ours(best.price) > s.limit_eth + 1e-12:
        raise ConnectorError("no quote for the whole amount at our limit or better")
    res = await c.accept(s.rfq, s.limit_eth)
    s.acceptance = {"filled": res.filled, "reason": res.reason, "at": time.time(),
                    "trades": [{k: t.get(k) for k in ("instrument_name", "direction", "amount", "price",
                                                      "fee", "fee_currency", "trade_id", "block_trade_id")}
                               for t in res.trades]}
    if res.filled:
        try:  # the trade is done whatever happens here: a failed record is reported, not raised
            s.acceptance["recorded"] = await record(rid, res.trades, s.by)
        except Exception as e:
            s.acceptance["record_error"] = f"{type(e).__name__}: {e}"
    return view(s)


async def record(rid: int, trades: list[dict[str, Any]], by: str = "", books_fn=None) -> list[int]:
    """An accepted RFQ's option trades into the cost table: one filled order per leg (via 'rfq'),
    each trade priced against the spline, Deribit's mark and the mid as they stand now, the fee
    as Deribit charged it. The hedge leg (a future) is left out. Returns the order ids."""
    from .bot import _books  # the bot's book reader: summary for the smile, each leg's own book
    legs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        if OPTION.fullmatch(t.get("instrument_name") or ""):
            legs[(t["instrument_name"], t["direction"])].append(t)
    if not legs:
        return []
    expiries = sorted({name.split("-")[1] for name, _ in legs})
    books = await (books_fn or _books)(expiries, sorted({name for name, _ in legs}))
    now = time.time()
    fits = {}
    for e in expiries:
        q = otm_quotes(e, books.get(e, []), dt.datetime.fromtimestamp(now, dt.timezone.utc))
        smile = spline_smile(q) if q is not None else None
        fits[e] = (q, smile) if smile is not None and fit_error(q, smile)[0] <= MAX_FIT_ERROR else (None, None)
    ids = []
    for (name, side), ts_ in legs.items():
        _, e, K, cp = name.split("-")
        b = next((x for x in books.get(e, []) if x["instrument_name"] == name), {})
        S = b.get("estimated_delivery_price") or ts_[0].get("index_price")
        q, smile = fits[e]
        f = spline_fair(q, float(K), cp, smile) if smile is not None else None
        mark = b.get("mark_price")
        bid, ask = b.get("best_bid_price"), b.get("best_ask_price")
        fair = f["fair"] if f else (mark * S if mark else None)
        if fair is None or not S:
            raise ConnectorError(f"{name}: no market to price the fill against")
        sign = 1.0 if side == "buy" else -1.0
        fills = []
        for t in ts_:
            usd = float(t["price"]) * S
            fee = float(t.get("fee") or 0.0) * S
            fills.append(dict(ts=(t.get("timestamp") or now * 1000) / 1000, intent_id=f"rfq{rid}",
                              trade_id=str(t["trade_id"]), price_eth=float(t["price"]), price_usd=usd,
                              qty=float(t["amount"]), fee_usd=fee, cost_usd=sign * (usd - fair) + fee,
                              fair_usd=fair, mode=NETWORK, mark_usd=mark * S if mark else None,
                              mid_usd=(bid + ask) / 2 * S if bid and ask else None,
                              S=S if f else None, iv=f["iv"] if f else None, vega=f["vega"] if f else None))
        note = f"Block RFQ #{rid}" + ("" if f else ": fair = Deribit's mark (no usable spline)")
        ids.append(await store.record_rfq_leg(name, side, NETWORK, fills, note, by, now))
    return ids


async def cancel(rid: int) -> dict[str, Any]:
    s = _get(rid)
    await client().cancel(s.rfq)
    return view(s)


def view(s: Sent) -> dict[str, Any]:
    r, now = s.rfq, time.time()

    def quotes(book):
        return [{"price": q.price, "ours_eth": r.ours(q.price), "ours_usd": round(r.ours(q.price) * s.spot, 2),
                 "amount": q.amount, "all_or_none": q.all_or_none, "makers": q.makers,
                 "inside": r.ours(q.price) <= s.limit_eth + 1e-12} for q in book]

    best = r.best()
    return {
        "network": NETWORK, "id": r.block_rfq_id, "label": r.label, "kind": s.kind, "state": r.state,
        "combo_id": r.combo_id, "side": r.side, "amount": r.amount, "legs": r.legs, "hedge": r.hedge,
        "min_trade_amount": r.min_trade_amount, "mark_price": r.mark_price,
        "sent_at": s.sent_at, "by": s.by, "expires": r.expires, "seconds_left": max(0.0, r.expires - now),
        "quotes_visible": now >= r.created + GRACE_SECONDS,
        "limit_eth": s.limit_eth, "limit_usd": round(s.limit_eth * s.spot, 2), "spot": s.spot,
        "limit_total_usd": round(s.limit_eth * s.spot * r.amount, 0),
        # the side we take: asks if we buy Deribit's structure, bids if we sell it
        "quotes": quotes(r.asks if r.side == "buy" else r.bids),
        "best": None if best is None else {"ours_eth": r.ours(best.price),
                                           "ours_usd": round(r.ours(best.price) * s.spot, 2),
                                           "total_usd": round(r.ours(best.price) * s.spot * r.amount, 0),
                                           "inside": r.ours(best.price) <= s.limit_eth + 1e-12},
        "acceptance": s.acceptance,
    }
