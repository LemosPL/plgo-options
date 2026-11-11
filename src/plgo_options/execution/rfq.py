"""From an optimizer solution to Deribit Block RFQ structures (preview only: nothing is sent).

A Block RFQ is one structure: legs with integer ratios times an amount, quoted by market
makers as one price (private/create_block_rfq, then private/accept_block_rfq with our worst
acceptable price). A solution's Deribit trades become:

  * one RFQ per box (the optimizer's BOX_NEUTRALIZER trades, grouped by their structure):
    four legs of equal size, ratio 1, priced as a rate. Its allowance is in bps of the box's
    notional (strike width), like the optimizer's box_fee_bps;
  * one RFQ per expiry for the other legs, in a round unit (``unit`` contracts): each leg's
    ratio is its quantity over the unit, rounded down, the ratios divided by their common
    factor. What rounding leaves over (under one unit a leg) goes to the execution bot on
    screen. A leg smaller than one unit goes to the bot whole. Its allowance is in vol
    points times the structure's vega (the sum over its legs, each counted once);
  * with ``hedge``, the directional RFQ carries a hedge leg in the same expiry's future for its
    net delta (one hedge leg per RFQ, fee-free in Deribit's Block RFQ until the end of 2026), so
    makers quote it delta-neutral. Off by default: the solution's delta is the optimizer's
    choice (it may be hedging the book), and a hedge leg would change the portfolio it chose.
    The structure's delta is shown either way.

Prices are per unit of the structure's amount, signed as Deribit prices a combo: each leg's
price times its ratio, added for a buy and subtracted for a sell (to confirm on testnet). Fair
is the smoothing spline's (smile_fit.spline_fair, as the bot prices). For each structure:

    screen   crossing every leg at the touch now, against fair, plus each leg's taker fee
    limit    fair + allowance: the worst price we would accept (a buy pays at most this)
    fees     the screen's taker fees, and the RFQ's estimate: Deribit's combo discount waives
             the cheaper of the buy legs' and the sell legs' fees, at the screen's rate (the
             options block rate is to confirm with Deribit)

Box legs are kept apart from the other legs even on the same instrument, so a box goes out
as a box. Leftovers come back in the shape of execution-bot orders.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections import defaultdict
from functools import reduce
from typing import Any

from scipy.stats import norm

from plgo_options.market_data.smile_recorder import expiry_books
from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd
from plgo_options.pricing.smile_fit import otm_quotes, parse_exp, spline_fair, spline_smile

from .from_optimizer import _listed, deribit_legs, fit_warnings

BOX = "BOX_NEUTRALIZER"


def split(trades: list[dict], asset: str = "ETH") -> tuple[list[list[dict]], list[dict], list[dict]]:
    """(boxes, other legs, skipped): each box its own deribit_legs, the rest netted together."""
    groups: dict[str, list[dict]] = defaultdict(list)
    rest = []
    for t in trades:
        if t.get("strategy") == BOX:
            groups[str(t.get("strategy_instrument") or "box")].append(t)
        else:
            rest.append(t)
    boxes, skipped = [], []
    for g in groups.values():
        legs, sk = deribit_legs(g, asset)
        skipped += sk
        if legs:
            boxes.append(legs)
    legs, sk = deribit_legs(rest, asset)
    return boxes, legs, skipped + sk


def ratios(qtys: list[int], unit: int) -> tuple[list[int], int, list[int]]:
    """(ratios, amount, leftovers): ratio x amount + leftover = qty for each leg."""
    r = [q // unit for q in qtys]
    g = reduce(math.gcd, [x for x in r if x], 0) or 1
    r = [x // g for x in r]
    amount = unit * g
    return r, amount, [q - x * amount for q, x in zip(qtys, r)]


def _fwd_delta(F: float, K: float, T: float, vol: float, cp: str) -> float:
    """Forward delta of one contract (ETH): N(d1) for a call, N(d1) - 1 for a put."""
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * math.sqrt(T))
    return float(norm.cdf(d1)) - (0.0 if cp == "C" else 1.0)


def _future(expiry: str) -> str:
    return f"ETH-{expiry}"


def price_structure(kind: str, legs: list[dict], ratio: list[int], amount: int, fits: dict, books: dict,
                    allowance_vp: float, box_bps: float, hedge: bool = False) -> dict[str, Any]:
    """One structure's legs priced now: fair, the touch, screen cost, fees, limit, hedge."""
    expiry = legs[0]["instrument"].split("-")[1]
    q, smile = fits[expiry]
    by = {b["instrument_name"]: b for b in books[expiry]}
    S = next(iter(by.values()))["estimated_delivery_price"]
    out_legs, fair = [], 0.0
    screen = vega = delta = 0.0
    fee_buy = fee_sell = 0.0
    for leg, r in zip(legs, ratio):
        if r == 0:
            continue
        _, _, K, cp = leg["instrument"].split("-")
        f = spline_fair(q, float(K), cp, smile)
        b = by.get(leg["instrument"], {})
        sign = 1.0 if leg["side"] == "buy" else -1.0
        bid, ask = b.get("best_bid_price"), b.get("best_ask_price")
        touch = ask if leg["side"] == "buy" else bid
        touch_usd = touch * S if touch else None
        fee = deribit_option_fee_usd(S, touch_usd if touch_usd else f["fair"])
        cross = (sign * (touch_usd - f["fair"]) if touch_usd else math.nan) + fee
        d = _fwd_delta(q["F"], float(K), q["T"], f["iv"], cp)
        fair += sign * r * f["fair"]
        screen += r * cross
        vega += r * f["vega"]
        delta += sign * r * d
        if leg["side"] == "buy":
            fee_buy += r * fee
        else:
            fee_sell += r * fee
        out_legs.append(dict(instrument=leg["instrument"], side=leg["side"], ratio=r, qty=r * amount,
                             fair_usd=round(f["fair"], 2), iv=round(f["iv"] * 100, 2), vega=round(f["vega"], 3),
                             delta=round(d, 3), inside=f["inside"],
                             bid_usd=None if not bid else round(bid * S, 2), ask_usd=None if not ask else round(ask * S, 2),
                             cross_usd=None if math.isnan(cross) else round(cross, 2)))
    if kind == "box":
        Ks = [float(x["instrument"].split("-")[2]) for x in out_legs]
        notional = (max(Ks) - min(Ks)) * max(ratio)
        allowance = box_bps / 1e4 * notional
        basis = f"{box_bps:g} bps of the ${notional:,.0f} strike width"
    else:
        allowance = allowance_vp * vega
        basis = f"{allowance_vp:g} vol pts x ${vega:,.2f} of vega"
    hedge_leg = None
    if hedge and kind != "box" and abs(delta * amount) * q["F"] >= 10:
        fut = _future(expiry)
        hedge_leg = dict(instrument_name=fut, direction="sell" if delta > 0 else "buy",
                     amount=round(abs(delta * amount) * q["F"]),   # USD: Deribit's ETH future is $1 a contract
                     delta_eth=round(-delta * amount, 1), price_note="the future's mark when sent")
    rfq_fee = max(fee_buy, fee_sell)
    return dict(kind=kind, expiry=expiry, amount=amount, legs=out_legs, S=round(S, 2),
                fair_usd=round(fair, 2), fair_eth=round(fair / S, 5),
                limit_usd=round(fair + allowance, 2), limit_eth=round((fair + allowance) / S, 5),
                allowance_usd=round(allowance, 2), allowance_basis=basis,
                screen_cost_usd=None if math.isnan(screen) else round(screen, 2),
                vega_usd=round(vega, 3), delta_eth=round(delta, 4), hedge=hedge_leg,
                delta_total_eth=round(delta * amount, 1), delta_total_usd=round(delta * amount * q["F"], 0),
                screen_fee_usd=round(fee_buy + fee_sell, 2), rfq_fee_usd=round(rfq_fee, 2),
                total=dict(fair_usd=round(fair * amount, 0), allowance_usd=round(allowance * amount, 0),
                           screen_cost_usd=None if math.isnan(screen) else round(screen * amount, 0),
                           screen_fee_usd=round((fee_buy + fee_sell) * amount, 0), rfq_fee_usd=round(rfq_fee * amount, 0)))


