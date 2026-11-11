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
