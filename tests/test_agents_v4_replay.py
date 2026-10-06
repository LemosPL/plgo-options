"""'Validate in v4': the endpoint that hands the v4 page a proposal's run settings and legs."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from plgo_options.agents import optimizer as opt_mod
from plgo_options.agents.policy import default_policy
from plgo_options.web.routes import agents as routes

LEG = {"kind": "option", "side": "Sell", "opt": "C", "strike": 3200, "expiry": "2026-12-25",
       "qty": 100, "counterparty": "Flowdesk", "is_unwind": True}


def _store(monkeypatch, row):
    async def get_proposal(pid):
        return row if row and pid == row["id"] else None

    async def get_policy(asset):
        return default_policy(asset)

    monkeypatch.setattr(routes.store, "get_proposal", get_proposal)
    monkeypatch.setattr(routes.store, "get_policy", get_policy)


@pytest.mark.asyncio
async def test_optimizer_proposal_replays_its_stored_params(monkeypatch):
    pol = default_policy("ETH")
    v = {"lam_factor": 0.5, "downside_factor": 1.1, "t90_weight": 0.5, "max_trades": 5, "max_qty": 5000.0}
    params = opt_mod.run_kwargs("ETH", pol.optimizer, v)
    _store(monkeypatch, {"id": 7, "asset": "ETH", "agent": "optimizer", "kind": "v4 am #1",
                         "summary": "x", "route": "chris", "reasons": [],
                         "proposal": {"legs": [LEG], "variant": v, "v4_params": params}})
    out = await routes.proposal_v4(7)
    assert out["params_source"] == "stored" and out["params"] == params
    assert out["legs"] == [LEG]


@pytest.mark.asyncio
async def test_old_optimizer_proposal_reads_the_summary(monkeypatch):
    _store(monkeypatch, {"id": 8, "asset": "ETH", "agent": "optimizer", "kind": "v4 am #1",
                         "summary": "λ0.5 κ1.1 T+90 0.5 max 7 trades / 5000 qty: fit +63%",
                         "route": "chris", "reasons": [], "proposal": {"legs": [LEG]}})
    out = await routes.proposal_v4(8)
    assert out["params_source"] == "summary"
    assert (out["params"]["lam_factor"], out["params"]["max_trades"]) == (0.5, 7)


@pytest.mark.asyncio
async def test_row_proposal_gets_todays_preset(monkeypatch):
    _store(monkeypatch, {"id": 9, "asset": "FIL", "agent": "row_watcher", "kind": "row +10",
                         "summary": "Sell perp", "route": "lucas", "reasons": [],
                         "proposal": {"legs": [{"kind": "perp", "side": "Sell", "qty": 1e6, "opt": "F"}]}})
    out = await routes.proposal_v4(9)
    assert out["params_source"] == "policy preset" and out["params"]["asset"] == "FIL"
    assert out["params"]["target_expiry"] == default_policy("FIL").optimizer.effective_expiry()


@pytest.mark.asyncio
async def test_unknown_proposal_is_404(monkeypatch):
    _store(monkeypatch, None)
    with pytest.raises(HTTPException) as e:
        await routes.proposal_v4(1)
    assert e.value.status_code == 404
