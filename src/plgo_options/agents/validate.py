"""Price a proposal and test it against the policy before anyone sees it.

What the Pricing tab shows a person - what each leg costs, what we lose at spot
to run it, the payoff - plus the policy tests on top: does it push the floor
down and by how much, does the book stay inside the approved max loss, is the
cost inside the limits. Every proposal (optimizer or row) goes through here
before the gate, and the gate reads the result.

The numbers come from the v4 engine itself (OptimizerUseCase -> OptimizerV3),
so they match v4 and Portfolio P&L: legs are valued on our vol surface with
bs_vec_bridge at each horizon, and priced at the counterparty's own
methodology where one is calibrated (pricing.cpty_pricing), otherwise at mid
plus the v4 VOLpts spread. Curves are judged at the target-expiry horizon, not
the Now curve.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import numpy as np

from plgo_options.agents.policy import AssetPolicy

DEFAULT_VOL_PTS = {"ETH": 5.0, "FIL": 40.0}
KEY_MONEYNESS = (-0.45, -0.20, 0.0, 0.35, 0.85)


def _expiry_dt(v: Any) -> datetime | None:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d%b%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s.upper(), fmt).replace(hour=8)
        except ValueError:
            continue
    return None


def _engine(pnl: dict, asset: str):
    from plgo_options.optimization.optim_usecase import OptimizerRunParams, OptimizerUseCase
    uc = OptimizerUseCase.from_portfolio_payload(copy.deepcopy(pnl), OptimizerRunParams(asset=asset))
    return uc.build_optimizer()


def price_legs(opt, asset: str, legs: list[dict]) -> tuple[list[dict], list[dict]]:
    """(engine trade dicts, per-leg pricing rows) for a proposal's legs."""
    from plgo_options.optimization.math_utils import bs_price
    from plgo_options.optimization.option_smile import OptionSmile
    from plgo_options.pricing.cpty_pricing import resolve_price

    S = float(opt.spot)
    smiles = [s for s in opt.vol_surface if s.get("dte", 0) > 0]
    smile = OptionSmile([{"expiry_code": s["expiry_code"], "expiry_date": s["expiry_date"],
                          "strikes": s["strikes"], "ivs": [iv / 100.0 for iv in s["ivs"]]} for s in smiles])
    vol_pts = DEFAULT_VOL_PTS.get(asset.upper(), 5.0)
    trades, rows = [], []
    now = datetime.now()
    for l in legs:
        if "BOX" in str(l.get("strategy") or "").upper():
            continue
        side = str(l.get("side") or ("Buy" if float(l.get("qty") or 0) > 0 else "Sell")).lower()
        sign = 1.0 if side == "buy" else -1.0
        qty = abs(float(l.get("qty") or 0))
        if qty <= 0:
            continue
        if l.get("kind") == "perp" or str(l.get("opt")).upper() == "F":
            trades.append({"opt": "F", "strike": S, "qty": sign * qty, "dte": 0, "iv_pct": 0.0, "bs_price_usd": S})
            rows.append({"leg": f"{side} perp", "qty": qty, "notional_usd": qty * S,
                         "price_usd": None, "mid_usd": None, "iv_pct": None,
                         "premium_usd": 0.0, "dealing_cost_usd": qty * S * 0.0002, "priced_by": "2 bps"})
            continue
        exp = _expiry_dt(l.get("expiry"))
        K, o = float(l.get("strike") or 0), str(l.get("opt") or "C").upper()
        if not exp or K <= 0:
            rows.append({"leg": f"{side} {o} {K:g}", "error": "missing expiry or strike"})
            continue
        dte = max((exp - now).total_seconds() / 86400.0, 0.0)
        T = dte / 365.25
        try:
            sigma = float(smile.compute_vol(exp, K))
        except Exception:
            sigma = 0.0
        if not (sigma > 0):
            rows.append({"leg": f"{side} {o} {K:g}", "error": "no vol for this expiry/strike"})
            continue
        mid = bs_price(S, K, T, 0.0, sigma, o)
        cp = resolve_price(str(l.get("counterparty") or ""), asset.upper(), S, K, T, o, side)
        if cp is not None:
            px, by = float(cp[0]), f"{l.get('counterparty')} methodology"
        else:
            vega_pt = abs(bs_price(S, K, T, 0.0, sigma + 0.01, o) - bs_price(S, K, T, 0.0, max(sigma - 0.01, 1e-4), o)) / 2
            px = mid + sign * vega_pt * vol_pts
            px, by = max(px, 0.0), f"mid ± {vol_pts:g} vol pts"
        trades.append({"opt": o, "strike": K, "qty": sign * qty, "dte": dte, "iv_pct": sigma * 100,
                       "bs_price_usd": mid})
        rows.append({"leg": f"{side} {o} {K:g} {exp:%d%b%y}".upper(), "qty": qty, "counterparty": l.get("counterparty"),
                     "iv_pct": round(sigma * 100, 2), "mid_usd": mid, "price_usd": px,
                     "premium_usd": sign * qty * px,              # + = we pay
                     "dealing_cost_usd": abs(px - mid) * qty, "priced_by": by})
    return trades, rows


