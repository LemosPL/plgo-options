#!/usr/bin/env python3
"""Replay the execution bot's threshold rule on recorded quotes, for several start thresholds.

The bot (execution/bot.py) prices each option every 5 seconds against the expiry's smoothing
spline and takes the top of the book when its cost, fee included, is under a threshold that
ramps from start_vp to end_vp, then holds hard_vp in the final hour. This replays the same
decision (execution/threshold.decide, unchanged) on the books it would have seen:

  * every strike's top of book with its size, at each 5-second step, rebuilt from the quote
    recorder (each file starts with every quote, then one row per change);
  * marks, open interest and the future from the smile recorder's 5-minute snapshots (the
    future moved with the index in between; Deribit's mark vol interpolated);
  * one spline per step for the expiry (smile_fit.otm_quotes + spline_smile, as the bot).

Step 1 (--precompute, slow: ~60 ms a fit, run once, all cores) caches, for each step, the
index and each leg's fair value, vol, vega, whether it is inside the spline, its top of
book and Deribit's mark vol, an hour a file under data/cache/execution_replay/<expiry>/.

Step 2 (default) works the same parent orders from each window start (every --every hours
with a full window recorded) for each start threshold, with the bot's cap and 5-minute
rest after a fill, and reports per threshold, over the windows:

  * filled share, and hours until the last leg finished;
  * cost per contract against the spline, Deribit's mark and the fee, in vol points
    (the cost over the filled contracts' vega), as the Execution Bot page shows;
  * shortfall against arrival: each fill against the leg's fair value when the order
    started, which adds what the market did while we waited (the spline moves with it,
    so the cost against the spline leaves it out). Noisy: it is mostly the market's drift.

--validate replays the 9 Nov 2026 paper run (orders 16-19, start 0.5) and prints it next to
the fills the bot recorded, to check the replay sees what the bot saw.

Caveats: fills take the recorded top of book, which our taking does not deplete (the 5-minute
rest stands in for the book refilling); the bot's own feed and the recorder's are two
WebSocket connections, so the same second can differ slightly.

    .venv/bin/python -u scripts/execution_replay.py --precompute
    .venv/bin/python -u scripts/execution_replay.py --validate
    .venv/bin/python -u scripts/execution_replay.py --starts 0.3,0.35,0.4,0.45,0.5
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.execution.models import BookDepth  # noqa: E402
from plgo_options.execution.threshold import ThresholdOrder, decide  # noqa: E402
from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd  # noqa: E402
from plgo_options.pricing.smile_fit import otm_quotes, spline_fair, spline_smile  # noqa: E402

QUOTES = ROOT / "data" / "cache" / "quote_recorder"
SMILES = ROOT / "data" / "cache" / "smile_recorder"
CACHE = ROOT / "data" / "cache" / "execution_replay"
STEP, COOLDOWN, H = 5.0, 300.0, 3600.0
# The 9 Nov 2026 paper run: Optimizer v2's Thursday solution on 25DEC26, the box left out
LEGS = [("ETH-25DEC26-2000-P", "buy", 5000), ("ETH-25DEC26-2200-P", "sell", 2573),
        ("ETH-25DEC26-2400-C", "buy", 5000), ("ETH-25DEC26-2500-C", "buy", 3406)]
FIELDS = ("fair", "iv", "vega", "inside", "bid", "bsize", "ask", "asize", "mark_iv")


# ---------- step 1: the books and the spline at every step ----------
def _quote_files() -> list[tuple[float, Path]]:
    out = []
    for p in sorted(QUOTES.glob("*/*.csv.gz")):
        day, hms = p.parent.name, p.name[:6]
        t = dt.datetime.strptime(day + hms, "%Y-%m-%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
        out.append((t, p))
    return out


def _smile_snaps(expiry: str) -> list[tuple[float, Path]]:
    out = []
    for p in sorted(SMILES.glob("*/*.json.gz")):
        day, hms = p.parent.name, p.name[:6]
        try:
            t = dt.datetime.strptime(day + hms, "%Y-%m-%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
        out.append((t, p))
    return out


def _load_snap(p: Path, expiry: str) -> tuple[float, dict[str, dict]]:
    d = json.loads(gzip.decompress(p.read_bytes()))
    return (d["t0"] + d["t1"]) / 2, {b["instrument_name"]: b for b in d["books"].get(expiry, [])}


def _hour(args: tuple[str, float, list[str]]) -> str:
    """Cache one hour of steps: index, and per leg FIELDS (NaN where there is no fit)."""
    expiry, h0, legs = args
    out = CACHE / expiry / f"{dt.datetime.fromtimestamp(h0, dt.timezone.utc):%Y%m%d%H}.npz"
    if out.exists():
        return f"{out.name} cached"
    files = [(t, p) for t, p in _quote_files() if h0 - 120 <= t < h0 + H]
    snaps = [(t, p) for t, p in _smile_snaps(expiry) if h0 - 900 <= t < h0 + H + 900]
    steps = np.arange(h0, h0 + H, STEP)
    S = np.full(len(steps), np.nan)
    data = {f: np.full((len(steps), len(legs)), np.nan) for f in FIELDS}
    if not files or len(snaps) < 2:
        np.savez_compressed(out, steps=steps, S=S, **data)
        return f"{out.name} no data"
    loaded = [_load_snap(p, expiry) for _, p in snaps]
    snap_t = np.array([t for t, _ in loaded])
    snap_books = [b for _, b in loaded]
    marks = {n: np.array([b[n]["mark_iv"] if n in b else np.nan for b in snap_books]) for n in legs}
    name_ok = lambda n: n == "index" or f"-{expiry}-" in n  # noqa: E731
    frames = []
    for i, (t, p) in enumerate(files):
        try:
            f = pd.read_csv(p)
        except Exception:
            continue
        f = f[f.instrument.map(name_ok)]
        frames.append((t, files[i + 1][0] if i + 1 < len(files) else t + 60, f.sort_values("recv_ms")))
    for j, t in enumerate(steps):
        cur = [fr for fr in frames if fr[0] <= t + 1e-9][-1:]  # the file covering t, or the last before it
        if not cur or t - cur[0][0] > 75:                     # no file within a minute: a gap
            continue
        f = cur[0][2]
        state = f[f.recv_ms <= t * 1000].groupby("instrument").last()
        if "index" not in state.index:
            continue
        S_t = float(state.loc["index", "bid"])
        k = int(np.searchsorted(snap_t, t, side="right")) - 1
        if k < 0 or t - snap_t[k] > 900:
            continue
        books0 = snap_books[k]
        idx0 = next(iter(books0.values()))["index_price"] if books0 else None
        if not idx0:
            continue
        books = []
        for name, b in books0.items():
            r = state.loc[name] if name in state.index else None
            bid = float(r.bid) if r is not None and pd.notna(r.bid) and r.bid > 0 else None
            ask = float(r.ask) if r is not None and pd.notna(r.ask) and r.ask > 0 else None
            ba = float(r.bid_amount) if r is not None and pd.notna(r.bid_amount) else 0.0
            aa = float(r.ask_amount) if r is not None and pd.notna(r.ask_amount) else 0.0
            books.append({**b, "best_bid_price": bid, "best_ask_price": ask, "best_bid_amount": ba,
                          "best_ask_amount": aa, "estimated_delivery_price": S_t, "index_price": S_t,
                          "underlying_price": b["underlying_price"] * S_t / idx0})
        now = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        try:
            q = otm_quotes(expiry, books, now)
            smile = spline_smile(q) if q is not None else None
        except Exception:
            q = None
        if q is None:
            continue
        S[j] = S_t
        by = {b["instrument_name"]: b for b in books}
        for i, name in enumerate(legs):
            b = by.get(name)
            if b is None:
                continue
            _, _, K, cp = name.split("-")
            fv = spline_fair(q, float(K), cp, smile)
            m = marks[name]
            ok = np.isfinite(m)
            data["mark_iv"][j, i] = np.interp(t, snap_t[ok], m[ok]) if ok.sum() >= 2 else np.nan
            data["fair"][j, i], data["iv"][j, i], data["vega"][j, i] = fv["fair"], fv["iv"], fv["vega"]
            data["inside"][j, i] = float(fv["inside"])
            for fld, key in (("bid", "best_bid_price"), ("bsize", "best_bid_amount"),
                             ("ask", "best_ask_price"), ("asize", "best_ask_amount")):
                data[fld][j, i] = b[key] if b[key] is not None else np.nan
    np.savez_compressed(out, steps=steps, S=S, **data)
    return f"{out.name} {int(np.isfinite(S).sum())}/{len(steps)} steps"


def precompute(expiry: str, legs: list[str], workers: int) -> None:
    (CACHE / expiry).mkdir(parents=True, exist_ok=True)
    files = _quote_files()
    h_first = math.floor(files[0][0] / H) * H
    h_last = math.floor(files[-1][0] / H) * H - H     # the last full hour
    hours = np.arange(h_first, h_last + H, H)
    print(f"{len(hours)} hours, {workers} workers", flush=True)
    with ProcessPoolExecutor(workers) as ex:
        for msg in ex.map(_hour, [(expiry, float(h), legs) for h in hours]):
            print(msg, flush=True)


def load(expiry: str) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    parts = [np.load(p) for p in sorted((CACHE / expiry).glob("*.npz"))]
    steps = np.concatenate([p["steps"] for p in parts])
    S = np.concatenate([p["S"] for p in parts])
    data = {f: np.concatenate([p[f] for p in parts]) for f in FIELDS}
    return steps, S, data


# ---------- step 2: work the orders ----------
def work(steps, S, data, legs, t0: float, hours: float, start_vp: float, end_vp: float,
         hard_vp: float, cap: float) -> dict | None:
    """One window from t0: the fills of each leg, and the arrival fair values."""
    j0 = int(np.searchsorted(steps, t0))
    j1 = int(np.searchsorted(steps, t0 + hours * H))
    if j1 >= len(steps) or j0 >= j1:
        return None
    cover = float(np.isfinite(S[j0:j1]).mean())
    first = [next((j for j in range(j0, j1) if np.isfinite(data["fair"][j, i])), None) for i in range(len(legs))]
    if any(f is None for f in first):
        return None
    arrival = [(data["fair"][f, i], data["vega"][f, i]) for i, f in enumerate(first)]
    orders = [ThresholdOrder(id=i, instrument=n, side=side, qty=float(q), filled=0.0, created_ts=t0,
                             deadline_ts=t0 + hours * H, start_vp=start_vp, end_vp=end_vp,
                             hard_vp=hard_vp, cap=cap) for i, (n, side, q) in enumerate(legs)]
    last_fill = [-1e18] * len(legs)
    fills: list[dict] = []
    done_ts = [None] * len(legs)
    for j in range(j0, j1):
        if not np.isfinite(S[j]):
            continue
        t = float(steps[j])
        for i, o in enumerate(orders):
            if o.remaining <= 0 or t - last_fill[i] < COOLDOWN or not np.isfinite(data["fair"][j, i]):
                continue
            fair = dict(fair=data["fair"][j, i], iv=data["iv"][j, i], vega=data["vega"][j, i],
                        inside=bool(data["inside"][j, i]))
            bid, ask = data["bid"][j, i], data["ask"][j, i]
            bids = [(bid, data["bsize"][j, i])] if np.isfinite(bid) else []
            asks = [(ask, data["asize"][j, i])] if np.isfinite(ask) else []
            d = decide(o, t, fair, float(S[j]), BookDepth(o.instrument, bids, asks, None, t))
            if d.action != "take":
                continue
            usd = d.limit_price * S[j]
            sign = 1.0 if o.side == "buy" else -1.0
            mark_usd = fair["fair"] + (data["mark_iv"][j, i] - fair["iv"] * 100) * fair["vega"]
            fills.append(dict(leg=i, ts=t, qty=d.qty, vega=fair["vega"], vega0=arrival[i][1],
                              spline=sign * (usd - fair["fair"]), mark=sign * (usd - mark_usd),
                              fee=deribit_option_fee_usd(float(S[j]), usd), arrival=sign * (usd - arrival[i][0]),
                              theta=d.theta_vp))
            orders[i] = o = replace(o, filled=o.filled + d.qty)
            last_fill[i] = t
            if o.remaining <= 0:
                done_ts[i] = t
    return dict(t0=t0, cover=cover, fills=fills, orders=orders, done_ts=done_ts)


def vp(fills: list[dict], key: str, w: str = "vega") -> float:
    v = sum(f["qty"] * f[w] for f in fills)
    return sum(f["qty"] * f[key] for f in fills) / v if v else float("nan")


def summarize(run: dict, legs) -> dict:
    f = run["fills"]
    qty = sum(q for _, _, q in legs)
    filled = sum(o.filled for o in run["orders"])
    hrs = [(d - run["t0"]) / H for d in run["done_ts"] if d is not None]
    return dict(filled=filled / qty, all_done=all(d is not None for d in run["done_ts"]),
                hours=max(hrs) if len(hrs) == len(legs) else float("nan"),
                spline=vp(f, "spline"), mark=vp(f, "mark"), fee=vp(f, "fee"),
                arrival=vp(f, "arrival", "vega0"), usd_spline=sum(x["qty"] * x["spline"] for x in f),
                usd_mark=sum(x["qty"] * x["mark"] for x in f), usd_fee=sum(x["qty"] * x["fee"] for x in f),
                usd_arrival=sum(x["qty"] * x["arrival"] for x in f), max_theta=max((x["theta"] for x in f), default=0))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--expiry", default="25DEC26")
    p.add_argument("--precompute", action="store_true")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--validate", action="store_true")
    p.add_argument("--starts", default="0.3,0.35,0.4,0.45,0.5")
    p.add_argument("--end", type=float, default=0.7)
    p.add_argument("--hard", type=float, default=0.9)
    p.add_argument("--cap", type=float, default=500)
    p.add_argument("--hours", type=float, default=12)
    p.add_argument("--every", type=float, default=2, help="hours between window starts")
    p.add_argument("--min-cover", type=float, default=0.95, help="share of steps with data a window needs")
    a = p.parse_args()
    legs = LEGS
    names = [n for n, _, _ in legs]
    if a.precompute:
        precompute(a.expiry, names, a.workers)
        return
    steps, S, data = load(a.expiry)
    print(f"cache: {dt.datetime.fromtimestamp(steps[0], dt.timezone.utc):%d %b %H:%M} to "
          f"{dt.datetime.fromtimestamp(steps[-1], dt.timezone.utc):%d %b %H:%M} UTC, "
          f"{np.isfinite(S).mean():.0%} of steps with a fit", flush=True)

    if a.validate:
        import sqlite3
        db = sqlite3.connect(ROOT / "data" / "plgo_options.db")
        t0 = db.execute("select min(created_ts) from exec_orders where id between 16 and 19").fetchone()[0]
        run = work(steps, S, data, legs, t0, 12, 0.5, 0.7, 0.9, 500)
        rows = db.execute("""select o.instrument, f.ts, f.qty, f.price_usd, f.fair_usd, t.vega from exec_fills f
                             join exec_orders o on o.id = f.order_id
                             join exec_ticks t on t.order_id = f.order_id and t.ts = f.ts
                             where o.id between 16 and 19""").fetchall()
        print(f"\nreplay of the 9 Nov paper run from {dt.datetime.fromtimestamp(t0, dt.timezone.utc):%H:%M} UTC "
              f"(start 0.5, end 0.7, hard 0.9, cap 500), data on {run['cover']:.0%} of steps")
        print(f"{'leg':22} {'replay: fills':>13} {'done (h)':>8} {'vs spline':>9}   {'bot: fills':>10} {'done (h)':>8} {'vs spline':>9}")
        for i, (n, side, q) in enumerate(legs):
            rf = [f for f in run["fills"] if f["leg"] == i]
            bf = [r for r in rows if r[0] == n]
            sign = 1 if side == "buy" else -1
            bvega = {r: r[5] for r in bf}                     # the vega the bot priced the fill with
            bv = sum(r[2] * bvega[r] for r in bf)
            bsp = sum(r[2] * sign * (r[3] - r[4]) for r in bf) / bv if bv else float("nan")
            rdone = (run["done_ts"][i] - t0) / H if run["done_ts"][i] else float("nan")
            bdone = (max(r[1] for r in bf) - t0) / H if bf else float("nan")
            print(f"{side + ' ' + n[12:]:22} {len(rf):>6} {sum(f['qty'] for f in rf):>6.0f} {rdone:>8.1f} "
                  f"{vp(rf, 'spline'):>9.3f}   {len(bf):>4} {sum(r[2] for r in bf):>5.0f} {bdone:>8.1f} {bsp:>9.3f}")
        return

    starts = [float(x) for x in a.starts.split(",")]
    t_first = math.ceil(steps[0] / H) * H
    windows = np.arange(t_first, steps[-1] - a.hours * H, a.every * H)
    res = {s: [] for s in starts}
    used = []
    for t0 in windows:
        runs = {s: work(steps, S, data, legs, float(t0), a.hours, s, max(s, a.end), a.hard, a.cap) for s in starts}
        if any(r is None or r["cover"] < a.min_cover for r in runs.values()):
            continue
        used.append(t0)
        for s, r in runs.items():
            res[s].append(summarize(r, legs))
    print(f"\n{len(used)} windows of {a.hours:g} h, starting every {a.every:g} h from "
          f"{dt.datetime.fromtimestamp(used[0], dt.timezone.utc):%d %b %H:%M} to "
          f"{dt.datetime.fromtimestamp(used[-1], dt.timezone.utc):%d %b %H:%M} UTC; "
          f"legs: {', '.join(f'{s} {q} {n[12:]}' for n, s, q in legs)}; end {a.end:g}, hard {a.hard:g}, cap {a.cap:g}")
    print("vol points per contract, quantity x vega weighted; mean over windows (worst window in brackets)\n")
    print(f"{'start':>5} {'filled':>7} {'all done':>8} {'hours':>11} {'vs spline':>9} {'vs mark':>8} {'fee':>6} "
          f"{'all-in mk':>9} {'vs arrival':>16} {'$ spline+fee':>12}")
    for s in starts:
        d = pd.DataFrame(res[s])
        print(f"{s:>5.2f} {d.filled.mean():>7.1%} {d.all_done.mean():>8.0%} "
              f"{d.hours.mean():>5.1f} ({d.hours.max():>4.1f}) {d.spline.mean():>9.3f} {d.mark.mean():>8.3f} "
              f"{d.fee.mean():>6.3f} {(d.mark + d.fee).mean():>9.3f} {d.arrival.mean():>7.3f} (±{d.arrival.std():.2f}) "
              f"{(d.usd_spline + d.usd_fee).mean():>12,.0f}")
    out = CACHE / a.expiry / "replay_results.csv"
    pd.concat([pd.DataFrame(res[s]).assign(start=s, t0=used) for s in starts]).to_csv(out, index=False)
    print(f"\nper window: {out}")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # app modules leave threads that hang interpreter exit
