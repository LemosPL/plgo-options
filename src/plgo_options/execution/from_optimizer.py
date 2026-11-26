"""From an optimizer solution to execution-bot orders.

``deribit_legs`` keeps the solution's option trades booked with Deribit, names
each as a Deribit instrument (ETH-25DEC26-2500-C), nets trades on the same
instrument (an unwind and a new leg can cancel out) and rounds to whole
contracts. Everything dropped comes back with the reason, so the page can show
what will not be executed and why.

The solution's perp trades with Deribit net into one ETH-PERPETUAL leg (qty in ETH, kind "perp"), which
the bot works alongside the options, keeping pace with their fills (perp.py).

``preview`` adds what the bot would see right now: whether the instrument is
listed, the spline's fair value (every strike in the fit), the best cost at the touch
in USD and vol points, and the starting threshold.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from typing import Any

import httpx

from plgo_options.pricing.smile_fit import fit_error, otm_quotes, parse_exp, spline_fair, spline_smile

from . import perp
from .models import BookDepth
from .threshold import MAX_FIT_ERROR, ThresholdOrder, decide

API = "https://www.deribit.com/api/v2/public"
CODE = re.compile(r"\d{1,2}[A-Z]{3}\d{2}")
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def expiry_code(t: dict) -> str | None:
    """Deribit expiry code of an optimizer trade: from its instrument name, else its date."""
    parts = str(t.get("instrument") or "").split("-")
    if len(parts) >= 2 and CODE.fullmatch(parts[1]):
        return parts[1]
    raw = str(t.get("expiry") or "")[:10]
    try:
        d = dt.date.fromisoformat(raw)
    except ValueError:
        return raw if CODE.fullmatch(raw) else None
    return f"{d.day}{MONTHS[d.month - 1]}{d.strftime('%y')}"


def deribit_legs(trades: list[dict], asset: str = "ETH") -> tuple[list[dict], list[dict]]:
    """(legs, skipped). A leg: instrument, side, qty (whole contracts), sources (labels
    of the solution's trades netted into it)."""
    net: dict[str, dict] = {}
    skipped = []
    hedge: dict | None = None
    for t in trades:
        label = f"{t.get('side', '')} {abs(float(t.get('qty') or 0)):g} {t.get('opt', '')} {t.get('strike', '')} " \
                f"{expiry_code(t) or t.get('expiry', '')} @ {t.get('counterparty') or '—'}".strip()
        cpty = str(t.get("counterparty") or "").strip()
        if cpty.lower() != "deribit":
            skipped.append({"trade": label, "reason": f"booked with {cpty or 'no counterparty'}, not Deribit"})
            continue
        opt = str(t.get("opt") or "").upper()
        if opt not in ("C", "P"):
            what = str(t.get("instrument") or "").strip() or (f"{asset}-PERPETUAL" if opt == "F"   # the optimizer's perp
                                                               else f"{opt or '?'} {t.get('strike', '')}".strip())
            label = f"{t.get('side', '')} {abs(float(t.get('qty') or 0)):g} {what} @ {cpty}"
            if what.upper() != f"{asset}-PERPETUAL":
                skipped.append({"trade": label, "reason": "not an option or the perp: put it on by hand"})
                continue
            qty = float(t.get("qty") or 0)
            side = str(t.get("side") or "").lower()
            hedge = hedge or {"instrument": f"{asset}-PERPETUAL", "signed": 0.0, "sources": []}
            hedge["signed"] += abs(qty) * (-1 if side.startswith("s") or (not side and qty < 0) else 1)
            hedge["sources"].append(label)
            continue
        code = expiry_code(t)
        strike = float(t.get("strike") or 0)
        if code is None or strike <= 0 or strike != int(strike):
            skipped.append({"trade": label, "reason": "no Deribit expiry code or whole strike"})
            continue
        qty = float(t.get("qty") or 0)
        side = str(t.get("side") or "").lower()
        signed = abs(qty) * (-1 if side.startswith("s") or (not side and qty < 0) else 1)
        name = f"{asset}-{code}-{int(strike)}-{opt}"
        leg = net.setdefault(name, {"instrument": name, "signed": 0.0, "sources": []})
        leg["signed"] += signed
        leg["sources"].append(label)
    legs = []
    for leg in net.values():
        q = math.floor(abs(leg["signed"]) + 0.5)
        if q < 1:
            why = "nets to zero" if abs(leg["signed"]) < 1e-9 else f"nets to {abs(leg['signed']):.2f}, under 1 contract"
            skipped.append({"trade": " + ".join(leg["sources"]), "reason": why})
            continue
        legs.append({"instrument": leg["instrument"], "side": "buy" if leg["signed"] > 0 else "sell", "qty": q,
                     "sources": leg["sources"]})
    if hedge:
        q = round(abs(hedge["signed"]), 2)
        if q < 0.01:
            skipped.append({"trade": " + ".join(hedge["sources"]), "reason": "the perp trades net to zero"})
        else:
            legs.append({"instrument": hedge["instrument"], "side": "buy" if hedge["signed"] > 0 else "sell", "qty": q,
                         "sources": hedge["sources"], "kind": "perp"})
    return legs, skipped


async def _listed(asset: str) -> set[str]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/get_instruments", params={"currency": asset, "kind": "option", "expired": "false"})
        r.raise_for_status()
    return {i["instrument_name"] for i in r.json()["result"]}


async def preview(trades: list[dict], asset: str = "ETH", start_vp: float = 0.5, hours: float = 12.0,
                  listed_fn=_listed, books_fn=None, perp_fn=None) -> dict[str, Any]:
    """deribit_legs plus, per leg, what the bot would see now. The books are read as the bot reads
    them (``preview_books``): one book summary for the smile, the legs' own books for their sizes."""
    legs, skipped = deribit_legs(trades, asset)
    hedges = [x for x in legs if x.get("kind") == "perp"]
    perp_fn = perp_fn or perp.production_book
    legs = [x for x in legs if x.get("kind") != "perp"]
    now = time.time()
    names = await listed_fn(asset) if legs else set()
    ok = []
    for leg in legs:
        if leg["instrument"] not in names:
            skipped.append({"trade": " + ".join(leg["sources"]),
                            "reason": f"{leg['instrument']} is not listed on Deribit (an interpolated expiry or strike?)"})
            continue
        exp = parse_exp(leg["instrument"].split("-")[1]).timestamp()
        leg["expires_in_h"] = round((exp - now) / 3600, 1)
        if exp <= now + hours * 3600:
            skipped.append({"trade": " + ".join(leg["sources"]),
                            "reason": f"{leg['instrument']} expires within the {hours:g} h deadline"})
            continue
        ok.append(leg)
    fits: dict[str, tuple] = {}
    if ok:
        books = await (books_fn or preview_books)(sorted({x["instrument"].split("-")[1] for x in ok}),
                                                  sorted({x["instrument"] for x in ok}))
        for leg in ok:
            leg.update(_price_now(leg, books.get(leg["instrument"].split("-")[1], []), now, start_vp, fits))
    warnings = fit_warnings(fits)
    for leg in ok:
        if leg["instrument"].split("-")[1] in {w["expiry"] for w in warnings}:
            leg["note"] = "spline off its quotes: the bot waits" + (f"; {leg['note']}" if leg.get("note") else "")
    for h in hedges:
        h.update(await _perp_now(h, perp_fn, bool(ok)))
    return {"legs": ok + hedges, "skipped": skipped, "asof": now, "warnings": warnings}


async def _perp_now(leg: dict, perp_fn, with_options: bool) -> dict:
    """The perp leg as the bot would see it now: mid, the touch's cost and size, in USD."""
    pace = ("keeps pace with the option orders' fills" if with_options
            else "no option order with it: sent all at once")
    try:
        b = await perp_fn()
    except (httpx.HTTPError, KeyError):
        return {"note": f"perp hedge, {pace}; no book now"}
    bid, ask = b.get("best_bid_price"), b.get("best_ask_price")
    if not bid or not ask:
        return {"note": f"perp hedge, {pace}; no two-sided book now"}
    mid = (bid + ask) / 2
    touch = ask if leg["side"] == "buy" else bid
    size = b.get("best_ask_amount" if leg["side"] == "buy" else "best_bid_amount") or 0
    cost = abs(touch - mid) + perp.FEE_RATE * touch
    return {"fair_usd": round(mid, 2), "notional_usd": round(leg["qty"] * mid), "best_cost_usd": round(cost, 2),
            "best_cost_bps": round(1e4 * cost / mid, 1), "now_qty": round(min(leg["qty"], size / mid), 2),
            "note": f"perp hedge, {pace}"}


async def preview_books(expiries: list[str], instruments: list[str]) -> dict[str, list[dict]]:
    """The expiries' books as the bot reads them without its quote feed (bot._books): every option's
    prices, marks and the forward from one book summary call, the instruments' own books for their
    sizes. ~1 s, where reading all ~90 books of an expiry took ~13 s and ran into Deribit's per-IP
    rate limit (20 Nov 2026). The other strikes have no sizes, so the spline weighs them by spread only."""
    import httpx

    from plgo_options.market_data.smile_recorder import RateLimited

    from .bot import _books  # the bot imports this module's neighbours: import here, not at the top
    try:
        return await _books(expiries, instruments)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:  # the page says so (routes: a 503 with a readable message)
            raise RateLimited("Deribit rate limit") from e
        raise


def fit_warnings(fits: dict[str, tuple]) -> list[dict]:
    """The expiries whose spline is off its quotes (smile_fit.fit_error over the bot's MAX_FIT_ERROR):
    their fair values are not to be trusted, and the bot does not trade them until the books refill."""
    out = []
    for expiry, (q, smile) in sorted(fits.items()):
        if q is None or smile is None:
            continue
        rms, worst = fit_error(q, smile)
        if rms > MAX_FIT_ERROR:
            out.append({"expiry": expiry, "rms": round(rms, 2), "worst": round(worst, 1), "limit": MAX_FIT_ERROR,
                        "message": f"{expiry}: the spline is {rms:.2f} half-spreads off its quotes (worst strike "
                                   f"{worst:.1f}; normal is about 0.15, the bot waits above {MAX_FIT_ERROR:g}). "
                                   f"The books are thin, often around Deribit's 08:00 UTC expiry: the fair values "
                                   f"and costs below are not reliable. Try again in a few minutes."})
    return out


def _price_now(leg: dict, books: list[dict], now: float, start_vp: float, fits: dict[str, tuple]) -> dict:
    """fits: {expiry: (otm_quotes, spline_smile)}, filled on first use and shared by the legs."""
    expiry = leg["instrument"].split("-")[1]
    book = next((b for b in books if b["instrument_name"] == leg["instrument"]), None)
    if book is None:
        return {"note": "no book to price against"}
    if expiry not in fits:
        q = otm_quotes(expiry, books, dt.datetime.fromtimestamp(now, dt.timezone.utc))
        fits[expiry] = (q, spline_smile(q) if q is not None else None)
    q, smile = fits[expiry]
    if q is None:
        return {"note": "no book to price against"}
    fair = spline_fair(q, float(leg["instrument"].split("-")[2]), leg["instrument"].split("-")[3], smile)
    o = ThresholdOrder(0, leg["instrument"], leg["side"], leg["qty"], 0, now, now + 12 * 3600, start_vp, start_vp,
                       start_vp, leg["qty"])
    depth = BookDepth(leg["instrument"], [tuple(x) for x in book.get("bids") or []],
                      [tuple(x) for x in book.get("asks") or []])
    d = decide(o, now, fair, book["estimated_delivery_price"], depth)
    vg = fair["vega"]
    return {"fair_usd": round(fair["fair"], 2), "iv": round(fair["iv"] * 100, 1), "vega": round(vg, 2),
            "best_cost_usd": None if d.best_cost is None else round(d.best_cost, 2),
            "best_cost_vp": None if d.best_cost is None or not vg else round(d.best_cost / vg, 2),
            "now_qty": d.qty, "note": "" if fair["inside"] else "outside the spline's strikes: the bot will not trade it"}
