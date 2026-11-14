"""Cash-settled forwards: fair value, MTM and risk.

A forward fixes today the price K at which ``qty`` tokens are exchanged at
maturity; cash-settled, so at maturity only qty x (fixing - K) changes hands,
in USD/USDC. Before then it is marked against the fair forward

    F = spot x (1 + carry x T),   T = days to maturity / 365

with ``carry`` an annual rate set per asset (0 = forward at spot, the same
zero-rate assumption the options book uses). Signed so that a forward we
BOUGHT gains when spot rises:

    MTM   = sign x qty x (F - K)
    delta = sign x qty x (1 + carry x T)      tokens
    theta = -sign x qty x spot x carry / 365  $ per day (the carry rolling off)

No gamma or vega: the payoff is linear in spot.
"""
from __future__ import annotations

from datetime import date


def years_to(maturity: date, today: date) -> float:
    return max((maturity - today).days, 0) / 365.0


def side_sign(side: str) -> float:
    s = str(side or "").strip().lower()
    if s in ("buy", "long", "b"):
        return 1.0
    if s in ("sell", "short", "s"):
        return -1.0
    raise ValueError(f"side must be buy or sell, not {side!r}")


def fair_forward(spot: float, carry: float, T: float) -> float:
    return spot * (1.0 + carry * T)


def value(side: str, qty: float, forward_price: float, spot: float, carry: float, T: float) -> dict:
    """MTM and risk of one forward at this spot."""
    sgn = side_sign(side)
    F = fair_forward(spot, carry, T)
    delta = sgn * qty * (1.0 + carry * T)
    return {
        "fair_forward": F,
        "mtm": sgn * qty * (F - forward_price),
        "delta": delta,
        "delta_usd": delta * spot,
        "theta": -sgn * qty * spot * carry / 365.0 if T > 0 else 0.0,
        "notional_usd": qty * forward_price,
    }


def settlement_pnl(side: str, qty: float, forward_price: float, fixing: float) -> float:
    """Cash paid to us (+) or by us (-) at maturity."""
    return side_sign(side) * qty * (fixing - forward_price)


# ── Stop-loss / take-profit levels ───────────────────────────────────────
# Levels are prices of the UNDERLYING spot (the market you watch), booked per
# forward. Direction follows the side: a forward we SOLD loses as spot rises,
# so its stop sits above spot and its target below; a forward we BOUGHT is the
# mirror image. A level is "hit" once spot has reached it, "near" when spot is
# within NEAR_PCT of it.
NEAR_PCT = 2.0


def level_watch(side: str, qty: float, forward_price: float, spot: float, carry: float, T: float,
                stop_loss: float | None, take_profit: float | None, near_pct: float = NEAR_PCT) -> dict:
    """Where spot is against this forward's stop / target, and the MTM at each."""
    sgn = side_sign(side)

    def one(level: float | None, kind: str) -> dict | None:
        if not level or level <= 0:
            return None
        # Stop is hit by an ADVERSE move (up for a short, down for a long),
        # target by a favourable one.
        adverse_up = sgn < 0
        up = adverse_up if kind == "stop" else not adverse_up
        hit = spot >= level if up else spot <= level
        dist_pct = (level / spot - 1.0) * 100.0 if spot > 0 else None
        near = (not hit) and dist_pct is not None and abs(dist_pct) <= near_pct
        return {
            "level": level,
            "dist_pct": dist_pct,                       # + = level above spot
            "mtm_at": value(side, qty, forward_price, level, carry, T)["mtm"],
            "status": "hit" if hit else "near" if near else "ok",
        }

    stop, target = one(stop_loss, "stop"), one(take_profit, "target")
    if stop and stop["status"] == "hit":
        overall = "stop_hit"
    elif target and target["status"] == "hit":
        overall = "target_hit"
    elif stop and stop["status"] == "near":
        overall = "near_stop"
    elif target and target["status"] == "near":
        overall = "near_target"
    elif stop or target:
        overall = "watching"
    else:
        overall = "none"
    return {"stop": stop, "target": target, "status": overall, "near_pct": near_pct}


