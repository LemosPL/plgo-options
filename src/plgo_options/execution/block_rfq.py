"""Deribit Block RFQ, taker side: create, read the quotes, accept, cancel.

Rides on a ``DeribitConnector`` for auth, transport, the testnet default and the
production opt-in. Nothing here is wired to the bot or the page yet: rfq.py
builds the structures, this sends them, and a person decides when (step 3).

What the Deribit API does that shapes this file (docs.deribit.com, Block RFQ):

    We send each leg's size; Deribit returns the structure as ratios times an
    amount, and may write it the other way round (every leg flipped). So
    ``create`` reads the legs back, checks each ratio x amount is the size we
    asked for, and records ``side``: "buy" if Deribit's legs go our way, "sell"
    if they are all flipped. Anything else is cancelled and raised: we never
    accept a structure that is not the one we meant.

    A taker sees quotes as aggregated ``bids`` and ``asks`` on the RFQ itself
    (private/get_block_rfqs), hidden for the first 5 seconds after creation. Quotes
    are priced per unit of the RFQ's amount, in Deribit's direction of the
    structure. ``Rfq.ours(price)`` turns one into what we pay per unit (negative
    when we receive), so limits compare one way whatever Deribit's orientation.

    ``accept`` goes out fill-or-kill for the whole amount at our worst price,
    so it fills entirely or not at all. An RFQ that filled cannot fill twice,
    so a timeout is settled by reading the RFQ back, never by resending.

    Labels are not unique on Deribit either. ``create`` looks the label up
    among our recent RFQs first and adopts an RFQ it finds rather than opening
    a second (a stray RFQ costs nothing but taker rating; still, one per label).

Scopes: ``block_rfq:read_write`` to create and accept, ``block_trade:read`` for the
resulting trades. ``check_scope`` says which is missing before anything is sent.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .deribit import (
    BLOCK_RFQ_SCOPES,
    _NOT_PLACED,
    _OUTCOME_UNKNOWN,
    ConnectorError,
    DeribitConnector,
    DeribitError,
    TransportError,
)

logger = logging.getLogger(__name__)

SCOPES = BLOCK_RFQ_SCOPES
GRACE_SECONDS = 5.0  # quotes are hidden from the taker this long after creation
_RECENT = 100  # how many of our latest RFQs a label lookup covers


@dataclass
class Quote:
    price: float  # Deribit's, per unit of amount, in Deribit's direction of the structure
    amount: float
    all_or_none: bool
    makers: list[str]
    expires_at: float  # seconds


@dataclass
class Rfq:
    block_rfq_id: int
    label: str
    side: str  # how we trade Deribit's structure: "buy" = its legs as written
    amount: float
    legs: list[dict[str, Any]]  # Deribit's: instrument_name, direction, ratio
    state: str
    created: float
    expires: float
    min_trade_amount: float | None = None
    mark_price: float | None = None  # Deribit's price for its structure, per unit
    combo_id: str | None = None
    hedge: dict[str, Any] | None = None
    bids: list[Quote] = field(default_factory=list)
    asks: list[Quote] = field(default_factory=list)

    def ours(self, price: float) -> float:
        """A Deribit price for its structure, as what we pay per unit of ours."""
        return price if self.side == "buy" else -price

    def best(self) -> Quote | None:
        """The best quote on the side we trade: the lowest ask to buy, the highest bid to sell,
        among those that can fill the whole amount (an all-or-none quote is always the whole)."""
        book = self.asks if self.side == "buy" else self.bids
        full = [q for q in book if q.all_or_none or q.amount >= self.amount - 1e-9]
        if not full:
            return None
        return min(full, key=lambda q: q.price) if self.side == "buy" else max(full, key=lambda q: q.price)

    @property
    def open(self) -> bool:
        return self.state in ("open", "created")


@dataclass
class Acceptance:
    filled: bool
    rfq: Rfq
    reason: str | None = None
    block_trades: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trades(self) -> list[dict[str, Any]]:
        return [t for b in self.block_trades for t in b.get("trades", [])]


class BlockRfqClient:
    def __init__(self, conn: DeribitConnector, currency: str = "ETH") -> None:
        self.conn = conn
        self.currency = currency

    # -- scope ------------------------------------------------------------------

    async def check_scope(self) -> dict[str, Any]:
        """Which Block RFQ scopes the key has. Raises if it cannot create or accept."""
        await self.conn._access_token()
        granted = set((self.conn._scope or "").split())
        missing = [s for s in SCOPES if s not in granted]
        out = {"network": "testnet" if self.conn.testnet else "production",
               "scope": self.conn._scope, "missing": missing}
        if "block_rfq:read_write" in missing:
            raise ConnectorError(f"API key lacks block_rfq:read_write (scope: {self.conn._scope!r})")
        return out

    # -- create -----------------------------------------------------------------

    async def create(self, legs: list[dict[str, Any]], label: str, *, makers: list[str] | None = None,
                     disclosed: bool = True, hedge: dict[str, Any] | None = None) -> Rfq:
        """Open an RFQ for ``legs`` ({instrument, side, qty}), or adopt the one open under ``label``."""
        if not legs or len(label) > 64:
            raise ConnectorError("an RFQ needs legs and a label of at most 64 characters")
        if not disclosed and len(makers or []) < 5:
            raise ConnectorError("an anonymous RFQ needs at least 5 makers targeted")
        await self.check_scope()
        found = await self._by_label(label)
        if found is not None:
            return await self._checked(found, legs, label)
        params: dict[str, Any] = {
            "legs": [{"instrument_name": x["instrument"], "amount": x["qty"], "direction": x["side"]}
                     for x in legs],
            "label": label, "disclosed": disclosed}
        if makers:
            params["makers"] = list(makers)
        if hedge:
            params["hedge"] = json.dumps(hedge)  # Deribit takes the hedge leg as a JSON string
        try:
            raw = await self.conn._rpc("private/create_block_rfq", params)
        except (TransportError, DeribitError) as exc:
            if not _unknown(exc):
                raise
            logger.error("%s: create outcome unknown (%s); checking by label", label, exc)
            await self.conn._sleep(1.0)
            raw = await self._by_label(label)
            if raw is None:
                raise ConnectorError(f"{label}: create failed and no RFQ landed ({exc})") from exc
        return await self._checked(raw, legs, label)

    async def _checked(self, raw: dict[str, Any], legs: list[dict[str, Any]], label: str) -> Rfq:
        try:
            return _rfq(raw, legs)
        except ConnectorError:
            # Not the structure we asked for: leave nothing open for makers to quote.
            logger.error("%s: Deribit's structure differs from ours, cancelling RFQ %s",
                         label, raw.get("block_rfq_id"))
            try:
                await self.conn._rpc("private/cancel_block_rfq", {"block_rfq_id": raw["block_rfq_id"]})
            except ConnectorError as exc:
                logger.error("%s: cancelling RFQ %s failed: %s", label, raw.get("block_rfq_id"), exc)
            raise

    async def _by_label(self, label: str) -> dict[str, Any] | None:
        res = await self.conn._rpc("private/get_block_rfqs", {
            "role": "taker", "currency": self.currency, "count": _RECENT})
        rows = [r for r in (res or {}).get("block_rfqs", [])
                if r.get("label") == label and r.get("state") in ("open", "created")]
        return min(rows, key=lambda r: r.get("creation_timestamp") or 0) if rows else None

    # -- read, cancel -------------------------------------------------------------

    async def read(self, rfq: Rfq) -> Rfq:
        """The RFQ now: its state and the quotes on both sides."""
        res = await self.conn._rpc("private/get_block_rfqs", {
            "block_rfq_id": rfq.block_rfq_id, "role": "taker", "currency": self.currency})
        rows = [r for r in (res or {}).get("block_rfqs", []) if r.get("block_rfq_id") == rfq.block_rfq_id]
        if not rows:
            raise ConnectorError(f"RFQ {rfq.block_rfq_id} not found")
        _update(rfq, rows[0])
        return rfq

    async def cancel(self, rfq: Rfq) -> Rfq:
        """Close an RFQ. One that already closed (filled, expired) is read back, not an error."""
        try:
            raw = await self.conn._rpc("private/cancel_block_rfq", {"block_rfq_id": rfq.block_rfq_id})
        except DeribitError as exc:
            if exc.code in _NOT_PLACED or exc.code in _OUTCOME_UNKNOWN:
                raise
            return await self.read(rfq)
        _update(rfq, raw)
        return rfq

    # -- accept -----------------------------------------------------------------

    async def accept(self, rfq: Rfq, limit: float) -> Acceptance:
        """Take the whole amount at ``limit`` or better: what we pay per unit of our structure, in
        the structure's currency (negative to receive). Fill-or-kill, so all of it or none."""
        if limit is None or not math.isfinite(limit):
            raise ConnectorError("accept needs a finite limit")
        price = rfq.ours(limit)  # back into Deribit's direction: worst price to buy, or to sell
        params: dict[str, Any] = {
            "block_rfq_id": rfq.block_rfq_id, "amount": rfq.amount, "direction": rfq.side,
            "price": price, "time_in_force": "fill_or_kill",
            "legs": [{"instrument_name": x["instrument_name"], "direction": x["direction"],
                      "ratio": x["ratio"]} for x in rfq.legs]}
        if rfq.hedge:
            params["hedge"] = json.dumps(rfq.hedge)
        try:
            raw = await self.conn._rpc("private/accept_block_rfq", params)
        except (TransportError, DeribitError) as exc:
            if not _unknown(exc):
                if isinstance(exc, DeribitError) and exc.code not in _NOT_PLACED:
                    return Acceptance(False, rfq, reason=exc.reason)  # e.g. no quote at our price
                raise
            logger.error("RFQ %s: accept outcome unknown (%s); reading it back", rfq.block_rfq_id, exc)
            await self.conn._sleep(1.0)
            await self.read(rfq)
            if rfq.state != "filled":
                return Acceptance(False, rfq, reason=f"not filled after an unanswered accept ({exc})")
            return Acceptance(True, rfq, block_trades=await self.block_trades(rfq))
        trades = (raw or {}).get("block_trades") or []
        await self.read(rfq)
        if not trades and rfq.state == "filled":
            trades = await self.block_trades(rfq)
        return Acceptance(bool(trades), rfq, block_trades=trades,
                          reason=None if trades else "no trade: no quote at our price for the whole amount")

    async def block_trades(self, rfq: Rfq) -> list[dict[str, Any]]:
        return await self.conn._rpc("private/get_block_trades", {
            "currency": self.currency, "block_rfq_id": rfq.block_rfq_id, "count": 101}) or []


