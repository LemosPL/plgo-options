#!/usr/bin/env python3
"""When to cross: cost thresholds for working a large option order as a taker, replayed
on the order books the smile recorder saved.

A parent order (one option, buy or sell, a size, a deadline) is worked at every recorded
snapshot (5 min apart). The cost of a book level, USD per contract, is

    buy:  ask level - fair + taker fee        sell:  fair - bid level + taker fee

where fair is the arbitrage-free smoothing spline through the out-of-the-money mid vols
(smile_fit.fit_spline), fitted WITHOUT the traded strike so its own quote does not pull
the curve, and the fee is Deribit's min(0.0003 ETH, 12.5% of the premium).

A policy sets a threshold theta(hours elapsed, share filled). At each snapshot the order
takes every level costing <= theta, at most --cap contracts; in the last hour it takes
any level (urgency), and what is still open at the deadline is reported as a shortfall.
Every snapshot with a full window of data after it is a start time; results average
over them.

Policies (thresholds in USD per contract, or vol points with --vol):

    --fixed  1.75,2               one threshold throughout
    --ramp   1.6-2.25             linear from the first to the second over the window
                                  (reached at the start of the last hour)
    --step   1.75-2.25@8          the first until hour 8, then the second
    --behind 1.7+1                the first plus (second x lag), lag = how far the share
                                  filled trails a straight line to the deadline

With no policy given, the defaults come from this option's own cost history: fixed at
the 25th / 50th / 75th percentile of the touch cost, a ramp and a step from the 25th to
the 75th, and a behind-schedule rule from the 25th.

Caveats: the book is seen every 5 minutes and assumed to refill between snapshots (our
own taking does not move it); a few days of data overlap heavily across start times.

    .venv/bin/python -u scripts/execution_threshold_study.py --expiry 25DEC26 --strike 2500 --cp C
    .venv/bin/python -u scripts/execution_threshold_study.py --expiry 25DEC26 --strike 2500 --cp C \\
        --side buy --qty 10000 --hours 12 --cap 500 --ramp 1.6-2.25 --step 1.75-2.25@8 --fixed 1.75,2
    .venv/bin/python -u scripts/execution_threshold_study.py ... --vol --ramp 0.5-0.7
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.pricing.smile_fit import vega  # noqa: E402
from smile_flex_study import quotes, spline_fit, spline_price  # noqa: E402


def ladder(root: Path, expiry: str, K0: float, cp: str) -> pd.DataFrame:
    """Every recorded book level of the option, with its cost against the spline."""
    name = f"ETH-{expiry}-{K0:g}-{cp}"
    rows = []
    for f in sorted(root.glob("*/*.json.gz")):
        d = json.loads(gzip.decompress(f.read_bytes()))
        books = d["books"].get(expiry)
        b = next((x for x in books or [] if x["instrument_name"] == name), None)
        if b is None:
            continue
        ts = (d["t0"] + d["t1"]) / 2
        q = quotes(expiry, books, dt.datetime.fromtimestamp(ts, dt.timezone.utc))
        if q is None:
            continue
        idx = np.arange(len(q["K"]))
        spl, _ = spline_fit(q, idx[q["K"] != K0], True)
        fair = spline_price(q, spl, [K0], [cp])[0]
        S = b["estimated_delivery_price"]
        vg = vega(q["F"], K0, q["T"], float(spl(np.log(K0 / q["F"]))) / 100, q["DF"])
        for side, levels in (("buy", b["asks"]), ("sell", b["bids"])):
            for lvl, (px, amt) in enumerate(levels):
                usd = px * S
                fee = min(0.0003 * S, 0.125 * usd)
                rows.append(dict(ts=ts, side=side, level=lvl, px_usd=usd, amt=amt, fee=fee, fair=fair, vega=vg,
                                 cost=(usd - fair if side == "buy" else fair - usd) + fee))
    return pd.DataFrame(rows)


def parse_policies(args, scale: float, touch: pd.Series, hours: float) -> dict:
    """{label: theta(h, filled)} in USD; scale converts the CLI's units to USD."""
    last = hours - 1
    pol = {}
    for x in filter(None, (args.fixed or "").split(",")):
        a = float(x) * scale
        pol[f"fixed {x}"] = lambda h, f, a=a: a
    for x in filter(None, (args.ramp or "").split(",")):
        a, b = (float(v) * scale for v in x.split("-"))
        pol[f"ramp {x}"] = lambda h, f, a=a, b=b: a + (b - a) * min(h / last, 1)
    for x in filter(None, (args.step or "").split(",")):
        ab, at = x.split("@")
        a, b = (float(v) * scale for v in ab.split("-"))
        pol[f"step {x}"] = lambda h, f, a=a, b=b, at=float(at): a if h < at else b
    for x in filter(None, (args.behind or "").split(",")):
        a, k = (float(v) * scale for v in x.split("+"))
        pol[f"behind {x}"] = lambda h, f, a=a, k=k: a + k * max(0.0, h / last - f)
    if pol:
        return pol
    unit = 1 / scale
    q25, q50, q75 = (round(touch.quantile(p) * unit, 2) for p in (0.25, 0.5, 0.75))
    ns = argparse.Namespace(fixed=f"{q25},{q50},{q75}", ramp=f"{q25}-{q75}", step=f"{q25}-{q75}@{round(hours * 2 / 3)}",
                            behind=f"{q25}+{round(q75 - q25, 2) * 2}")
    return parse_policies(ns, scale, touch, hours)


