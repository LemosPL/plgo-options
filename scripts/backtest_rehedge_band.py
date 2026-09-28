#!/usr/bin/env python3
"""Backtest: how often should the book's delta be re-hedged with the perp?

Question it answers: the strategy doc's B1 rows re-hedge only when spot
moves +/-10% (ETH) or +/-15% (FIL) from Monday's reference, each row at most
once per week. A long-options book pays time decay every day and earns it
back only by re-hedging as the price chops around, so a wide, once-a-week
trigger may be giving that back in exactly the sideways market that has
been hurting us. This replays today's actual option book over past price
paths and compares the hedge P&L of:

  unhedged      never trade the perp (what the book is today)
  static        flatten delta once at the start, never adjust
  doc_rows      the B1 perp rows: flatten when spot first crosses +/-step
                from Monday's reference, each direction once per week
  daily         flatten once a day at the 16:00 UK handover (15:00 UTC)
  band_$X       check every hour; flatten when |net delta| > $X
                (the same trade-to-zero rule as delta_hedger.check_rehedge)

Method, and what it deliberately holds fixed:

* The book: every active C/P trade for the asset in the DB (or the app's
  trade export, --trades-xlsx) with expiry after --asof, taken as-is (strikes, quantities, expiries).
* Sticky moneyness replay: for each historical start date the strikes are
  rescaled by (spot then / spot today) and quantities by the inverse, so
  every window starts with the same dollar shape and the same days to
  expiry as today's book. Legs that expire inside a window settle at
  intrinsic and stop contributing delta.
* Implied vol is flat and constant (--iv-eth / --iv-fil). The option P&L
  path is therefore identical across policies, so the differences between
  policies are purely hedging P&L - they are not affected by the vol level
  beyond its small effect on the deltas being hedged.
* Hourly Binance spot closes; triggers act on the hourly close, not the
  intrabar touch a resting order would get.
* Costs: --cost-bps of perp notional per trade, plus actual Binance USD-M
  funding history on the perp position (longs pay shorts when positive).

Windows are classified by realized vol relative to the implied vol used, so
the "slow market" rows (realized well below implied) show what each policy
does in the regime the doc is worried about.

Usage:
    .venv/bin/python scripts/backtest_rehedge_band.py
    .venv/bin/python scripts/backtest_rehedge_band.py --detrend
    .venv/bin/python scripts/backtest_rehedge_band.py --db data/plgo_options.prod.db
    .venv/bin/python scripts/backtest_rehedge_band.py --trades-xlsx ~/Downloads/PLGO_Trades_2026-09-28.xlsx
    .venv/bin/python scripts/backtest_rehedge_band.py --asset ETH --days 1095 --cost-bps 5
    .venv/bin/python scripts/backtest_rehedge_band.py --bands 100000,250000,500000,1000000 --csv-out out.csv
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "plgo_options.db"
CACHE_DIR = ROOT / "data" / "cache"

SPOT_URL = "https://api.binance.com/api/v3/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
SYMBOL = {"ETH": "ETHUSDT", "FIL": "FILUSDT"}
# B1: the perp-only rows sit one step from Monday's reference.
DOC_ROW_STEP = {"ETH": 0.10, "FIL": 0.15}
HOURS_PER_YEAR = 24 * 365.25
HANDOVER_UTC_HOUR = 15  # 16:00 UK in summer time; close enough for a daily check


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _get_json(client: httpx.Client, url: str, params: dict) -> list:
    backoff = 1.0
    while True:
        resp = client.get(url, params=params)
        if resp.status_code in (429, 418):
            time.sleep(float(resp.headers.get("Retry-After", backoff)))
            backoff = min(backoff * 2, 60)
            continue
        resp.raise_for_status()
        time.sleep(0.25)
        return resp.json()


def fetch_hourly_spot(client: httpx.Client, symbol: str, start_ms: int) -> pd.Series:
    rows: list[list] = []
    cursor = start_ms
    while True:
        page = _get_json(client, SPOT_URL, {"symbol": symbol, "interval": "1h", "startTime": cursor, "limit": 1000})
        if not page:
            break
        rows += page
        cursor = page[-1][0] + 1
        if len(page) < 1000:
            break
    idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
    return pd.Series([float(r[4]) for r in rows], index=idx, name=symbol)


def fetch_funding(client: httpx.Client, symbol: str, start_ms: int) -> pd.Series:
    rows: list[dict] = []
    cursor = start_ms
    while True:
        page = _get_json(client, FUNDING_URL, {"symbol": symbol, "startTime": cursor, "limit": 1000})
        if not page:
            break
        rows += page
        cursor = int(page[-1]["fundingTime"]) + 1
        if len(page) < 1000:
            break
    # Funding times carry a few ms of jitter; floor to the hour so they line
    # up with the spot bars.
    idx = pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True).floor("h")
    return pd.Series([float(r["fundingRate"]) for r in rows], index=idx, name=symbol).groupby(level=0).sum()


def load_market(asset: str, days: int, refresh: bool) -> pd.DataFrame:
    """Hourly spot close + funding rate paid at that hour (0 elsewhere)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{SYMBOL[asset]}_1h_{days}d_{date.today().isoformat()}.csv"
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
    else:
        start_ms = int((time.time() - days * 86_400) * 1000)
        with httpx.Client(timeout=20) as client:
            spot = fetch_hourly_spot(client, SYMBOL[asset], start_ms)
            funding = fetch_funding(client, SYMBOL[asset], start_ms)
        df = pd.DataFrame({"spot": spot})
        df["funding"] = funding.reindex(df.index).fillna(0.0)
        df.to_csv(cache)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


