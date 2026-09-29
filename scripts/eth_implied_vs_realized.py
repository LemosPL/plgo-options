#!/usr/bin/env python3
"""ETH implied vol (variance-swap basis) against realized vol, over time.

Implied: Deribit's DVOL index, a 30-day variance-swap vol computed VIX-style
from the whole ETH option strip (model-free, not ATM). Daily closes from
2021-03-24, the start of the index, via the public API.

Realized: 30-day vol from daily Binance ETHUSDT closes (00:00 UTC, the same
fixing as DVOL's daily close), zero-mean squared log returns annualized on
365 days, the variance-swap convention. Two versions per date:

  rv_trailing   the 30 days that ended at that date (known at the time)
  rv_forward    the 30 days that followed it (what DVOL was pricing)

dvol - rv_forward is the variance risk premium an option buyer paid.

--chain also computes variance-swap vol per listed expiry from today's
Deribit chain (the same VIX formula on mark prices), to check DVOL and to
read the vol at tenors DVOL doesn't cover, such as the book's December expiry.

FIL has no listed options anywhere, so --asset FIL gives the realized side only.

Usage:
    .venv/bin/python scripts/eth_implied_vs_realized.py
    .venv/bin/python scripts/eth_implied_vs_realized.py --asset FIL
    .venv/bin/python scripts/eth_implied_vs_realized.py --chain --csv-out eth_vol.csv
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

DERIBIT = "https://www.deribit.com/api/v2/public"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
DVOL_START_MS = 1_600_000_000_000  # before the index began (2021-03-24)


def fetch_dvol(client: httpx.Client) -> pd.Series:
    end, rows = int(time.time() * 1000), []
    while True:
        res = client.get(f"{DERIBIT}/get_volatility_index_data", params={
            "currency": "ETH", "resolution": "1D", "start_timestamp": DVOL_START_MS, "end_timestamp": end,
        }).json()["result"]
        rows = res["data"] + rows
        if not res["data"] or not res.get("continuation"):
            break
        end = res["continuation"]
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"]).drop_duplicates("t")
    # A daily candle's close is the value at 00:00 UTC the next day (today's
    # is the live value); index it there, the same fixing as the closes.
    idx = pd.to_datetime(df["t"], unit="ms") + pd.Timedelta(days=1)
    return pd.Series(df["c"].to_numpy(), index=idx, name="dvol").sort_index()


def fetch_daily_closes(client: httpx.Client, symbol: str = "ETHUSDT", start: str = "2021-01-01") -> pd.Series:
    cursor, rows = int(pd.Timestamp(start).timestamp() * 1000), []
    while True:
        page = client.get(BINANCE_KLINES, params={"symbol": symbol, "interval": "1d",
                                                  "startTime": cursor, "limit": 1000}).json()
        rows += page
        if len(page) < 1000:
            break
        cursor = page[-1][0] + 1
        time.sleep(0.3)
    # A daily kline closes at 00:00 UTC the next day; index it by that close.
    idx = pd.to_datetime([r[0] for r in rows], unit="ms") + pd.Timedelta(days=1)
    return pd.Series([float(r[4]) for r in rows], index=idx, name="close")


def realized_vol(closes: pd.Series, window: int = 30) -> pd.DataFrame:
    """Trailing and forward realized vol (%), variance-swap convention."""
    var = (np.log(closes).diff() ** 2).rolling(window).sum() * 365 / window
    return pd.DataFrame({"rv_trailing": np.sqrt(var) * 100, "rv_forward": np.sqrt(var.shift(-window)) * 100})


def implied_vs_realized(dvol: pd.Series, closes: pd.Series, window: int = 30) -> pd.DataFrame:
    df = realized_vol(closes, window)
    df.insert(0, "dvol", dvol)
    return df.dropna(subset=["dvol"])


def chain_variance_swap_vols(client: httpx.Client) -> pd.DataFrame:
    """VIX-formula variance-swap vol per expiry, from Deribit mark prices."""
    summ = client.get(f"{DERIBIT}/get_book_summary_by_currency",
                      params={"currency": "ETH", "kind": "option"}).json()["result"]
    now = datetime.fromtimestamp(summ[0]["creation_timestamp"] / 1000, tz=timezone.utc)
    rows = []
    for s in summ:
        _, exp, strike, cp = s["instrument_name"].split("-")
        rows.append({"exp": datetime.strptime(exp, "%d%b%y").replace(hour=8, tzinfo=timezone.utc),
                     "K": float(strike), "cp": cp, "mark": s["mark_price"], "F": s["underlying_price"],
                     "iv": s["mark_iv"]})
    d = pd.DataFrame(rows)
    out = []
    for exp, g in d.groupby("exp"):
        T = (exp - now).total_seconds() / (365 * 86_400)
        if T < 2 / 365:
            continue
        F = g["F"].median()
        calls = g[g.cp == "C"].set_index("K")["mark"]
        puts = g[g.cp == "P"].set_index("K")["mark"]
        strikes = np.sort(g["K"].unique())
        K0 = strikes[strikes <= F].max() if (strikes <= F).any() else strikes.min()
        # Out-of-the-money option at each strike (both at K0), in USD at the
        # forward: Deribit marks are in ETH per option on 1 ETH.
        q = np.array([
            puts.get(K, np.nan) if K < K0 else calls.get(K, np.nan) if K > K0
            else (calls.get(K, np.nan) + puts.get(K, np.nan)) / 2
            for K in strikes
        ]) * F
        ok = np.isfinite(q) & (q > 0)
        K, q = strikes[ok], q[ok]
        if len(K) < 3:
            continue
        var = (2 / T) * np.sum(np.gradient(K) / K**2 * q) - (F / K0 - 1) ** 2 / T
        atm = g.iloc[(g["K"] - F).abs().argsort()[:2]]["iv"].mean()
        out.append({"expiry": exp.date(), "days": round(T * 365, 1), "forward": round(F),
                    "var_swap_vol": round(100 * np.sqrt(var), 2), "atm_iv": round(atm, 2)})
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", choices=["ETH", "FIL"], default="ETH",
                    help="FIL has no listed options, so FIL gives realized vol only")
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--chain", action="store_true", help="also compute var-swap vols from today's chain")
    ap.add_argument("--csv-out", type=Path)
    args = ap.parse_args()

    if args.asset == "FIL":
        with httpx.Client(timeout=30) as client:
            df = realized_vol(fetch_daily_closes(client, "FILUSDT"), args.window).dropna(subset=["rv_trailing"])
        print(f"FIL {args.window}d realized, {df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}: "
              f"today {df['rv_trailing'].iloc[-1]:.1f}%, mean {df['rv_trailing'].mean():.1f}%")
        for year, g in df.groupby(df.index.year):
            print(f"  {year}: mean {g['rv_trailing'].mean():.0f}%  range {g['rv_trailing'].min():.0f}-{g['rv_trailing'].max():.0f}%")
        if args.csv_out:
            df.to_csv(args.csv_out)
        return

    with httpx.Client(timeout=30) as client:
        df = implied_vs_realized(fetch_dvol(client), fetch_daily_closes(client), args.window)
        chain = chain_variance_swap_vols(client) if args.chain else None

    f = df.dropna(subset=["rv_forward"])
    spread = f["dvol"] - f["rv_forward"]
    print(f"{len(df)} days, {df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}")
    print(f"today: DVOL {df['dvol'].iloc[-1]:.1f}%, trailing {args.window}d realized {df['rv_trailing'].iloc[-1]:.1f}%")
    print(f"DVOL minus next-{args.window}d realized: mean {spread.mean():+.1f} pts, "
          f"median {spread.median():+.1f}, implied above realized on {(spread > 0).mean():.0%} of days")
    for year, g in f.groupby(f.index.year):
        s = g["dvol"] - g["rv_forward"]
        print(f"  {year}: DVOL {g['dvol'].mean():.0f}%  realized {g['rv_forward'].mean():.0f}%  "
              f"spread {s.mean():+.1f}  implied above {(s > 0).mean():.0%}")
    if chain is not None:
        print("\nvariance-swap vol by expiry, today's chain:")
        print(chain.to_string(index=False))
    if args.csv_out:
        df.to_csv(args.csv_out)
        print(f"\ndaily series -> {args.csv_out}")


if __name__ == "__main__":
    main()
