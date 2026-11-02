"""Sandbox pricing: value and compare dealer offers on a token with no listed
options market (ZAMA first).

An offer is a list of legs seen from OUR side (we are the token holder):

    {"kind": "put" | "call" | "forward", "side": "buy" | "sell",
     "qty": tokens, "strike": K (the forward price for a forward)}

plus an upfront premium (USD, + = we receive) and the collateral we would
post. Everything is priced on the forward

    F = spot x (1 + carry x T)

(the same convention as pricing/forwards.py; carry < 0 when shorting the token
costs funding/borrow), Black-76 with no discounting (the books' zero-rate
assumption), and a linear skew in log-moneyness:

    sigma(K) = atm_vol + skew_pts/100 x (-ln(K/F) / 0.10)

i.e. ``skew_pts`` vol points added per 10% the strike sits below the forward
(> 0 = puts richer than calls, the usual shape for a token with vesting supply
overhang). No listed options exist, so atm_vol is anchored on realised vol.

"Effective sale price" puts offers of different sizes on one axis: what each
hedged token ends up sold for at expiry, = (Q x S_T + payoff + premium -
collateral cost) / Q with Q the offer's hedged quantity.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

MIN_VOL = 0.05
KINDS = ("put", "call", "forward")


def years_to(expiry: date, today: date) -> float:
    return max((expiry - today).days, 0) / 365.0


def sign(side: str) -> float:
    s = str(side or "").strip().lower()
    if s in ("buy", "long", "b"):
        return 1.0
    if s in ("sell", "short", "s"):
        return -1.0
    raise ValueError(f"side must be buy or sell, not {side!r}")


def forward_price(spot: float, carry: float, T: float) -> float:
    return spot * (1.0 + carry * T)


def vol_at(K: float, F: float, atm_vol: float, skew_pts: float) -> float:
    return max(atm_vol + skew_pts / 100.0 * (-math.log(K / F) / 0.10), MIN_VOL)


def black(F: float, K: float, T: float, vol: float, kind: str) -> float:
    if T <= 0 or vol <= 0:
        return max(F - K, 0.0) if kind == "call" else max(K - F, 0.0)
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if kind == "call":
        return F * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - F * norm.cdf(-d1)


def black_greeks(F: float, K: float, T: float, vol: float, kind: str) -> dict:
    """dV/dF and dV/dvol (per 1 vol point) of one option."""
    if T <= 0:
        itm = (F > K) if kind == "call" else (F < K)
        return {"delta": (1.0 if kind == "call" else -1.0) if itm else 0.0, "vega": 0.0}
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return {"delta": norm.cdf(d1) if kind == "call" else norm.cdf(d1) - 1.0,
            "vega": F * norm.pdf(d1) * math.sqrt(T) / 100.0}


def check_leg(leg: dict) -> dict:
    kind = str(leg.get("kind", "")).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"leg kind must be one of {KINDS}, not {leg.get('kind')!r}")
    qty, strike = float(leg.get("qty") or 0), float(leg.get("strike") or 0)
    if qty <= 0 or strike <= 0:
        raise ValueError("leg qty and strike must be > 0")
    return {"kind": kind, "side": "buy" if sign(leg.get("side")) > 0 else "sell", "qty": qty, "strike": strike}


def hedge_qty(legs: list[dict]) -> float:
    """Tokens the offer hedges: the larger of what it can make us sell
    (short calls + short forwards) and what it protects (long puts)."""
    sold = sum(l["qty"] for l in legs if l["side"] == "sell" and l["kind"] in ("call", "forward"))
    protected = sum(l["qty"] for l in legs if l["side"] == "buy" and l["kind"] == "put")
    return max(sold, protected) or max((l["qty"] for l in legs), default=0.0)


def leg_value(leg: dict, F: float, T: float, atm_vol: float, skew_pts: float, carry_factor: float) -> dict:
    s, q, K = sign(leg["side"]), leg["qty"], leg["strike"]
    if leg["kind"] == "forward":
        return {"vol": None, "unit_price": F - K, "value": s * q * (F - K),
                "delta": s * q * carry_factor, "vega": 0.0}
    vol = vol_at(K, F, atm_vol, skew_pts)
    px = black(F, K, T, vol, leg["kind"])
    g = black_greeks(F, K, T, vol, leg["kind"])
    return {"vol": vol, "unit_price": px, "value": s * q * px,
            "delta": s * q * g["delta"] * carry_factor, "vega": s * q * g["vega"]}


def leg_payoff(leg: dict, ST: np.ndarray) -> np.ndarray:
    s, q, K = sign(leg["side"]), leg["qty"], leg["strike"]
    if leg["kind"] == "forward":
        return s * q * (ST - K)
    intrinsic = np.maximum(ST - K, 0.0) if leg["kind"] == "call" else np.maximum(K - ST, 0.0)
    return s * q * intrinsic


def collateral_cost(collateral_usd: float, rate: float, T: float) -> float:
    return max(collateral_usd or 0.0, 0.0) * rate * T


def value_offer(offer: dict, spot: float, atm_vol: float, skew_pts: float, carry: float,
                collateral_rate: float, today: date) -> dict:
    """Our fair value of the offer at this spot: legs + premium - collateral cost.
    Negative = what we give the dealer."""
    expiry = offer["expiry"] if isinstance(offer["expiry"], date) else date.fromisoformat(str(offer["expiry"])[:10])
    T = years_to(expiry, today)
    F = forward_price(spot, carry, T)
    cf = 1.0 + carry * T
    legs = [check_leg(l) for l in offer["legs"]]
    priced = [{**l, **leg_value(l, F, T, atm_vol, skew_pts, cf)} for l in legs]
    legs_value = sum(p["value"] for p in priced)
    premium = float(offer.get("premium_usd") or 0.0)
    coll = collateral_cost(float(offer.get("collateral_usd") or 0.0), collateral_rate, T)
    Q = float(offer.get("hedge_qty") or 0.0) or hedge_qty(legs)
    notional = Q * spot
    net = legs_value + premium - coll
    return {
        "T": T, "days": (expiry - today).days, "forward": F, "legs": priced,
        "legs_value": legs_value, "premium_usd": premium, "collateral_cost": coll,
        "net_value": net, "net_value_pct": net / notional if notional else 0.0,
        "hedge_qty": Q, "notional_usd": notional,
        "delta": sum(p["delta"] for p in priced), "vega": sum(p["vega"] for p in priced),
    }


def effective_price(offer: dict, ST: np.ndarray, collateral_rate: float, today: date) -> np.ndarray:
    """USD per hedged token received at expiry, for each terminal price."""
    legs = [check_leg(l) for l in offer["legs"]]
    Q = float(offer.get("hedge_qty") or 0.0) or hedge_qty(legs)
    if Q <= 0:
        return np.asarray(ST, dtype=float)
    expiry = date.fromisoformat(str(offer["expiry"])[:10])
    extra = float(offer.get("premium_usd") or 0.0) - collateral_cost(
        float(offer.get("collateral_usd") or 0.0), collateral_rate, years_to(expiry, today))
    total = Q * ST + sum(leg_payoff(l, ST) for l in legs) + extra
    return total / Q


def floor_cap(offer: dict, collateral_rate: float, today: date) -> dict:
    """Worst and best effective sale price (None = unbounded above)."""
    lo = effective_price(offer, np.array([1e-9]), collateral_rate, today)[0]
    hi1, hi2 = effective_price(offer, np.array([1e3, 2e3]), collateral_rate, today)
    return {"floor": float(lo), "cap": float(hi1) if abs(hi2 - hi1) < 1e-9 else None}


def distribution_stats(offer: dict, spot: float, atm_vol: float, carry: float,
                       collateral_rate: float, today: date, n: int = 2000) -> dict:
    """Effective-price distribution with S_T lognormal around the forward at the
    ATM vol (zero drift beyond carry). Quantile grid, so deterministic."""
    expiry = date.fromisoformat(str(offer["expiry"])[:10])
    T = years_to(expiry, today)
    F = forward_price(spot, carry, T)
    u = (np.arange(n) + 0.5) / n
    sd = atm_vol * math.sqrt(max(T, 1e-9))
    ST = F * np.exp(-0.5 * sd * sd + sd * norm.ppf(u))
    eff = effective_price(offer, ST, collateral_rate, today)
    unhedged = ST
    return {
        "expected": float(eff.mean()), "p05": float(np.quantile(eff, 0.05)), "p95": float(np.quantile(eff, 0.95)),
        "unhedged_expected": float(unhedged.mean()), "unhedged_p05": float(np.quantile(unhedged, 0.05)),
        "unhedged_p95": float(np.quantile(unhedged, 0.95)),
    }


def option_probabilities(offer: dict, spot: float, atm_vol: float, carry: float, today: date) -> list[dict]:
    """Chance each option strike ends in the money (lognormal at ATM vol)."""
    expiry = date.fromisoformat(str(offer["expiry"])[:10])
    T = years_to(expiry, today)
    F = forward_price(spot, carry, T)
    out = []
    for l in (check_leg(x) for x in offer["legs"]):
        if l["kind"] == "forward":
            continue
        if T <= 0:
            p = float((F > l["strike"]) if l["kind"] == "call" else (F < l["strike"]))
        else:
            sd = atm_vol * math.sqrt(T)
            d2 = (math.log(F / l["strike"]) - 0.5 * sd * sd) / sd
            p = float(norm.cdf(d2) if l["kind"] == "call" else norm.cdf(-d2))
        out.append({"kind": l["kind"], "side": l["side"], "strike": l["strike"], "prob_itm": p})
    return out


def implied_skew(offer: dict, spot: float, atm_vol: float, carry: float, collateral_rate: float,
                 today: date, lo: float = -150.0, hi: float = 150.0) -> float | None:
    """Skew (vol points per 10%) at which the offer is worth exactly zero to
    us at this ATM vol: the skew the dealer is implicitly charging. None if no
    skew in [lo, hi] makes it fair."""
    f = lambda k: value_offer(offer, spot, atm_vol, k, carry, collateral_rate, today)["net_value"]
    try:
        a, b = f(lo), f(hi)
        if a == 0:
            return lo
        if a * b > 0:
            return None
        return float(brentq(f, lo, hi, xtol=1e-4))
    except (ValueError, ZeroDivisionError):
        return None


def implied_carry(forward_px: float, spot: float, T: float) -> float | None:
    """Annual carry that makes a forward price fair at this spot."""
    if T <= 0 or spot <= 0:
        return None
    return (forward_px / spot - 1.0) / T


def solve_strike(solve_for: str, fixed_strike: float, spot: float, T: float, atm_vol: float,
                 skew_pts: float, carry: float, target_premium_per_token: float = 0.0,
                 call_ratio: float = 1.0) -> float | None:
    """Collar solver. We buy 1 put and sell ``call_ratio`` calls per token.
    solve_for="call": given the put strike, the call strike such that
    call_ratio x C(Kc) - P(Kp) = target premium per token (0 = zero cost).
    solve_for="put": given the call strike, the put strike. None if no strike
    achieves it (e.g. the put is worth more than any call can pay for)."""
    F = forward_price(spot, carry, T)
    px = lambda K, kind: black(F, K, T, vol_at(K, F, atm_vol, skew_pts), kind)
    if solve_for == "call":
        need = px(fixed_strike, "put") + target_premium_per_token
        g = lambda Kc: call_ratio * px(Kc, "call") - need
        lo, hi = F * 1e-3, F * 50
    elif solve_for == "put":
        need = call_ratio * px(fixed_strike, "call") - target_premium_per_token
        g = lambda Kp: px(Kp, "put") - need
        lo, hi = F * 1e-3, F * 50
    else:
        raise ValueError("solve_for must be call or put")
    try:
        if g(lo) * g(hi) > 0:
            return None
        return float(brentq(g, lo, hi, xtol=F * 1e-7))
    except (ValueError, ZeroDivisionError):
        return None


# ── vesting schedule ─────────────────────────────────────────────

def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    days = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days))


def unlock_schedule(total: float, cliff_date: date, cliff_pct: float, linear_months: int) -> list[dict]:
    """Cliff tranche on cliff_date, then the rest in equal monthly tranches
    over the following ``linear_months`` months."""
    out = [{"date": cliff_date, "qty": total * cliff_pct}]
    rest = total * (1.0 - cliff_pct)
    for i in range(1, max(int(linear_months), 0) + 1):
        out.append({"date": _add_months(cliff_date, i), "qty": rest / linear_months})
    cum = 0.0
    for t in out:
        cum += t["qty"]
        t["cumulative"] = cum
    return out


def unlocked_by(schedule: list[dict], d: date) -> float:
    return sum(t["qty"] for t in schedule if t["date"] <= d)