@dataclass
class Leg:
    opt: str        # "C" or "P"
    strike: float
    qty: float      # signed, + long
    expiry: date


def load_book_xlsx(asset: str, asof: date, paths: list[Path]) -> list[Leg]:
    """Active C/P legs from the app's Trade Management export(s)
    (PLGO_Trades_<date>.xlsx: ID, Status, Side Long/Short, Type, Instrument,
    Expiry, Strike, Qty, ...). The asset comes from the Instrument prefix."""
    import openpyxl

    legs, seen = [], set()
    for path in paths:
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        rows = ws.iter_rows(values_only=True)
        col = {name: i for i, name in enumerate(next(rows))}
        for r in rows:
            if r[col["ID"]] is None or r[col["ID"]] in seen:
                continue
            seen.add(r[col["ID"]])
            if str(r[col["Status"]]).lower() != "active":
                continue
            if not str(r[col["Instrument"]] or "").upper().startswith(asset + "-"):
                continue
            opt = str(r[col["Type"]] or "")[:1].upper()
            if opt not in ("C", "P"):
                continue
            exp = r[col["Expiry"]]
            exp = exp.date() if isinstance(exp, datetime) else date.fromisoformat(str(exp)[:10])
            if exp <= asof:
                continue
            sign = 1.0 if str(r[col["Side"]]).lower() in ("long", "buy") else -1.0
            legs.append(Leg(opt, float(r[col["Strike"]]), sign * float(r[col["Qty"]]), exp))
    return legs


def load_book(asset: str, asof: date, db_path: Path = DB_PATH, xlsx: list[Path] | None = None) -> list[Leg]:
    if xlsx:
        return load_book_xlsx(asset, asof, xlsx)
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT side, option_type, expiry, strike, qty FROM trades "
        "WHERE status = 'active' AND asset = ? AND option_type IN ('Call', 'Put')",
        (asset,),
    ).fetchall()
    con.close()
    legs = []
    for side, opt_type, expiry, strike, qty in rows:
        exp = date.fromisoformat(str(expiry)[:10])
        if exp <= asof:
            continue
        sign = 1.0 if str(side).lower() == "buy" else -1.0
        legs.append(Leg(opt_type[0].upper(), float(strike), sign * float(qty), exp))
    return legs


