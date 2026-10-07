"""The Optimizer agent: Lucas's Optimizer v4 routine, automated.

What Lucas does by hand on the v4 page:
  1. Load Risk Profile.
  2. Tune the parametric target (downside / upside).
  3. Pick the maturity (e.g. 25 Dec) and the counterparty (e.g. Flowdesk).
  4. Try λ 0.3-0.5, κ 1-1.1, T+90 0.2 or 0.5.
  5. Squeeze max trades (5-15) and max qty, looking for a better profile
     with fewer, smaller trades - every line pays a counterparty spread.

This module does exactly that with the same engine the page calls
(``OptimizerUseCase``), gathering the book ONCE and running every combination
in a worker thread so the web app stays responsive. Each run is scored on the
curve the P&L matrix shows - the target-expiry horizon, in dollars:

    score = track gain %  -  cost_per_100k × (cost / $100k)  -  per_line × option lines

where track gain is how much closer the after-curve's shape gets to the
target's than today's book, summed over the tracking spots. (The engine's own
fit gain is measured on today's curve, level-free and density-weighted, so a
run can score well on it while the expiry matrix drifts off the target; it is
kept for reference only.) A run is disqualified when it:
  - gives back more than ``max_giveback_usd`` versus today's book at a key spot,
  - ends up further from the target than today's book at any key spot,
  - fails a policy test the gate would apply (floor, max loss, the view) -
    the same code as agents/validate.py.
Each target's passing runs go to the gate best first; the first one the
validation and the gate accept becomes the proposal. Nothing is traded.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import math
import re
from datetime import date, datetime
from typing import Any

from plgo_options.agents.policy import AssetPolicy, OptimizerPreset
from plgo_options.agents.validate import curve_tests

KEY_MONEYNESS = (-0.45, -0.20, 0.0, 0.35, 0.85)
# Where the result is compared with the target (spot itself is the anchor).
TRACK_MONEYNESS = (-0.45, -0.30, -0.20, -0.10, 0.10, 0.20, 0.35, 0.60, 0.85)
DEFAULT_VOL_PTS = {"ETH": 5.0, "FIL": 40.0}


def build_grid(preset: OptimizerPreset) -> list[dict[str, Any]]:
    grid = [
        {"lam_factor": l, "downside_factor": k, "t90_weight": t, "max_trades": m, "max_qty": q}
        for l, k, t, m, q in itertools.product(
            preset.lam_grid, preset.downside_grid, preset.t90_grid,
            preset.max_trades_grid, preset.max_qty_grid)
    ]
    return grid[: max(1, preset.max_runs)]


def _one(preset: OptimizerPreset, lams, trades, qtys) -> list[dict[str, Any]]:
    return [{"lam_factor": round(l, 4), "downside_factor": k, "t90_weight": t, "max_trades": m, "max_qty": q}
            for l, k, t, m, q in itertools.product(lams, preset.downside_grid, preset.t90_grid, trades, qtys)]


def coarse_grid(preset: OptimizerPreset) -> list[dict[str, Any]]:
    """Pass 1: every lam_grid value at the first max_trades and the largest
    max_qty (the full grid when no refinement is configured)."""
    if not preset.lam_refine_step:
        return build_grid(preset)
    return _one(preset, preset.lam_grid, preset.max_trades_grid[:1], [max(preset.max_qty_grid)])


def best_variant(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The best-scoring run's variant. Disqualified runs still show where the
    engine works best, so when every run is out they are ranked on tracking alone."""
    if not runs:
        return None
    return max(runs, key=_search_score)["variant"]


def _search_score(s: dict[str, Any]) -> float:
    return s["score"] if not s["disqualified"] else s["track_gain_pct"] - 1e6


def refine_lams(preset: OptimizerPreset, score_by_lam: dict[float, float]) -> list[float]:
    """Pass 2: every lam_refine_step between the best coarse λ and its better
    neighbour in lam_grid - best 2.5, neighbours 2.0 / 3.0, 3.0 scored higher ->
    2.5, 2.6 ... 3.0."""
    step = preset.lam_refine_step
    lams = sorted(l for l in preset.lam_grid if l in score_by_lam)
    if not step or not lams:
        return []
    best = max(lams, key=lambda l: score_by_lam[l])
    i = lams.index(best)
    nbs = [lams[j] for j in (i - 1, i + 1) if 0 <= j < len(lams)]
    if not nbs:
        return [best]
    nb = max(nbs, key=lambda l: score_by_lam[l])
    lo, hi = min(best, nb), max(best, nb)
    n = int(round((hi - lo) / step))
    return [round(lo + k * step, 4) for k in range(n + 1)]