def _unknown(exc: ConnectorError) -> bool:
    """The request may have been carried out: settle it by reading back, never by resending."""
    if isinstance(exc, TransportError):
        return exc.maybe_sent
    return isinstance(exc, DeribitError) and exc.code in _OUTCOME_UNKNOWN


# -- parsing --------------------------------------------------------------------

def _rfq(raw: dict[str, Any], legs: list[dict[str, Any]]) -> Rfq:
    """Deribit's RFQ, checked against the legs we sent, with our side of it."""
    amount = float(raw["amount"])
    got = {x["instrument_name"]: x for x in raw.get("legs", [])}
    want = {x["instrument"]: x for x in legs}
    if set(got) != set(want):
        raise ConnectorError(f"RFQ {raw.get('block_rfq_id')}: legs {sorted(got)} are not ours {sorted(want)}")
    same = flipped = 0
    for name, w in want.items():
        g = got[name]
        if abs(float(g["ratio"]) * amount - float(w["qty"])) > 1e-6 * max(1.0, float(w["qty"])):
            raise ConnectorError(f"RFQ {raw.get('block_rfq_id')}: {name} is {g['ratio']} x {amount}, "
                                 f"not the {w['qty']} we asked for")
        if g["direction"] == w["side"]:
            same += 1
        else:
            flipped += 1
    if same and flipped:
        raise ConnectorError(f"RFQ {raw.get('block_rfq_id')}: some legs flipped and some not")
    rfq = Rfq(block_rfq_id=int(raw["block_rfq_id"]), label=raw.get("label") or "",
              side="buy" if same else "sell", amount=amount, legs=list(raw.get("legs", [])),
              state=raw.get("state", "open"), created=(raw.get("creation_timestamp") or 0) / 1000,
              expires=(raw.get("expiration_timestamp") or 0) / 1000)
    _update(rfq, raw)
    return rfq


def _quotes(rows: list[dict[str, Any]] | None) -> list[Quote]:
    return [Quote(price=float(q["price"]), amount=float(q.get("amount") or 0.0),
                  all_or_none=q.get("execution_instruction") == "all_or_none",
                  makers=list(q.get("makers") or []), expires_at=(q.get("expires_at") or 0) / 1000)
            for q in rows or [] if q.get("price") is not None]


def _update(rfq: Rfq, raw: dict[str, Any]) -> None:
    rfq.state = raw.get("state", rfq.state)
    rfq.bids = _quotes(raw.get("bids"))
    rfq.asks = _quotes(raw.get("asks"))
    rfq.min_trade_amount = raw.get("min_trade_amount", rfq.min_trade_amount)
    rfq.mark_price = raw.get("mark_price", rfq.mark_price)
    rfq.combo_id = raw.get("combo_id", rfq.combo_id)
    rfq.hedge = raw.get("hedge", rfq.hedge)
    if raw.get("expiration_timestamp"):
        rfq.expires = raw["expiration_timestamp"] / 1000