# ---------------------------------------------------------------------------
# Option paths (policy-independent)
# ---------------------------------------------------------------------------

def bs_value_delta(S: np.ndarray, K: np.ndarray, T: np.ndarray, sigma: float, is_call: np.ndarray):
    """Vectorised Black-Scholes (r=0) value and delta, shape (steps, legs).
    Legs with T <= 0 are at intrinsic with zero delta (settled)."""
    live = T > 0
    Tc = np.where(live, T, 1.0)
    sqrtT = np.sqrt(Tc)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * Tc) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    call = S * ndtr(d1) - K * ndtr(d2)
    put = call - S + K
    value = np.where(is_call, call, put)
    delta = np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    intrinsic = np.where(is_call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
    return np.where(live, value, intrinsic), np.where(live, delta, 0.0)


def option_paths(legs: list[Leg], spot0_today: float, path: np.ndarray, hours_to_expiry: np.ndarray, sigma: float,
                 rescale: bool = True):
    """Book value ($) and net delta (tokens) along one path, with the book
    rescaled to the path's starting spot (sticky moneyness, constant $ shape)."""
    scale = path[0] / spot0_today if rescale else 1.0
    K = np.array([l.strike for l in legs]) * scale
    q = np.array([l.qty for l in legs]) / scale
    is_call = np.array([l.opt == "C" for l in legs])
    steps = np.arange(len(path))[:, None]
    T = (hours_to_expiry[None, :] - steps) / HOURS_PER_YEAR
    value, delta = bs_value_delta(path[:, None], K[None, :], T, sigma, is_call[None, :])
    return value @ q, delta @ q


# ---------------------------------------------------------------------------
# Hedging policies
# ---------------------------------------------------------------------------

def run_policy(name: str, times: pd.DatetimeIndex, path: np.ndarray, funding: np.ndarray,
               opt_delta: np.ndarray, cost_bps: float, asset: str, band_usd: float | None = None) -> dict:
    n = len(path)
    perp = 0.0
    fees = funding_paid = perp_pnl = 0.0
    trades = 0
    step = DOC_ROW_STEP[asset]
    ref = path[0]
    fired_up = fired_dn = False
    last_week = times[0].isocalendar()[:2]
    last_day_hedged = None

    def trade_to_zero(i: int) -> None:
        nonlocal perp, fees, trades
        qty = -(opt_delta[i] + perp)
        if qty == 0.0:
            return
        fees += abs(qty) * path[i] * cost_bps / 10_000.0
        perp += qty
        trades += 1

    if name != "unhedged":
        trade_to_zero(0)

    for i in range(1, n):
        # Carry the position held over the last hour first, then decide.
        perp_pnl += perp * (path[i] - path[i - 1])
        if funding[i]:
            funding_paid += perp * path[i] * funding[i]   # + = we paid

        if name in ("unhedged", "static"):
            continue
        if name == "daily":
            if times[i].hour == HANDOVER_UTC_HOUR and times[i].date() != last_day_hedged:
                trade_to_zero(i)
                last_day_hedged = times[i].date()
        elif name == "doc_rows":
            week = times[i].isocalendar()[:2]
            if week != last_week and times[i].weekday() == 0 and times[i].hour >= 8:
                ref, fired_up, fired_dn, last_week = path[i], False, False, week
            if not fired_up and path[i] >= ref * (1 + step):
                trade_to_zero(i)
                fired_up = True
            elif not fired_dn and path[i] <= ref * (1 - step):
                trade_to_zero(i)
                fired_dn = True
        elif name.startswith("band"):
            if abs(opt_delta[i] + perp) * path[i] > band_usd:
                trade_to_zero(i)

    hedge_pnl = perp_pnl - fees - funding_paid
    return {"policy": name, "trades": trades, "perp_pnl": perp_pnl, "fees": fees,
            "funding": funding_paid, "hedge_pnl": hedge_pnl,
            "end_delta_usd": (opt_delta[-1] + perp) * path[-1]}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def backtest_asset(asset: str, args) -> pd.DataFrame:
    legs = load_book(asset, args.asof, args.db, args.trades_xlsx)
    if not legs:
        print(f"{asset}: no active option legs after {args.asof}, skipping")
        return pd.DataFrame()
    sigma = (args.iv_eth if asset == "ETH" else args.iv_fil) / 100.0
    mkt = load_market(asset, args.days, args.refresh)
    spot_today = float(mkt["spot"].iloc[-1])

    asof_dt = datetime(args.asof.year, args.asof.month, args.asof.day, tzinfo=timezone.utc)
    hours_to_expiry = np.array([(datetime(l.expiry.year, l.expiry.month, l.expiry.day, 8, tzinfo=timezone.utc)
                                 - asof_dt).total_seconds() / 3600 for l in legs])
    window_h = int(min(args.window_days * 24, hours_to_expiry.max()))

    # Book greeks today, for the report header (no rescaling: today's strikes).
    def today(spot: float, hours: np.ndarray = hours_to_expiry):
        v, d = option_paths(legs, spot_today, np.array([spot]), hours, sigma, rescale=False)
        return v[0], d[0]
    v0, d0 = today(spot_today)
    gamma_1pct = (today(spot_today * 1.01)[0] + today(spot_today * 0.99)[0] - 2 * v0) / 2  # $ from a 1% move
    theta_day = today(spot_today, hours_to_expiry - 24)[0] - v0
    print(f"\n=== {asset} === {len(legs)} legs, spot {spot_today:,.4g}, IV {sigma:.0%}, window {window_h / 24:.0f}d")
    print(f"  today: delta ${d0 * spot_today:,.0f}   gamma ${gamma_1pct:,.0f} per 1% move   "
          f"theta ${theta_day:,.0f}/day   break-even daily move {sigma / np.sqrt(365.25):.2%}")
    if gamma_1pct < 0:
        print("  NOTE: book is net SHORT gamma - tighter re-hedging is expected to COST money here,"
              " in exchange for lower P&L variance.")

    policies = ["unhedged", "static", "doc_rows", "daily"] + [f"band_{int(b)}" for b in args.bands]
    starts = range(0, len(mkt) - window_h, args.step_days * 24)
    out = []
    for s in starts:
        w = mkt.iloc[s:s + window_h + 1]
        path = w["spot"].to_numpy()
        if args.detrend:
            # Same hourly moves, minus the window's net drift: isolates what
            # the re-hedging earns from chop from what the path's trend did.
            path = path * (path[0] / path[-1]) ** (np.arange(len(path)) / (len(path) - 1))
        value, delta = option_paths(legs, spot_today, path, hours_to_expiry, sigma)
        rets = np.diff(np.log(path))
        rv = rets.std() * np.sqrt(HOURS_PER_YEAR)
        for p in policies:
            band = float(p.split("_")[1]) if p.startswith("band") else None
            r = run_policy(p, w.index, path, w["funding"].to_numpy(), delta, args.cost_bps, asset, band)
            r.update(asset=asset, start=w.index[0].date(), rv=rv, iv=sigma,
                     move=path[-1] / path[0] - 1, option_pnl=value[-1] - value[0])
            r["total_pnl"] = r["option_pnl"] + r["hedge_pnl"]
            out.append(r)
    return pd.DataFrame(out)


def summarize(df: pd.DataFrame) -> None:
    for asset, a in df.groupby("asset"):
        iv = a["iv"].iloc[0]
        a = a.copy()
        a["regime"] = np.select([a["rv"] < 0.8 * iv, a["rv"] > 1.2 * iv], ["slow (RV<0.8xIV)", "fast (RV>1.2xIV)"],
                                "normal")
        n_win = a["start"].nunique()
        print(f"\n--- {asset}: {n_win} windows, realized vol median {a['rv'].median():.0%} vs IV {iv:.0%} ---")
        base = a[a.policy == "unhedged"].set_index("start")["total_pnl"]
        rows = []
        for p, g in a.groupby("policy", sort=False):
            g = g.set_index("start")
            rows.append({
                "policy": p,
                "trades/win": g["trades"].mean(),
                "fees": g["fees"].mean(),
                "funding": g["funding"].mean(),
                "hedge P&L": g["hedge_pnl"].mean(),
                "total P&L": g["total_pnl"].mean(),
                "total P&L stdev": g["total_pnl"].std(),
                "worst window": g["total_pnl"].min(),
                "beats unhedged": (g["total_pnl"] > base).mean(),
            })
        t = pd.DataFrame(rows).set_index("policy")
        with pd.option_context("display.float_format", lambda x: f"{x:,.0f}", "display.width", 200):
            t2 = t.copy()
            t2["beats unhedged"] = (t["beats unhedged"] * 100).round().astype(int).astype(str) + "%"
            t2["trades/win"] = t["trades/win"].round(1)
            print(t2.to_string())

        print(f"\n  mean hedge P&L by regime ($ per window; option P&L is the same for every policy)")
        piv = a.pivot_table(index="policy", columns="regime", values="hedge_pnl", aggfunc="mean", sort=False)
        counts = a[a.policy == "unhedged"]["regime"].value_counts()
        piv.columns = [f"{c} n={counts.get(c, 0)}" for c in piv.columns]
        with pd.option_context("display.float_format", lambda x: f"{x:,.0f}", "display.width", 200):
            print(piv.to_string())

        last = a[a.start == a.start.max()].set_index("policy")
        print(f"\n  most recent window (from {a.start.max()}, RV {last['rv'].iloc[0]:.0%}, move {last['move'].iloc[0]:+.1%}):")
        with pd.option_context("display.float_format", lambda x: f"{x:,.0f}", "display.width", 200):
            print(last[["trades", "fees", "funding", "hedge_pnl", "total_pnl"]].to_string())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", choices=["ETH", "FIL", "both"], default="both")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="trades DB (e.g. data/plgo_options.prod.db)")
    ap.add_argument("--trades-xlsx", type=Path, nargs="+",
                    help="read the book from the app's Trade Management export(s) instead of the DB")
    ap.add_argument("--asof", type=date.fromisoformat, default=date.today())
    ap.add_argument("--days", type=int, default=730, help="history to replay over")
    ap.add_argument("--window-days", type=int, default=90)
    ap.add_argument("--step-days", type=int, default=7, help="days between window starts")
    ap.add_argument("--iv-eth", type=float, default=52.0, help="flat implied vol %% (Deribit DVOL ~52 on 2026-09-28)")
    ap.add_argument("--iv-fil", type=float, default=100.0, help="flat implied vol %%")
    ap.add_argument("--cost-bps", type=float, default=2.0, help="perp cost per trade, bps of notional")
    ap.add_argument("--bands", type=lambda s: [float(x) for x in s.split(",")],
                    default=[150_000, 300_000, 600_000, 1_200_000], help="$ delta bands, comma-separated")
    ap.add_argument("--detrend", action="store_true",
                    help="remove each window's net drift, keep its hourly chop (isolates the re-hedging effect)")
    ap.add_argument("--refresh", action="store_true", help="re-download market data")
    ap.add_argument("--csv-out", type=Path)
    args = ap.parse_args()

    assets = ["ETH", "FIL"] if args.asset == "both" else [args.asset]
    df = pd.concat([backtest_asset(a, args) for a in assets], ignore_index=True)
    if df.empty:
        sys.exit("nothing to backtest")
    summarize(df)
    if args.csv_out:
        df.to_csv(args.csv_out, index=False)
        print(f"\nper-window results -> {args.csv_out}")


if __name__ == "__main__":
    main()
