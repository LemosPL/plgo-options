"""The sweep tests several target profiles and ranks each on its own."""

from __future__ import annotations

import pytest

from plgo_options.agents import decisions
from plgo_options.agents import optimizer as opt
from plgo_options.agents.policy import OptimizerPreset, default_policy


def test_default_targets_are_the_v_then_the_saved_profiles():
    t = OptimizerPreset().targets("ETH")
    assert t[0]["file"] is None and t[0]["name"].startswith("V ")
    files = [x["file"] for x in t[1:]]
    assert "ETH - target.csv" in files and len(t) <= 4
    assert all(f.startswith("ETH - ") for f in files)
    assert all(x["file"] is None or x["file"].startswith("FIL - ") for x in OptimizerPreset().targets("FIL"))


def test_explicit_target_grid_and_legacy_file():
    p = OptimizerPreset(target_grid=["parametric", "ETH - target shifted.csv"])
    assert [x["file"] for x in p.targets("ETH")] == [None, "ETH - target shifted.csv"]
    assert [x["file"] for x in OptimizerPreset(target_profile_file="X.csv").targets("ETH")] == ["X.csv"]


def test_run_kwargs_carries_the_target_file():
    p = OptimizerPreset()
    v = opt.build_grid(p)[0]
    assert opt.run_kwargs("ETH", p, v, {"name": "t", "file": "ETH - target.csv"})["target_profile_file"] == "ETH - target.csv"
    assert opt.run_kwargs("ETH", p, v, {"name": "V", "file": None})["target_profile_file"] is None


def test_coarse_pass_spans_lambda_and_fil_trades_in_fil_size():
    c = opt.coarse_grid(OptimizerPreset())
    assert [v["lam_factor"] for v in c] == [0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    assert {v["max_trades"] for v in c} == {5}
    assert default_policy("FIL").optimizer.max_qty_grid == [5_000_000.0]
    fil = decisions.apply_sheet(default_policy("FIL"), decisions.SWEEP_2026_10_06["FIL"])
    assert fil.optimizer.max_qty_grid == [5_000_000.0] and fil.optimizer.lam_grid[-1] == 3.5


def test_refine_between_best_and_better_neighbour():
    p = OptimizerPreset()
    sc = {0.1: 1, 0.5: 2, 1.0: 3, 1.5: 4, 2.0: 5, 2.5: 9, 3.0: 8, 3.5: 1}
    assert opt.refine_lams(p, sc) == [2.5, 2.6, 2.7, 2.8, 2.9, 3.0]          # Lucas's example
    assert opt.refine_lams(p, {**sc, 0.1: 99, 0.5: 50}) == [0.1, 0.2, 0.3, 0.4, 0.5]  # best at the edge
    assert opt.refine_lams(OptimizerPreset(lam_refine_step=None), sc) == []


@pytest.mark.asyncio
async def test_sweep_returns_the_best_run_per_target(monkeypatch):
    pol = default_policy("ETH")
    pol.optimizer.target_grid = ["parametric", "ETH - target.csv"]

    async def book(asset):
        return {"spot": 2700, "totals": {}}, {}

    def run_one(pnl, collateral, kw):
        # Fits against the CSV look far better; each target must still get its own best.
        base = 80 if kw["target_profile_file"] else 20
        return {"status": "ok", "optimizer_converged": True, "spot": 2700,
                "fit_error_before": 100.0, "fit_error_after": 100.0 - base * kw["lam_factor"] / 4,
                "total_cost_usd": 10_000, "trades": [], "spot_ladder": [], "before_payoff": [],
                "after_payoff": []}

    monkeypatch.setattr(opt, "gather_book", book)
    monkeypatch.setattr(opt, "_run_one", run_one)
    res = await opt.sweep(pol)
    # Fit rises with λ here, so each target refines 3.0-3.5: 8 coarse + 6 λ x 2 caps - 2 already run.
    assert res["lam_refined"] == {t: [3.0, 3.1, 3.2, 3.3, 3.4, 3.5] for t in res["targets"]}
    assert res["runs"] == 2 * (8 + 12 - 2) and len(res["best"]) == 2
    assert res["best"][0]["target"]["file"] is None
    assert res["best"][1]["target"]["file"] == "ETH - target.csv"
    assert all(b["variant"]["lam_factor"] == 3.5 for b in res["best"])


def test_three_targets_by_default_and_manual_pick_wins():
    assert [t["file"] for t in OptimizerPreset().targets("ETH")][:1] == [None]
    assert len(OptimizerPreset().targets("ETH")) == 3
    pick = ["ETH - target shifted v2.csv", "parametric", "ETH - target.csv"]
    assert [t["file"] for t in OptimizerPreset(target_grid=pick).targets("ETH")] == \
        ["ETH - target shifted v2.csv", None, "ETH - target.csv"]


def test_runs_are_judged_on_the_target_expiry_curve_not_now():
    from datetime import date, timedelta
    p = OptimizerPreset(target_expiry=(date.today() + timedelta(days=80)).strftime("%d%b%y").upper(),
                        next_expiry=None)
    ladder = [2000.0, 2700.0, 3600.0, 5000.0]
    res = {"status": "ok", "optimizer_converged": True, "spot": 2700, "spot_ladder": ladder,
           "fit_error_before": 100, "fit_error_after": 50, "total_cost_usd": 0, "trades": [],
           # Now: gives back $1M on the downside; at T+90 it is better everywhere.
           "before_payoff": [0, 0, 0, 0], "after_payoff": [-1e6, 0, 0, 0],
           "before": {"payoff_by_horizon": {"0": [0, 0, 0, 0], "30": [0, 0, 0, 0], "90": [0, 0, 0, 0]}},
           "after": {"payoff_by_horizon": {"0": [-1e6, 0, 0, 0], "30": [-5e5, 0, 0, 0], "90": [1e5, 0, 1e5, 1e5]}}}
    assert opt.judge_horizon(res, p) == "90"
    s = opt.summarize(res, opt.build_grid(p)[0], p)
    assert s["judged_on_days"] == 90 and not s["disqualified"]
    assert s["after_payoff"] == [1e5, 0, 1e5, 1e5] and s["now_after_payoff"] == [-1e6, 0, 0, 0]