def check_levels_order(side: str, stop_loss: float | None, take_profit: float | None) -> str | None:
    """Error text if the stop and target are the wrong way round for the side."""
    if not stop_loss or not take_profit:
        return None
    if side_sign(side) < 0 and not stop_loss > take_profit:
        return "for a sell (short) forward the stop loss must be above the take profit"
    if side_sign(side) > 0 and not stop_loss < take_profit:
        return "for a buy (long) forward the stop loss must be below the take profit"
    return None


# ── Synthetic forwards (options booked as a forward structure) ───────────
# Long call + short put at the same strike and expiry, same size, is a long
# forward at that strike (put-call parity, zero rate: C - P = S - K at any vol,
# so the pair's MTM is qty x (S - K) whatever each leg is marked at). Short
# call + long put is the short. Matched only within one deal: same
# counterparty and trade date (the Deals convention), same expiry, same
# strike, same qty.
QTY_MATCH_TOL = 1e-6


def _norm_side(s: str) -> float:
    return side_sign(s)


def find_synthetic_forwards(trades: list[dict]) -> list[dict]:
    """Pairs of active option legs that together are a forward.

    `trades` are rows of the trades table (side Buy/Sell, option_type
    Call/Put, strike, expiry, qty, counterparty, trade_date, premium_usd
    signed +received/-paid). Each leg is used at most once."""
    groups: dict[tuple, dict[str, list[dict]]] = {}
    # Legs per deal (counterparty, trade date): a pair that IS the whole deal is
    # "standalone" (booked as a forward structure); one inside a bigger deal is
    # flagged embedded so the monitor can leave it out by default.
    deal_legs: dict[tuple, int] = {}
    for t in trades:
        dk = (str(t.get("counterparty") or "").strip().lower(), str(t.get("trade_date") or "")[:10])
        deal_legs[dk] = deal_legs.get(dk, 0) + 1
    for t in trades:
        typ = str(t.get("option_type") or "").strip().lower()
        if typ not in ("call", "put"):
            continue
        k = (str(t.get("counterparty") or "").strip().lower(), str(t.get("trade_date") or "")[:10],
             str(t.get("expiry") or "")[:10], round(float(t["strike"]), 9))
        groups.setdefault(k, {"call": [], "put": []})[typ].append(t)
    out = []
    for g in groups.values():
        used: set = set()
        for c in sorted(g["call"], key=lambda r: r["id"]):
            sc = _norm_side(c["side"])
            for p in sorted(g["put"], key=lambda r: r["id"]):
                if p["id"] in used or _norm_side(p["side"]) != -sc:
                    continue
                q = float(c["qty"])
                if abs(float(p["qty"]) - q) > QTY_MATCH_TOL * max(q, 1.0):
                    continue
                used.add(p["id"])
                K = float(c["strike"])
                prem = float(c.get("premium_usd") or 0.0) + float(p.get("premium_usd") or 0.0)
                out.append({
                    "key": f"syn:{c['id']}-{p['id']}",
                    "side": "buy" if sc > 0 else "sell",
                    "qty": q,
                    "strike": K,
                    # All-in price once the net premium is folded in.
                    "effective_price": K - sc * prem / q if q else K,
                    "net_premium_usd": prem,
                    "expiry": str(c["expiry"])[:10],
                    "counterparty": c.get("counterparty") or "",
                    "trade_date": str(c.get("trade_date") or "")[:10],
                    "standalone": deal_legs.get((str(c.get("counterparty") or "").strip().lower(),
                                                 str(c.get("trade_date") or "")[:10]), 0) == 2,
                    "call": c, "put": p,
                })
                break
    out.sort(key=lambda s: (s["expiry"], s["counterparty"].lower(), s["key"]))
    return out


# ── Round trips: closing forwards matched against opening ones ───────────
# A buy on a short forward's counterparty and maturity closes (part of) it;
# cash-settled, the gain is locked in and paid at the maturity fixing.
# Manual pairs (picked on the Monitor) go first, the rest is matched FIFO by
# trade date. A close is tagged take_profit / stop_loss when its price is at
# or through the opening leg's level (within LEVEL_MATCH_PCT), else "closed".
LEVEL_MATCH_PCT = 1.0
CLOSE_REASONS = ("take_profit", "stop_loss", "roll", "closed")


