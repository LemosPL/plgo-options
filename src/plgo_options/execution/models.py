"""Interface contract v1 between the trading logic and the Deribit connector.

Quantities are in the instrument's own Deribit units (``amount``): USD for the
inverse ETH-PERPETUAL, ETH for options and the linear USDC perp. Prices are in
the instrument's quote currency: USD for perps, ETH for ETH options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

CONTRACT_VERSION = "v1"

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market", "post_only"]
TimeInForce = Literal["GTC", "IOC", "good_til_day"]


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"  # may carry a partial fill
    REJECTED = "rejected"


@dataclass(frozen=True)
class OrderIntent:
    """One order the logic asks the connector to place.

    ``intent_id`` is set by the logic and makes a resend safe: the connector
    returns the existing order for an id it has already seen instead of
    placing a second one (on Deribit it travels as the order ``label``).
    """

    intent_id: str
    proposal_id: str
    instrument: str
    side: Side
    qty: float
    order_type: OrderType = "limit"
    limit_price: float | None = None
    max_slippage_bps: float | None = None
    time_in_force: TimeInForce = "GTC"
    reduce_only: bool = False
    version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"{self.intent_id}: qty must be positive, got {self.qty}")
        if len(self.intent_id) > 64:
            raise ValueError(f"{self.intent_id}: intent_id over Deribit's 64-char label limit")
        if self.order_type in ("limit", "post_only") and self.limit_price is None:
            raise ValueError(f"{self.intent_id}: {self.order_type} order needs a limit_price")
        if self.order_type == "market" and self.max_slippage_bps is None:
            raise ValueError(f"{self.intent_id}: market order needs max_slippage_bps")
        if self.order_type == "post_only" and self.time_in_force == "IOC":
            raise ValueError(f"{self.intent_id}: post_only cannot be IOC")


@dataclass(frozen=True)
class Fill:
    intent_id: str
    order_id: str
    instrument: str
    side: Side
    qty: float
    price: float
    fee: float
    maker: bool
    trade_id: str
    ts: float


@dataclass
class OrderState:
    """Where an intent stands. Returned by every connector call that touches an order."""

    order_id: str
    intent: OrderIntent
    status: OrderStatus
    fills: list[Fill] = field(default_factory=list)
    reject_reason: str | None = None

    @property
    def filled_qty(self) -> float:
        return sum(f.qty for f in self.fills)

    @property
    def remaining(self) -> float:
        return max(self.intent.qty - self.filled_qty, 0.0)

    @property
    def avg_price(self) -> float | None:
        q = self.filled_qty
        return sum(f.qty * f.price for f in self.fills) / q if q else None

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def done(self) -> bool:
        return self.status is not OrderStatus.OPEN


@dataclass(frozen=True)
class BookTop:
    """Top of the order book. A missing side is None (thin option books often have one)."""

    instrument: str
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    mark: float | None = None
    ts: float = 0.0

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.mark


@dataclass(frozen=True)
class Position:
    instrument: str
    size: float  # signed, instrument units
    avg_price: float
    delta: float | None = None  # as Deribit reports it


@dataclass(frozen=True)
class AccountState:
    currency: str
    equity: float
    initial_margin: float
    maintenance_margin: float
    available_funds: float
