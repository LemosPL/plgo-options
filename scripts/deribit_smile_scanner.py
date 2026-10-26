"""Deribit smile scanner: spread-weighted smoothing spline per expiry, flags
options quoted outside their neighbours' fair value.

Internal research prototype, not wired into the app. Hits Deribit's public
market-data endpoints only (no API key) and prints a report. It places no orders.

Per expiry:
    1) Forward + discount factor from put-call parity: weighted regression of
       (C - P) in USD on K, near-the-money strikes where both sides are quoted.
       C - P = DF * (F - K), so slope = -DF and intercept = DF * F.
       Reported as r = -ln(DF)/T and repo/carry q = r - ln(F/S)/T.
    2) OTM side only (calls K >= F, puts K < F): bid/ask/mid Black vols.
       Smoothing spline of total variance w = vol^2 T against k = ln(K/F),
       weighted by 1 / half-spread (in total-variance units, i.e. spread in vol
       points, not dollars), weights capped at WCAP x median.
       Smoothing target s = smooth * N: residuals ~ one half-spread on average.
    3) Fair value for each strike is leave-one-out (refit without that strike),
       so a mispriced quote can't pull the curve onto itself. Wing endpoints
       have no LOO fit and are not signalled.
    4) Signal when the quote is through fair by more than fees + min_edge:
       buy  if ask + fee < fair - min_edge * vega
       sell if bid - fee > fair + min_edge * vega
       ITM options are priced off the same smile (parity), so they're scanned too,
       down to |delta| 0.85 (deeper is a forward bet, not a vol one).

Checks (reported, not enforced): butterfly (call price convex in K on the
fitted curve) and calendar (total variance non-decreasing in T at fixed k).

Simplifications:
    - top-of-book only: book summary has no sizes, so nothing about depth
    - inverse options: USD value = ETH price x index at fetch time
    - Deribit option fee 0.0003 ETH per contract (maker and taker, per
      get_instruments), capped at 12.5% of premium
    - a quote through fair is not necessarily stale: check news, the
      neighbouring expiries and Deribit's own mark before acting

Usage:
    python scripts/deribit_smile_scanner.py
    python scripts/deribit_smile_scanner.py --currency ETH --min-edge 0.5 --out /tmp/scan
"""
import argparse
import datetime as dt
import os
import re

import httpx
import numpy as np
import pandas as pd
from scipy.interpolate import UnivariateSpline
from scipy.optimize import brentq
from scipy.stats import norm

API = "https://www.deribit.com/api/v2/public"
MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)
FEE_ETH, FEE_CAP = 0.0003, 0.125
WCAP = 4.0           # cap on weight relative to the median weight in an expiry
MAX_SPREAD_VOL = 10  # quotes wider than this (vol points) are left out of the fit
MIN_POINTS = 6
MIN_DF_DAYS = 30     # shorter expiries: parity slope too noisy to pin DF, hold it at 1
TICK_ETH = 0.0001    # Deribit option tick below 0.005 ETH (0.0005 above)
MIN_TICKS = 5        # quotes under 5 ticks are tick noise, not mispricing
MIN_VEGA = 0.5       # USD per vol point per contract
MAX_ABS_DELTA = 0.85 # deeper ITM is a bet on the parity forward, not on vol


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


# ---------- data ----------
def parse_exp(s):  # "27NOV26" -> 08:00 UTC
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", s)
    return dt.datetime(2000 + int(m[3]), MON[m[2]], int(m[1]), 8, tzinfo=dt.timezone.utc)


def fetch(currency):
    with httpx.Client(timeout=30) as c:
        rows = c.get(f"{API}/get_book_summary_by_currency", params={"currency": currency, "kind": "option"}).json()["result"]
        spot = c.get(f"{API}/get_index_price", params={"index_name": f"{currency.lower()}_usd"}).json()["result"]["index_price"]
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for r in rows:
        _, e, K, cp = r["instrument_name"].split("-")
        exp = parse_exp(e)
        S = r["estimated_delivery_price"]
        out.append(dict(inst=r["instrument_name"], expiry=e, exp=exp, T=(exp - now).total_seconds() / (365 * 86400),
                        K=float(K), cp=cp, S=S,
                        bid=r["bid_price"] * S if r["bid_price"] else np.nan,
                        ask=r["ask_price"] * S if r["ask_price"] else np.nan,
                        mark=r["mark_price"] * S, mark_iv=r["mark_iv"], F0=r["underlying_price"],
                        oi=r["open_interest"], vol24=r["volume"]))
    return pd.DataFrame(out), spot, now


