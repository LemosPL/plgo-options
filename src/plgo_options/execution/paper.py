"""PaperConnector: the Connector protocol without a Deribit account.

Fills against whatever book ``quotes`` returns: live Deribit public tickers
(``deribit_quotes``) for shadow runs, or a stub in tests. The fill model is
deliberately pessimistic:
    - a marketable order takes the touch, up to the displayed size (unknown
      size = fills in full), and the rest is cancelled (IOC) or rests (GTC);
    - a resting order fills only once the opposite side trades through its
      price, checked whenever the order is polled. Sitting at the touch never
      fills, so paper results understate passive fills, never overstate them.

Fees come from ``fee_fn``; the default is a flat rate on qty x price, a
placeholder until it is checked against Deribit's current fee schedule.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .models import (
    AccountState,
    BookTop,
    Fill,
    OrderIntent,
    OrderState,
    OrderStatus,
    Position,
)

QuoteSource = Callable[[str], Awaitable[BookTop]]
FeeFn = Callable[[str, float, float, bool], float]  # (instrument, qty, price, maker) -> fee


def flat_fee(maker_rate: float = 0.0, taker_rate: float = 0.0005) -> FeeFn:
    def fee(_instrument: str, qty: float, price: float, maker: bool) -> float:
        return (maker_rate if maker else taker_rate) * qty * price
    return fee


def deribit_quotes(client: Any) -> QuoteSource:
    """A QuoteSource over market_data.deribit_client.DeribitClient (public ``ticker``).

    Reads the raw ticker rather than the client's schemas, which drop the
    displayed sizes; without them a paper fill of any size would clear at the
    touch. Only the top level is known, so a large order fills the touch size
    and no deeper: pessimistic, like the rest of the fill model. The client
    caches responses for 10 s, so steps shorter than that see the same book.
    """
    async def quotes(instrument: str) -> BookTop:
        raw = await client._get("ticker", {"instrument_name": instrument})
        return BookTop(instrument=instrument, best_bid=raw.get("best_bid_price"),
                       best_ask=raw.get("best_ask_price"), bid_size=raw.get("best_bid_amount"),
                       ask_size=raw.get("best_ask_amount"), mark=raw.get("mark_price"),
                       ts=time.time())
    return quotes


class PaperConnector:
    def __init__(self, quotes: QuoteSource, fee_fn: FeeFn | None = None,
                 equity: float = 0.0, currency: str = "ETH",
                 clock: Callable[[], float] = time.time) -> None:
        self._quotes = quotes
        self._fee = fee_fn or flat_fee()
        self._equity = equity
        self._currency = currency
        self._clock = clock
        self._ids = itertools.count(1)
        self._orders: dict[str, OrderState] = {}
        self._by_intent: dict[str, str] = {}
        self._positions: dict[str, tuple[float, float]] = {}  # instrument -> (size, avg_price)
        self.is_healthy = True
        self.submitted: list[OrderIntent] = []  # every intent actually placed, for tests and logs

    # -- Connector protocol ---------------------------------------------------

    async def submit(self, intent: OrderIntent) -> OrderState:
        if intent.intent_id in self._by_intent:
            return self._orders[self._by_intent[intent.intent_id]]
        state = OrderState(order_id=f"paper-{next(self._ids)}", intent=intent,
                           status=OrderStatus.OPEN)
        self._orders[state.order_id] = state
        self._by_intent[intent.intent_id] = state.order_id
        self.submitted.append(intent)

        book = await self._quotes(intent.instrument)
        touch = book.best_ask if intent.side == "buy" else book.best_bid
        size = book.ask_size if intent.side == "buy" else book.bid_size

        if intent.order_type == "market":
            ref = book.mid
            worst = None if ref is None else ref * (
                1 + (intent.max_slippage_bps or 0) / 1e4 * (1 if intent.side == "buy" else -1))
            if touch is None or worst is None or not _within(intent.side, touch, worst):
                return self._reject(state, "no liquidity inside max_slippage_bps")
            self._fill(state, min(intent.qty, size or intent.qty), touch, maker=False)
            return self._close(state)

        crosses = touch is not None and _within(intent.side, touch, intent.limit_price)
        if intent.order_type == "post_only":
            if crosses:
                return self._reject(state, "post_only would take liquidity")
            return state
        if crosses:
            self._fill(state, min(intent.qty, size or intent.qty), touch, maker=False)
        if intent.time_in_force == "IOC" or state.remaining == 0:
            return self._close(state)
        return state

    async def cancel(self, order_id: str) -> OrderState:
        state = await self.order(order_id)
        if not state.done:
            state.status = OrderStatus.CANCELLED
        return state

    async def order(self, order_id: str) -> OrderState:
        state = self._orders[order_id]
        if not state.done:
            await self._match_resting(state)
        return state

    async def book(self, instrument: str) -> BookTop:
        return await self._quotes(instrument)

    async def positions(self) -> list[Position]:
        return [Position(instrument=i, size=s, avg_price=p)
                for i, (s, p) in self._positions.items() if s]

    async def account(self) -> AccountState:
        return AccountState(currency=self._currency, equity=self._equity, initial_margin=0.0,
                            maintenance_margin=0.0, available_funds=self._equity)

    async def healthy(self) -> bool:
        return self.is_healthy

    # -- fill model -----------------------------------------------------------

    async def _match_resting(self, state: OrderState) -> None:
        intent = state.intent
        book = await self._quotes(intent.instrument)
        opposite = book.best_ask if intent.side == "buy" else book.best_bid
        size = book.ask_size if intent.side == "buy" else book.bid_size
        if opposite is not None and _within(intent.side, opposite, intent.limit_price):
            self._fill(state, min(state.remaining, size or state.remaining),
                       intent.limit_price, maker=True)
            if state.remaining == 0:
                state.status = OrderStatus.FILLED

    def _fill(self, state: OrderState, qty: float, price: float, maker: bool) -> None:
        intent = state.intent
        state.fills.append(Fill(
            intent_id=intent.intent_id, order_id=state.order_id, instrument=intent.instrument,
            side=intent.side, qty=qty, price=price,
            fee=self._fee(intent.instrument, qty, price, maker), maker=maker,
            trade_id=f"{state.order_id}-t{len(state.fills) + 1}", ts=self._clock()))
        signed = qty if intent.side == "buy" else -qty
        size, avg = self._positions.get(intent.instrument, (0.0, 0.0))
        new = size + signed
        if size == 0 or (size > 0) == (signed > 0):
            avg = (size * avg + signed * price) / new
        elif (new > 0) != (size > 0) and new != 0:
            avg = price  # flipped through flat: the remainder opened at this price
        self._positions[intent.instrument] = (new, avg if new else 0.0)

    @staticmethod
    def _close(state: OrderState) -> OrderState:
        state.status = OrderStatus.FILLED if state.remaining == 0 else OrderStatus.CANCELLED
        return state

    @staticmethod
    def _reject(state: OrderState, reason: str) -> OrderState:
        state.status = OrderStatus.REJECTED
        state.reject_reason = reason
        return state


def _within(side: str, price: float, limit: float | None) -> bool:
    """Is ``price`` acceptable to a ``side`` order limited at ``limit``?"""
    if limit is None:
        return False
    return price <= limit if side == "buy" else price >= limit
