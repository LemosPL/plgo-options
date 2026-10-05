"""The Optimizer agent: Lucas's Optimizer v4 routine, automated.

What Lucas does by hand on the v4 page:
  1. Load Risk Profile.
  2. Tune the parametric target (downside / upside).
  3. Pick the maturity (e.g. 25 Dec) and the counterparty (e.g. Flowdesk).
  4. Try λ 0.3-0.5, κ 1-1.1, T+90 0.2 or 0.5.
  5. Squeeze max trades (5, 7, 8) and max qty, looking for a better profile
     with fewer, smaller trades - every line pays a counterparty spread.

This module does exactly that with the same engine the page calls
(``OptimizerUseCase``), gathering the book ONCE and running every combination
in a worker thread so the web app stays responsive. Each run is scored:

    score = fit gain %  -  cost_per_100k × (cost / $100k)  -  per_line × option lines

and any run that gives back more than ``max_giveback_usd`` versus today's book
at a key spot (-45%, -20%, spot, +35%, +85%) is disqualified. The top three go
to the gate, then the handover. Nothing is traded.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import math
from typing import Any

from plgo_options.agents.policy import AssetPolicy, OptimizerPreset

KEY_MONEYNESS = (-0.45, -0.20, 0.0, 0.35, 0.85)
DEFAULT_VOL_PTS = {"ETH": 5.0, "FIL": 40.0}


def build_grid(preset: OptimizerPreset) -> list[dict[str, Any]]:
    grid = [
        {"lam_factor": l, "downside_factor": k, "t90_weight": t, "max_trades": m, "max_qty": q}
        for l, k, t, m, q in itertools.product(
            preset.lam_grid, preset.downside_grid, preset.t90_grid,
            preset.max_trades_grid, preset.max_qty_grid)
    ]
    return grid[: max(1, preset.max_runs)]


def run_kwargs(asset: str, preset: OptimizerPreset, variant: dict[str, Any]) -> dict[str, Any]:
    """OptimizerRunParams kwargs - the same fields the v4 page posts."""
    return dict(
        asset=asset.upper(),
        lam_factor=variant["lam_factor"],
        mu_factor=preset.mu_factor,
        target_expiry=preset.effective_expiry(),
        cone_width_sigma=1.5,
        cone_quarterly_only=True,
        unwind_discount=preset.unwind_discount,
        new_position_penalty=preset.new_position_penalty,
        roll_dte_threshold=preset.roll_dte_threshold,
        roll_itm_only=False,
        collateral_budget_pct=preset.collateral_budget_pct,
        is_replay=False,
        counterparties=preset.counterparties or None,
        cash_neutrality_factor=preset.cash_neutrality_factor,
        max_qty=variant.get("max_qty"),
        max_trades=variant.get("max_trades"),
        enable_box_neutralizer=preset.enable_box_neutralizer,
        enable_delta_rehedge=preset.enable_delta_rehedge,
        delta_band_usd=preset.delta_band_usd,
        downside_factor=variant["downside_factor"],
        t90_weight=variant["t90_weight"],
        atm_concentration=preset.atm_concentration,
        parametric_low_floor_ratio=preset.target_down_ratio,
        parametric_trough_payoff=preset.target_trough_payoff,
        parametric_high_plateau_ratio=preset.target_up_ratio,
        bid_ask_vol_pts=DEFAULT_VOL_PTS.get(asset.upper(), 5.0),
        target_profile_file=preset.target_profile_file,
        enable_composite_unwind=preset.enable_composite_unwind,
    )


def _nearest(ladder: list[float], x: float) -> int:
    return min(range(len(ladder)), key=lambda i: abs(ladder[i] - x))


def summarize(result: dict[str, Any], variant: dict[str, Any], preset: OptimizerPreset) -> dict[str, Any]:
    """Reduce one engine result to what the ranking and the handover need."""
    trades = result.get("trades") or []
    opt_lines = [t for t in trades if "BOX" not in str(t.get("strategy") or "").upper()]
    spot = float(result.get("spot") or 0)
    ladder = [float(x) for x in (result.get("spot_ladder") or [])]
    before = result.get("before_payoff") or []
    after = result.get("after_payoff") or []
    deltas = []
    if ladder and before and after and spot:
        for m in KEY_MONEYNESS:
            i = _nearest(ladder, spot * (1 + m))
            deltas.append({"moneyness": m, "spot": ladder[i],
                           "change_usd": float(after[i]) - float(before[i])})
    fb, fa = result.get("fit_error_before"), result.get("fit_error_after")
    fit_gain = 100.0 * (fb - fa) / fb if fb else 0.0
    cost = float(result.get("total_cost_usd") or 0)
    gross_contracts = sum(abs(float(t.get("qty") or 0)) for t in opt_lines)
    worst = min([d["change_usd"] for d in deltas], default=0.0)
    disq = []
    if not result.get("optimizer_converged"):
        disq.append("did not converge")
    if result.get("status") != "ok":
        disq.append(result.get("message") or "no result")
    if worst < -preset.max_giveback_usd:
        disq.append(f"gives back ${-worst:,.0f} at a key spot")
    score = (fit_gain - preset.score_cost_per_100k * cost / 100_000
             - preset.score_per_option_line * len(opt_lines))
    return {
        "variant": variant,
        "fit_gain_pct": round(fit_gain, 2),
        "cost_usd": round(cost, 0),
        "net_premium_usd": round(float(result.get("net_premium_generated") or 0), 0),
        "option_lines": len(opt_lines),
        "all_lines": len(trades),
        "gross_contracts": round(gross_contracts, 0),
        "gross_notional_usd": round(gross_contracts * spot, 0),
        "key_spot_changes": deltas,
        "worst_change_usd": round(worst, 0),
        "score": round(score, 2) if not disq else -1e9,
        "disqualified": disq,
        "spot": spot,
        "trades": trades,
        "spot_ladder": ladder,
        "before_payoff": before,
        "after_payoff": after,
        "target_payoff": result.get("target_payoff") or [],
    }


def rank(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best score first; ties go to fewer lines, then lower cost."""
    return sorted(summaries, key=lambda s: (-s["score"], s["option_lines"], s["cost_usd"]))


