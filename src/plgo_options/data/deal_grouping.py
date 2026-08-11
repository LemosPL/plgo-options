"""Group flat option legs into multi-leg "deals" (composites/structures).

This is the single source of truth for turning a counterparty's flat list of
trade legs into structures (call/put spreads, straddles/strangles, risk
reversals, iron condors, ...). It was originally written for the read-only
Deals / Risk screen (``web/routes/deals.py``) and is factored out here so the
optimizer can reuse the exact same grouping — a leg is a member of the same
composite whether you're looking at the Deals screen or letting the optimizer
decide what to unwind.

Grouping precedence, per counterparty:
  1. A manual override map (from the Deals screen UI), if supplied — pins
     legs to a group_id explicitly.
  2. A real booking ticket id (``trade_id``) that ties >=2 legs together
     (semantics vary by counterparty: some book one id per structure, some
     one id per leg, so a singleton id carries no grouping information and
     falls through to (3)).
  3. Trade date + a heuristic decomposition that greedily reassembles
     vertical spreads, straddles/strangles, risk reversals/collars and iron
     condors/butterflies from same-day legs, leaving anything left over as
     naked singles.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


# ---------------------------------------------------------------------------
# Strategy classification
# ---------------------------------------------------------------------------

def classify_structure(legs: list[dict]) -> tuple[str, str]:
    """Classify a set of legs into (short_label, description).

    Each leg dict needs ``opt`` ("C"/"P"), ``sign`` (+1 long / -1 short) and
    ``strike``.
    """
    n = len(legs)
    calls = [l for l in legs if l["opt"] == "C"]
    puts = [l for l in legs if l["opt"] == "P"]
    longs = [l for l in legs if l["sign"] > 0]
    shorts = [l for l in legs if l["sign"] < 0]

    if n == 1:
        l = legs[0]
        side = "Long" if l["sign"] > 0 else "Short"
        kind = "Call" if l["opt"] == "C" else "Put"
        return f"{side} {kind}", f"Single {side.lower()} {kind.lower()} @ {_fmt_k(l['strike'])}"

    if n == 2:
        a, b = sorted(legs, key=lambda x: x["strike"])
        # Vertical spreads (same option type, one long one short)
        if len(calls) == 2 and len(longs) == 1:
            bull = a["sign"] > 0  # long the lower strike => bullish
            label = "Bull Call Spread" if bull else "Bear Call Spread"
            return label, f"Call spread {_fmt_k(a['strike'])}/{_fmt_k(b['strike'])}"
        if len(puts) == 2 and len(longs) == 1:
            bull = b["sign"] > 0  # long the higher strike => bullish (debit) ... else bearish
            label = "Bull Put Spread" if a["sign"] < 0 else "Bear Put Spread"
            return label, f"Put spread {_fmt_k(a['strike'])}/{_fmt_k(b['strike'])}"
        # Straddle / strangle (one call one put)
        if len(calls) == 1 and len(puts) == 1:
            same_k = abs(calls[0]["strike"] - puts[0]["strike"]) < 1e-9
            if len(longs) == 2:
                return ("Long Straddle" if same_k else "Long Strangle",
                        "Long call + long put")
            if len(shorts) == 2:
                return ("Short Straddle" if same_k else "Short Strangle",
                        "Short call + short put")
            # Mixed: risk reversal / collar
            return "Risk Reversal / Collar", "Long one side, short the other"
        return "2-Leg Structure", "Two-leg combination"

    if n == 4 and len(calls) == 2 and len(puts) == 2:
        # Iron condor / butterfly: short the inner strikes, long the outer wings.
        # Reverse (long inner, short outer) is the debit "reverse" variant.
        short_strikes = sorted(l["strike"] for l in shorts)
        long_strikes = sorted(l["strike"] for l in longs)
        if len(shorts) == 2 and len(longs) == 2:
            short_inner = (min(long_strikes) <= min(short_strikes)
                           and max(long_strikes) >= max(short_strikes))
            long_inner = (min(short_strikes) <= min(long_strikes)
                          and max(short_strikes) >= max(long_strikes))
            if short_inner:
                if abs(short_strikes[0] - short_strikes[1]) < 1e-9:
                    return "Iron Butterfly", "Short straddle + protective wings"
                return "Iron Condor", "Short strangle + protective wings"
            if long_inner:
                if abs(long_strikes[0] - long_strikes[1]) < 1e-9:
                    return "Reverse Iron Butterfly", "Long straddle funded by short wings"
                return "Reverse Iron Condor", "Long strangle funded by short wings"
        return "4-Leg Structure", "Four-leg combination"

    nc, npu = len(calls), len(puts)
    return f"Custom ({n}-leg)", f"{nc} call leg(s), {npu} put leg(s)"


def _fmt_k(k: float) -> str:
    if k >= 100:
        return f"{int(round(k))}"
    return f"{k:g}"


# ---------------------------------------------------------------------------
# Structure decomposition
# ---------------------------------------------------------------------------
# A same-counterparty, same-day booking may actually be several distinct
# structures (e.g. two call spreads + a put spread + naked legs) rather than one
# big "Custom (N-leg)" blob. Without a trade/ticket id we can't know for sure,
# so we *suggest* a decomposition by greedily pairing legs into recognisable
# structures. The user can override the grouping in the UI.

def decompose_legs(legs: list[dict]) -> list[list[dict]]:
    """Split a leg set into a list of recognisable sub-structures.

    Small, already-clean structures (<=4 legs that classify to a named strategy)
    are kept intact. Anything else is decomposed within each expiry: vertical
    spreads first, then straddles/strangles, then risk reversals, then the
    remaining naked legs as singles.
    """
    label, _ = classify_structure(legs)
    clean = len(legs) <= 4 and "Custom" not in label and "Structure" not in label
    if clean:
        return [legs]

    by_exp: dict[str, list[dict]] = defaultdict(list)
    for l in legs:
        by_exp[l["expiry"]].append(l)
    out: list[list[dict]] = []
    for _exp, elegs in by_exp.items():
        out.extend(_decompose_one_expiry(elegs))
    return out


def _decompose_one_expiry(legs: list[dict]) -> list[list[dict]]:
    used: set = set()

    def avail(pred):
        return [l for l in legs if l["id"] not in used and pred(l)]

    structures: list[list[dict]] = []

    # 1. Vertical spreads — same option type, opposite side, paired by size.
    call_verticals: list[list[dict]] = []
    put_verticals: list[list[dict]] = []
    for opt in ("C", "P"):
        longs = sorted(avail(lambda l: l["opt"] == opt and l["sign"] > 0), key=lambda l: -l["qty"])
        shorts = sorted(avail(lambda l: l["opt"] == opt and l["sign"] < 0), key=lambda l: -l["qty"])
        bucket = call_verticals if opt == "C" else put_verticals
        for a, b in zip(longs, shorts):
            bucket.append([a, b])
            used.add(a["id"]); used.add(b["id"])

    # 1b. Reassemble a call vertical + put vertical of matching size into a
    # single iron condor / butterfly, so the whole 4-leg structure stays one
    # deal (correct label + net premium) instead of two split spreads. Only
    # merge when it actually classifies as an iron structure; otherwise keep
    # the two spreads separate.
    used_put_v: set = set()
    for cv in call_verticals:
        cv_qty = min(l["qty"] for l in cv)
        merged = False
        for pi, pv in enumerate(put_verticals):
            if pi in used_put_v:
                continue
            if abs(min(l["qty"] for l in pv) - cv_qty) < 1e-6:
                combo = cv + pv
                label, _ = classify_structure(combo)
                if "Iron" in label:
                    structures.append(combo)
                    used_put_v.add(pi)
                    merged = True
                    break
        if not merged:
            structures.append(cv)
    for pi, pv in enumerate(put_verticals):
        if pi not in used_put_v:
            structures.append(pv)

    # 2. Straddles / strangles — same-side call + put.
    for sgn in (1, -1):
        cs = sorted(avail(lambda l: l["opt"] == "C" and l["sign"] == sgn), key=lambda l: -l["qty"])
        ps = sorted(avail(lambda l: l["opt"] == "P" and l["sign"] == sgn), key=lambda l: -l["qty"])
        for a, b in zip(cs, ps):
            structures.append([a, b])
            used.add(a["id"]); used.add(b["id"])

    # 3. Risk reversals / collars — remaining call + put (necessarily opposite side).
    cs = sorted(avail(lambda l: l["opt"] == "C"), key=lambda l: -l["qty"])
    ps = sorted(avail(lambda l: l["opt"] == "P"), key=lambda l: -l["qty"])
    for a, b in zip(cs, ps):
        structures.append([a, b])
        used.add(a["id"]); used.add(b["id"])

    # 4. Whatever is left over — naked single legs.
    for l in legs:
        if l["id"] not in used:
            structures.append([l])
            used.add(l["id"])

    return structures


# ---------------------------------------------------------------------------
# Per-counterparty grouping (auto, with optional manual override)
# ---------------------------------------------------------------------------

def group_composite_legs(
    legs: list[dict], overrides: dict[str, str] | None = None,
) -> list[tuple[str, list[dict]]]:
    """Group one counterparty's legs into composites.

    Each leg dict needs at least: ``id``, ``opt`` ("C"/"P"), ``sign``
    (+1 long / -1 short), ``strike``, ``expiry``, ``trade_id``, ``trade_date``.

    Returns a list of ``(group_id, legs)`` pairs. ``overrides`` is
    ``{leg_id(str): group_id(str)}`` — when non-empty, it fully replaces
    auto-grouping for this counterparty's legs (legs missing from the map
    fall back to a per-trade-date group), matching the manual grouping the
    Deals screen UI offers.
    """
    if overrides:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for leg in legs:
            gid = overrides.get(str(leg["id"])) or f"date:{leg.get('trade_date', '')}"
            grouped[gid].append(leg)
        return list(grouped.items())

    # Auto grouping. Prefer the booking's real ticket id (trade_id) when it
    # actually ties several legs together — see module docstring for why a
    # singleton id doesn't count.
    tid_counts = Counter(leg.get("trade_id") or "" for leg in legs)
    with_id: dict[str, list[dict]] = defaultdict(list)
    without_id: list[dict] = []
    for leg in legs:
        tid = leg.get("trade_id") or ""
        if tid and tid_counts[tid] >= 2:
            with_id[tid].append(leg)
        else:
            without_id.append(leg)

    out: list[tuple[str, list[dict]]] = []
    for tid, glegs in with_id.items():
        out.append((f"tid:{tid}", glegs))

    by_date: dict[str, list[dict]] = defaultdict(list)
    for leg in without_id:
        by_date[leg.get("trade_date", "")].append(leg)
    for tdate, dlegs in by_date.items():
        for i, struct in enumerate(decompose_legs(dlegs)):
            out.append((f"{tdate}#{i}", struct))

    return out


def compute_composite_ids(
    positions: list[dict], overrides: dict[str, dict[str, str]] | None = None,
) -> dict[Any, str]:
    """Assign a composite id to every position/leg.

    ``positions`` is a flat list of dicts (one per DB trade row/leg) with at
    least ``id``, ``counterparty``, ``opt`` ("C"/"P"), ``strike``, ``expiry``;
    side is taken from ``sign``/``side`` if present, else derived from
    ``net_qty`` (positive = long). ``trade_id``/``trade_date`` are optional
    but improve grouping quality when present.

    ``overrides`` is ``{counterparty: {leg_id(str): group_id(str)}}``, the
    same shape the Deals screen UI produces/consumes.

    Returns ``{position_id: composite_id}``. Composite ids are namespaced
    ``f"{counterparty}|{group_id}"`` — the same id format the Deals screen
    assigns, so a composite unwound by the optimizer maps 1:1 onto a "deal"
    a trader would see there. A single-leg group still gets an id (equal to
    its own leg's group), so callers should only treat groups with >=2
    members as an actual composite worth linking.
    """
    overrides = overrides or {}
    by_cpty: dict[str, list[dict]] = defaultdict(list)
    for p in positions:
        strike = float(p.get("strike") or 0.0)
        if strike <= 0:
            continue
        opt = "C" if str(p.get("opt") or p.get("option_type") or "").strip().upper().startswith("C") else "P"
        if "sign" in p:
            sign = 1.0 if float(p["sign"]) >= 0 else -1.0
        elif "side" in p and str(p["side"]).strip().lower() in ("long", "short", "buy", "sell"):
            sign = 1.0 if str(p["side"]).strip().lower() in ("long", "buy") else -1.0
        else:
            sign = 1.0 if float(p.get("net_qty") or 0.0) >= 0 else -1.0
        cpty = str(p.get("counterparty") or "").strip()
        by_cpty[cpty].append({
            "id": p.get("id"),
            "opt": opt,
            "sign": sign,
            "strike": strike,
            "expiry": str(p.get("expiry") or "").strip(),
            "trade_id": str(p.get("trade_id") or "").strip(),
            "trade_date": str(p.get("trade_date") or "").strip(),
            "qty": abs(float(p.get("net_qty") or p.get("qty") or 0.0)),
        })

    composite_id_by_position: dict[Any, str] = {}
    for cpty, legs in by_cpty.items():
        for group_id, glegs in group_composite_legs(legs, overrides.get(cpty)):
            cid = f"{cpty}|{group_id}"
            for leg in glegs:
                composite_id_by_position[leg["id"]] = cid

    return composite_id_by_position