def validate(pnl: dict, policy: AssetPolicy, legs: list[dict]) -> dict[str, Any]:
    """Price `legs` against today's book and run the policy tests.

    Returns the Pricing-tab figures (premium, dealing cost, P&L at spot now and
    at the horizon), the floor and max-loss tests with dollar amounts, and a
    list of findings: ("fail" | "chris" | "ok", text, rule).
    """
    asset = policy.asset.upper()
    opt = _engine(pnl, asset)
    S = float(opt.spot)
    trades, rows = price_legs(opt, asset, legs)
    out: dict[str, Any] = {"spot": S, "legs": rows, "findings": []}
    find = out["findings"]
    errs = [r for r in rows if r.get("error")]
    for r in errs:
        find.append(("fail", f"Could not price {r['leg']}: {r['error']}", "B3"))
    if not trades:
        return out

    preset = policy.optimizer
    exp = _expiry_dt(preset.effective_expiry())
    H = max(int(round((exp - datetime.now()).total_seconds() / 86400.0)), 1) if exp else 90
    ladder = np.array(opt.spot_ladder, dtype=float)
    before, after, _ = opt.build_payoffs([0, H], ladder, trades)
    b0, a0 = np.array(before["0"]), np.array(after["0"])
    bH, aH = np.array(before[str(H)]), np.array(after[str(H)])
    chg = aH - bH

    premium = sum(r.get("premium_usd") or 0.0 for r in rows)
    dealing = sum(r.get("dealing_cost_usd") or 0.0 for r in rows)
    at = lambda arr, x: float(np.interp(x, ladder, arr))
    out.update(
        horizon_days=H, net_premium_usd=premium, dealing_cost_usd=dealing,
        # What a person sees on Pricing: at mid the trade is worth what we pay,
        # so the loss at spot today is the dealing cost.
        pnl_at_spot_now_usd=at(a0, S) - at(b0, S) - dealing,
        pnl_at_spot_horizon_usd=at(aH, S) - at(bH, S) - dealing,
        key_spots=[{"moneyness": m, "spot": S * (1 + m), "change_usd": at(chg, S * (1 + m)) - dealing}
                   for m in KEY_MONEYNESS],
        spot_ladder=ladder.tolist(), before_payoff=bH.tolist(), after_payoff=aH.tolist(),
    )

    # 1. The floor (A2): no worse below it, beyond the quote tolerance.
    steps = policy.row_steps_pct or [30]
    floor = policy.floor_price or S * (1 - steps[-1] / 100)
    below = ladder <= floor
    worst_below = float((chg[below]).min()) - dealing if below.any() else 0.0
    out.update(floor=floor, floor_change_usd=at(chg, floor) - dealing, floor_worst_change_usd=worst_below)
    tol = policy.cost_tolerance_usd
    if worst_below < -tol:
        find.append(("fail", f"Pushes the floor down: ${-worst_below:,.0f} worse below {floor:,.4g} at T+{H}d "
                             f"(tolerance ${tol:,.0f})", "A2"))
    else:
        find.append(("ok", f"Floor {floor:,.4g} held at T+{H}d (worst change below it ${worst_below:,.0f})", "A2"))

    # 2. Approved max loss (decision sheet line 5): the book's worst P&L from
    #    the floor up to +85% must not get worse, and must stay inside the
    #    approved max loss unless it was already outside and this improves it.
    zone = (ladder >= floor) & (ladder <= S * 1.85)
    if zone.any():
        wb, wa = float(bH[zone].min()), float(aH[zone].min()) - dealing
        limit = preset.target_trough_payoff
        out.update(worst_before_usd=wb, worst_after_usd=wa, max_loss_limit_usd=limit)
        if wa < wb - tol:
            find.append(("fail", f"Worsens the book's worst loss at T+{H}d: ${wb:,.0f} -> ${wa:,.0f}", "A3"))
        elif wa < limit and wa < wb:
            find.append(("fail", f"Book's worst loss ${wa:,.0f} at T+{H}d is beyond the approved ${limit:,.0f}", "A3"))
        else:
            find.append(("ok", f"Worst loss at T+{H}d ${wb:,.0f} -> ${wa:,.0f} (approved {limit:,.0f})", "A3"))

    # 3. What it costs to run. Routing on it is the gate's cost check (A3),
    #    which reads net_cost_usd as set from these numbers.
    cost_out = max(premium, 0.0) + dealing
    find.append(("info", f"Costs ${cost_out:,.0f} to run (net premium ${premium:,.0f}, dealing ${dealing:,.0f}); "
                         f"P&L at spot now ${out['pnl_at_spot_now_usd']:,.0f}, at T+{H}d "
                         f"${out['pnl_at_spot_horizon_usd']:,.0f}", "A3"))
    out["cost_to_run_usd"] = cost_out
    return out


def apply_to_proposal(prop: dict, v: dict) -> dict:
    """Write the validated numbers onto a proposal so the gate tests them."""
    prop["validation"] = {k: v.get(k) for k in (
        "horizon_days", "net_premium_usd", "dealing_cost_usd", "cost_to_run_usd", "pnl_at_spot_now_usd",
        "pnl_at_spot_horizon_usd", "key_spots", "floor", "floor_change_usd", "floor_worst_change_usd",
        "worst_before_usd", "worst_after_usd", "max_loss_limit_usd", "legs", "findings")}
    if v.get("spot_ladder"):
        prop["spot_ladder"], prop["before_payoff"], prop["after_payoff"] = (
            v["spot_ladder"], v["before_payoff"], v["after_payoff"])
        prop["judged_on_days"] = v.get("horizon_days")
    if v.get("cost_to_run_usd") is not None:
        prop["net_cost_usd"] = (v.get("net_premium_usd") or 0.0) + (v.get("dealing_cost_usd") or 0.0)
    return prop
