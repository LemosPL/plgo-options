from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm


# ---------------------------------------------------------------------------
# BS greeks helper
# ---------------------------------------------------------------------------
def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt: str) -> float:
    if T <= 0:
        return max(S - K, 0.0) if opt == "C" else max(K - S, 0.0)

    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if opt == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_vec(spots: np.ndarray, K: float, T: float, r: float, sigma: float, opt: str) -> np.ndarray:
    if T <= 0:
        return np.maximum(spots - K, 0.0) if opt == "C" else np.maximum(K - spots, 0.0)

    d1 = (np.log(spots / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if opt == "C":
        return spots * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - spots * norm.cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt: str) -> tuple[float, float, float, float, float]:
    if T <= 0 or sigma <= 0:
        price = max(S - K, 0.0) if opt == "C" else max(K - S, 0.0)
        delta = (1.0 if S > K else 0.0) if opt == "C" else (-1.0 if S < K else 0.0)
        return delta, 0.0, 0.0, 0.0, price

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = norm.pdf(d1)

    if opt == "C":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1.0

    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100.0
    theta = (
        -(S * pdf_d1 * sigma) / (2 * sqrtT)
        - r * K * math.exp(-r * T) * (norm.cdf(d2) if opt == "C" else norm.cdf(-d2))
    ) / 365.25

    return delta, gamma, theta, vega, price
