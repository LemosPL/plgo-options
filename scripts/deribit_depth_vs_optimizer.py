#!/usr/bin/env python3
"""How much of what the optimizer trades could Deribit fill at once, and at what cost?

Question behind it: pricing Deribit at its best bid/ask (pricing/exchange_quotes)
is only right for small size. Before building an internal market maker with a
size-aware quote, measure the gap between the sizes the optimizer proposes and
the depth Deribit actually shows.

Sizes come from the saved optimizer use cases (data/optimization_snapshots/
usecases/*.json, result.trades, new option legs). Their expiries are mostly
past, so each leg is mapped to TODAY's Deribit option with the nearest days to
expiry and the nearest moneyness (strike / spot) of the same type, and that
order book (public/get_order_book) is walked for the leg's size:

    touch_x       qty / size shown at the touch on our side
    fill_pct      share of qty the whole visible book could fill right now
    touch_vp      half-spread at the touch, in vol points (what the current
                  exchange_book model charges, fee aside)
    vwap_vp       cost of the fillable part vs mid, in vol points (impact included)

Vol points line up with the optimizer's VOLpts input (default 0.75).
Visible depth is a floor: market makers refill, and block RFQ is not visible
here, so this measures instant on-screen liquidity, not capacity over time.

    .venv/bin/python -u scripts/deribit_depth_vs_optimizer.py              # one snapshot
    .venv/bin/python -u scripts/deribit_depth_vs_optimizer.py --every 30   # every 30 min, Ctrl-C stops
    .venv/bin/python -u scripts/deribit_depth_vs_optimizer.py --summary    # summarize every saved snapshot

Each run appends one row per leg to data/cache/deribit_depth/legs.csv (ignored by git).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.pricing.exchange_quotes import _implied_vol  # noqa: E402

API = "https://www.deribit.com/api/v2/public"
USECASES = ROOT / "data" / "optimization_snapshots" / "usecases"
OUT = ROOT / "data" / "cache" / "deribit_depth" / "legs.csv"
YEAR_S = 365.25 * 86400
FIELDS = ["run_at", "source", "src_spot", "src_dte", "src_moneyness", "opt", "side", "qty",
          "instrument", "dte", "spot", "touch_size", "book_size", "touch_x", "fill_pct",
          "mid_iv", "touch_vp", "vwap_vp", "levels"]


def load_legs(include_unwinds: bool, max_qty: float) -> list[dict]:
    legs = []
    for f in sorted(glob.glob(str(USECASES / "*.json"))):
        try:
            d = json.load(open(f))
        except (OSError, ValueError):
            continue  # a few early snapshots were written truncated
        oi = d.get("optimizer_input") or {}
        spot = float(oi.get("spot") or 0)
        if str(oi.get("asset", "ETH")).upper() != "ETH" or spot <= 0:
            continue
        for t in (d.get("result") or {}).get("trades") or []:
            qty = abs(float(t.get("qty") or 0))
            if t.get("opt") not in ("C", "P") or not qty or qty > max_qty:
                continue
            if t.get("is_unwind") and not include_unwinds:
                continue
            legs.append({"source": Path(f).name, "src_spot": spot, "src_dte": int(t.get("dte") or 0),
                         "src_moneyness": float(t["strike"]) / spot, "opt": t["opt"],
                         "side": "buy" if float(t["qty"]) > 0 else "sell", "qty": qty})
    return legs


async def get(client: httpx.AsyncClient, method: str, **params) -> dict | list:
    for attempt in range(4):
        r = await client.get(f"{API}/{method}", params=params)
        if r.status_code == 429:  # rate limited: back off and retry
            await asyncio.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()["result"]
    raise RuntimeError(f"{method}: rate limited")


def nearest(instruments: list[dict], spot: float, leg: dict, now: float) -> dict | None:
    """Today's option closest to the leg in days to expiry, then moneyness."""
    same = [i for i in instruments if i["option_type"][0].upper() == leg["opt"]]
    if not same:
        return None
    dte = lambda i: (i["expiration_timestamp"] / 1000 - now) / 86400  # noqa: E731
    live = [i for i in same if dte(i) >= 1]
    exp = min({i["expiration_timestamp"] for i in live},
              key=lambda e: abs((e / 1000 - now) / 86400 - leg["src_dte"]))
    on_exp = [i for i in live if i["expiration_timestamp"] == exp]
    return min(on_exp, key=lambda i: abs(i["strike"] / spot - leg["src_moneyness"]))


