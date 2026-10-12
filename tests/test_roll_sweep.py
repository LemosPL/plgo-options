"""Roll sweep: pick legs by DTE (never G20), close them, find the best replacement."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from plgo_options.agents import desk
from plgo_options.agents import optimizer as opt
from plgo_options.agents.policy import default_policy


def _pos(i, cp, opt_, k, days, qty=-1_000_000):
    return {"id": i, "counterparty": cp, "opt": opt_, "strike": k, "side": "Short" if qty < 0 else "Long",
            "expiry": (date.today() + timedelta(days=days)).isoformat(), "net_qty": qty,
            "current_mtm": -10_000.0, "instrument": f"FIL-X-{k}-{opt_}"}


@pytest.fixture
def book(monkeypatch):
    positions = [_pos(1, "Flowdesk", "C", 1.5, 18), _pos(2, "KeyRock", "P", 1.35, 18),
                 _pos(3, "G20", "P", 1.799, 4), _pos(4, "Flowdesk", "C", 2.0, 74),
                 _pos(5, "Flowdesk", "F", 0, 18)]

    async def portfolio_pnl(asset="FIL", include_expired=False):
        return {"spot": 1.25, "positions": positions}

    import plgo_options.web.routes.portfolio as port
    monkeypatch.setattr(port, "portfolio_pnl", portfolio_pnl)
    return positions


@pytest.mark.asyncio
async def test_candidates_are_the_window_minus_g20_and_perps(book):
    pol = default_policy("FIL")
    pol.optimizer.counterparties = []
    legs = await opt.roll_candidates(pol, 20)
    assert [l["id"] for l in legs] == [1, 2]                 # G20 (4 DTE) never; 74 DTE and the perp out
    assert legs[1]["itm"] is True and legs[0]["itm"] is False


@pytest.mark.asyncio
async def test_candidates_follow_the_sweep_counterparties(book):
    pol = default_policy("FIL")
    pol.optimizer.counterparties = ["KeyRock"]
    assert [l["id"] for l in await opt.roll_candidates(pol, 20)] == [2]


def test_roll_runs_close_exactly_the_chosen_legs_into_the_chosen_expiry():
    pol = default_policy("FIL")
    rp = opt.roll_policy(pol, {"ids": [1, 2], "into": "27MAR27"})
    assert rp.optimizer.effective_expiry() == "27MAR27" and pol.optimizer.target_expiry == "25DEC26"
    kw = opt.roll_kwargs({"roll_dte_threshold": 7}, {"ids": ["1", 2]})
    assert kw["roll_dte_threshold"] == -1 and kw["forced_roll_ids"] == [1, 2]
    assert opt.roll_kwargs({"roll_dte_threshold": 7}, None) == {"roll_dte_threshold": 7}


@pytest.mark.asyncio
async def test_roll_sweep_drops_legs_it_may_not_roll(book, monkeypatch):
    seen = {}

    async def get_policy(asset):
        p = default_policy("FIL")
        p.optimizer.counterparties = []
        return p

    async def fake_optimizer(ctx):
        seen.update(ctx)
        return {"facts": "ok"}

    monkeypatch.setattr(desk.store, "get_policy", get_policy)
    monkeypatch.setattr(desk, "optimizer", fake_optimizer)
    await desk.roll_sweep({"assets": ["FIL"], "roll": {"ids": [1, 3, 99], "into": "25DEC26"}})
    assert seen["roll"]["ids"] == [1]                          # G20 leg 3 and unknown 99 dropped
    out = await desk.roll_sweep({"assets": ["FIL"], "roll": {"ids": [3]}})
    assert "no legs to roll" in out["facts"]