# ---------- step 1: forward + DF from parity ----------
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


# ---------- step 2: smile points ----------
def smile_points(g, F, DF):
    T = g["T"].iloc[0]
    pts = []
    for r in g.itertuples():
        if (r.cp == "C") != (r.K >= F):  # OTM side only
            continue
        ivb = implied(r.bid, F, r.K, T, r.cp, DF)
        iva = implied(r.ask, F, r.K, T, r.cp, DF)
        if not (np.isfinite(ivb) and np.isfinite(iva)) or iva <= ivb:
            continue
        spread_vol = (iva - ivb) * 100
        if spread_vol > MAX_SPREAD_VOL:
            continue
        mid = (ivb + iva) / 2
        hs_w = mid * T * (iva - ivb)  # half-spread in total variance: dw = 2 vol T dvol, dvol = spread/2
        pts.append(dict(K=r.K, k=np.log(r.K / F), w=mid * mid * T, hs_w=hs_w, iv_mid=mid, spread_vol=spread_vol))
    return pd.DataFrame(pts).sort_values("k").reset_index(drop=True) if pts else pd.DataFrame()


def spline(k, w, wt, smooth):
    return UnivariateSpline(k, w, w=wt, k=3, s=smooth * len(k), ext=3)


def fit_smile(pts, smooth):
    wt = 1 / pts.hs_w.values
    wt = np.minimum(wt, WCAP * np.median(wt))
    k, w = pts.k.values, pts.w.values
    full = spline(k, w, wt, smooth)
    loo = np.full(len(k), np.nan)
    for i in range(1, len(k) - 1):  # interior only: endpoints would be pure extrapolation
        m = np.arange(len(k)) != i
        if m.sum() > 4:
            loo[i] = spline(k[m], w[m], wt[m], smooth)(k[i])
    return full, loo, wt


# ---------- checks ----------
def butterfly_ok(sp, F, T, DF, kmin, kmax):
    """Density = d2(price)/dK2 >= 0. Differenced on OTM prices (puts below F, calls above),
    each side separately: deep-ITM prices lose the density to cancellation noise."""
    bad, peak = [], 0.0
    for cp, lo, hi in (("P", kmin, min(kmax, 0.0)), ("C", max(kmin, 0.0), kmax)):
        if hi - lo < 1e-6:
            continue
        Ks = F * np.exp(np.linspace(lo, hi, 120))
        px = np.array([black(F, K, T, np.sqrt(max(sp(np.log(K / F)), 1e-8) / T), cp, DF) for K in Ks])
        h = np.diff(Ks)
        d2 = 2 * (px[2:] / (h[1:] * (h[:-1] + h[1:])) - px[1:-1] / (h[:-1] * h[1:]) + px[:-2] / (h[:-1] * (h[:-1] + h[1:])))
        peak = max(peak, d2.max())
        bad += list(zip(Ks[1:-1], d2))
    bad_K = [K for K, d in bad if d < -1e-4 * peak]  # tolerance: rounding in the grid
    return (True, None) if not bad_K else (False, (min(bad_K), max(bad_K)))


def calendar_issues(fits):
    issues = []
    exps = sorted(fits, key=lambda e: fits[e]["T"])
    for e1, e2 in zip(exps, exps[1:]):
        a, b = fits[e1], fits[e2]
        lo, hi = max(a["kmin"], b["kmin"]), min(a["kmax"], b["kmax"])
        if lo >= hi:
            continue
        ks = np.linspace(lo, hi, 50)
        bad = ks[b["sp"](ks) < a["sp"](ks)]
        if len(bad):
            issues.append(f"{e1}->{e2}: w decreases on k in [{bad.min():+.2f}, {bad.max():+.2f}]")
    return issues


