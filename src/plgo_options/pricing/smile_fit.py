"""Smile fitting for Deribit option expiries, shared by the research scripts
(deribit_smile_scanner, smile_showcase) and the smile recorder.

    fit_forward     forward + discount factor from put-call parity
    implied, black, vega   scalar Black helpers (forward, discounted)
    book_frame      order books of one expiry -> one row per instrument, USD
    fit_svi         raw SVI fitted in price to a target, |price - target| / half-spread,
                    with Gatheral's butterfly density g(k) >= 0 enforced by penalty
    fit_spline      cubic smoothing spline of implied vol (GCV), refitted with the same
                    butterfly penalty when it has negative density between the strikes
    otm_quotes      one expiry's two-sided out-of-the-money strikes: mids, half-spreads, mid vols
    spline_smile    the arbitrage-free spline through an expiry's OTM mid vols, fitted once
    spline_fair     one option's fair value from that spline (its own strike included)
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

import numpy as np
import pandas as pd
from scipy.interpolate import BSpline, make_smoothing_spline
from scipy.optimize import brentq, least_squares, minimize_scalar
from scipy.stats import norm

MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)
MIN_DF_DAYS = 30     # shorter expiries: parity slope too noisy to pin DF, hold it at 1
K_RANGE = 2.5        # |log(K/F)| over which the no-arbitrage check runs
PENALTY = 50.0       # weight on negative density / variance in the fit's residuals
G_FLOOR = 0.01       # the spline repair holds g above this margin, so the fine-grid check passes


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


def spline_density(spl, k, T):
    """Total variance w and Gatheral's g(k) of a spline in implied vol points, from its
    exact derivatives (a finite-difference g is wrong at the ends of the range)."""
    v, v1, v2 = spl(k) / 100, spl.derivative(1)(k) / 100, spl.derivative(2)(k) / 100
    w, w1, w2 = v * v * T, 2 * T * v * v1, 2 * T * (v1 * v1 + v * v2)
    return w, (1 - k * w1 / (2 * w)) ** 2 - w1 ** 2 / 4 * (1 / w + 0.25) + w2 / 2


def _gcv_lam(k, y, wt, spl) -> float:
    """make_smoothing_spline picks its smoothness by generalized cross-validation but does
    not return it: recover it as the lam whose spline reproduces the GCV curve."""
    yk = spl(k)

    def miss(ll):
        return np.sum(wt * (make_smoothing_spline(k, y, w=wt, lam=10.0 ** ll)(k) - yk) ** 2)
    grid = np.arange(-10, 4.01, 0.25)
    ll = grid[int(np.argmin([miss(x) for x in grid]))]
    return 10.0 ** minimize_scalar(miss, bounds=(ll - 0.25, ll + 0.25), method="bounded").x


def fit_spline(k, iv_pct, half_vp, T, arb_free: bool = True) -> tuple[BSpline, bool, bool]:
    """Cubic smoothing spline of implied vol (vol points) against log-moneyness k (sorted),
    weights 1 / half-spread^2 in vol points, smoothness by GCV.

    The spline follows the mids, and wide one-sided quotes in the wings can have mids that
    are themselves arbitraged (a call mid above a lower strike's). With arb_free, a spline
    with negative density between the first and last strike is refitted: same objective
    (weighted squared error + lam x integral of f''^2, lam from the GCV fit) plus the SVI
    fit's butterfly penalty, on the same knots, starting from the GCV coefficients.
    Returns the spline, whether it was repaired, and whether g >= 0 and w > 0 hold."""
    k, y, half_vp = np.asarray(k, float), np.asarray(iv_pct, float), np.asarray(half_vp, float)
    wt = 1 / half_vp ** 2
    spl = make_smoothing_spline(k, y, w=wt)
    kc = np.linspace(k[0], k[-1], 8001)

    def ok(s):
        w, g = spline_density(s, kc, T)
        return bool(w.min() > 0 and g.min() >= 0)
    if not arb_free or ok(spl):
        return spl, False, ok(spl)
    kg = np.linspace(k[0], k[-1], 401)
    rough = np.sqrt(_gcv_lam(k, y, wt, spl) * (kg[1] - kg[0]))   # sum of squares ~ lam * integral f''^2
    t, eye = spl.t, np.eye(len(spl.c))

    def basis(x, n):  # column i: n-th derivative of the i-th B-spline at x
        return np.column_stack([BSpline(t, e, 3).derivative(n)(x) if n else BSpline(t, e, 3)(x) for e in eye])
    Bk, B2g = basis(k, 0), basis(kg, 2)                  # vol points
    B = [basis(kc, n) / 100 for n in range(3)]           # v, v', v'' as fractions

    def parts(c):
        v, v1, v2 = (b @ c for b in B)
        w, w1, w2 = v * v * T, 2 * T * v * v1, 2 * T * (v1 * v1 + v * v2)
        a = 1 - kc * w1 / (2 * w)
        g = a * a - w1 * w1 / 4 * (1 / w + 0.25) + w2 / 2
        return v, v1, v2, w, w1, a, g

    def res(c):
        _, _, _, w, _, _, g = parts(c)
        return np.concatenate([(Bk @ c - y) / half_vp, rough * (B2g @ c),
                               np.minimum(g - G_FLOOR, 0) * PENALTY, np.minimum(w, 0) * PENALTY])

    def jac(c):
        v, v1, v2, w, w1, a, g = parts(c)
        dw = 2 * T * v[:, None] * B[0]
        dw1 = 2 * T * (v1[:, None] * B[0] + v[:, None] * B[1])
        dw2 = 2 * T * (2 * v1[:, None] * B[1] + v2[:, None] * B[0] + v[:, None] * B[2])
        gw = a * kc * w1 / w ** 2 + w1 * w1 / (4 * w * w)
        gw1 = -a * kc / w - w1 / 2 * (1 / w + 0.25)
        dg = gw[:, None] * dw + gw1[:, None] * dw1 + 0.5 * dw2
        return np.vstack([Bk / half_vp[:, None], rough * B2g,
                          (g < G_FLOOR)[:, None] * dg * PENALTY, (w < 0)[:, None] * dw * PENALTY])
    fixed = BSpline(t, least_squares(res, spl.c, jac=jac).x, 3)
    return fixed, True, ok(fixed)


def otm_quotes(expiry: str, books: list[dict], now: dt.datetime, min_strikes: int = 8) -> dict | None:
    """The two-sided out-of-the-money strikes of one expiry (put below the parity
    forward, call above), with mids, half-spreads and mid vols. None if too few."""
    g = book_frame(books, expiry, now)
    F, DF, _, _ = fit_forward(g)
    T = float(g["T"].iloc[0])
    o = g[(g.cp == "C") == (g.K >= F)].sort_values("K")
    o = o[np.isfinite(o.bid) & np.isfinite(o.ask) & (o.ask > o.bid)]
    ivb = np.array([implied(r.bid, F, r.K, T, r.cp, DF) for r in o.itertuples()])
    iva = np.array([implied(r.ask, F, r.K, T, r.cp, DF) for r in o.itertuples()])
    ok = np.isfinite(ivb) & np.isfinite(iva)
    o = o[ok]
    if len(o) < min_strikes:
        return None
    mid = ((o.bid + o.ask) / 2).to_numpy()
    iv_mid = np.array([implied(m_, F, r.K, T, r.cp, DF) for m_, r in zip(mid, o.itertuples())])
    return dict(K=o.K.to_numpy(), CP=o.cp.to_list(), mid=mid, iv_mid=iv_mid,
                half=((o.ask - o.bid) / 2).to_numpy(), mark=o.mark.to_numpy(), F=F, T=T, DF=DF,
                spot=float(g.spot.median()), atm=float(np.median((ivb[ok] + iva[ok]) / 2)))


def spline_smile(q: dict, drop: float | None = None) -> tuple[Any, tuple[float, float], bool]:
    """The arbitrage-free smoothing spline through otm_quotes' mid vols, in vol points
    against k = log(K/F), every strike included unless ``drop`` is given:
    (spline, (k of the lowest strike, k of the highest), arb-free)."""
    keep = q["K"] != drop
    K, iv = q["K"][keep], q["iv_mid"][keep]
    vg = np.array([vega(q["F"], k_, q["T"], v, q["DF"]) for k_, v in zip(K, iv)])
    k = np.log(K / q["F"])
    spl, _, free = fit_spline(k, iv * 100, q["half"][keep] / vg, q["T"])
    return spl, (float(k[0]), float(k[-1])), free


def spline_fair(q: dict, K0: float, cp: str, smile: tuple | None = None, loo: bool = False) -> dict:
    """Fair value of (K0, cp) from the arbitrage-free smoothing spline through the OTM
    mid vols of every strike, K0's own quote included: USD price, vol (fraction), vega
    (USD per vol point), whether K0 lies inside the fitted strikes (outside, the spline
    extrapolates) and whether it is arb-free. ``smile``: spline_smile(q) already fitted,
    shared by the expiry's strikes. ``loo``: refit without K0 instead (leave-one-out)."""
    spl, (lo, hi), free = spline_smile(q, K0) if loo else smile or spline_smile(q)
    k0 = math.log(K0 / q["F"])
    v0 = max(float(spl(k0)) / 100, 1e-4)
    return dict(fair=float(black_vec(q["F"], [K0], q["T"], v0, [cp], q["DF"])[0]), iv=v0,
                vega=float(vega(q["F"], K0, q["T"], v0, q["DF"])), inside=bool(lo <= k0 <= hi),
                arb_free=free)
