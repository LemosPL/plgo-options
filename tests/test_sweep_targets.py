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


def test_grid_spans_lambda_and_fil_trades_in_fil_size():
    lams = {v["lam_factor"] for v in opt.build_grid(OptimizerPreset())}
    assert lams == {0.3, 0.4, 0.5}
    assert default_policy("FIL").optimizer.max_qty_grid == [5_000_000.0]
    fil = decisions.apply_sheet(default_policy("FIL"), decisions.SWEEP_2026_10_06["FIL"])
    assert fil.optimizer.max_qty_grid == [5_000_000.0] and fil.optimizer.lam_grid == [0.3, 0.4, 0.5]


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
                "fit_error_before": 100.0, "fit_error_after": 100.0 - base * kw["lam_factor"],
                "total_cost_usd": 10_000, "trades": [], "spot_ladder": [], "before_payoff": [],
                "after_payoff": []}

    monkeypatch.setattr(opt, "gather_book", book)
    monkeypatch.setattr(opt, "_run_one", run_one)
    res = await opt.sweep(pol)
    assert res["runs"] == 12 and len(res["best"]) == 2
    assert res["best"][0]["target"]["file"] is None
    assert res["best"][1]["target"]["file"] == "ETH - target.csv"
    assert all(b["variant"]["lam_factor"] == 0.5 for b in res["best"])