def group_key(f: dict) -> str:
    return f"{str(f['asset']).upper()}|{str(f['counterparty']).strip().lower()}|{str(f['maturity_date'])[:10]}"


def close_reason(open_side: str, close_price: float, stop: float | None, target: float | None,
                 tol_pct: float = LEVEL_MATCH_PCT) -> str:
    short = side_sign(open_side) < 0
    tol = tol_pct / 100.0
    if target:
        if (close_price <= target * (1 + tol)) if short else (close_price >= target * (1 - tol)):
            return "take_profit"
    if stop:
        if (close_price >= stop * (1 - tol)) if short else (close_price <= stop * (1 + tol)):
            return "stop_loss"
    return "closed"


def match_round_trips(legs: list[dict], manual: list[dict], levels: dict[str, dict] | None = None):
    """(open qty by leg id, round trips). `legs`: open forwards (id, asset,
    counterparty, side, qty, forward_price, trade_date, maturity_date);
    `manual`: forward_closes rows. Pairs that no longer fit (a leg gone,
    same side, other group, nothing left) are returned with "invalid"."""
    levels = levels or {}
    by_id = {int(l["id"]): l for l in legs}
    rem = {i: float(l["qty"]) for i, l in by_id.items()}
    trips, invalid = [], []

    def trip(o, c, q, reason, source, close_row=None):
        so = side_sign(o["side"])
        lv = levels.get(f"fwd:{o['id']}") or {}
        if reason is None:
            reason = close_reason(o["side"], float(c["forward_price"]), lv.get("stop_loss"), lv.get("take_profit"))
        trips.append({
            "group": group_key(o), "asset": str(o["asset"]).upper(), "counterparty": o["counterparty"],
            "maturity_date": str(o["maturity_date"])[:10], "side": o["side"], "qty": q,
            "open_id": int(o["id"]), "open_price": float(o["forward_price"]), "open_date": str(o["trade_date"])[:10],
            "close_id": int(c["id"]), "close_price": float(c["forward_price"]), "close_date": str(c["trade_date"])[:10],
            "realised_usd": so * q * (float(c["forward_price"]) - float(o["forward_price"])),
            "reason": reason, "source": source, "close_row_id": close_row,
            "stop_loss": lv.get("stop_loss"), "take_profit": lv.get("take_profit"),
        })

    def order(a, b):
        ka = (str(a["trade_date"])[:10], int(a["id"]))
        kb = (str(b["trade_date"])[:10], int(b["id"]))
        return (a, b) if ka <= kb else (b, a)

    for m in manual:
        a, b = by_id.get(int(m["open_id"])), by_id.get(int(m["close_id"]))
        if not a or not b or a is b or side_sign(a["side"]) == side_sign(b["side"]) or group_key(a) != group_key(b):
            invalid.append({**m, "why": "legs no longer open or not offsetting"})
            continue
        o, c = order(a, b)
        q = min(float(m["qty"]), rem[int(o["id"])], rem[int(c["id"])])
        if q <= 1e-12:
            invalid.append({**m, "why": "nothing left to close on these legs"})
            continue
        rem[int(o["id"])] -= q
        rem[int(c["id"])] -= q
        trip(o, c, q, m.get("reason") or "closed", "manual", m.get("id"))

    groups: dict[str, list[dict]] = {}
    for l in by_id.values():
        groups.setdefault(group_key(l), []).append(l)
    for g in groups.values():
        g.sort(key=lambda l: (str(l["trade_date"])[:10], int(l["id"])))
        lots: list[dict] = []          # open lots, all of one side
        for l in g:
            i = int(l["id"])
            while rem[i] > 1e-12 and lots and side_sign(lots[0]["side"]) != side_sign(l["side"]):
                o = lots[0]
                q = min(rem[i], rem[int(o["id"])])
                rem[i] -= q
                rem[int(o["id"])] -= q
                trip(o, l, q, None, "auto")
                if rem[int(o["id"])] <= 1e-12:
                    lots.pop(0)
            if rem[i] > 1e-12:
                lots.append(l)
    trips.sort(key=lambda t: (t["close_date"], t["close_id"], t["open_id"]))
    return {i: max(q, 0.0) for i, q in rem.items()}, trips, invalid
