"""Deal action signals — the "trigger map" behind Deals / Risk.

Answers one question per deal: *at what spot level should I do something, and
what exactly would I do?*

Instead of waiting for spot to move and then computing advice, this scans a spot
ladder around the current price and finds the levels at which each deal's
recommended action switches on. The UI renders those as a band around spot; the
same output later feeds the 09:00 Slack brief and the proximity alerts.

Four action kinds (all *paired* — a close is never emitted on its own, per the
desk rule that protection is only ever replaced, never simply dropped):

  ``recycle``  short leg has decayed to pennies and gone deep OTM → buy it back
               for almost nothing and sell a fresh one nearer spot.
  ``tighten``  a vertical whose BOTH strikes are deep ITM has its payoff pinned
               at max; the width beyond spot buys no optionality and only ties
               up collateral → pull the far leg in.
  ``increase`` deal is working (unrealised profit vs premium collected) → add
               size at strikes re-centred on the trigger spot.
  ``reduce``   deal is underwater by a multiple of the premium collected → close
               part of the offending short and re-strike it further out.

Every candidate is priced with the *same* term-structure vol surface as the
Pricing tab and the Deals page (``build_market_context`` → ``_iv_from_surface``),
carries a bid/ask charge, and must clear an economic gate. The gate is the whole
point: without it the tool recommends churn that loses money on spread.

Sticky-strike convention: a leg's IV is looked up once at its own strike and
held fixed as we hypothetically move spot. DTE is also held at today's value —
these are *price*-triggered signals ("if ETH hits 4,120"), not forecasts of the
book weeks from now.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date

import numpy as np
from scipy.stats import norm

from plgo_options.web.routes.deals import build_deals_payload, _lognormal_mass
from plgo_options.web.routes.portfolio import (
    DEFAULT_IV,
    _bs_vec,
    _iso_to_date,
    _iv_from_surface,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SignalConfig:
    """Every threshold is tunable from the UI — this is an experimentation tool.

    Hardcoding these would defeat the purpose: the whole point of phase 1 is to
    turn the dials against the live book and see what fires.
    """

    # --- recycle: worthless short → fresh short near spot -------------------
    recycle_decay_pct: float = 10.0      # cost-to-close ≤ X% of premium collected
    recycle_delta_max: float = 0.05      # and |delta| ≤ this (genuinely far OTM)
    recycle_min_dte: int = 7             # under this, just let it expire
    reopen_target_delta: float = 0.20    # |delta| of the replacement short
    # Recycling ALWAYS adds tail risk — selling a 0.20-delta option instead of a
    # 0.02-delta one is the business model, so max-loss is the wrong thing to
    # gate on by default. But the ratio can get extreme (collecting $9k while
    # adding $200k of tail risk), so this cap is available: reject when added
    # tail risk exceeds N x the net premium. 0 = off, which is the default —
    # how much tail risk a premium desk will wear is a policy call, not ours.
    recycle_max_risk_ratio: float = 0.0

    # --- tighten: deep-ITM vertical → narrower width ------------------------
    tighten_itm_pct: float = 15.0        # BOTH strikes ITM by ≥ X% of spot
    tighten_width_keep_pct: float = 50.0 # reopen far leg so width = X% of original
    tighten_min_dte: int = 7
    tighten_min_efficiency: float = 3.0  # (margin freed + risk cut) per $ spent

    # --- increase: working deal → add size ---------------------------------
    increase_unreal_pct: float = 40.0    # unrealised ≥ X% of premium collected
    increase_size_pct: float = 25.0      # add X% of existing qty
    increase_min_dte: int = 14

    # --- reduce: underwater deal → de-risking re-strike --------------------
    reduce_loss_pct: float = 150.0       # cost-to-close ≥ X% of premium collected
    reduce_size_pct: float = 30.0        # re-strike X% of the offending leg
    reduce_reopen_delta: float = 0.10    # |delta| of the safer replacement
    reduce_min_efficiency: float = 3.0   # max-loss improvement per $ spent

    # --- execution economics ----------------------------------------------
    bid_ask_atm_pct: float = 2.0         # ATM half-spread, delta-scaled outwards
    min_spread_delta: float = 0.05       # floor so wings aren't priced as free
    min_net_usd: float = 1000.0          # premium-collecting actions must clear this

    # --- ladder & proximity bands -----------------------------------------
    ladder_pct: float = 40.0             # scan spot ±X%
    ladder_steps: int = 81
    approach_pct: float = 10.0           # "approaching" band
    imminent_pct: float = 5.0            # "imminent" band

    def clamp(self) -> "SignalConfig":
        """Keep user-supplied values inside sane bounds."""
        self.ladder_pct = min(max(self.ladder_pct, 5.0), 90.0)
        self.ladder_steps = int(min(max(self.ladder_steps, 21), 401))
        self.imminent_pct = max(self.imminent_pct, 0.1)
        self.approach_pct = max(self.approach_pct, self.imminent_pct)
        self.min_spread_delta = min(max(self.min_spread_delta, 0.01), 0.5)
        return self


KINDS = ("recycle", "tighten", "increase", "reduce")


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _sign(leg: dict) -> float:
    return 1.0 if str(leg.get("side", "")).lower().startswith("l") else -1.0


def _strike_step(spot: float) -> float:
    """Round replacement strikes to something a desk would actually quote."""
    if spot >= 1000:
        return 50.0
    if spot >= 100:
        return 5.0
    if spot >= 10:
        return 0.5
    if spot >= 1:
        return 0.1
    return 0.01


def _round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(round(x / step) * step, 8)


def _delta_vec(spots: np.ndarray, K: float, T: float, sigma: float, opt: str) -> np.ndarray:
    """Vectorised BS delta (r = 0), matching ``bs_greeks`` for scalars."""
    spots = np.maximum(np.asarray(spots, dtype=float), 1e-9)
    if T <= 0 or sigma <= 0 or K <= 0:
        if opt == "C":
            return np.where(spots > K, 1.0, 0.0)
        return np.where(spots < K, -1.0, 0.0)
    d1 = (np.log(spots / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return norm.cdf(d1) if opt == "C" else norm.cdf(d1) - 1.0


def _intrinsic(grid: np.ndarray, K: float, opt: str) -> np.ndarray:
    return np.maximum(grid - K, 0.0) if opt == "C" else np.maximum(K - grid, 0.0)


# ---------------------------------------------------------------------------
# Pricer — one vol surface, cached, scalar + vectorised
# ---------------------------------------------------------------------------


class _Pricer:
    def __init__(self, ctx: dict, today: date):
        self.smiles = ctx.get("smiles") or {}
        self.dates = ctx.get("deribit_dates") or {}
        self.today = today
        self._iv: dict[tuple, float] = {}
        self._dte: dict[str, int] = {}

    def iv(self, expiry: str, strike: float) -> float:
        """Sigma as a fraction. Term-structure interpolated, DEFAULT_IV fallback."""
        key = (expiry, round(float(strike), 6))
        hit = self._iv.get(key)
        if hit is None:
            raw = _iv_from_surface(expiry, strike, self.smiles, self.dates, self.today)
            hit = (raw / 100.0) if raw is not None else DEFAULT_IV
            self._iv[key] = hit
        return hit

    def dte(self, expiry: str) -> int:
        hit = self._dte.get(expiry)
        if hit is None:
            d = _iso_to_date(expiry)
            hit = max((d - self.today).days, 0) if d else 0
            self._dte[expiry] = hit
        return hit

    def value(self, expiry: str, strike: float, opt: str, spot: float) -> float:
        T = self.dte(expiry) / 365.25
        if T <= 0 or spot <= 0:
            return float(max(spot - strike, 0.0) if opt == "C" else max(strike - spot, 0.0))
        sigma = self.iv(expiry, strike)
        return float(_bs_vec(np.array([spot], dtype=float), strike, T, 0.0, sigma, opt)[0])

    def delta(self, expiry: str, strike: float, opt: str, spot: float) -> float:
        T = self.dte(expiry) / 365.25
        sigma = self.iv(expiry, strike)
        return float(_delta_vec(np.array([spot], dtype=float), strike, T, sigma, opt)[0])

    def curve(self, expiry: str, strike: float, opt: str, ladder: np.ndarray):
        """(value, delta) arrays across the whole ladder in one shot.

        This is what keeps the scan cheap: one vectorised call per leg instead of
        one Black-Scholes call per (leg, ladder point).
        """
        T = self.dte(expiry) / 365.25
        sigma = self.iv(expiry, strike)
        if T <= 0:
            vals = _intrinsic(ladder, strike, opt)
        else:
            vals = _bs_vec(ladder, strike, T, 0.0, sigma, opt)
        return np.asarray(vals, dtype=float), _delta_vec(ladder, strike, T, sigma, opt)

    def strike_for_delta(self, expiry: str, opt: str, spot: float,
                         target_delta: float, step: float) -> float | None:
        """Strike whose |delta| ≈ ``target_delta`` at ``spot``.

        |delta| is monotone in K (falling for calls, rising for puts), so plain
        bisection in log-strike converges fast and can't pick a wrong branch.
        """
        if self.dte(expiry) <= 0 or spot <= 0 or target_delta <= 0:
            return None
        lo, hi = spot * 0.2, spot * 5.0
        for _ in range(50):
            mid = math.sqrt(lo * hi)
            d = abs(self.delta(expiry, mid, opt, spot))
            if opt == "C":
                if d > target_delta:
                    lo = mid          # need a higher strike to shed delta
                else:
                    hi = mid
            else:
                if d > target_delta:
                    hi = mid          # need a lower strike to shed delta
                else:
                    lo = mid
        k = _round_step(math.sqrt(lo * hi), step)
        return k if k > 0 else None

    def half_spread(self, price: float, delta: float, cfg: SignalConfig) -> float:
        """Per-contract half-spread, delta-scaled (house convention).

        ATM pays ``bid_ask_atm_pct``; wings pay proportionally more, which is
        what stops "close this worthless leg for free" from looking profitable.
        """
        d = max(abs(delta), cfg.min_spread_delta)
        return (cfg.bid_ask_atm_pct / 100.0) / (2.0 * d) * abs(price)


# ---------------------------------------------------------------------------
# Package construction
# ---------------------------------------------------------------------------


def _build_package(kind: str, deal: dict, grid: np.ndarray, pr: _Pricer,
                   cfg: SignalConfig, eval_spot: float,
                   closes: list[tuple[dict, float]], opens: list[dict],
                   rationale: str) -> dict | None:
    """Cost a paired close/open action at ``eval_spot`` and measure its effect.

    ``closes`` = [(existing leg payload, qty to close)]; ``opens`` = [{side, opt,
    strike, expiry, qty}]. Cash is signed the same way as everywhere else in the
    app: **+ received, − paid**.
    """
    if not closes and not opens:
        return None

    base = np.asarray(deal["payoff"], dtype=float)
    after = base.copy()

    gross_cash = 0.0
    spread_cost = 0.0
    mtm_delta = 0.0          # change in this deal's mark-to-market
    close_rows, open_rows = [], []

    for leg, qty_close in closes:
        if qty_close <= 0 or leg["qty"] <= 0:
            continue
        frac = min(qty_close / leg["qty"], 1.0)
        s = _sign(leg)
        price = pr.value(leg["expiry"], leg["strike"], leg["opt"], eval_spot)
        delta = pr.delta(leg["expiry"], leg["strike"], leg["opt"], eval_spot)
        cash = s * qty_close * price          # long → sell → receive; short → buy → pay
        gross_cash += cash
        spread_cost += qty_close * pr.half_spread(price, delta, cfg)
        mtm_delta -= s * qty_close * price    # that MTM leaves the book

        # Swap the closed portion's future payoff for its realised P&L.
        leg_payoff = np.asarray(leg["payoff"], dtype=float)
        after -= frac * leg_payoff
        after += frac * float(leg.get("premium_usd") or 0.0) + cash

        close_rows.append({
            "leg_id": leg["id"], "side": leg["side"], "opt": leg["opt"],
            "strike": leg["strike"], "expiry": leg["expiry"],
            "qty": round(qty_close, 4), "price": round(price, 4),
            "cash": round(cash, 2), "delta": round(delta, 4),
        })

    for op in opens:
        qty = float(op.get("qty") or 0.0)
        K = float(op.get("strike") or 0.0)
        if qty <= 0 or K <= 0:
            return None
        s = 1.0 if str(op["side"]).lower().startswith("l") else -1.0
        price = pr.value(op["expiry"], K, op["opt"], eval_spot)
        delta = pr.delta(op["expiry"], K, op["opt"], eval_spot)
        cash = -s * qty * price               # buy → pay; sell → receive
        gross_cash += cash
        spread_cost += qty * pr.half_spread(price, delta, cfg)
        mtm_delta += s * qty * price

        after += s * qty * _intrinsic(grid, K, op["opt"]) + cash

        open_rows.append({
            "side": op["side"], "opt": op["opt"], "strike": K, "expiry": op["expiry"],
            "qty": round(qty, 4), "price": round(price, 4),
            "cash": round(cash, 2), "delta": round(delta, 4),
            "iv_pct": round(pr.iv(op["expiry"], K) * 100, 1),
        })

    net_cash = gross_cash - spread_cost
    after -= spread_cost

    # --- effect on the payoff profile -------------------------------------
    max_profit_after = float(np.max(after))
    max_loss_after = float(np.min(after))
    d_max_loss = max_loss_after - float(deal.get("max_loss") or np.min(base))
    d_max_profit = max_profit_after - float(deal.get("max_profit") or np.max(base))

    # --- P(profit), re-derived at the *evaluation* spot -------------------
    # The deal payload's prob_mass is anchored at today's spot; for a trigger
    # 10% away that distribution has moved, so recompute rather than reuse it.
    d_prob = None
    prob_before = prob_after = None
    horizon = deal.get("expiry") or ""
    t_h = max(int(deal.get("days_to_expiry") or 0), 0) / 365.25
    if horizon and t_h > 0 and eval_spot > 0:
        sigma_atm = pr.iv(horizon, eval_spot)
        mass, _ = _lognormal_mass(grid, eval_spot, sigma_atm, t_h)
        if mass is not None:
            prob_before = float(np.sum(mass[base > 0]))
            prob_after = float(np.sum(mass[after > 0]))
            d_prob = prob_after - prob_before

    # --- collateral liability proxy ---------------------------------------
    # Matches the desk's policy definition (liability = Σ |negative MTM|), so a
    # package that makes this deal's MTM less negative genuinely frees margin.
    mtm_before = sum(_sign(l) * l["qty"]
                     * pr.value(l["expiry"], l["strike"], l["opt"], eval_spot)
                     for l in deal["legs"])
    liab_before = max(0.0, -mtm_before)
    liab_after = max(0.0, -(mtm_before + mtm_delta))
    d_margin = liab_after - liab_before          # negative = frees collateral

    pkg = {
        "kind": kind,
        "eval_spot": round(eval_spot, 4),
        "rationale": rationale,
        "close": close_rows,
        "open": open_rows,
        "gross_cash": round(gross_cash, 2),
        "spread_cost": round(spread_cost, 2),
        "net_cash": round(net_cash, 2),
        "d_margin": round(d_margin, 2),
        "d_max_loss": round(d_max_loss, 2),
        "d_max_profit": round(d_max_profit, 2),
        "d_prob_profit": (round(d_prob, 5) if d_prob is not None else None),
        "prob_profit_before": (round(prob_before, 5) if prob_before is not None else None),
        "prob_profit_after": (round(prob_after, 5) if prob_after is not None else None),
        "max_loss_after": round(max_loss_after, 2),
        "max_profit_after": round(max_profit_after, 2),
        "after_payoff": [round(float(x), 2) for x in after],
    }
    pkg.update(_gate(kind, pkg, cfg))
    return pkg


def _gate(kind: str, pkg: dict, cfg: SignalConfig) -> dict:
    """The economic gate. Different kinds are worth doing for different reasons."""
    net = pkg["net_cash"]
    cost = max(0.0, -net)
    margin_freed = max(0.0, -pkg["d_margin"])

    # d_max_loss already has the trade's cash baked into it (the payoff curve is
    # shifted by net_cash), so dividing it by `cost` would charge for the cost
    # twice and understate every efficiency ratio. Back the cash out to get the
    # STRUCTURAL risk change — for a width-reducing trade this comes out exactly
    # equal to qty x width removed, which is what the ratio should be measuring.
    risk_cut = max(0.0, pkg["d_max_loss"] - net)
    pkg["structural_risk_cut"] = round(pkg["d_max_loss"] - net, 2)

    if kind in ("recycle", "increase"):
        # These exist to collect premium — they must actually pay.
        ok = net >= cfg.min_net_usd
        why = (f"nets {net:,.0f} after spread"
               if ok else f"nets only {net:,.0f} after spread (needs >= {cfg.min_net_usd:,.0f})")
        added_risk = max(0.0, -pkg["structural_risk_cut"])
        ratio = (added_risk / net) if net > 0 else None
        if ok and cfg.recycle_max_risk_ratio > 0 and net > 0 \
                and ratio > cfg.recycle_max_risk_ratio:
            ok = False
            why = (f"nets {net:,.0f} but adds {added_risk:,.0f} of tail risk "
                   f"({ratio:.0f}x, cap {cfg.recycle_max_risk_ratio:g}x)")
        elif ok and added_risk > 0 and ratio is not None:
            why += f"; adds {added_risk:,.0f} tail risk ({ratio:.0f}x)"
        return {"passes_gate": ok, "gate_reason": why,
                "efficiency": (round(ratio, 2) if ratio is not None else None)}

    if kind == "tighten":
        # Exists to free collateral / cut width. May cost a little premium.
        eff = (margin_freed + risk_cut) / cost if cost > 1 else float("inf")
        ok = (margin_freed + risk_cut) > 0 and eff >= cfg.tighten_min_efficiency
        why = (f"frees {margin_freed:,.0f} collateral + cuts {risk_cut:,.0f} of width "
               f"for {cost:,.0f}"
               if ok else
               f"only {margin_freed + risk_cut:,.0f} of benefit for {cost:,.0f} cost "
               f"(needs {cfg.tighten_min_efficiency:g}x)")
        return {"passes_gate": ok, "gate_reason": why,
                "efficiency": (None if eff == float("inf") else round(eff, 2))}

    # reduce — judged purely on risk bought per dollar spent, never on cash.
    eff = risk_cut / cost if cost > 1 else float("inf")
    ok = risk_cut > 0 and eff >= cfg.reduce_min_efficiency
    why = (f"removes {risk_cut:,.0f} of tail risk for {cost:,.0f}"
           if ok else
           f"removes only {risk_cut:,.0f} of tail risk for {cost:,.0f} "
           f"(needs {cfg.reduce_min_efficiency:g}x)")
    return {"passes_gate": ok, "gate_reason": why,
            "efficiency": (None if eff == float("inf") else round(eff, 2))}


# ---------------------------------------------------------------------------
# Per-kind conditions (evaluated across the ladder) + package builders
# ---------------------------------------------------------------------------


class _DealScan:
    """Pre-computed per-leg value/delta curves for one deal across the ladder."""

    def __init__(self, deal: dict, pr: _Pricer, ladder: np.ndarray):
        self.deal = deal
        self.pr = pr
        self.ladder = ladder
        self.vals: dict[int, np.ndarray] = {}
        self.deltas: dict[int, np.ndarray] = {}
        for leg in deal["legs"]:
            v, d = pr.curve(leg["expiry"], leg["strike"], leg["opt"], ladder)
            self.vals[leg["id"]] = v
            self.deltas[leg["id"]] = d
        # Deal MTM across the ladder = Σ signed_qty · value.
        self.mtm = np.zeros_like(ladder)
        for leg in deal["legs"]:
            self.mtm += _sign(leg) * leg["qty"] * self.vals[leg["id"]]


def _recycle_cond(scan: _DealScan, leg: dict, cfg: SignalConfig) -> np.ndarray:
    """Short leg decayed to pennies AND far enough OTM to be worth replacing."""
    pr = scan.pr
    if _sign(leg) >= 0:
        return np.zeros_like(scan.ladder, dtype=bool)
    if pr.dte(leg["expiry"]) < cfg.recycle_min_dte:
        return np.zeros_like(scan.ladder, dtype=bool)
    premium = float(leg.get("premium_usd") or 0.0)   # + received on a short
    if premium <= 0:
        return np.zeros_like(scan.ladder, dtype=bool)
    cost_to_close = leg["qty"] * scan.vals[leg["id"]]
    return ((cost_to_close <= premium * cfg.recycle_decay_pct / 100.0)
            & (np.abs(scan.deltas[leg["id"]]) <= cfg.recycle_delta_max))


def _recycle_package(scan: _DealScan, leg: dict, cfg: SignalConfig,
                     grid: np.ndarray, spot: float) -> dict | None:
    pr = scan.pr
    step = _strike_step(spot)
    K_new = pr.strike_for_delta(leg["expiry"], leg["opt"], spot,
                               cfg.reopen_target_delta, step)
    if not K_new or abs(K_new - leg["strike"]) < step / 2:
        return None
    return _build_package(
        "recycle", scan.deal, grid, pr, cfg, spot,
        closes=[(leg, leg["qty"])],
        opens=[{"side": "Short", "opt": leg["opt"], "strike": K_new,
                "expiry": leg["expiry"], "qty": leg["qty"]}],
        rationale=(
            f"Short {leg['opt']} {leg['strike']:g} has decayed to "
            f"{leg['qty'] * pr.value(leg['expiry'], leg['strike'], leg['opt'], spot):,.0f} "
            f"vs {float(leg.get('premium_usd') or 0):,.0f} collected — buy it back and "
            f"re-sell at {K_new:g} ({cfg.reopen_target_delta:.2f} delta)."
        ),
    )


def _tighten_pairs(deal: dict) -> list[tuple[dict, dict]]:
    """Same-expiry, same-type, opposite-sign leg pairs = the verticals."""
    legs = deal["legs"]
    out = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            a, b = legs[i], legs[j]
            if a["expiry"] != b["expiry"] or a["opt"] != b["opt"]:
                continue
            if _sign(a) * _sign(b) >= 0 or a["strike"] == b["strike"]:
                continue
            out.append((a, b))
    return out


def _tighten_cond(scan: _DealScan, pair: tuple[dict, dict],
                  cfg: SignalConfig) -> np.ndarray:
    """Both strikes deep ITM → payoff pinned at max, width is dead weight."""
    a, b = pair
    if scan.pr.dte(a["expiry"]) < cfg.tighten_min_dte:
        return np.zeros_like(scan.ladder, dtype=bool)
    ok = np.ones_like(scan.ladder, dtype=bool)
    thresh = cfg.tighten_itm_pct / 100.0
    for leg in (a, b):
        K = leg["strike"]
        moneyness = ((scan.ladder - K) / scan.ladder if leg["opt"] == "C"
                     else (K - scan.ladder) / scan.ladder)
        ok &= moneyness >= thresh
    return ok


def _tighten_package(scan: _DealScan, pair: tuple[dict, dict], cfg: SignalConfig,
                     grid: np.ndarray, spot: float) -> dict | None:
    a, b = pair
    near, far = (a, b) if abs(a["strike"] - spot) <= abs(b["strike"] - spot) else (b, a)
    width = abs(far["strike"] - near["strike"])
    if width <= 0:
        return None
    step = _strike_step(spot)
    direction = 1.0 if far["strike"] > near["strike"] else -1.0
    new_width = max(width * cfg.tighten_width_keep_pct / 100.0, step)
    K_new = _round_step(near["strike"] + direction * new_width, step)
    if K_new <= 0 or abs(K_new - far["strike"]) < step / 2 or K_new == near["strike"]:
        return None

    qty = min(near["qty"], far["qty"])   # only the overlapping portion is a spread
    return _build_package(
        "tighten", scan.deal, grid, scan.pr, cfg, spot,
        closes=[(far, qty)],
        opens=[{"side": far["side"], "opt": far["opt"], "strike": K_new,
                "expiry": far["expiry"], "qty": qty}],
        rationale=(
            f"{far['opt']} {near['strike']:g}/{far['strike']:g} is fully ITM at "
            f"{spot:,.0f} — the {width:g}-wide wing buys no optionality. Pull the "
            f"{far['side'].lower()} leg to {K_new:g} (width {width:g} → {abs(K_new - near['strike']):g})."
        ),
    )


def _increase_cond(scan: _DealScan, cfg: SignalConfig) -> np.ndarray:
    """Premium-collecting deal sitting on unrealised profit."""
    deal = scan.deal
    credit = float(deal.get("net_credit") or 0.0)
    if credit <= 0 or int(deal.get("days_to_expiry") or 0) < cfg.increase_min_dte:
        return np.zeros_like(scan.ladder, dtype=bool)
    unrealised = credit + scan.mtm      # mtm is negative for a short book
    return unrealised >= credit * cfg.increase_unreal_pct / 100.0


def _increase_package(scan: _DealScan, cfg: SignalConfig, grid: np.ndarray,
                      spot: float, spot_now: float) -> dict | None:
    """Add a fraction of the same structure, strikes re-centred on ``spot``.

    Scaling every strike by ``spot / spot_now`` preserves each leg's moneyness,
    so at today's spot this is simply "more of the same" and at a trigger level
    above it's the same shape shifted up.
    """
    deal = scan.deal
    step = _strike_step(spot)
    f = (spot / spot_now) if spot_now > 0 else 1.0
    opens = []
    for leg in deal["legs"]:
        qty = leg["qty"] * cfg.increase_size_pct / 100.0
        if qty <= 0:
            continue
        K = _round_step(leg["strike"] * f, step)
        if K <= 0:
            return None
        opens.append({"side": leg["side"], "opt": leg["opt"], "strike": K,
                      "expiry": leg["expiry"], "qty": qty})
    if not opens:
        return None
    shifted = " at strikes x{:.3f}".format(f) if abs(f - 1.0) > 0.005 else " at the same strikes"
    return _build_package(
        "increase", deal, grid, scan.pr, cfg, spot,
        closes=[], opens=opens,
        rationale=(
            f"{deal['strategy']} is showing unrealised profit against "
            f"{float(deal.get('net_credit') or 0):,.0f} collected — add "
            f"{cfg.increase_size_pct:g}% more{shifted}."
        ),
    )


def _reduce_cond(scan: _DealScan, cfg: SignalConfig) -> np.ndarray:
    """Cost to exit has run past a multiple of the premium collected."""
    deal = scan.deal
    credit = float(deal.get("net_credit") or 0.0)
    if credit <= 0:
        return np.zeros_like(scan.ladder, dtype=bool)
    cost_to_close = -scan.mtm            # positive when you'd pay to get out
    return cost_to_close >= credit * cfg.reduce_loss_pct / 100.0


def _reduce_package(scan: _DealScan, cfg: SignalConfig, grid: np.ndarray,
                    spot: float) -> dict | None:
    """Close part of the worst short leg and re-strike it further out."""
    pr = scan.pr
    shorts = [l for l in scan.deal["legs"]
              if _sign(l) < 0 and pr.dte(l["expiry"]) > 0]
    if not shorts:
        return None
    # "Worst" = the short costing the most to buy back right now.
    worst = max(shorts, key=lambda l: l["qty"] * pr.value(l["expiry"], l["strike"], l["opt"], spot))
    qty = worst["qty"] * cfg.reduce_size_pct / 100.0
    if qty <= 0:
        return None
    step = _strike_step(spot)
    K_new = pr.strike_for_delta(worst["expiry"], worst["opt"], spot,
                                cfg.reduce_reopen_delta, step)
    if not K_new or abs(K_new - worst["strike"]) < step / 2:
        return None
    return _build_package(
        "reduce", scan.deal, grid, pr, cfg, spot,
        closes=[(worst, qty)],
        opens=[{"side": "Short", "opt": worst["opt"], "strike": K_new,
                "expiry": worst["expiry"], "qty": qty}],
        rationale=(
            f"Exit cost has run past {cfg.reduce_loss_pct:g}% of the "
            f"{float(scan.deal.get('net_credit') or 0):,.0f} collected. Buy back "
            f"{cfg.reduce_size_pct:g}% of the short {worst['opt']} {worst['strike']:g} "
            f"and re-strike at {K_new:g} ({cfg.reduce_reopen_delta:.2f} delta)."
        ),
    )


# ---------------------------------------------------------------------------
# Trigger-level detection
# ---------------------------------------------------------------------------


def _find_triggers(cond: np.ndarray, ladder: np.ndarray, i0: int):
    """Given a boolean condition across the ladder, return (active, up, down).

    ``up``/``down`` are the nearest spot levels above/below the current price at
    which the condition first turns on. If it's already on, there's nothing to
    wait for and both are None.
    """
    active = bool(cond[i0])
    if active:
        return True, None, None
    up = down = None
    for i in range(i0 + 1, len(ladder)):
        if cond[i]:
            up = _refine(ladder, cond, i - 1, i)
            break
    for i in range(i0 - 1, -1, -1):
        if cond[i]:
            down = _refine(ladder, cond, i + 1, i)
            break
    return False, up, down


def _refine(ladder: np.ndarray, cond: np.ndarray, i_false: int, i_true: int) -> float:
    """Report the boundary as the midpoint of the bracketing ladder cells.

    The ladder is fine enough (81 points over ±40% ≈ 1% steps) that a full
    bisection on the predicate would be false precision — the IV surface itself
    isn't that sharp.
    """
    return float((ladder[i_false] + ladder[i_true]) / 2.0)


def _state_for(distance_pct: float | None, cfg: SignalConfig) -> str:
    if distance_pct is None:
        return "live"
    if distance_pct <= cfg.imminent_pct:
        return "imminent"
    if distance_pct <= cfg.approach_pct:
        return "approaching"
    return "watch"


_STATE_RANK = {"live": 0, "imminent": 1, "approaching": 2, "watch": 3}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def scan_deals(asset: str = "ETH", include_expired: bool = False,
                     overrides: dict | None = None,
                     cfg: SignalConfig | None = None,
                     kinds: list[str] | None = None,
                     include_rejected: bool = False) -> dict:
    """Scan every deal for actionable signals and their trigger levels."""
    cfg = (cfg or SignalConfig()).clamp()
    wanted = set(kinds or KINDS)
    payload, ctx = await build_deals_payload(asset, include_expired, overrides)

    spot = float(payload.get("spot") or 0.0)
    grid = np.asarray(payload.get("grid") or [], dtype=float)
    deals = payload.get("deals") or []
    out_base = {
        "asset": payload.get("asset", asset.upper()),
        "spot": spot,
        "config": asdict(cfg),
        "kinds": sorted(wanted),
        "deals_scanned": len(deals),
        "grid": payload.get("grid") or [],
    }
    if spot <= 0 or not len(grid) or not deals:
        return {**out_base, "ladder": [], "signals": [],
                "counterparties": [], "warning": (
                    "No live spot price — cannot scan." if spot <= 0 else None)}

    today = date.today()
    pr = _Pricer(ctx, today)

    # Ladder with the live spot inserted so "now" is an exact ladder point.
    lo, hi = spot * (1 - cfg.ladder_pct / 100.0), spot * (1 + cfg.ladder_pct / 100.0)
    ladder = np.geomspace(max(lo, 1e-6), hi, cfg.ladder_steps)
    ladder = np.unique(np.append(ladder, spot))
    i0 = int(np.argmin(np.abs(ladder - spot)))

    signals: list[dict] = []
    liability: dict[str, float] = {}

    for deal in deals:
        if not deal.get("legs"):
            continue
        scan = _DealScan(deal, pr, ladder)

        cpty = deal.get("counterparty") or "—"
        liability[cpty] = liability.get(cpty, 0.0) + max(0.0, -float(scan.mtm[i0]))

        subjects: list[tuple[str, str, np.ndarray, object]] = []
        if "recycle" in wanted:
            for leg in deal["legs"]:
                cond = _recycle_cond(scan, leg, cfg)
                if cond.any():
                    subjects.append((
                        "recycle",
                        f"{leg['side']} {leg['opt']} {leg['strike']:g}",
                        cond, leg))
        if "tighten" in wanted:
            for pair in _tighten_pairs(deal):
                cond = _tighten_cond(scan, pair, cfg)
                if cond.any():
                    a, b = pair
                    subjects.append((
                        "tighten",
                        f"{a['opt']} {a['strike']:g}/{b['strike']:g}",
                        cond, pair))
        if "increase" in wanted:
            cond = _increase_cond(scan, cfg)
            if cond.any():
                subjects.append(("increase", deal.get("strategy") or "deal", cond, None))
        if "reduce" in wanted:
            cond = _reduce_cond(scan, cfg)
            if cond.any():
                subjects.append(("reduce", deal.get("strategy") or "deal", cond, None))

        for kind, label, cond, subject in subjects:
            active, up, down = _find_triggers(cond, ladder, i0)
            # Build the full costed package only where it matters: at the live
            # spot if it's already on, otherwise at each nearest boundary.
            points = [(None, spot)] if active else [
                (d, lvl) for d, lvl in (("up", up), ("down", down)) if lvl is not None
            ]
            for direction, level in points:
                pkg = _package_for(kind, scan, subject, cfg, grid, level, spot)
                if pkg is None:
                    continue
                if not pkg["passes_gate"] and not include_rejected:
                    continue
                dist = None if direction is None else abs(level - spot) / spot * 100.0
                signals.append({
                    "id": f"{deal['id']}|{kind}|{label}|{direction or 'now'}",
                    "deal_id": deal["id"],
                    "counterparty": cpty,
                    "strategy": deal.get("strategy"),
                    "expiry": deal.get("expiry"),
                    "days_to_expiry": deal.get("days_to_expiry"),
                    "leg_ids": deal.get("leg_ids"),
                    "kind": kind,
                    "subject": label,
                    "state": _state_for(dist, cfg),
                    "trigger_spot": (None if level is None else round(float(level), 4)),
                    "direction": direction,
                    "distance_pct": (None if dist is None else round(dist, 2)),
                    "package": pkg,
                })

    signals.sort(key=lambda s: (_STATE_RANK.get(s["state"], 9),
                                s["distance_pct"] if s["distance_pct"] is not None else -1,
                                -abs(s["package"]["net_cash"])))

    counts = {k: sum(1 for s in signals if s["state"] == k)
              for k in ("live", "imminent", "approaching", "watch")}
    return {
        **out_base,
        "ladder": [round(float(x), 4) for x in ladder],
        "signals": signals,
        "counts": counts,
        "counterparties": [
            {"counterparty": c, "liability_usd": round(v, 2)}
            for c, v in sorted(liability.items(), key=lambda kv: -kv[1])
        ],
        "warning": None,
    }


def _package_for(kind: str, scan: _DealScan, subject, cfg: SignalConfig,
                 grid: np.ndarray, level: float, spot_now: float) -> dict | None:
    at = level if level is not None else spot_now
    if kind == "recycle":
        return _recycle_package(scan, subject, cfg, grid, at)
    if kind == "tighten":
        return _tighten_package(scan, subject, cfg, grid, at)
    if kind == "increase":
        return _increase_package(scan, cfg, grid, at, spot_now)
    if kind == "reduce":
        return _reduce_package(scan, cfg, grid, at)
    return None
