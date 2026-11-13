"""Markouts: how the market moved after each fill, 5 seconds to 1 hour on.

At a fill the bot stores the market as it was (exec_markouts, horizon 0: index, the spline's fair
value, the mid, the spline vol and vega). Then, on its passes, ``observe`` reads the market again
at each horizon in HORIZONS for every fill of the last hour: the option's mid and the expiry's
spline, as the bot prices. The scheduler keeps calling the bot while observations are due, so a
markout is taken even after the order has finished.

``summary`` reports, per horizon, in vol points (USD per contract over the fill's vega, weighted by
contracts x vega), signed so that positive is the market moving our way, and delta-hedged with the
fill's Black delta at the index (the option's own move, not ETH's):

    spline   sign x (fair(t+h) - fair(t)) - sign x delta x (S(t+h) - S(t))
             the fair value moving after our take: negative, we traded just before the market
             went against us (adverse selection); none of the half-spread is in it
    mid      sign x (mid(t+h) - price) - sign x delta x (S(t+h) - S(t))
             what the fill is worth against the screen mid later: starts near minus the half-spread
             crossed; drifting further down after a take is makers requoting worse (live fills only:
             a paper fill touched no real book)

Paper fills measure our timing; testnet fills' prices are testnet's, so only their spline column means
anything; live fills measure all of it.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from scipy.stats import norm

from plgo_options.pricing.smile_fit import fit_error, otm_quotes, parse_exp, spline_fair, spline_smile

from . import store
from .threshold import MAX_FIT_ERROR

HORIZONS = (5.0, 30.0, 60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0)
LATE = 60.0  # an observation more than this past due is recorded empty: the bot was not running then


def _late(h: float) -> float:
    return max(15.0, min(LATE, 0.2 * h))


async def observe(now: float, books_fn: Callable[[list[str], list[str]], Awaitable[dict[str, list[dict]]]]) -> int:
    """Record the markouts due now; returns how many were recorded (missed ones included)."""
    due = await store.due_markouts(now, HORIZONS, LATE)
    if not due:
        return 0
    rows, take = [], []
    for d in due:
        if now - d["due"] > _late(d["horizon"]):  # too late to say what the market was then
            rows.append({"fill_id": d["fill_id"], "horizon": d["horizon"], "ts": now})
        else:
            take.append(d)
    if take:
        expiries = sorted({d["instrument"].split("-")[1] for d in take})
        books = await books_fn(expiries, sorted({d["instrument"] for d in take}))
        fits = {}
        for e in expiries:
            q = otm_quotes(e, books.get(e, []), dt.datetime.fromtimestamp(now, dt.timezone.utc))
            smile = spline_smile(q) if q is not None else None
            ok = smile is not None and fit_error(q, smile)[0] <= MAX_FIT_ERROR
            fits[e] = (q, smile if ok else None)  # a spline off its quotes gives no fair value
        for d in take:
            _, e, K, cp = d["instrument"].split("-")
            b = next((x for x in books.get(e, []) if x["instrument_name"] == d["instrument"]), None)
            if b is None:
                rows.append({"fill_id": d["fill_id"], "horizon": d["horizon"], "ts": now})
                continue
            S = b["estimated_delivery_price"]
            bid, ask = b.get("best_bid_price"), b.get("best_ask_price")
            q, smile = fits[e]
            f = spline_fair(q, float(K), cp, smile) if smile is not None else None
            rows.append({"fill_id": d["fill_id"], "horizon": d["horizon"], "ts": now, "S": S,
                         "mid_usd": (bid + ask) / 2 * S if bid and ask else None,
                         "fair_usd": f["fair"] if f and f["inside"] else None,
                         "iv": f["iv"] if f else None, "vega": f["vega"] if f else None})
    await store.add_markouts(rows)
    return len(rows)


def _delta(instrument: str, S: float, iv: float, ts: float) -> float:
    _, e, K, cp = instrument.split("-")
    T = max((parse_exp(e).timestamp() - ts) / (365 * 86400), 1e-6)
    d1 = (math.log(S / float(K)) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return float(norm.cdf(d1)) - (0.0 if cp == "C" else 1.0)


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """{horizons, all: {h: {spline_vp, mid_vp, n}}, legs: [per order, the same]} from store.markout_rows."""
    by_fill: dict[int, dict[float, dict]] = defaultdict(dict)
    for r in rows:
        by_fill[r["fill_id"]][r["horizon"]] = r
    acc: dict[Any, dict[float, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    legs: dict[int, dict[str, Any]] = {}
    for obs in by_fill.values():
        r0 = obs.get(0.0)
        if r0 is None or not r0["vega"] or r0["S"] is None or not r0["iv"]:
            continue
        sg = 1.0 if r0["side"] == "buy" else -1.0
        dl = _delta(r0["instrument"], r0["S"], r0["iv"], r0["fill_ts"])
        w = r0["qty"] * r0["vega"]
        legs.setdefault(r0["order_id"], {"id": r0["order_id"], "instrument": r0["instrument"], "side": r0["side"],
                                         "mode": r0["mode"], "fills": 0, "qty": 0.0, "last_ts": 0.0})
        g = legs[r0["order_id"]]
        g["fills"] += 1
        g["qty"] += r0["qty"]
        g["last_ts"] = max(g["last_ts"], r0["fill_ts"])
        for h in HORIZONS:
            r = obs.get(h)
            if r is None or r["S"] is None:
                continue
            hedge = sg * dl * (r["S"] - r0["S"])
            vals = {}
            if r["fair_usd"] is not None and r0["fair_usd"] is not None:
                vals["spline"] = sg * (r["fair_usd"] - r0["fair_usd"]) - hedge
            if r["mid_usd"] is not None:
                vals["mid"] = sg * (r["mid_usd"] - r0["price_usd"]) - hedge
            for key in ("all", r0["order_id"]):
                a = acc[key][h]
                for name, v in vals.items():
                    a[f"{name}_usd"] += v * r0["qty"]
                    a[f"{name}_w"] += w
                a["n"] += 1 if vals else 0

    def cells(key):
        out = {}
        for h in HORIZONS:
            a = acc[key].get(h, {})
            out[str(int(h))] = {name: (a[f"{name}_usd"] / a[f"{name}_w"] if a.get(f"{name}_w") else None)
                                for name in ("spline", "mid")} | {"n": int(a.get("n", 0))}
        return out

    return {"horizons": [int(h) for h in HORIZONS], "all": cells("all"),
            "legs": [g | {"cells": cells(g["id"])} for g in sorted(legs.values(), key=lambda g: -g["last_ts"])]}