def simulate(levels: pd.DataFrame, theta, qty: float, hours: float, cap: float | None) -> pd.DataFrame:
    """One parent order per start time: average cost paid, quantity left to the last hour, fill time, shortfall."""
    by = {t: g.sort_values("cost")[["cost", "amt"]].to_numpy() for t, g in levels.groupby("ts")}
    ticks = np.array(sorted(by))
    out = []
    for s in ticks[ticks + hours * 3600 <= ticks[-1] + 60]:
        left, paid, urgent, done = qty, 0.0, 0.0, np.nan
        for t in ticks[(ticks >= s) & (ticks < s + hours * 3600)]:
            h = (t - s) / 3600
            last = h >= hours - 1
            th = np.inf if last else theta(h, 1 - left / qty)
            room = left if cap is None or last else min(left, cap)
            for c, a in by[t]:
                if room <= 0 or c > th:
                    break
                take = min(a, room)
                room -= take
                left -= take
                paid += take * c
                urgent += take if last else 0
            if left <= 0:
                done = h
                break
        out.append(dict(start=s, cost=paid / (qty - left) if left < qty else np.nan, urgent=urgent,
                        hours=done, shortfall=left))
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expiry", required=True, help="Deribit expiry code, e.g. 25DEC26")
    ap.add_argument("--strike", type=float, required=True)
    ap.add_argument("--cp", choices=("C", "P"), required=True)
    ap.add_argument("--side", choices=("buy", "sell", "both"), default="both")
    ap.add_argument("--qty", type=float, default=10_000, help="contracts (ETH)")
    ap.add_argument("--hours", type=float, default=12, help="deadline")
    ap.add_argument("--cap", type=float, default=500, help="max contracts per snapshot outside the last hour; 0 = none")
    ap.add_argument("--vol", action="store_true", help="thresholds in vol points (x the option's median vega)")
    ap.add_argument("--fixed")
    ap.add_argument("--ramp")
    ap.add_argument("--step")
    ap.add_argument("--behind")
    ap.add_argument("--dir", type=Path, default=ROOT / "data" / "cache" / "smile_recorder")
    args = ap.parse_args()

    lv = ladder(args.dir.expanduser(), args.expiry, args.strike, args.cp)
    if lv.empty:
        sys.exit(f"no recorded books for ETH-{args.expiry}-{args.strike:g}-{args.cp} under {args.dir}")
    ts = np.sort(lv.ts.unique())
    vg, fee = lv.vega.median(), lv.fee.median()
    fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%d %b %H:%M")  # noqa: E731
    print(f"ETH-{args.expiry}-{args.strike:g}-{args.cp}: {len(ts)} snapshots, {fmt(ts[0])} .. {fmt(ts[-1])} UTC")
    print(f"fair (spline without this strike) median ${lv.fair.median():.2f}, vega ${vg:.2f} per vol point, "
          f"fee ${fee:.2f}; costs in USD per contract, fee included")
    cap = args.cap or None
    for side in (("buy", "sell") if args.side == "both" else (args.side,)):
        x = lv[lv.side == side]
        touch = x[x.level == 0].cost
        depth = x.groupby("ts").amt.sum()
        print(f"\n{side.upper()} {args.qty:,.0f} within {args.hours:g} h, at most {cap or 'any'} per snapshot")
        print(f"  touch: cost median ${touch.median():.2f} (25%-75% ${touch.quantile(.25):.2f}-${touch.quantile(.75):.2f}), "
              f"size median {x[x.level == 0].amt.median():,.0f}; visible depth median {depth.median():,.0f}")
        print(f"  {'cost <=':>10}{'snapshots with some':>21}{'mean available':>16}")
        for c in np.quantile(touch, [0.05, 0.25, 0.5, 0.75, 0.95]).round(2):
            avail = x[x.cost <= c].groupby("ts").amt.sum().reindex(depth.index, fill_value=0)
            print(f"  {'$' + format(c, '.2f'):>10}{100 * (avail > 0).mean():>20.0f}%{avail.mean():>16,.0f}")
        policies = parse_policies(args, vg if args.vol else 1.0, touch, args.hours)
        rows = []
        for label, theta in policies.items():
            r = simulate(x, theta, args.qty, args.hours, cap)
            rows.append((label, r))
        n = len(rows[0][1])
        if n == 0:
            print(f"  fewer than {args.hours:g} h of data: nothing to simulate")
            continue
        print(f"\n  {n} start times; cost per contract over them (vol points in brackets)")
        print(f"  {'policy' + (' (vol pts)' if args.vol else ' ($)'):26}{'average':>15}{'90%':>8}{'worst':>8}"
              f"{'last-hour qty':>15}{'h to fill':>10}{'shortfall':>10}")
        for label, r in rows:
            c = r.cost
            print(f"  {label:26}{c.mean():>7.2f} ({c.mean() / vg:4.2f}){c.quantile(.9):>8.2f}{c.max():>8.2f}"
                  f"{r.urgent.mean():>15,.0f}{r.hours.median():>10.1f}{r.shortfall.mean():>10,.0f}")
    print("\nlast-hour qty: crossed at any cost in the final hour; shortfall: not filled even then "
          "(the recorded book is 10 levels deep). Start times overlap: treat small differences as noise.")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # app imports can leave threads that hang interpreter exit
