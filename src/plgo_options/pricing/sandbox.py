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
assumption), and a skew in log-moneyness x = ln(K/F):

    sigma(K) = atm_vol + skew_pts/100 x L x tanh(-x / (0.10 L)),   L = 4

i.e. near the money ``skew_pts`` vol points added per 10% the strike sits
below the forward (> 0 = puts richer than calls, the usual shape for a token
with vesting supply overhang), saturating at +/- L x skew_pts in the wings. A
straight line drove far call vols to the floor, an arbitrage that showed up as
an implied distribution whose mean was not the forward. No listed options
exist, so atm_vol is anchored on realised vol.

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


SKEW_SATURATION = 4.0


def vol_at(K: float, F: float, atm_vol: float, skew_pts: float) -> float:
    L = SKEW_SATURATION
    return max(atm_vol + skew_pts / 100.0 * L * math.tanh(-math.log(K / F) / (0.10 * L)), MIN_VOL)


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


class TerminalDistribution:
    """Where the token lands at expiry, implied by the same smile the offers
    are priced on (Breeden-Litzenberger, zero rates): CDF(K) = 1 + dC/dK,
    density = d2C/dK2. With skew 0 this is the lognormal at the ATM vol; with
    put skew the left tail fattens, exactly as it does in the prices, so the
    probabilities and the valuations never disagree."""

    def __init__(self, spot: float, atm_vol: float, skew_pts: float, carry: float, T: float, n: int = 1500):
        self.T = max(T, 1e-6)
        self.F = forward_price(spot, carry, T)
        sd = max(atm_vol, MIN_VOL) * math.sqrt(self.T)
        # Wide enough for the tails of a ~130% vol token, in log space.
        k = np.linspace(math.log(self.F) - 7 * sd - 0.5, math.log(self.F) + 7 * sd + 0.5, n)
        K = np.exp(k)
        h = K * 1e-4
        L = SKEW_SATURATION

        def c(x):  # vectorised black() / vol_at() calls
            vol = np.maximum(atm_vol + skew_pts / 100.0 * L * np.tanh(-np.log(x / self.F) / (0.10 * L)), MIN_VOL)
            sd_ = vol * math.sqrt(self.T)
            d1 = (np.log(self.F / x) + 0.5 * sd_ * sd_) / sd_
            return self.F * norm.cdf(d1) - x * norm.cdf(d1 - sd_)
        cdf = 1.0 + (c(K + h) - c(K - h)) / (2 * h)
        cdf = np.maximum.accumulate(np.clip(cdf, 0.0, 1.0))  # arbitrage noise never makes it non-monotone
        cdf[0], cdf[-1] = 0.0, 1.0
        self.K, self.cdf_grid = K, cdf

    def cdf(self, x) -> np.ndarray:
        return np.interp(np.asarray(x, dtype=float), self.K, self.cdf_grid, left=0.0, right=1.0)

    def quantile(self, u) -> np.ndarray:
        cdf, K = self.cdf_grid, self.K
        keep = np.concatenate(([True], np.diff(cdf) > 1e-12))
        return np.interp(np.asarray(u, dtype=float), cdf[keep], K[keep])

    def density(self, x) -> np.ndarray:
        """Probability per unit of price on the grid x (display)."""
        x = np.asarray(x, dtype=float)
        mid = 0.5 * (x[1:] + x[:-1])
        edges = np.concatenate(([max(x[0] - (mid[0] - x[0]), 1e-12)], mid, [x[-1] + (x[-1] - mid[-1])]))
        return np.diff(self.cdf(edges)) / np.diff(edges)

    def samples(self, n: int = 4000) -> np.ndarray:
        return self.quantile((np.arange(n) + 0.5) / n)


def distribution_stats(offer: dict, spot: float, atm_vol: float, carry: float,
                       collateral_rate: float, today: date, skew_pts: float = 0.0, n: int = 4000) -> dict:
    """Effective-price distribution under the smile-implied terminal
    distribution (TerminalDistribution). Quantile grid, so deterministic."""
    expiry = date.fromisoformat(str(offer["expiry"])[:10])
    dist = TerminalDistribution(spot, atm_vol, skew_pts, carry, years_to(expiry, today))
    ST = dist.samples(n)
    eff = effective_price(offer, ST, collateral_rate, today)
    return {
        "expected": float(eff.mean()), "p05": float(np.quantile(eff, 0.05)), "p95": float(np.quantile(eff, 0.95)),
        "unhedged_expected": float(ST.mean()), "unhedged_p05": float(np.quantile(ST, 0.05)),
        "unhedged_p95": float(np.quantile(ST, 0.95)),
        "zones": landing_zones(offer, dist),
    }


def landing_zones(offer: dict, dist: "TerminalDistribution") -> dict:
    """Chance the trade lands in each region of its payoff: below the lowest
    bought put (protected), above the highest sold call (capped), or in
    between (we just sell at spot). For a forward: below vs above its price
    (better or worse off than unhedged)."""
    legs = [check_leg(l) for l in offer["legs"]]
    puts = [l["strike"] for l in legs if l["kind"] == "put" and l["side"] == "buy"]
    calls = [l["strike"] for l in legs if l["kind"] == "call" and l["side"] == "sell"]
    fwds = [l["strike"] for l in legs if l["kind"] == "forward"]
    if fwds and not puts and not calls:
        p = float(dist.cdf(fwds[0]))
        return {"kind": "forward", "below": p, "above": 1.0 - p, "level": fwds[0]}
    lo = max(puts) if puts else None   # protection starts paying below the highest put
    hi = min(calls) if calls else None  # upside is given away above the lowest call
    p_lo = float(dist.cdf(lo)) if lo else 0.0
    p_hi = 1.0 - float(dist.cdf(hi)) if hi else 0.0
    return {"kind": "options", "protected": p_lo, "between": max(1.0 - p_lo - p_hi, 0.0), "capped": p_hi,
            "put_level": lo, "call_level": hi}


def option_probabilities(offer: dict, spot: float, atm_vol: float, carry: float, today: date,
                         skew_pts: float = 0.0) -> list[dict]:
    """Chance each option strike ends in the money (smile-implied)."""
    expiry = date.fromisoformat(str(offer["expiry"])[:10])
    T = years_to(expiry, today)
    out = []
    dist = TerminalDistribution(spot, atm_vol, skew_pts, carry, T) if T > 0 else None
    F = forward_price(spot, carry, T)
    for l in (check_leg(x) for x in offer["legs"]):
        if l["kind"] == "forward":
            continue
        if dist is None:
            p = float((F > l["strike"]) if l["kind"] == "call" else (F < l["strike"]))
        else:
            below = float(dist.cdf(l["strike"]))
            p = 1.0 - below if l["kind"] == "call" else below
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