# ---------- run ----------
def scan(currency="ETH", smooth=1.0, min_edge=0.25, min_days=2.0):
    df, spot, now = fetch(currency)
    df = df[df["T"] > min_days / 365]
    fits, summary, signals = {}, [], []
    for e, g in df.groupby("expiry"):
        T = g["T"].iloc[0]
        F, DF, npairs, how = fit_forward(g)
        pts = smile_points(g, F, DF)
        if len(pts) < MIN_POINTS:
            summary.append(dict(expiry=e, T_days=T * 365, F=F, note=f"only {len(pts)} usable strikes"))
            continue
        sp, loo, wt = fit_smile(pts, smooth)
        resid_vol = (pts.iv_mid - np.sqrt(np.maximum(sp(pts.k.values), 1e-8) / T)) * 100
        fly_ok, fly_where = butterfly_ok(sp, F, T, DF, pts.k.min(), pts.k.max())
        fits[e] = dict(sp=sp, T=T, kmin=pts.k.min(), kmax=pts.k.max())
        r = -np.log(DF) / T
        summary.append(dict(expiry=e, T_days=T * 365, F=F, F_deribit=g.F0.iloc[0], DF=DF, r=r,
                            q=r - np.log(F / spot) / T, fwd_from=f"{how}, {npairs} pairs", n=len(pts),
                            rms_resid_vol=float(np.sqrt(np.average(resid_vol ** 2, weights=wt))),
                            atm_vol=float(np.sqrt(sp(0.0) / T) * 100),
                            butterfly="ok" if fly_ok else f"NEG density K {fly_where[0]:.0f}-{fly_where[1]:.0f}"))
        fair_w = dict(zip(pts.K, loo))
        for o in g.itertuples():
            fw = fair_w.get(o.K, np.nan)  # LOO fair at strikes in the fit; in-curve otherwise
            k = np.log(o.K / F)
            edge_pt = np.isnan(fw)
            if edge_pt:
                if not pts.k.iloc[0] < k < pts.k.iloc[-1]:
                    continue  # outside quoted range or a wing endpoint: no neighbours on one side
                fw = float(sp(k))
            vol = np.sqrt(max(fw, 1e-8) / T)
            fair = black(F, o.K, T, vol, o.cp, DF)
            vg = vega(F, o.K, T, vol, DF)
            delta = norm.cdf((np.log(F / o.K) + 0.5 * vol * vol * T) / (vol * np.sqrt(T))) - (o.cp == "P")
            if vg < MIN_VEGA or abs(delta) > MAX_ABS_DELTA:
                continue
            for side, px in (("BUY", o.ask), ("SELL", o.bid)):
                if not np.isfinite(px) or px < MIN_TICKS * TICK_ETH * o.S:
                    continue
                fee = min(FEE_ETH * o.S, FEE_CAP * px)
                edge = (fair - px - fee) if side == "BUY" else (px - fair - fee)
                if edge / vg >= min_edge:
                    signals.append(dict(inst=o.inst, side=side, px=px, fair=fair, edge_usd=edge, edge_vol=edge / vg,
                                        fair_vol=vol * 100, quote_vol=implied(px, F, o.K, T, o.cp, DF) * 100,
                                        mark_vol=o.mark_iv, delta=delta,
                                        vega=vg, oi=o.oi, vol24=o.vol24, in_fit=not edge_pt))
    summ = pd.DataFrame(summary).sort_values("T_days")
    sig = pd.DataFrame(signals)
    if len(sig):
        sig = sig.sort_values("edge_vol", ascending=False)
    return summ, sig, calendar_issues(fits), spot, now


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--currency", default="ETH")
    ap.add_argument("--smooth", type=float, default=1.0, help="spline target: mean weighted residual^2 (1 = one half-spread)")
    ap.add_argument("--min-edge", type=float, default=0.25, help="edge after fees, in vol points")
    ap.add_argument("--min-days", type=float, default=2.0)
    ap.add_argument("--out", help="directory to write summary.csv / signals.csv")
    a = ap.parse_args()
    summ, sig, cal, spot, now = scan(a.currency, a.smooth, a.min_edge, a.min_days)
    pd.set_option("display.width", 250, "display.max_columns", 30, "display.max_rows", 200)
    print(f"{a.currency} index {spot:.2f} at {now:%Y-%m-%d %H:%M} UTC\n")
    print("Per expiry (rms_resid_vol: weighted, vol points)")
    print(summ.round(dict(T_days=1, F=2, F_deribit=2, DF=5, r=4, q=4, rms_resid_vol=2, atm_vol=2)).to_string(index=False))
    print("\nCalendar check:", "ok" if not cal else "")
    for c in cal:
        print("  " + c)
    print(f"\nQuotes through LOO fair by >= {a.min_edge} vol pts after fees: {len(sig)}")
    if len(sig):
        print(sig.round(dict(px=2, fair=2, edge_usd=2, edge_vol=2, fair_vol=2, quote_vol=2, delta=2, vega=2)).to_string(index=False))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        summ.to_csv(os.path.join(a.out, "summary.csv"), index=False)
        sig.to_csv(os.path.join(a.out, "signals.csv"), index=False)


if __name__ == "__main__":
    main()