async def preview_rfq(trades: list[dict], asset: str = "ETH", unit: int = 100, allowance_vp: float = 0.2,
                      box_bps: float = 40.0, hedge: bool = False, listed_fn=_listed,
                      books_fn=expiry_books) -> dict[str, Any]:
    """The solution's Deribit trades as Block RFQ structures, priced now, and the leftovers."""
    boxes, legs, skipped = split(trades, asset)
    now = time.time()
    names = await listed_fn(asset) if boxes or legs else set()

    def listed(ls: list[dict]) -> list[dict]:
        ok = []
        for x in ls:
            if x["instrument"] not in names:
                skipped.append({"trade": " + ".join(x["sources"]), "reason": f"{x['instrument']} is not listed on Deribit"})
            elif parse_exp(x["instrument"].split("-")[1]).timestamp() <= now:
                skipped.append({"trade": " + ".join(x["sources"]), "reason": f"{x['instrument']} has expired"})
            else:
                ok.append(x)
        return ok

    boxes = [b for b in (listed(b) for b in boxes) if b]
    legs = listed(legs)
    structs: list[tuple[str, list[dict], list[int], int]] = []
    leftovers: list[dict] = []
    for b in boxes:
        r, amount, left = ratios([x["qty"] for x in b], min(x["qty"] for x in b))
        structs.append(("box", b, r, amount))
        leftovers += [dict(instrument=x["instrument"], side=x["side"], qty=lo, why="box legs of unequal size")
                      for x, lo in zip(b, left) if lo]
    by_exp: dict[str, list[dict]] = defaultdict(list)
    for x in legs:
        by_exp[x["instrument"].split("-")[1]].append(x)
    for exp, ls in by_exp.items():
        r, amount, left = ratios([x["qty"] for x in ls], unit)
        if sum(1 for x in r if x) >= 1:
            structs.append(("directional", ls, r, amount))
        leftovers += [dict(instrument=x["instrument"], side=x["side"], qty=lo,
                           why=f"under one {unit}-contract unit" if lo == x["qty"] else f"rounding to {amount}-contract units")
                      for x, lo in zip(ls, left) if lo]
    out = []
    if structs:
        books, _, _ = await books_fn(sorted({s[1][0]["instrument"].split("-")[1] for s in structs}))
        fits = {}
        for e in books:
            qd = otm_quotes(e, books[e], dt.datetime.fromtimestamp(now, dt.timezone.utc))
            fits[e] = (qd, spline_smile(qd) if qd is not None else None)
        for kind, ls, r, amount in structs:
            e = ls[0]["instrument"].split("-")[1]
            if fits.get(e, (None,))[0] is None:
                skipped += [{"trade": " + ".join(x["sources"]), "reason": f"{e}: too few two-sided strikes to price"} for x in ls]
                continue
            out.append(price_structure(kind, ls, r, amount, fits, books, allowance_vp, box_bps, hedge))
    warnings = fit_warnings(fits) if structs else []
    for s in out:
        s["fit_warning"] = next((w["message"] for w in warnings if w["expiry"] == s["expiry"]), None)
    return {"structures": out, "leftovers": leftovers, "skipped": skipped, "asof": now, "warnings": warnings,
            "params": {"unit": unit, "allowance_vp": allowance_vp, "box_bps": box_bps, "hedge": hedge}}
