"""From an optimizer solution to execution-bot orders.

``deribit_legs`` keeps the solution's option trades booked with Deribit, names
each as a Deribit instrument (ETH-25DEC26-2500-C), nets trades on the same
instrument (an unwind and a new leg can cancel out) and rounds to whole
contracts. Everything dropped comes back with the reason, so the page can show
what will not be executed and why.

``preview`` adds what the bot would see right now: whether the instrument is
listed, the spline's fair value without the strike, the best cost at the touch
in USD and vol points, and the starting threshold.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from typing import Any

import httpx

from plgo_options.market_data.smile_recorder import expiry_books
from plgo_options.pricing.smile_fit import otm_quotes, parse_exp, spline_fair

from .models import BookDepth
from .threshold import ThresholdOrder, decide

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
    for t in trades:
        label = f"{t.get('side', '')} {abs(float(t.get('qty') or 0)):g} {t.get('opt', '')} {t.get('strike', '')} " \
                f"{expiry_code(t) or t.get('expiry', '')} @ {t.get('counterparty') or '—'}".strip()
        cpty = str(t.get("counterparty") or "").strip()
        if cpty.lower() != "deribit":
            skipped.append({"trade": label, "reason": f"booked with {cpty or 'no counterparty'}, not Deribit"})
            continue
        opt = str(t.get("opt") or "").upper()
        if opt not in ("C", "P"):
            skipped.append({"trade": label, "reason": "not an option leg"})
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
    return legs, skipped


async def _listed(asset: str) -> set[str]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/get_instruments", params={"currency": asset, "kind": "option", "expired": "false"})
        r.raise_for_status()
    return {i["instrument_name"] for i in r.json()["result"]}


async def preview(trades: list[dict], asset: str = "ETH", start_vp: float = 0.5, hours: float = 12.0,
                  listed_fn=_listed, books_fn=expiry_books) -> dict[str, Any]:
    """deribit_legs plus, per leg, what the bot would see now."""
    legs, skipped = deribit_legs(trades, asset)
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
    if ok:
        books, _, _ = await books_fn(sorted({x["instrument"].split("-")[1] for x in ok}))
        for leg in ok:
            leg.update(_price_now(leg, books.get(leg["instrument"].split("-")[1], []), now, start_vp))
    return {"legs": ok, "skipped": skipped, "asof": now}


def _price_now(leg: dict, books: list[dict], now: float, start_vp: float) -> dict:
    expiry = leg["instrument"].split("-")[1]
    book = next((b for b in books if b["instrument_name"] == leg["instrument"]), None)
    q = otm_quotes(expiry, books, dt.datetime.fromtimestamp(now, dt.timezone.utc)) if book else None
    if book is None or q is None:
        return {"note": "no book to price against"}
    fair = spline_fair(q, float(leg["instrument"].split("-")[2]), leg["instrument"].split("-")[3])
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
