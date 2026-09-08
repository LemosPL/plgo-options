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


def bs_vec_bridge(
    S0: float, spots: np.ndarray, K: float, dte_days: float, h_days: float, sigma: float, opt: str,
) -> np.ndarray:
    """Vectorized option value at horizon day `h_days`, given today's spot S0
    and a hypothetical spot `spots` AT day `h_days` (r=0, matching bs_vec).

    If the option is still alive at the pillar (dte_days >= h_days), this is
    an exact Markov reprice — identical to bs_vec at the option's own
    residual time-to-expiry. Nothing about the path before h_days matters
    once you condition on the spot AT h_days.

    If the option has already expired by the pillar (dte_days < h_days), a
    plain intrinsic-at-`spots` snap implicitly assumes spot was already at
    its day-h_days value back on the option's own expiry date — i.e. that
    the path was frozen from expiry to the pillar. Instead this prices the
    conditional expectation of the payoff at the option's TRUE expiry,
    conditioned on both S0 today and `spots` at day h_days: a Brownian
    bridge on log-spot (GBM). The conditional distribution of
    log(S_dte) | S_0, S_{h_days} is Normal with

        mean = ln(S0) + (dte/h_days) * (ln(spots) - ln(S0))
        var  = sigma^2 * dte * (h_days - dte) / h_days      [dte, h_days in years]

    (variance vanishes at both endpoints and peaks mid-bridge — pinning the
    path at both ends necessarily reduces uncertainty at every point between
    them). The conditional expected payoff then has the usual BS closed
    form, with forward S_eff = exp(mean + var/2) and total variance `var`.
    """
    if dte_days >= h_days or h_days <= 0:
        T_h = max(dte_days - h_days, 0.0) / 365.25
        return bs_vec(spots, K, T_h, 0.0, sigma, opt)

    if dte_days <= 0:
        # Already resolved as of today (dte<=0) — the bridge's near end is
        # pinned at S0 itself, so the payoff is a known constant, not a
        # function of the hypothetical pillar spot at all.
        payoff = max(S0 - K, 0.0) if opt == "C" else max(K - S0, 0.0)
        return np.full_like(spots, payoff, dtype=float)

    t_y = dte_days / 365.25
    T_y = h_days / 365.25
    var_log = sigma * sigma * t_y * (T_y - t_y) / T_y
    mean_log = math.log(S0) + (t_y / T_y) * (np.log(spots) - math.log(S0))

    if var_log <= 0 or sigma <= 0:
        s_t = np.exp(mean_log)
        return np.maximum(s_t - K, 0.0) if opt == "C" else np.maximum(K - s_t, 0.0)

    sqrt_v = math.sqrt(var_log)
    s_eff = np.exp(mean_log + 0.5 * var_log)
    d1 = (np.log(s_eff / K) + 0.5 * var_log) / sqrt_v
    d2 = d1 - sqrt_v

    if opt == "C":
        return s_eff * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - s_eff * norm.cdf(-d1)


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
