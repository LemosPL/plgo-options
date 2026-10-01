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
    this codebase's band width was originally calibrated against) rather than
    to the edge of the band itself.

    ``band`` is in the underlying's own token units, same as ``mismatch`` —
    this function is asset-agnostic. Callers on a book that spans assets with
    very different per-token prices (ETH vs FIL, say) should derive it from a
    dollar target divided by spot rather than pass a fixed token count, or the
    same nominal band means wildly different real risk tolerance on each book
    (see optimizer_v3._build_delta_rehedge_trades' delta_band_usd).

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


@dataclass
class CounterpartyRehedgePlan:
    """Per-counterparty split of a breached rehedge: who trades what."""
    decision: RehedgeDecision            # the whole-book check that gated it
    mismatch_by_cp: dict[str, float]     # each counterparty's own unhedged delta
    legs: dict[str, float]               # signed perp qty to trade, per counterparty
    residual: float                      # book mismatch left after the legs trade


def plan_counterparty_rehedge(
        option_delta_by_cp: dict[str, float],
        perp_by_cp: dict[str, float],
        band: float,
        min_leg: float = 0.0,
        eligible=None,
) -> CounterpartyRehedgePlan:
    """Split a delta rehedge across counterparties so each hedge nets against
    the options it offsets.

    The trigger is unchanged — the WHOLE book's mismatch (every counterparty's
    option delta plus every perp, wherever held) against ``band``, so a book
    whose counterparties offset each other is left alone. Once breached, each
    eligible counterparty trades a perp that flattens its OWN mismatch to zero
    (option delta + perp already held there): that is the leg that shrinks its
    collateral exposure, rather than one hedge on a venue nothing nets against.
    A venue holding a perp but no options (the exchange) has the perp as its
    whole mismatch, so the same rule unwinds it as the counterparty hedges take
    over.

    ``eligible`` (keys, or None for all) limits which counterparties may trade
    — the run's counterparty scope. Legs smaller than ``min_leg`` tokens are
    skipped rather than traded for noise. All keys must already be normalized
    (the caller lower-cases counterparty names); units are underlying tokens.
    """
    keys = set(option_delta_by_cp) | set(perp_by_cp)
    mismatch_by_cp = {
        k: float(option_delta_by_cp.get(k, 0.0)) + float(perp_by_cp.get(k, 0.0))
        for k in keys
    }
    book_mismatch = sum(mismatch_by_cp.values())
    breached = abs(book_mismatch) > band
    decision = RehedgeDecision(
        net_option_delta=sum(option_delta_by_cp.values()),
        perp_position=sum(perp_by_cp.values()),
        mismatch=book_mismatch,
        band=band,
        breached=breached,
        trade_qty=-book_mismatch if breached else 0.0,
    )
    legs: dict[str, float] = {}
    if breached:
        allowed = None if eligible is None else set(eligible)
        for k, m in mismatch_by_cp.items():
            if allowed is not None and k not in allowed:
                continue
            if abs(m) < max(min_leg, 1e-9):
                continue
            legs[k] = -m
    return CounterpartyRehedgePlan(
        decision=decision,
        mismatch_by_cp=mismatch_by_cp,
        legs=legs,
        residual=book_mismatch + sum(legs.values()),
    )