def _vkey(v: dict[str, Any]) -> tuple:
    return (round(v["lam_factor"], 4), v["downside_factor"], v["t90_weight"], v["max_trades"], v["max_qty"])


def run_kwargs(asset: str, preset: OptimizerPreset, variant: dict[str, Any],
               target: dict[str, Any] | None = None) -> dict[str, Any]:
    """OptimizerRunParams kwargs - the same fields the v4 page posts. ``target``
    is one of preset.targets(); None keeps the preset's own target."""
    target_file = target["file"] if target is not None else preset.target_profile_file
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
        target_profile_file=target_file,
        enable_composite_unwind=preset.enable_composite_unwind,
    )


_SUMMARY_RE = re.compile(
    r"λ\s*(?P<lam>[\d.]+)\s*κ\s*(?P<kappa>[\d.]+)\s*T\+90\s*(?P<t90>[\d.]+)\s*max\s*(?P<trades>\d+)"
    r"(?:\s*trades)?\s*/\s*(?P<qty>[\d.,]+)")


def variant_for(proposal_row: dict[str, Any], preset: OptimizerPreset) -> tuple[dict[str, Any], str]:
    """The sweep variant behind a stored proposal, and where it came from.

    New optimizer proposals carry it; older ones only have it in the summary
    line ("λ0.5 κ1.1 T+90 0.5 max 5 trades / 5000 qty"); row and manual
    proposals never had one, so they get the policy preset's first grid point.
    """
    p = proposal_row.get("proposal") or {}
    if p.get("variant"):
        return dict(p["variant"]), "stored"
    m = _SUMMARY_RE.search(proposal_row.get("summary") or "")
    if m:
        return ({"lam_factor": float(m["lam"]), "downside_factor": float(m["kappa"]),
                 "t90_weight": float(m["t90"]), "max_trades": int(m["trades"]),
                 "max_qty": float(m["qty"].replace(",", ""))}, "summary")
    return coarse_grid(preset)[0], "policy preset"


def _nearest(ladder: list[float], x: float) -> int:
    return min(range(len(ladder)), key=lambda i: abs(ladder[i] - x))


def days_to_expiry(code: str, today: date | None = None) -> int | None:
    try:
        return (datetime.strptime(code, "%d%b%y").date() - (today or date.today())).days
    except (TypeError, ValueError):
        return None


def judge_horizon(result: dict[str, Any], preset: OptimizerPreset) -> str | None:
    """The P&L curve the sweep is judged on: the engine horizon nearest the
    target expiry (25DEC26 is ~80 days out today, so the 90-day curve), not
    the Now curve. None when the engine returned no horizon curves."""
    keys = [k for k in ((result.get("after") or {}).get("payoff_by_horizon") or {})
            if k in ((result.get("before") or {}).get("payoff_by_horizon") or {})]
    nums = [k for k in keys if str(k).lstrip("-").isdigit()]
    if not nums:
        return None
    dte = days_to_expiry(preset.effective_expiry())
    want = dte if dte is not None and dte > 0 else 90
    return min(nums, key=lambda k: (abs(int(k) - want), -int(k)))