def walk(book: dict, leg: dict, inst: dict, now: float) -> dict:
    """Fill the leg's qty through the visible book; costs in vol points vs mid."""
    und = float(book.get("underlying_price") or book.get("index_price"))
    K, opt = float(inst["strike"]), leg["opt"]
    T = (inst["expiration_timestamp"] / 1000 - now) / YEAR_S
    levels = book["asks"] if leg["side"] == "buy" else book["bids"]
    bid, ask = book.get("best_bid_price"), book.get("best_ask_price")
    mid = (bid + ask) / 2 if bid and ask else book.get("mark_price")
    iv = lambda px_eth: _implied_vol(px_eth * und, und, K, T, opt)  # noqa: E731
    out = {"instrument": inst["instrument_name"], "dte": round(T * 365.25, 1), "spot": und,
           "touch_size": levels[0][1] if levels else 0.0,
           "book_size": sum(a for _, a in levels), "levels": len(levels)}
    left, cost, filled = leg["qty"], 0.0, 0.0
    for px, amt in levels:
        take = min(left, amt)
        cost, filled, left = cost + take * px, filled + take, left - take
        if left <= 0:
            break
    out["touch_x"] = round(leg["qty"] / out["touch_size"], 1) if out["touch_size"] else None
    out["fill_pct"] = round(100 * filled / leg["qty"], 1)
    mid_iv = iv(mid) if mid else None
    sign = 1 if leg["side"] == "buy" else -1
    touch_iv = iv(levels[0][0]) if levels else None
    vwap_iv = iv(cost / filled) if filled else None
    out["mid_iv"] = round(mid_iv * 100, 2) if mid_iv else None
    out["touch_vp"] = round(sign * (touch_iv - mid_iv) * 100, 2) if touch_iv and mid_iv else None
    out["vwap_vp"] = round(sign * (vwap_iv - mid_iv) * 100, 2) if vwap_iv and mid_iv else None
    return out


async def snapshot(legs: list[dict], depth: int) -> list[dict]:
    now = time.time()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with httpx.AsyncClient(timeout=15) as client:
        spot = float((await get(client, "get_index_price", index_name="eth_usd"))["index_price"])
        instruments = await get(client, "get_instruments", currency="ETH", kind="option",
                                expired="false")
        mapped = [(leg, nearest(instruments, spot, leg, now)) for leg in legs]
        names = sorted({i["instrument_name"] for _, i in mapped if i})
        sem = asyncio.Semaphore(5)
        books: dict[str, dict] = {}

        async def fetch(name: str) -> None:
            async with sem:
                books[name] = await get(client, "get_order_book", instrument_name=name, depth=depth)

        await asyncio.gather(*(fetch(n) for n in names))
    rows = []
    for leg, inst in mapped:
        if inst is None:
            continue
        rows.append({"run_at": run_at, **leg, **walk(books[inst["instrument_name"]], leg, inst, now)})
    return rows


def append(rows: list[dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new = not OUT.exists()
    with OUT.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def _med(xs: list) -> str:
    xs = [float(x) for x in xs if x not in (None, "")]
    return f"{statistics.median(xs):,.1f}" if xs else "-"


def report(rows: list[dict]) -> None:
    def bucket(r: dict) -> str:
        q = float(r["qty"])
        return ("< 1k" if q < 1_000 else "1k-5k" if q < 5_000 else "5k-20k" if q < 20_000
                else ">= 20k")

    def dte_bucket(r: dict) -> str:
        d = float(r["dte"])
        return "< 30d" if d < 30 else "30-90d" if d < 90 else ">= 90d"

    runs = sorted({r["run_at"] for r in rows})
    print(f"\n{len(rows)} legs over {len(runs)} snapshot(s), {runs[0]} .. {runs[-1]}")
    full = sum(1 for r in rows if float(r["fill_pct"]) >= 100)
    print(f"fully fillable from the visible book: {full} of {len(rows)} "
          f"({100 * full / len(rows):.0f}%)\n")
    head = (f"{'':10}{'legs':>6}{'med qty':>10}{'qty/touch':>11}{'fill %':>8}"
            f"{'touch vp':>10}{'vwap vp':>9}")
    for title, key in (("by size (ETH)", bucket), ("by expiry", dte_bucket)):
        print(title)
        print(head)
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(key(r), []).append(r)
        order = ["< 1k", "1k-5k", "5k-20k", ">= 20k", "< 30d", "30-90d", ">= 90d"]
        for g in sorted(groups, key=order.index):
            rs = groups[g]
            print(f"{g:10}{len(rs):>6}{_med([r['qty'] for r in rs]):>10}"
                  f"{_med([r['touch_x'] for r in rs]):>11}{_med([r['fill_pct'] for r in rs]):>8}"
                  f"{_med([r['touch_vp'] for r in rs]):>10}{_med([r['vwap_vp'] for r in rs]):>9}")
        print()
    print("vp = vol points vs mid (the optimizer's VOLpts default is 0.75). vwap vp covers "
          "only the part the book can fill.")


async def main(args: argparse.Namespace) -> None:
    if args.summary:
        if not OUT.exists():
            print(f"nothing saved yet at {OUT}")
            return
        with OUT.open() as fh:
            report(list(csv.DictReader(fh)))
        return
    legs = load_legs(args.include_unwinds, args.max_qty)
    print(f"{len(legs)} optimizer legs from {USECASES.relative_to(ROOT)}", flush=True)
    while True:
        rows = await snapshot(legs, args.depth)
        append(rows)
        report(rows)
        print(f"appended to {OUT.relative_to(ROOT)}", flush=True)
        if not args.every:
            return
        await asyncio.sleep(args.every * 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--every", type=float, default=0, help="minutes between snapshots (0 = once)")
    p.add_argument("--depth", type=int, default=100, help="order book levels to read")
    p.add_argument("--max-qty", type=float, default=100_000,
                   help="drop legs above this size (a few early July runs proposed 700k+ ETH)")
    p.add_argument("--include-unwinds", action="store_true",
                   help="also count unwinds (those trade back with the OTC counterparty)")
    p.add_argument("--summary", action="store_true", help="summarize every saved snapshot")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
