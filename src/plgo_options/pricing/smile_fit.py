"""Smile fitting for Deribit option expiries, shared by the research scripts
(deribit_smile_scanner, smile_showcase) and the smile recorder.

    fit_forward     forward + discount factor from put-call parity
    implied, black, vega   scalar Black helpers (forward, discounted)
    book_frame      order books of one expiry -> one row per instrument, USD
    fit_svi         raw SVI fitted in price to a target, |price - target| / half-spread,
                    with Gatheral's butterfly density g(k) >= 0 enforced by penalty
"""

from __future__ import annotations

import datetime as dt
import re

import numpy as np
import pandas as pd
from scipy.optimize import brentq, least_squares
from scipy.stats import norm

MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)
MIN_DF_DAYS = 30     # shorter expiries: parity slope too noisy to pin DF, hold it at 1
K_RANGE = 2.5        # |log(K/F)| over which the no-arbitrage check runs
PENALTY = 50.0       # weight on negative density / variance in the fit's residuals


# ---------- Black (forward, discounted) ----------
def black(F, K, T, vol, cp, DF=1.0):
    sd = vol * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if cp == "C":
        return DF * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return DF * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def vega(F, K, T, vol, DF=1.0):  # USD per vol point
    sd = vol * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    return DF * F * norm.pdf(d1) * np.sqrt(T) / 100


def implied(price, F, K, T, cp, DF=1.0):
    intrinsic = DF * max(F - K, 0) if cp == "C" else DF * max(K - F, 0)
    if not np.isfinite(price) or price <= intrinsic + 1e-9:
        return np.nan
    try:
        return brentq(lambda v: black(F, K, T, v, cp, DF) - price, 1e-3, 8.0)
    except ValueError:
        return np.nan


def parse_exp(s):  # "27NOV26" -> 08:00 UTC
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", s)
    return dt.datetime(2000 + int(m[3]), MON[m[2]], int(m[1]), 8, tzinfo=dt.timezone.utc)


# ---------- forward + DF from parity ----------
def fit_forward(g):
    F0 = g.F0.iloc[0]
    c = g[g.cp == "C"].set_index("K")
    p = g[g.cp == "P"].set_index("K")
    Ks = c.index.intersection(p.index)
    Ks = [K for K in Ks if abs(np.log(K / F0)) < 0.2
          and np.isfinite(c.bid[K]) and np.isfinite(c.ask[K]) and np.isfinite(p.bid[K]) and np.isfinite(p.ask[K])]
    if len(Ks) < 3:
        return F0, 1.0, len(Ks), "underlying (few pairs)"
    Ks = np.array(Ks)
    y = np.array([(c.bid[K] + c.ask[K]) / 2 - (p.bid[K] + p.ask[K]) / 2 for K in Ks])
    spr = np.array([(c.ask[K] - c.bid[K]) + (p.ask[K] - p.bid[K]) for K in Ks])
    b, a = np.polyfit(Ks, y, 1, w=1 / np.maximum(spr, 1e-6))
    DF = -b
    if g["T"].iloc[0] * 365 < MIN_DF_DAYS or not 0.95 <= DF <= 1.0:  # hold DF at 1, refit F only
        F = float(np.average(y + Ks, weights=1 / np.maximum(spr, 1e-6) ** 2))
        return F, 1.0, len(Ks), "parity (DF held at 1)"
    return a / DF, DF, len(Ks), "parity"


# ---------- order books -> frame ----------
def book_frame(books: list[dict], expiry: str, now: dt.datetime) -> pd.DataFrame:
    exp = parse_exp(expiry)
    rows = []
    for b in books:
        _, _, K, cp = b["instrument_name"].split("-")
        S = b["estimated_delivery_price"]  # Deribit quotes options in ETH: USD = ETH price x S
        bid, ask = b.get("best_bid_price") or 0, b.get("best_ask_price") or 0
        rows.append(dict(K=float(K), cp=cp, T=(exp - now).total_seconds() / (365 * 86400),
                         bid=bid * S if bid else np.nan, ask=ask * S if ask else np.nan,
                         mark=b["mark_price"] * S, mark_iv=b["mark_iv"], F0=b["underlying_price"],
                         oi=b["open_interest"], vol24=(b.get("stats") or {}).get("volume") or 0.0,
                         bsize=b.get("best_bid_amount") or 0.0, asize=b.get("best_ask_amount") or 0.0,
                         spot=b["index_price"]))
    return pd.DataFrame(rows)


# ---------- raw SVI ----------
def svi_w(p, k):
    a, b, rho, m, s = p
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s * s))


def density(p, k):
    """Total variance w and Gatheral's g(k); g >= 0 everywhere = no butterfly arbitrage."""
    a, b, rho, m, s = p
    w = svi_w(p, k)
    w1 = b * (rho + (k - m) / np.sqrt((k - m) ** 2 + s * s))
    w2 = b * s * s / ((k - m) ** 2 + s * s) ** 1.5
    return w, (1 - k * w1 / (2 * w)) ** 2 - w1 ** 2 / 4 * (1 / w + 0.25) + w2 / 2


def black_vec(F, K, T, v, cp, DF=1.0):
    """Discounted Black prices, vectorised over strikes, vols and call/put flags."""
    K = np.asarray(K, float)
    s = v * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    call = DF * (F * norm.cdf(d1) - K * norm.cdf(d2))
    put = DF * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
    return np.where(np.asarray(cp) == "C", call, put)


def svi_iv(p, K, F, T):
    return np.sqrt(np.maximum(svi_w(p, np.log(np.asarray(K, float) / F)), 1e-9) / T)


def fit_svi(K, CP, target, half, F, T, DF, atm, x0=None) -> tuple[np.ndarray, bool]:
    """Raw SVI fitted in price: minimise |price - target| / half-spread with negative
    density or variance penalised. Best of 9 starts, or one start from x0 (warm start).
    Returns the parameters and whether g(k) >= 0 and w > 0 hold on |k| <= K_RANGE."""
    kg = np.linspace(-K_RANGE, K_RANGE, 801)

    def res(p):
        w, gk = density(p, kg)
        return np.concatenate([(black_vec(F, K, T, svi_iv(p, K, F, T), CP, DF) - target) / half,
                               np.minimum(gk, 0) * PENALTY, np.minimum(w, 0) * PENALTY])
    lb, ub = [-1, 1e-4, -0.999, -1, 1e-3], [1, 5, 0.999, 1, 2]
    starts = [x0] if x0 is not None else [[atm * atm * T * 0.3, 0.2, rho0, 0.0, s0]
                                          for rho0 in (-0.4, 0.0, 0.3) for s0 in (0.2, 0.5, 0.8)]
    best = None
    for x in starts:
        r = least_squares(res, np.clip(x, lb, ub), bounds=(lb, ub))
        if best is None or r.cost < best.cost:
            best = r
    w, gk = density(best.x, np.linspace(-K_RANGE, K_RANGE, 20001))
    return best.x, bool(gk.min() >= 0 and w.min() > 0)