def pareto(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs no other run beats on fit, cost and line count at once."""
    ok = [s for s in summaries if not s["disqualified"]]
    front = []
    for s in ok:
        dominated = any(
            o is not s and o["fit_gain_pct"] >= s["fit_gain_pct"] and o["cost_usd"] <= s["cost_usd"]
            and o["option_lines"] <= s["option_lines"]
            and (o["fit_gain_pct"], -o["cost_usd"], -o["option_lines"])
            != (s["fit_gain_pct"], -s["cost_usd"], -s["option_lines"])
            for o in ok)
        if not dominated:
            front.append(s)
    return rank(front)


async def gather_book(asset: str) -> tuple[dict, dict]:
    """The same inputs /api/optimization/run assembles, fetched once."""
    from plgo_options.data.deal_grouping import compute_composite_ids
    from plgo_options.web.routes.optimization import _build_perp_positions, _fetch_collateral_by_cp
    from plgo_options.web.routes.portfolio import portfolio_pnl

    pnl = await portfolio_pnl(asset=asset.upper(), include_expired=False)
    try:
        perps = await _build_perp_positions(asset.upper(), pnl)
        if perps:
            pnl["positions"] = list(pnl.get("positions") or []) + perps
    except Exception:
        pass
    positions = pnl.get("positions") or []
    ids = compute_composite_ids(positions, None)
    for p in positions:
        p["composite_id"] = ids.get(p.get("id"))
    collateral = await _fetch_collateral_by_cp()
    return pnl, collateral


def _run_one(pnl: dict, collateral: dict, kwargs: dict) -> dict:
    from plgo_options.optimization.optim_usecase import OptimizerRunParams, OptimizerUseCase
    params = OptimizerRunParams(collateral_by_cp=collateral, enforce_collateral_cap=False, **kwargs)
    usecase = OptimizerUseCase.from_portfolio_payload(copy.deepcopy(pnl), params)
    return usecase.run()


async def sweep(policy: AssetPolicy, custom_spot: float | None = None,
                variants: list[dict] | None = None) -> dict[str, Any]:
    """Run the grid for one asset. Returns ranked summaries + the best three."""
    preset = policy.optimizer
    pnl, collateral = await gather_book(policy.asset)
    if custom_spot:
        pnl["spot"] = custom_spot
        pnl["eth_spot"] = custom_spot
    grid = variants or build_grid(preset)
    summaries, errors = [], []
    for v in grid:
        kw = run_kwargs(policy.asset, preset, v)
        try:
            result = await asyncio.to_thread(_run_one, pnl, collateral, kw)
            summaries.append(summarize(result, v, preset))
        except Exception as e:                      # one bad combination must not stop the sweep
            errors.append({"variant": v, "error": f"{type(e).__name__}: {e}"[:300]})
    ranked = rank(summaries)
    return {
        "asset": policy.asset,
        "target": {"expiry": preset.effective_expiry(), "counterparties": preset.counterparties,
                   "trough": preset.target_trough_payoff, "down": preset.target_down_ratio,
                   "up": preset.target_up_ratio, "file": preset.target_profile_file},
        "runs": len(grid),
        "errors": errors,
        "ranked": ranked,
        "pareto": pareto(summaries),
        "best": [s for s in ranked if not s["disqualified"]][:3],
        "spot": float(pnl.get("spot") or 0),
        "book_mtm": float((pnl.get("totals") or {}).get("current_total_mtm") or 0),
    }


def to_proposal(asset: str, s: dict[str, Any]) -> dict[str, Any]:
    """Turn a ranked run into a gate proposal (legs in the gate's shape)."""
    legs = []
    for t in s["trades"]:
        opt = str(t.get("opt") or "").upper()
        legs.append({
            "kind": "perp" if opt == "F" else "option",
            "side": t.get("side"), "opt": opt, "strike": t.get("strike"),
            "expiry": str(t.get("expiry") or ""), "qty": t.get("qty"),
            "counterparty": t.get("counterparty"), "is_unwind": bool(t.get("is_unwind")),
            "strategy": t.get("strategy"), "instrument": t.get("instrument"),
        })
    return {
        "asset": asset, "source": "optimizer", "purpose": "shape", "legs": legs,
        "net_cost_usd": -float(s["net_premium_usd"]) + float(s["cost_usd"]),
        "notional_usd": s["gross_notional_usd"], "spot": s["spot"],
        "spot_ladder": s["spot_ladder"], "before_payoff": s["before_payoff"],
        "after_payoff": s["after_payoff"],
        "reason": f"Optimizer v4 sweep: fit +{s['fit_gain_pct']}% with {s['option_lines']} lines",
    }


def compare_curves(a: dict[str, Any], b: dict[str, Any], move_pct: float) -> dict[str, Any]:
    """B2a two-curve check between two best runs (morning vs afternoon).

    Agree = the same instruments traded in the same direction. Opposite sides on
    the same instrument = an input problem; nobody trades until it is found.
    """
    def book(s):
        out = {}
        for t in s.get("trades") or []:
            if "BOX" in str(t.get("strategy") or "").upper():
                continue
            key = f"{t.get('counterparty')}|{t.get('opt')}|{t.get('strike')}|{t.get('expiry')}"
            out[key] = 1 if str(t.get("side")).lower() == "buy" else -1
        return out
    ba, bb = book(a), book(b)
    common = set(ba) & set(bb)
    opposite = [k for k in common if ba[k] != bb[k]]
    overlap = len(common) / max(1, len(set(ba) | set(bb)))
    if opposite:
        verdict = "disagree"
    elif overlap >= 0.5 or (not ba and not bb):
        verdict = "agree"
    else:
        verdict = "differ"
    return {"verdict": verdict, "overlap": round(overlap, 2), "opposite": opposite,
            "spot_move_pct": round(move_pct, 2)}
