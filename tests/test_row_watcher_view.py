"""Row watcher: rows wait for this week's reference, last week's row trades
are superseded, and under the "up" view an up-row never proposes a perp sale."""

from __future__ import annotations

import pytest

from plgo_options.agents import desk
from plgo_options.agents.policy import default_policy
from plgo_options.agents.rows import week_key, week_start_utc


@pytest.fixture
def env(monkeypatch):
    state = {"proposals": [], "superseded": [], "fired": []}
    pol = default_policy("FIL")
    pol.view, pol.reference_price, pol.reference_set_on = "up", 1.0, week_key()
    pol.row_steps_pct = [10, 20, 30]
    pol.stop_price = 0.5
    state["pol"] = pol

    async def get_policy(asset):
        return pol if asset == "FIL" else default_policy("ETH")

    async def get_spot(asset):
        return 1.21 if asset == "FIL" else None

    async def supersede_open(asset, agent, keep, created_before=None):
        state["superseded"].append((asset, agent, created_before))
        return 0

    async def mark_fired(asset, week, key, spot):
        state["fired"].append((asset, key))
        return True

    async def add_proposal(*a, **k):
        state["proposals"].append(a)
        return len(state["proposals"])

    async def empty(*a, **k):
        return {}

    monkeypatch.setattr(desk.store, "get_policy", get_policy)
    monkeypatch.setattr(desk, "get_spot", get_spot)
    monkeypatch.setattr(desk.store, "supersede_open", supersede_open)
    monkeypatch.setattr(desk.store, "mark_fired", mark_fired)
    monkeypatch.setattr(desk.store, "add_proposal", add_proposal)
    monkeypatch.setattr(desk, "get_collateral", empty)
    return state


@pytest.mark.asyncio
async def test_up_view_does_not_sell_perp_on_an_up_row(env):
    out = await desk.row_watcher({})
    assert env["fired"] == [("FIL", "+20")]
    assert env["proposals"] == []
    assert "view is UP" in out["facts"]


@pytest.mark.asyncio
async def test_rows_wait_for_this_weeks_reference(env):
    env["pol"].reference_set_on = "2026-01-05"
    out = await desk.row_watcher({})
    assert env["fired"] == []
    assert "reference not set yet" in out["facts"]


@pytest.mark.asyncio
async def test_last_weeks_row_proposals_are_superseded(env):
    await desk.row_watcher({})
    assert ("FIL", "row_watcher", week_start_utc()) in env["superseded"]


def test_week_start_is_uk_monday_midnight():
    ws = week_start_utc()
    assert ws[:10] <= week_key() and ws.endswith("+00:00")
