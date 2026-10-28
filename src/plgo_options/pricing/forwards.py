"""Cash-settled forwards: fair value, MTM and risk.

A forward fixes today the price K at which ``qty`` tokens are exchanged at
maturity; cash-settled, so at maturity only qty x (fixing - K) changes hands,
in USD/USDC. Before then it is marked against the fair forward

    F = spot x (1 + carry x T),   T = days to maturity / 365

with ``carry`` an annual rate set per asset (0 = forward at spot, the same
zero-rate assumption the options book uses). Signed so that a forward we
BOUGHT gains when spot rises:

    MTM   = sign x qty x (F - K)
    delta = sign x qty x (1 + carry x T)      tokens
    theta = -sign x qty x spot x carry / 365  $ per day (the carry rolling off)

No gamma or vega: the payoff is linear in spot.
"""
from __future__ import annotations

from datetime import date


def years_to(maturity: date, today: date) -> float:
    return max((maturity - today).days, 0) / 365.0


def side_sign(side: str) -> float:
    s = str(side or "").strip().lower()
    if s in ("buy", "long", "b"):
        return 1.0
    if s in ("sell", "short", "s"):
        return -1.0
    raise ValueError(f"side must be buy or sell, not {side!r}")


def fair_forward(spot: float, carry: float, T: float) -> float:
    return spot * (1.0 + carry * T)


def value(side: str, qty: float, forward_price: float, spot: float, carry: float, T: float) -> dict:
    """MTM and risk of one forward at this spot."""
    sgn = side_sign(side)
    F = fair_forward(spot, carry, T)
    delta = sgn * qty * (1.0 + carry * T)
    return {
        "fair_forward": F,
        "mtm": sgn * qty * (F - forward_price),
        "delta": delta,
        "delta_usd": delta * spot,
        "theta": -sgn * qty * spot * carry / 365.0 if T > 0 else 0.0,
        "notional_usd": qty * forward_price,
    }


def settlement_pnl(side: str, qty: float, forward_price: float, fixing: float) -> float:
    """Cash paid to us (+) or by us (-) at maturity."""
    return side_sign(side) * qty * (fixing - forward_price)
