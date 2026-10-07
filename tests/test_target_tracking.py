"""7 Oct 2026: proposals must follow the target on the curve the P&L matrix
shows, and pass the gate's own floor / max-loss / view tests in the sweep."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from plgo_options.agents import desk, gate
from plgo_options.agents import optimizer as opt
from plgo_options.agents.policy import default_policy
from plgo_options.agents.validate import curve_tests

SPOT = 2700.0
LADDER = [SPOT * (1 + m / 100) for m in range(-60, 125, 5)]


def v_target(x):
    """A V: trough at spot, +$1M per 1% move either way."""
    return -20e6 + 1e6 * abs(x / SPOT - 1) * 100


def policy():
    pol = default_policy("ETH")
    pol.optimizer.target_expiry = (date.today() + timedelta(days=80)).strftime("%d%b%y").upper()
    pol.optimizer.next_expiry = None
    pol.view = "big_move"
    return pol


def result(after_fn, fit_after=50.0, cost=50_000.0):
    before = [0.0] * len(LADDER)                       # today's book: flat
    after = [after_fn(x) for x in LADDER]
    return {"status": "ok", "optimizer_converged": True, "spot": SPOT, "spot_ladder": LADDER,
            "fit_error_before": 100.0, "fit_error_after": fit_after, "total_cost_usd": cost,
            "trades": [{"strategy": "PUT", "qty": 100, "side": "Buy", "opt": "P", "strike": 2400,
                        "expiry": "2026-12-25", "counterparty": "Flowdesk"}],
            "target_payoff": [v_target(x) for x in LADDER],
            "before_payoff": before, "after_payoff": after,
            "before": {"payoff_by_horizon": {"0": before, "80": before}},
            "after": {"payoff_by_horizon": {"0": after, "80": after}}}


VAR = {"lam_factor": 1.0, "downside_factor": 1.1, "t90_weight": 0.5, "max_trades": 5, "max_qty": 5000}


def test_ranked_on_tracking_the_target_not_the_engine_fit():
    pol = policy()
    # Half-way to the V on both wings, but the engine says only +10% fit.
    follows = opt.summarize(result(lambda x: 0.5e6 * abs(x / SPOT - 1) * 100, fit_after=90.0),
                            VAR, pol.optimizer, pol)
    # The engine likes this one (+80% fit) but it hardly moves the expiry curve.
    flat = opt.summarize(result(lambda x: 0.02e6 * abs(x / SPOT - 1) * 100, fit_after=20.0),
                         VAR, pol.optimizer, pol)
    assert follows["track_gain_pct"] > 40 and flat["track_gain_pct"] < 5
    assert opt.rank([flat, follows])[0] is follows


def test_moving_away_from_the_target_at_a_key_spot_is_out():
    pol = policy()
    # Great on the downside, but sells the upside: at +35% / +85% the book
    # ends up further from the V than it is today.
    s = opt.summarize(result(lambda x: 1e6 * (1 - x / SPOT) * 100), VAR, pol.optimizer, pol)
    assert any(d.startswith("moves away from the target") for d in s["disqualified"])
    assert "+35%" in s["disqualified"][0] or any("+85%" in d for d in s["disqualified"])


def test_up_view_rejects_giving_away_the_upside():
    pol = policy()
    pol.view, pol.upside_target_price = "up", 3500.0
    # The book's worst loss is at spot; the trade adds downside and sells some upside.
    before = [-2e6 + 1e4 * abs(x - SPOT) for x in LADDER]
    after = [b + (1e6 if x < SPOT else (-3e5 if x > SPOT * 1.2 else 0.0)) for b, x in zip(before, LADDER)]
    t = curve_tests(LADDER, before, after, SPOT, 0.0, pol, 80)
    fails = [f for f in t["findings"] if f[0] == "fail"]
    assert len(fails) == 1 and fails[0][2] == "A1" and "upside" in fails[0][1]
    # The same trade is fine under a big-move view.
    pol.view = "big_move"
    assert not [f for f in curve_tests(LADDER, before, after, SPOT, 0.0, pol, 80)["findings"] if f[0] == "fail"]


def test_sweep_applies_the_gates_floor_test():
    pol = policy()
    pol.floor_price = 2300.0
    # Better everywhere except a $200k dent below the floor: the gate would
    # reject it, so the sweep must not rank it.
    s = opt.summarize(result(lambda x: (-2e5 if x < 2300 else 0.6e6 * abs(x / SPOT - 1) * 100), cost=0.0),
                      VAR, pol.optimizer, pol)
    assert any("floor" in d for d in s["disqualified"])


@pytest.mark.asyncio
async def test_desk_walks_down_the_ranking_until_the_gate_accepts(monkeypatch):
    pol = policy()
    pol.optimizer.target_grid = ["parametric"]
    a = opt.summarize(result(lambda x: 0.6e6 * abs(x / SPOT - 1) * 100), VAR, pol.optimizer, pol)
    b = opt.summarize(result(lambda x: 0.5e6 * abs(x / SPOT - 1) * 100),
                      {**VAR, "max_trades": 7}, pol.optimizer, pol)
    b["trades"] = [dict(b["trades"][0], strike=2300)]
    tname = pol.optimizer.targets("ETH")[0]["name"]
    for s in (a, b):
        s["target"] = {"name": tname, "file": None}

    async def fake_sweep(p):
        return {"asset": "ETH", "target": {"expiry": "x", "counterparties": []}, "targets": [tname],
                "runs": 2, "errors": [], "ranked": [a, b], "pareto": [a, b], "best": [a],
                "candidates": {tname: [a, b]}, "spot": SPOT, "book": {}, "book_mtm": 0.0}

    async def validated(prop, p, book=None):
        # Our pricing finds the first one pushes the floor down.
        bad = prop["variant"]["max_trades"] == 5
        prop["validation"] = {"findings": [("fail" if bad else "ok", "floor", "A2")]}
        return prop

    filed, superseded = [], []

    async def add_proposal(agent, asset, kind, summary, route, reasons, proposal):
        filed.append((route, proposal["variant"]["max_trades"], summary))
        return len(filed)

    async def supersede_open(asset, agent, keep):
        superseded.append(keep)
        return 3

    async def get_policy(a):
        return pol

    async def nothing(*a, **k):
        return []

    async def gate_ctx(p, spot, book=None, perp=None):
        return gate.GateContext(policy=p, spot=spot)

    monkeypatch.setattr(desk.opt_mod, "sweep", fake_sweep)
    monkeypatch.setattr(desk, "_validated", validated)
    monkeypatch.setattr(desk, "_gate_ctx", gate_ctx)
    monkeypatch.setattr(desk, "get_perp", lambda a: nothing())
    monkeypatch.setattr(desk.store, "get_policy", get_policy)
    monkeypatch.setattr(desk.store, "add_proposal", add_proposal)
    monkeypatch.setattr(desk.store, "supersede_open", supersede_open)
    monkeypatch.setattr(desk.store, "recent_curves", lambda a, n: nothing())
    monkeypatch.setattr(desk.store, "save_curve", lambda *a, **k: nothing())
    out = await desk.optimizer({"assets": ["ETH"], "label": "am"})
    assert [(r, m) for r, m, _ in filed] == [(gate.CHRIS, 7)]
    assert "rank 2" in filed[0][2] and superseded == [[1]]
    assert "superseded" in out["facts"]


@pytest.mark.asyncio
async def test_supersede_clears_old_open_proposals_even_when_nothing_new(tmp_path, monkeypatch):
    import aiosqlite
    from plgo_options.agents import store
    db = await aiosqlite.connect(tmp_path / "t.db")
    await db.execute("CREATE TABLE agent_proposals (id INTEGER PRIMARY KEY, asset TEXT, agent TEXT, "
                     "status TEXT, decided_by TEXT, decided_at TEXT)")
    await db.executemany("INSERT INTO agent_proposals (id, asset, agent, status) VALUES (?,?,?,?)",
                         [(1, "ETH", "optimizer", "open"), (2, "ETH", "optimizer", "open"),
                          (3, "FIL", "optimizer", "open"), (4, "ETH", "row_watcher", "open")])
    await db.commit()

    async def get_db():
        return db

    monkeypatch.setattr(store, "get_db", get_db)
    assert await store.supersede_open("ETH", "optimizer", [2]) == 1
    assert await store.supersede_open("ETH", "optimizer", []) == 1
    rows = dict(await (await db.execute("SELECT id, status FROM agent_proposals")).fetchall())
    assert rows == {1: "superseded", 2: "superseded", 3: "open", 4: "open"}
    await db.close()
