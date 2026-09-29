"""monday-pack sets B1's Monday reference from spot.

The reference is an observation — where the market was when the week's levels
were struck — so the agent can set it. What it must never do is overwrite a
reference a person struck deliberately, move the mandate during a preview, or
touch anything else in the policy.
"""

from __future__ import annotations

import pytest

from plgo_options.agents import desk
from plgo_options.agents.policy import default_policy
from plgo_options.agents.rows import week_key


@pytest.fixture
def saved(monkeypatch):
    """Capture save_policy calls instead of writing to the database."""
    calls = []

    async def fake_save(policy, by=""):
        calls.append((policy, by))
        return policy

    monkeypatch.setattr(desk.store, "save_policy", fake_save)
    return calls


@pytest.mark.asyncio
async def test_sets_reference_from_spot_when_none(saved):
    pol = default_policy("ETH")
    pol.reference_price, pol.reference_set_on = None, None

    note = await desk.set_monday_reference("ETH", 2672.46, pol)

    assert len(saved) == 1
    written, by = saved[0]
    assert written.reference_price == 2672.46
    assert written.reference_set_on == week_key()
    assert by == "monday-pack"
    assert "none before" in note


@pytest.mark.asyncio
async def test_does_not_restrike_a_reference_already_set_this_week(saved):
    """A person may have struck the week deliberately; do not move it."""
    pol = default_policy("ETH")
    pol.reference_price, pol.reference_set_on = 3000.0, week_key()

    note = await desk.set_monday_reference("ETH", 2672.46, pol)

    assert note is None
    assert saved == []
    assert pol.reference_price == 3000.0


@pytest.mark.asyncio
async def test_restrikes_a_stale_reference_from_a_previous_week(saved):
    pol = default_policy("FIL")
    pol.reference_price, pol.reference_set_on = 1.40, "2020-01-06"

    note = await desk.set_monday_reference("FIL", 1.0552, pol)

    assert saved and saved[0][0].reference_price == 1.0552
    assert saved[0][0].reference_set_on == week_key()
    assert "was" in note


@pytest.mark.asyncio
@pytest.mark.parametrize("spot", [None, 0, -5])
async def test_a_missing_or_absurd_spot_writes_nothing(saved, spot):
    """A dead price feed must not strike the week at zero — that would put
    every row at zero and fire all of them."""
    pol = default_policy("ETH")
    pol.reference_price, pol.reference_set_on = None, None

    assert await desk.set_monday_reference("ETH", spot, pol) is None
    assert saved == []
    assert pol.reference_price is None


@pytest.mark.asyncio
async def test_touches_nothing_else_in_the_policy(saved):
    """Whether the limits are real is an A3 decision; setting B1 must not
    silently clear the EXAMPLE flag or move the floor, stop or limits."""
    pol = default_policy("ETH")
    pol.reference_price, pol.reference_set_on = None, None
    before = {k: getattr(pol, k) for k in
              ("is_example", "floor_price", "stop_price", "view",
               "max_single_trade_usd", "row_steps_pct", "allowed_counterparties")}

    await desk.set_monday_reference("ETH", 2672.46, pol)

    written = saved[0][0]
    for k, v in before.items():
        assert getattr(written, k) == v, f"{k} changed"


def _isolate_monday_pack(monkeypatch):
    """Cut monday-pack off from the network and the database.

    build_market_trend is imported inside the function, so it has to be patched
    on its own module; leaving it live makes this test hang on a real fetch
    rather than fail.
    """
    import plgo_options.web.market_trend as mt

    async def book(asset):
        return {"spot": 2672.46, "mtm": 0.0, "theta": 0.0}

    async def trend(asset, spot):
        return {}

    async def policy(asset):
        p = default_policy(asset)
        p.reference_price, p.reference_set_on = None, None
        return p

    async def fired(*a, **k):
        return set()

    async def proposals(*a, **k):
        return []

    monkeypatch.setattr(desk, "get_book", book)
    monkeypatch.setattr(mt, "build_market_trend", trend)
    monkeypatch.setattr(desk.store, "get_policy", policy)
    monkeypatch.setattr(desk.store, "fired_rows", fired)
    monkeypatch.setattr(desk.store, "list_proposals", proposals)


@pytest.mark.asyncio
async def test_preview_runs_do_not_move_the_mandate(saved, monkeypatch):
    """`deliver: false` is the "what would this say?" path — the Run button and
    every smoke test use it — so it must not write."""
    _isolate_monday_pack(monkeypatch)
    res = await desk.monday_pack({"deliver": False})
    assert saved == []
    assert "REFERENCE (set automatically" not in res["facts"]


@pytest.mark.asyncio
async def test_the_real_run_sets_both_books(saved, monkeypatch):
    _isolate_monday_pack(monkeypatch)
    res = await desk.monday_pack({"deliver": True})
    assert [p.asset for p, _ in saved] == ["ETH", "FIL"]
    assert all(p.reference_set_on == week_key() for p, _ in saved)
    assert "REFERENCE (set automatically" in res["facts"]


@pytest.mark.asyncio
async def test_run_agent_defaults_deliver_into_ctx(monkeypatch):
    """monday-pack can only tell a preview from the real run because run_agent
    puts `deliver` in ctx."""
    seen = {}

    async def spy(ctx):
        seen.update(ctx)
        return {"facts": "ok", "deliver": False}

    monkeypatch.setitem(desk.AGENTS, "monday-pack", spy)
    monkeypatch.setattr(desk.store, "kill_switch_on", _false)
    monkeypatch.setattr(desk.store, "start_run", _one)
    monkeypatch.setattr(desk.store, "finish_run", _noop)

    await desk.run_agent("monday-pack", {}, deliver=True, use_ai=False)
    assert seen.get("deliver") is True

    seen.clear()
    await desk.run_agent("monday-pack", {}, deliver=False, use_ai=False)
    assert seen.get("deliver") is False


async def _false(*a, **k):
    return False


async def _one(*a, **k):
    return 1


async def _noop(*a, **k):
    return None
