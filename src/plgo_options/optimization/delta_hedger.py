from __future__ import annotations

from dataclasses import dataclass

from .math_utils import bs_greeks
from .models import Position


@dataclass
class RehedgeDecision:
    """Whether the perp needs to trade right now to bring the book back
    within its delta band, and by how much."""
    net_option_delta: float
    perp_position: float
    mismatch: float
    band: float
    breached: bool
    trade_qty: float  # signed perp qty to trade; 0.0 if not breached


def net_option_delta(positions: list[Position], spot: float, r: float = 0.0) -> float:
    """Sum of qty x BS delta across every held C/P position, at the given spot.

    Perp/future legs (opt == "F") are excluded — those already carry delta 1:1
    by construction and are the instrument being used to OFFSET this figure,
    not part of what it measures.
    """
    total = 0.0
    for p in positions:
        opt = str(getattr(p, "opt", "") or "")
        if opt not in ("C", "P"):
            continue
        strike = float(p.strike)
        T = max(float(p.days_remaining), 0.0) / 365.25
        sigma = max(float(p.iv_pct or 0.0) / 100.0, 1e-6)
        qty = float(p.net_qty)
        delta, *_ = bs_greeks(spot, strike, T, r, sigma, opt)
        total += qty * delta
    return total


def check_rehedge(
        positions: list[Position],
        spot: float,
        perp_position: float,
        band: float,
        r: float = 0.0,
        extra_option_delta: float = 0.0,
) -> RehedgeDecision:
    """Compare net option delta + existing perp position against the band.

    ``mismatch`` is the book's unhedged delta right now (option delta not yet
    offset by the perp). A rehedge trades the perp back to fully flatten it
    ("trade to zero", the standard policy for this class of band-triggered
    control problem — see the (3c.nu^2 / 2.lambda)^(1/3) optimal-band result
    this codebase's band width (75 ETH) was calibrated against) rather than
    to the edge of the band itself.

    ``extra_option_delta`` folds in option delta not yet reflected as a
    Position — e.g. new option trades proposed in the same run (by a
    shape-fitting LP) that haven't actually executed and landed in the book
    yet, but should still count toward the mismatch this rehedge is sized to.
    """
    delta = net_option_delta(positions, spot, r=r) + extra_option_delta
    mismatch = delta + perp_position
    breached = abs(mismatch) > band
    trade_qty = -mismatch if breached else 0.0
    return RehedgeDecision(
        net_option_delta=delta,
        perp_position=perp_position,
        mismatch=mismatch,
        band=band,
        breached=breached,
        trade_qty=trade_qty,
    )


def perp_trade_cost(trade_qty: float, spot: float, cost_bps: float) -> float:
    """Execution cost of a single perp trade, in USD — flat bps of notional,
    the standard convention for perpetual futures (unlike the vega-based cost
    this codebase uses for options; a perp has no vega to price off of)."""
    return abs(trade_qty) * spot * cost_bps / 10_000.0
