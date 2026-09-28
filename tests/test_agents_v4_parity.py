"""The Optimizer agent must hand the engine exactly what the v4 page would.

Captures the OptimizerRunParams that POST /api/optimization/run builds from the
v4 page's request body, and compares it field by field with what the agent
builds from its preset - for the winning 28 Sep 2026 settings.
"""

import asyncio
from dataclasses import asdict

from plgo_options.agents import optimizer as opt
from plgo_options.agents.policy import OptimizerPreset
from plgo_options.optimization import optim_usecase
from plgo_options.optimization.optim_usecase import OptimizerRunParams
from plgo_options.web.routes import optimization as route


def test_agent_params_match_v4_page(monkeypatch):
    captured = {}

    async def fake_pnl(asset, include_expired=False):
        return {"spot": 2684.0, "positions": [], "totals": {}}

    async def fake_perp(asset, pnl):
        return None

    async def fake_coll():
        return {}

    class FakeUseCase:
        @classmethod
        def from_portfolio_payload(cls, pnl, params):
            captured["params"] = params
            return cls()

        def run(self):
            return {"status": "ok"}

    monkeypatch.setattr(route, "portfolio_pnl", fake_pnl)
    monkeypatch.setattr(route, "_build_perp_position", fake_perp)
    monkeypatch.setattr(route, "_fetch_collateral_by_cp", fake_coll)
    monkeypatch.setattr(route, "OptimizerUseCase", FakeUseCase)

    preset = OptimizerPreset()
    variant = {"lam_factor": 0.5, "downside_factor": 1.1, "t90_weight": 0.5, "max_trades": 5, "max_qty": 5000.0}

    # What the v4 page posts for these settings (app.js, btn-run-optv4).
    page_body = dict(
        asset="ETH", lam_factor=0.5, downside_factor=1.1, t90_weight=0.5, atm_concentration=0,
        mu_factor=2.3, cash_neutrality_factor=0.05, target_expiry="25DEC26", unwind_discount=0.2,
        new_position_penalty=0.04, roll_dte_threshold=7, roll_itm_only=False,
        collateral_budget_pct=0.05, max_qty=5000, max_trades=5, enable_box_neutralizer=True,
        enable_composite_unwind=True, counterparties=["Flowdesk"], bid_ask_vol_pts=5.0,
        enable_delta_rehedge=False, delta_band_usd=150000, target_profile_file=None,
        parametric_trough_payoff=-17_500_000, parametric_low_floor_ratio=0.85,
        parametric_high_plateau_ratio=0.75,
    )
    asyncio.run(route.run_optimizer(route.OptimizationParams(**page_body)))
    page = asdict(captured["params"])

    agent = asdict(OptimizerRunParams(collateral_by_cp={}, enforce_collateral_cap=False,
                                      **opt.run_kwargs("ETH", preset, variant)))
    diffs = {k: (page[k], agent[k]) for k in page if page[k] != agent[k]}
    assert diffs == {}, diffs