def track_target(ladder: list[float], spot: float, before, after, target,
                 preset: OptimizerPreset) -> dict[str, Any] | None:
    """How the after-curve follows the target's shape, against today's book.

    The target is a shape (the V's -$20M trough at spot is not a mark the book
    can be at), so both curves are compared with it relative to their own
    value at spot: distance(x) = (curve(x) - curve(spot)) - (target(x) - target(spot)).
    Returns None when the engine gave no target.
    """
    if not (ladder and before and after and target and spot):
        return None
    i0 = _nearest(ladder, spot)
    lo, hi = ladder[0], ladder[-1]
    pts, away = [], []
    for m in TRACK_MONEYNESS:
        x = spot * (1 + m)
        if not (lo <= x <= hi):
            continue
        i = _nearest(ladder, x)
        tgt = float(target[i]) - float(target[i0])
        db = (float(before[i]) - float(before[i0])) - tgt
        da = (float(after[i]) - float(after[i0])) - tgt
        pts.append({"moneyness": m, "spot": ladder[i], "before_usd": round(db, 0), "after_usd": round(da, 0)})
        allow = max(preset.track_tolerance_usd, preset.track_tolerance_pct * abs(db))
        if m in KEY_MONEYNESS and abs(da) > abs(db) + allow:
            away.append(f"{m:+.0%} ${(abs(da) - abs(db)) / 1e6:,.2f}M further")
    tot_b = sum(abs(p["before_usd"]) for p in pts)
    tot_a = sum(abs(p["after_usd"]) for p in pts)
    gain = 100.0 * (tot_b - tot_a) / tot_b if tot_b else 0.0
    return {"gain_pct": gain, "points": pts, "away": away,
            "distance_before_usd": tot_b, "distance_after_usd": tot_a}


def summarize(result: dict[str, Any], variant: dict[str, Any], preset: OptimizerPreset,
              policy: AssetPolicy | None = None) -> dict[str, Any]:
    """Reduce one engine result to what the ranking and the handover need.

    Everything is judged on the target-expiry curve (judge_horizon), because
    that is when the structure pays and it is what the P&L matrix shows; the
    Now curve is kept alongside for reference. With ``policy`` the gate's own
    curve tests (floor, max loss, view) run here too, on the engine's cost.
    """
    trades = result.get("trades") or []
    opt_lines = [t for t in trades if "BOX" not in str(t.get("strategy") or "").upper()]
    spot = float(result.get("spot") or 0)
    ladder = [float(x) for x in (result.get("spot_ladder") or [])]
    now_before = result.get("before_payoff") or []
    now_after = result.get("after_payoff") or []
    h = judge_horizon(result, preset)
    if h is not None:
        before = result["before"]["payoff_by_horizon"][h]
        after = result["after"]["payoff_by_horizon"][h]
    else:
        before, after = now_before, now_after
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
    track = track_target(ladder, spot, before, after, result.get("target_payoff"), preset)
    # No target from the engine: fall back to its own fit gain.
    track_gain = track["gain_pct"] if track else fit_gain
    disq = []
    if not result.get("optimizer_converged"):
        disq.append("did not converge")
    if result.get("status") != "ok":
        disq.append(result.get("message") or "no result")
    if worst < -preset.max_giveback_usd:
        disq.append(f"gives back ${-worst:,.0f} at a key spot")
    if track and track["away"]:
        disq.append("moves away from the target: " + ", ".join(track["away"]))
    policy_findings = []
    if policy is not None and ladder and before and after and spot:
        h_days = int(h) if h is not None else 0
        policy_findings = curve_tests(ladder, before, after, spot, cost, policy, h_days)["findings"]
        disq += [f"{text} ({rule})" for kind, text, rule in policy_findings if kind == "fail"]
    score = (track_gain - preset.score_cost_per_100k * cost / 100_000
             - preset.score_per_option_line * len(opt_lines))
    return {
        "variant": variant,
        "track_gain_pct": round(track_gain, 2),
        "track": track,
        "policy_findings": policy_findings,
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
        "judged_on_days": int(h) if h is not None else 0,
        "now_before_payoff": now_before,
        "now_after_payoff": now_after,
        "target_payoff": result.get("target_payoff") or [],
    }


