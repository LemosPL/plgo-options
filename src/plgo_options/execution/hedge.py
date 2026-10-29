"""Phase 1: turn a delta_hedger.RehedgeDecision into a perp ExecutionRequest.

The decision's ``trade_qty`` is in ETH; Deribit sizes each perp in its own
units, so the qty is converted here and nowhere else. The limit is the book
mid plus ``max_slippage_bps``: the most the hedge may cost relative to where
the market stood when the decision was made.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..optimization.delta_hedger import RehedgeDecision
from .algo import ExecutionRequest
from .models import BookTop


@dataclass(frozen=True)
class PerpSpec:
    unit: str  # "usd": amount is USD notional (inverse); "eth": amount is ETH (linear)
    qty_step: float
    tick_size: float


# Check against public/get_instruments (contract_size, min_trade_amount,
# tick_size) before the first live run; Deribit changes these occasionally.
PERP_SPECS: dict[str, PerpSpec] = {
    "ETH-PERPETUAL": PerpSpec(unit="usd", qty_step=1.0, tick_size=0.05),
}


def perp_amount(instrument: str, eth_qty: float, price: float) -> float:
    """ETH quantity -> the instrument's Deribit amount, rounded down to its step."""
    spec = PERP_SPECS[instrument]
    raw = eth_qty * price if spec.unit == "usd" else eth_qty
    return math.floor(raw / spec.qty_step + 1e-9) * spec.qty_step


def rehedge_request(
        decision: RehedgeDecision,
        book: BookTop,
        *,
        request_id: str,
        proposal_id: str,
        instrument: str = "ETH-PERPETUAL",
        max_slippage_bps: float = 10.0,
        steps: int = 3,
        step_seconds: float = 5.0,
) -> ExecutionRequest | None:
    """None when there is nothing to trade (inside the band, or below one qty step)."""
    if not decision.breached or decision.trade_qty == 0:
        return None
    ref = book.mid
    if ref is None:
        raise ValueError(f"{instrument}: no bid/ask/mark to price the hedge from")
    spec = PERP_SPECS[instrument]
    side = "buy" if decision.trade_qty > 0 else "sell"
    qty = perp_amount(instrument, abs(decision.trade_qty), ref)
    if qty <= 0:
        return None
    slip = max_slippage_bps / 1e4
    # Only flag reduce_only when the trade shrinks the existing perp without flipping it.
    shrinks = decision.perp_position * decision.trade_qty < 0
    reduce_only = shrinks and abs(decision.trade_qty) <= abs(decision.perp_position)
    return ExecutionRequest(
        request_id=request_id, proposal_id=proposal_id, instrument=instrument, side=side,
        qty=qty, limit_price=ref * (1 + slip) if side == "buy" else ref * (1 - slip),
        start="touch", steps=steps, step_seconds=step_seconds, tick_size=spec.tick_size,
        qty_step=spec.qty_step, reduce_only=reduce_only)
