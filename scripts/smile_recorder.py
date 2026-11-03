#!/usr/bin/env python3
"""Record Deribit ETH quarterly smiles every few minutes, to test SVI-timed taker execution.

The idea under test: a low-frequency optimizer says what to hold on an expiry;
a taker crosses only when the touch is cheap (or rich) against the SVI fit by
more than the fee. That only beats crossing at once if such dislocations occur
with size behind them, and revert. This records the evidence.

Each tick (default every 5 minutes, aligned to the clock):

    1) the next N quarterly expiries (last-Friday expiries in Mar/Jun/Sep/Dec,
       rolling forward automatically as one expires)
    2) every order book of those expiries, depth 10, each expiry read in one
       concurrent burst (its strikes within ~0.5 s of each other)
    3) per expiry, as in smile_showcase.py: parity forward + DF, OTM side, raw
       SVI fitted to the mid in price under the butterfly penalty
    4) per scored strike, a LEAVE-ONE-OUT fit (the strike's own quote removed,
       warm-started from the full fit), so a dislocated quote cannot drag the
       curve towards itself and hide its own edge
    5) taker edges net of Deribit's taker fee, in USD and vol points:
           edge_buy  = fit_loo - ask - fee      (> 0: the ask is cheap)
           edge_sell = bid - fit_loo - fee      (> 0: the bid is rich)

Written under data/cache/smile_recorder/ (ignored by git), one file per UTC day:

    raw/YYYY-MM-DD.jsonl.gz      every order book as received, to refit later another way
    strikes/YYYY-MM-DD.csv       one row per strike per tick (quotes, sizes, fits, edges)
    fits/YYYY-MM-DD.csv          one row per expiry per tick (forward, SVI, fit error)

    .venv/bin/python -u scripts/smile_recorder.py                 # every 5 min, Ctrl-C stops
    .venv/bin/python -u scripts/smile_recorder.py --once          # one tick
    .venv/bin/python -u scripts/smile_recorder.py --summary       # what has been recorded

Public endpoints only, no API key. Leave it running with:

    nohup caffeinate -i .venv/bin/python -u scripts/smile_recorder.py \\
        >> data/cache/smile_recorder/recorder.log 2>&1 &

(caffeinate stops the Mac from idling to sleep; a closed lid still sleeps it.)
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import datetime as dt
import glob
import gzip
import json
import os
import sys
import time
import traceback
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

import deribit_smile_scanner as sc
import smile_showcase as ss

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd  # noqa: E402

API = "https://www.deribit.com/api/v2/public"
OUT = ROOT / "data" / "cache" / "smile_recorder"
QUARTER_MONTHS = {3, 6, 9, 12}
EXPIRY_PAUSE = 4.0     # seconds between expiries, for the rate limit
BOOK_KEYS = ("instrument_name", "timestamp", "bids", "asks", "best_bid_price", "best_ask_price",
             "best_bid_amount", "best_ask_amount", "mark_price", "mark_iv", "underlying_price",
             "index_price", "estimated_delivery_price", "open_interest", "stats")
STRIKE_FIELDS = ["ts", "expiry", "T_days", "spot", "forward", "DF", "K", "cp", "scored",
                 "bid", "ask", "bsize", "asize", "bid_depth", "ask_depth", "mark",
                 "bid_iv", "ask_iv", "mark_iv", "fit", "fit_loo", "fit_iv", "fit_loo_iv", "vega",
                 "fee_buy", "fee_sell", "edge_buy", "edge_sell", "edge_buy_vp", "edge_sell_vp"]
FIT_FIELDS = ["ts", "expiry", "T_days", "snapshot_secs", "spot", "forward", "DF", "n_scored",
              "atm_iv", "a", "b", "rho", "m", "sigma", "arb_free", "fit_err_mid", "mark_err_mid",
              "loo_secs"]


def quarterlies(instruments: list[dict], n: int, now: float) -> list[str]:
    """The next n quarterly expiry codes with at least a day to run."""
    out = {}
    for i in instruments:
        e = dt.datetime.fromtimestamp(i["expiration_timestamp"] / 1000, dt.timezone.utc)
        last_friday = e.weekday() == 4 and e.day + 7 > calendar.monthrange(e.year, e.month)[1]
        if e.month in QUARTER_MONTHS and last_friday and e.timestamp() - now >= 86400:
            out[i["instrument_name"].split("-")[1]] = e
    return sorted(out, key=out.get)[:n]


async def snapshot(n_expiries: int, depth: int) -> tuple[dict[str, list[dict]], float, float]:
    async with httpx.AsyncClient(timeout=20) as c:
        async def get(method: str, **params) -> dict | list:
            for attempt in range(5):
                r = await c.get(f"{API}/{method}", params=params)
                if r.status_code != 429:  # rate limited: back off and retry
                    r.raise_for_status()
                    return r.json()["result"]
                await asyncio.sleep(1 + attempt)
            raise RuntimeError(f"{method} {params}: rate limited")

        inst = await get("get_instruments", currency="ETH", kind="option", expired="false")
        expiries = quarterlies(inst, n_expiries, time.time())
        names = [i["instrument_name"] for i in inst if i["instrument_name"].split("-")[1] in expiries]
        sem = asyncio.Semaphore(12)

        async def one(name: str) -> dict:
            async with sem:
                return await get("get_order_book", instrument_name=name, depth=depth)

        # One burst per expiry, with a pause for Deribit's rate-limit credits to refill:
        # read all at once, the first ~90 books come back in 0.5 s and the rest are
        # throttled over ~5 s, which skews strikes of the same expiry against each other.
        t0 = time.time()
        by_exp: dict[str, list[dict]] = {}
        for k, e in enumerate(expiries):
            if k:
                await asyncio.sleep(EXPIRY_PAUSE)
            books = await asyncio.gather(*(one(nm) for nm in names if nm.split("-")[1] == e))
            by_exp[e] = [{key: b.get(key) for key in BOOK_KEYS} for b in books]
        t1 = time.time()
    return by_exp, t0, t1


def analyse(expiry: str, books: list[dict], now: dt.datetime) -> tuple[list[dict], dict]:
    g = ss.book_frame(books, expiry, now)
    depth = {}
    for b in books:  # visible size on each side, all levels read
        _, _, K, cp = b["instrument_name"].split("-")
        depth[(float(K), cp)] = (sum(a for _, a in b["bids"] or []), sum(a for _, a in b["asks"] or []))
    F, DF, _, _ = sc.fit_forward(g)
    T = float(g["T"].iloc[0])
    spot = float(g.spot.median())
    otm = g[(g.cp == "C") == (g.K >= F)].sort_values("K").reset_index(drop=True)
    iv = lambda px, r: sc.implied(px, F, r.K, T, r.cp, DF) if np.isfinite(px) else np.nan  # noqa: E731
    otm["bid_iv"] = [iv(r.bid, r) for r in otm.itertuples()]
    otm["ask_iv"] = [iv(r.ask, r) for r in otm.itertuples()]
    otm["scored"] = (np.isfinite(otm.bid) & np.isfinite(otm.ask) & (otm.ask > otm.bid)
                     & np.isfinite(otm.bid_iv) & np.isfinite(otm.ask_iv))
    s = otm[otm.scored]
    if len(s) < 6:
        raise RuntimeError(f"{expiry}: only {len(s)} two-sided strikes")
    K, CP = s.K.to_numpy(), s.cp.to_list()
    mid, half = ((s.bid + s.ask) / 2).to_numpy(), ((s.ask - s.bid) / 2).to_numpy()
    atm = float(np.median((s.bid_iv + s.ask_iv) / 2))
    p, ok = ss.fit_svi(K, CP, mid, half, F, T, DF, atm)

    t_loo = time.time()
    loo = {}
    for j in range(len(K)):
        keep = np.arange(len(K)) != j
        pj, _ = ss.fit_svi(K[keep], [c for c, k in zip(CP, keep) if k], mid[keep], half[keep],
                           F, T, DF, atm, x0=p)
        loo[K[j]] = pj
    loo_secs = time.time() - t_loo

    rows = []
    ts = now.isoformat(timespec="seconds")
    for r in otm.itertuples():
        v = float(ss.svi_iv(p, [r.K], F, T)[0])
        pl = loo.get(r.K, p)  # one-sided strikes are not in the fit: the full fit is their LOO
        vl = float(ss.svi_iv(pl, [r.K], F, T)[0])
        fit = float(ss.black_vec(F, [r.K], T, v, [r.cp], DF)[0])
        fit_loo = float(ss.black_vec(F, [r.K], T, vl, [r.cp], DF)[0])
        vega = float(sc.vega(F, r.K, T, vl, DF))
        fee_buy = deribit_option_fee_usd(spot, r.ask) if np.isfinite(r.ask) else np.nan
        fee_sell = deribit_option_fee_usd(spot, r.bid) if np.isfinite(r.bid) else np.nan
        eb, es = fit_loo - r.ask - fee_buy, r.bid - fit_loo - fee_sell
        bd, ad = depth[(r.K, r.cp)]
        rows.append(dict(ts=ts, expiry=expiry, T_days=T * 365, spot=spot, forward=F, DF=DF, K=r.K, cp=r.cp,
                         scored=int(r.scored), bid=r.bid, ask=r.ask, bsize=r.bsize, asize=r.asize,
                         bid_depth=bd, ask_depth=ad, mark=r.mark, bid_iv=r.bid_iv * 100,
                         ask_iv=r.ask_iv * 100, mark_iv=r.mark_iv, fit=fit, fit_loo=fit_loo,
                         fit_iv=v * 100, fit_loo_iv=vl * 100, vega=vega, fee_buy=fee_buy, fee_sell=fee_sell,
                         edge_buy=eb, edge_sell=es, edge_buy_vp=eb / vega if vega > 0 else np.nan,
                         edge_sell_vp=es / vega if vega > 0 else np.nan))
    sr = [x for x in rows if x["scored"]]
    rms = lambda key: float(np.sqrt(np.mean([((x[key] - (x["bid"] + x["ask"]) / 2)  # noqa: E731
                                              / ((x["ask"] - x["bid"]) / 2)) ** 2 for x in sr])))
    fitrow = dict(ts=ts, expiry=expiry, T_days=T * 365, spot=spot, forward=F, DF=DF, n_scored=len(sr),
                  atm_iv=float(ss.svi_iv(p, [F], F, T)[0]) * 100,
                  **dict(zip(["a", "b", "rho", "m", "sigma"], map(float, p))), arb_free=int(ok),
                  fit_err_mid=rms("fit"), mark_err_mid=rms("mark"), loo_secs=loo_secs)
    return rows, fitrow


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows([{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()} for r in rows])


def tick(n_expiries: int, depth: int) -> None:
    by_exp, t0, t1 = asyncio.run(snapshot(n_expiries, depth))
    now = dt.datetime.fromtimestamp((t0 + t1) / 2, dt.timezone.utc)
    day = f"{now:%Y-%m-%d}"
    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / "raw" / f"{day}.jsonl.gz", "at") as fh:  # appended gzip members read back as one
        fh.write(json.dumps({"t0": t0, "t1": t1, "books": by_exp}, separators=(",", ":")) + "\n")
    fits = []
    for expiry, books in by_exp.items():
        try:
            rows, fitrow = analyse(expiry, books, now)
        except Exception as e:  # one bad expiry must not lose the others; raw is saved anyway
            print(f"{now:%H:%M:%S} {expiry}: {e!r}", flush=True)
            continue
        stamps = [b["timestamp"] for b in books]
        fitrow["snapshot_secs"] = round((max(stamps) - min(stamps)) / 1000, 2)
        append_csv(OUT / "strikes" / f"{day}.csv", STRIKE_FIELDS, rows)
        fits.append(fitrow)
        sr = [r for r in rows if r["scored"]]
        nb, ns = sum(r["edge_buy"] > 0 for r in sr), sum(r["edge_sell"] > 0 for r in sr)
        print(f"{now:%Y-%m-%d %H:%M:%S} {expiry:8} books {len(books):3} ({fitrow['snapshot_secs']:.1f} s)  F {fitrow['forward']:8.2f}"
              f"  ATM {fitrow['atm_iv']:5.2f}%  fit err {fitrow['fit_err_mid']:.3f} (mark {fitrow['mark_err_mid']:.3f})"
              f"  taker edge >0: buy {nb}, sell {ns} of {len(sr)}  (LOO {fitrow['loo_secs']:.1f} s)", flush=True)
    append_csv(OUT / "fits" / f"{day}.csv", FIT_FIELDS, fits)


def summary() -> None:
    files = sorted(glob.glob(str(OUT / "strikes" / "*.csv")))
    if not files:
        print(f"nothing recorded yet under {OUT}")
        return
    d = pd.concat([pd.read_csv(f) for f in files])
    d = d[d.scored == 1]
    print(f"{d.ts.nunique()} ticks, {d.ts.min()} .. {d.ts.max()}\n")
    print(f"{'expiry':9}{'rows':>7}{'buy>0 %':>9}{'sell>0 %':>9}{'med buy vp':>12}{'med sell vp':>12}"
          f"{'med size':>10}")
    for e, x in d.groupby("expiry", sort=False):
        b, s = x[x.edge_buy > 0], x[x.edge_sell > 0]
        size = pd.concat([b.asize, s.bsize])
        print(f"{e:9}{len(x):>7}{100 * len(b) / len(x):>9.1f}{100 * len(s) / len(x):>9.1f}"
              f"{b.edge_buy_vp.median() if len(b) else float('nan'):>12.2f}"
              f"{s.edge_sell_vp.median() if len(s) else float('nan'):>12.2f}"
              f"{size.median() if len(size) else float('nan'):>10.1f}")
    print("\nvp = vol points; edges are net of the taker fee against the leave-one-out SVI fit; "
          "size = touch size (ETH) on the side taken when the edge is positive.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--every", type=float, default=5, help="minutes between ticks, aligned to the clock")
    p.add_argument("--expiries", type=int, default=3, help="number of quarterly expiries to record")
    p.add_argument("--depth", type=int, default=10, help="order book levels to read")
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    p.add_argument("--summary", action="store_true", help="summarise what has been recorded")
    args = p.parse_args()
    if args.summary:
        return summary()
    while True:
        try:
            tick(args.expiries, args.depth)
        except Exception:  # network or API hiccup: log it and wait for the next tick
            traceback.print_exc()
        if args.once:
            return
        step = args.every * 60
        time.sleep(step - time.time() % step)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    sys.stdout.flush()
    os._exit(0)  # app imports can leave threads that hang interpreter exit
