"""work_order: one parent order, worked from passive to aggressive.

A parent ExecutionRequest becomes a sequence of child OrderIntents. Step 0
rests at the start price (our side's touch, or mid for thin option books);
each later step moves a fraction of the way to the far side; the last step
crosses with an IOC. Every child price is capped at ``limit_price`` - the
price the proposal was approved at - so the algo can fail to fill, but it
can never pay more than was approved.

Between steps the child rests for ``step_seconds``, then is polled and
cancelled; whatever filled counts and the next child works the remainder.
The kill switch and connector health are checked before every child and
after every wait. Child ids are ``<request_id>-<step>``, so re-running a
request after a crash resubmits nothing that already went out.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from .connector import Connector
from .models import BookTop, OrderIntent, OrderState, OrderStatus, Side

Outcome = Literal["filled", "partial", "unfilled", "halted", "connector_down", "rejected"]


@dataclass(frozen=True)
class ExecutionRequest:
    request_id: str
    proposal_id: str
    instrument: str
    side: Side
    qty: float
    limit_price: float  # worst acceptable price, from the approved proposal
    start: Literal["touch", "mid"] = "touch"
    steps: int = 5  # repricing steps after the first; the last one crosses
    step_seconds: float = 10.0
    tick_size: float = 0.05
    qty_step: float | None = None  # child qty rounded down to this (e.g. 1 USD on ETH-PERPETUAL)
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.limit_price <= 0 or self.tick_size <= 0 or self.steps < 1:
            raise ValueError(f"{self.request_id}: qty, limit_price, tick_size and steps "
                             "must be positive")


@dataclass
class ExecutionReport:
    request: ExecutionRequest
    outcome: Outcome
    children: list[OrderState] = field(default_factory=list)
    detail: str = ""

    @property
    def filled_qty(self) -> float:
        return sum(c.filled_qty for c in self.children)

    @property
    def avg_price(self) -> float | None:
        q = self.filled_qty
        return sum(c.filled_qty * (c.avg_price or 0) for c in self.children) / q if q else None

    @property
    def fees(self) -> float:
        return sum(c.fees for c in self.children)


def step_price(book: BookTop, req: ExecutionRequest, step: int) -> float | None:
    """The child price at ``step`` (0..req.steps), capped at the limit, on the tick grid.

    None when the book gives nothing to price from (no bid, no ask, no mark).
    """
    buy = req.side == "buy"
    near_touch = book.best_bid if buy else book.best_ask
    far = book.best_ask if buy else book.best_bid
    near = book.mid if req.start == "mid" else near_touch
    near = near if near is not None else book.mark
    far = far if far is not None else near
    if near is None or far is None:
        return None
    raw = near + (far - near) * step / req.steps
    capped = min(raw, req.limit_price) if buy else max(raw, req.limit_price)
    ticks = capped / req.tick_size
    # Round toward our own side so rounding never takes us past the limit.
    ticks = math.floor(ticks + 1e-9) if buy else math.ceil(ticks - 1e-9)
    return round(ticks * req.tick_size, 10)


async def _never() -> bool:
    return False


async def work_order(
        req: ExecutionRequest,
        connector: Connector,
        halted: Callable[[], Awaitable[bool]] = _never,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ExecutionReport:
    report = ExecutionReport(request=req, outcome="unfilled")

    async def stop_reason() -> Outcome | None:
        if await halted():
            return "halted"
        if not await connector.healthy():
            return "connector_down"
        return None

    for step in range(req.steps + 1):
        remaining = req.qty - report.filled_qty
        if req.qty_step:
            remaining = math.floor(remaining / req.qty_step + 1e-9) * req.qty_step
        if remaining <= 0:
            break
        if reason := await stop_reason():
            report.outcome = reason
            return report

        book = await connector.book(req.instrument)
        price = step_price(book, req, step)
        if price is None:
            await sleep(req.step_seconds)
            continue

        last = step == req.steps
        child = await connector.submit(OrderIntent(
            intent_id=f"{req.request_id}-{step}", proposal_id=req.proposal_id,
            instrument=req.instrument, side=req.side, qty=remaining, order_type="limit",
            limit_price=price, time_in_force="IOC" if last else "GTC",
            reduce_only=req.reduce_only))
        report.children.append(child)
        if child.status is OrderStatus.REJECTED:
            report.outcome = "rejected"
            report.detail = child.reject_reason or ""
            return report

        if not child.done:
            await sleep(req.step_seconds)
            reason = await stop_reason()
            # Cancel even when stopping: a halted bot must leave nothing resting.
            try:
                report.children[-1] = await connector.cancel(child.order_id)
            except Exception as exc:  # connector broken: say so loudly, never retry blind
                report.outcome = "connector_down"
                report.detail = f"cancel of {child.order_id} failed ({exc}); check resting orders"
                return report
            if reason:
                report.outcome = reason
                return report

    filled = report.filled_qty
    if req.qty - filled <= (req.qty_step or 0) * (1 - 1e-9) or math.isclose(filled, req.qty):
        report.outcome = "filled"
    elif filled > 0:
        report.outcome = "partial"
    return report
