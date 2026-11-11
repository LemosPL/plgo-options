"""The Execution Bot page's Block RFQ desk: send a previewed structure, watch the quotes, accept or cancel.

Testnet only, by construction: the client is built on a testnet connector with the testnet
key, whatever DERIBIT_TESTNET says, until the testnet run confirms the price sign and the
ratios (tests/test_block_rfq_testnet.py). A person sends and a person accepts; nothing here
acts on its own.

Each RFQ sent is remembered in this process with our limit (the preview's, per unit of
Deribit's amount) and the preview's spot, so its quotes show as what we would pay. An RFQ
lives a few minutes, so a restart forgets nothing worth keeping: it expires on its own.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .block_rfq import GRACE_SECONDS, BlockRfqClient, Rfq
from .deribit import ConnectorError, DeribitConnector, deribit_keys

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
        cid, secret = deribit_keys(True)
        if not (cid and secret):
            raise ConnectorError("no testnet key: set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET")
        _client = BlockRfqClient(DeribitConnector(cid, secret, testnet=True))
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
    return view(s)


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