def rank(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best score first; ties go to fewer lines, then lower cost."""
    return sorted(summaries, key=lambda s: (-s["score"], s["option_lines"], s["cost_usd"]))


def pareto(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs no other run beats on tracking, cost and line count at once."""
    ok = [s for s in summaries if not s["disqualified"]]
    front = []
    for s in ok:
        dominated = any(
            o is not s and o["track_gain_pct"] >= s["track_gain_pct"] and o["cost_usd"] <= s["cost_usd"]
            and o["option_lines"] <= s["option_lines"]
            and (o["track_gain_pct"], -o["cost_usd"], -o["option_lines"])
            != (s["track_gain_pct"], -s["cost_usd"], -s["option_lines"])
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
    targets = preset.targets(policy.asset)
    summaries, errors, refined, sized = [], [], {}, {}

    async def run_all(tgt: dict, vs: list[dict], done: set) -> list[dict]:
        out = []
        for v in vs:
            if _vkey(v) in done or len(done) >= max(1, preset.max_runs):
                continue
            done.add(_vkey(v))
            kw = run_kwargs(policy.asset, preset, v, tgt)
            try:
                result = await asyncio.to_thread(_run_one, pnl, collateral, kw)
                s = summarize(result, v, preset, policy)
                s["target"] = tgt
                out.append(s)
            except Exception as e:                  # one bad combination must not stop the sweep
                errors.append({"variant": v, "target": tgt["name"],
                               "error": f"{type(e).__name__}: {e}"[:300]})
        return out

    for tgt in targets:
        done: set = set()
        if variants:
            summaries += await run_all(tgt, variants, done)
            continue
        mine = await run_all(tgt, coarse_grid(preset), done)
        if not preset.lam_refine_step:
            summaries += mine
            continue
        # Disqualified runs still tell us where λ works best, so score them on tracking.
        by_lam: dict[float, float] = {}
        for s in mine:
            sc = _search_score(s)
            by_lam[s["variant"]["lam_factor"]] = max(by_lam.get(s["variant"]["lam_factor"], -1e18), sc)
        fine = refine_lams(preset, by_lam)
        refined[tgt["name"]] = fine
        # λ first (first max_trades, largest qty), then max trades at the best λ,
        # then max qty at the best (λ, trades): one axis at a time, so 5-15
        # trades x 1k-5k qty adds ~9 runs instead of multiplying the λ runs.
        qmax = max(preset.max_qty_grid)
        mine += await run_all(tgt, _one(preset, fine, preset.max_trades_grid[:1], [qmax]), done)
        b = best_variant(mine)
        if b:
            mine += await run_all(tgt, _one(preset, [b["lam_factor"]], preset.max_trades_grid, [qmax]), done)
            b = best_variant(mine)
            mine += await run_all(tgt, _one(preset, [b["lam_factor"]], [b["max_trades"]], preset.max_qty_grid), done)
            b = best_variant(mine)
            sized[tgt["name"]] = {"lam_factor": b["lam_factor"], "max_trades": b["max_trades"],
                                  "max_qty": b["max_qty"]}
        summaries += mine
    ranked = rank(summaries)
    # Gains are only comparable against the same target, so each target is
    # ranked on its own; the default target first. ``candidates`` holds each
    # target's passing runs best first (one per distinct trade list), which the
    # desk walks through the validation and the gate; ``best`` is the head.
    best, candidates = [], {}
    for tgt in targets:
        seen, mine = set(), []
        for s in ranked:
            if s["target"]["name"] != tgt["name"] or s["disqualified"]:
                continue
            key = tuple(sorted(f"{t.get('counterparty')}|{t.get('opt')}|{t.get('strike')}|{t.get('expiry')}|"
                               f"{t.get('side')}|{round(float(t.get('qty') or 0))}" for t in s["trades"]))
            if key in seen:
                continue
            seen.add(key)
            mine.append(s)
        candidates[tgt["name"]] = mine
        if mine:
            best.append(mine[0])
    return {
        "asset": policy.asset,
        "target": {"expiry": preset.effective_expiry(), "counterparties": preset.counterparties,
                   "trough": preset.target_trough_payoff, "down": preset.target_down_ratio,
                   "up": preset.target_up_ratio, "file": preset.target_profile_file},
        "targets": [t["name"] for t in targets],
        "runs": len(summaries) + len(errors),
        "lam_refined": refined,
        "sized": sized,
        "errors": errors,
        "ranked": ranked,
        "pareto": pareto(summaries),
        "best": best,
        "candidates": candidates,
        "spot": float(pnl.get("spot") or pnl.get("eth_spot") or 0),
        "book": pnl,                          # for the validation pass; not stored
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
        "judged_on_days": s.get("judged_on_days", 0),
        "target": s.get("target"),
        "reason": (f"Optimizer v4 sweep: {s['track_gain_pct']:+.1f}% closer to the target at T+"
                   f"{s.get('judged_on_days', 0)}d with {s['option_lines']} lines"),
        "track": s.get("track"),
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
