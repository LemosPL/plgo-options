"""Option payoff and Black-Scholes pricing utilities."""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "C",
) -> float:
    """Black-Scholes European option price."""
    if T <= 0:
        return max(S - K, 0.0) if option_type == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def payoff(
    spot_range: np.ndarray,
    strike: float,
    option_type: str,
    premium: float,
    quantity: float = 1.0,
    is_long: bool = True,
) -> np.ndarray:
    """Compute P&L at expiry for a single option leg."""
    direction = 1.0 if is_long else -1.0
    if option_type == "PERP":
        return direction * quantity * (spot_range - strike)
    if option_type == "C":
        intrinsic = np.maximum(spot_range - strike, 0.0)
    else:
        intrinsic = np.maximum(strike - spot_range, 0.0)
    return direction * quantity * (intrinsic - premium)


def strategy_payoff(
    spot_range: np.ndarray,
    legs: list[dict],
) -> np.ndarray:
    """
    Sum payoffs of multiple legs.
    Each leg: {"strike": float, "type": "C"|"P", "premium": float,
               "quantity": float, "is_long": bool}
    """
    total = np.zeros_like(spot_range, dtype=float)
    for leg in legs:
        total += payoff(
            spot_range,
            strike=leg["strike"],
            option_type=leg["type"],
            premium=leg["premium"],
            quantity=leg.get("quantity", 1.0),
            is_long=leg.get("is_long", True),
        )
    return total