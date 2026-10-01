"""Delta rehedge booked with the book's counterparties.

Each counterparty's delta is hedged by a perp held with that counterparty, so
the hedge nets against the options it offsets there (collateral) instead of
sitting on an exchange venue nothing nets against. These pin down the split,
the optimizer's trade dicts, the Collateral page netting, and the per-venue
perp book.

No network: the live perp feed is stubbed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest

from plgo_options.data import perp_repository as repo
from plgo_options.data.database import PERP_FUNDING_SCHEMA, PERP_TRADES_SCHEMA
from plgo_options.optimization.base_optimizer import perp_counterparties
from plgo_options.optimization.delta_hedger import plan_counterparty_rehedge
from plgo_options.optimization.optimizer_v3 import OptimizerV3
from plgo_options.web.routes import collateral, perps


# ── plan_counterparty_rehedge ─────────────────────────────────────────────

def test_within_band_trades_nothing():
    plan = plan_counterparty_rehedge({"keyrock": 40.0, "flowdesk": -10.0}, {}, band=50.0)
    assert not plan.decision.breached
    assert plan.legs == {}


def test_offsetting_counterparties_leave_the_book_alone():
    # Each counterparty is far off, but the book nets to flat: no rehedge.
    plan = plan_counterparty_rehedge({"keyrock": 500.0, "flowdesk": -500.0}, {}, band=50.0)
    assert not plan.decision.breached
    assert plan.legs == {}


def test_breach_flattens_each_counterparty_where_its_options_sit():
    plan = plan_counterparty_rehedge(
        {"keyrock": 300.0, "flowdesk": 120.0}, {"keyrock": -100.0}, band=50.0)
    assert plan.decision.breached
    assert plan.legs == {"keyrock": -200.0, "flowdesk": -120.0}
    assert plan.residual == pytest.approx(0.0)


def test_exchange_hedge_is_unwound_as_counterparty_hedges_take_over():
    plan = plan_counterparty_rehedge(
        {"keyrock": 300.0}, {"binance futures": -100.0}, band=50.0)
    assert plan.legs == {"keyrock": -300.0, "binance futures": 100.0}
    assert plan.residual == pytest.approx(0.0)


def test_scoped_out_counterparty_keeps_its_delta():
    plan = plan_counterparty_rehedge(
        {"keyrock": 300.0, "flowdesk": 120.0}, {}, band=50.0, eligible={"keyrock"})
    assert plan.legs == {"keyrock": -300.0}
    assert plan.residual == pytest.approx(120.0)


def test_legs_below_the_floor_are_skipped():
    plan = plan_counterparty_rehedge(
        {"keyrock": 300.0, "flowdesk": 2.0}, {}, band=50.0, min_leg=5.0)
    assert plan.legs == {"keyrock": -300.0}


def test_perp_candidates_go_to_otc_counterparties():
    assert perp_counterparties(["KeyRock", "Binance Futures", "Flowdesk"]) == ["KeyRock", "Flowdesk"]
    assert perp_counterparties([]) == ["Binance Futures"]
    assert perp_counterparties(None) == ["Binance Futures"]


# ── OptimizerV3._build_delta_rehedge_trades ───────────────────────────────

def _call(cp, strike, qty, iv=60.0, days=90):
    return SimpleNamespace(opt="C", counterparty=cp, strike=strike, net_qty=qty,
                           iv_pct=iv, days_remaining=days)


def _perp(cp, qty):
    return SimpleNamespace(opt="F", counterparty=cp, strike=2_000.0, net_qty=qty,
                           iv_pct=0.0, days_remaining=36_500)


def _optimizer(positions, spot=2_000.0):
    opt = object.__new__(OptimizerV3)
    opt.positions = positions
    opt.spot = spot
    opt.asset = "ETH"
    return opt


def _rehedge(opt, trades=(), counterparties=None, band_usd=150_000.0):
    return opt._build_delta_rehedge_trades(
        list(trades), set(), delta_band_usd=band_usd,
        perp_cost_bps={"KeyRock": 1.0}, unwind_discount=0.2,
        new_position_penalty=0.04, counterparties=counterparties)


def test_optimizer_books_each_leg_at_its_counterparty():
    # Deep ITM calls: delta ~1 per contract, so ~1,000 / ~400 tokens long.
    opt = _optimizer([_call("KeyRock", 100.0, 1_000), _call("Flowdesk", 100.0, 400),
                      _perp("Binance Futures", -300)])
    trades = {t["counterparty"]: t for t in _rehedge(opt)}
    assert set(trades) == {"KeyRock", "Flowdesk", "Binance Futures"}
    assert trades["KeyRock"]["qty"] == -1_000 and trades["KeyRock"]["side"] == "Sell"
    assert trades["Flowdesk"]["qty"] == -400
    assert trades["Binance Futures"]["qty"] == 300   # legacy exchange hedge unwound
    assert all(t["opt"] == "F" and t["strategy"] == "DELTA_REHEDGE" for t in trades.values())
    # Per-counterparty cost: KeyRock at its own 1bp, the others at the 2bp fallback.
    assert trades["KeyRock"]["cost_usd"] == pytest.approx(1_000 * 2_000 * 1.0 / 10_000)
    assert trades["Flowdesk"]["cost_usd"] == pytest.approx(400 * 2_000 * 2.0 / 10_000)


def test_optimizer_matches_perp_venue_to_option_spelling():
    # Perp recorded as "Keyrock", options as "KeyRock": one counterparty,
    # booked under the options' spelling.
    opt = _optimizer([_call("KeyRock", 100.0, 1_000), _perp("Keyrock", -200)])
    trades = _rehedge(opt)
    assert [(t["counterparty"], t["qty"]) for t in trades] == [("KeyRock", -800)]


def test_optimizer_counts_proposed_option_trades_and_respects_scope():
    opt = _optimizer([_call("KeyRock", 100.0, 1_000), _call("Flowdesk", 100.0, 400)])
    proposed = [{"opt": "C", "counterparty": "KeyRock", "delta_contribution": 200.0}]
    trades = _rehedge(opt, proposed, counterparties=["KeyRock"])
    assert [(t["counterparty"], t["qty"]) for t in trades] == [("KeyRock", -1_200)]


def test_optimizer_within_band_returns_nothing():
    opt = _optimizer([_call("KeyRock", 100.0, 10)])
    assert _rehedge(opt) == []


# ── Collateral netting ────────────────────────────────────────────────────

def _book(positions, by_venue):
    return {"positions": positions, "perp": {"by_venue": by_venue}}


def test_otc_perp_nets_against_its_counterparty_only():
    data = _book(
        [{"counterparty": "KeyRock", "current_mtm": -1_000.0},
         {"counterparty": "KeyRock", "current_mtm": 300.0},     # gross: never offsets
         {"counterparty": "Flowdesk", "current_mtm": -500.0}],
        [{"venue": "Keyrock", "is_exchange": False, "net_qty": -10.0,
          "avg_entry": 2_000.0, "unrealized_pnl_usd": 400.0},
         {"venue": "Binance Futures", "is_exchange": True, "net_qty": -5.0,
          "avg_entry": 2_000.0, "unrealized_pnl_usd": 900.0}])
    liab, display, counts = collateral._compute_liabilities(data)
    assert liab["keyrock"] == pytest.approx(600.0)    # 1,000 gross − 400 perp gain
    assert liab["flowdesk"] == pytest.approx(500.0)   # exchange perp nets nothing
    assert "binance futures" not in liab
    assert counts["keyrock"] == 3


def test_perp_netting_floors_at_zero_and_losing_perp_adds():
    gain = _book([{"counterparty": "KeyRock", "current_mtm": -100.0}],
                 [{"venue": "KeyRock", "is_exchange": False, "net_qty": 1.0,
                   "avg_entry": 1.0, "unrealized_pnl_usd": 900.0}])
    loss = _book([{"counterparty": "KeyRock", "current_mtm": -100.0}],
                 [{"venue": "KeyRock", "is_exchange": False, "net_qty": 1.0,
                   "avg_entry": 1.0, "unrealized_pnl_usd": -250.0}])
    assert collateral._compute_liabilities(gain)[0]["keyrock"] == 0.0
    assert collateral._compute_liabilities(loss)[0]["keyrock"] == pytest.approx(350.0)


# ── Per-venue perp book ───────────────────────────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    async def make():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(PERP_TRADES_SCHEMA)
        await db.execute(PERP_FUNDING_SCHEMA)
        return db

    db = asyncio.run(make())

    async def get_db():
        return db

    async def market(_asset):
        return {"mark": None, "mark_venue": "stub", "mark_price": 2_100.0,
                "funding_apr_pct": None, "interval_hours": None, "market_error": None}

    monkeypatch.setattr(perps, "get_db", get_db)
    monkeypatch.setattr(perps, "_market", market)
    yield db
    asyncio.run(db.close())


def test_summary_sums_venues_and_keeps_each_book(ledger):
    async def run():
        await repo.add_trade(ledger, "ETH", "Sell", 100, 2_000.0, "2026-09-01")
        await repo.add_trade(ledger, "ETH", "Sell", 50, 2_200.0, "2026-09-02", venue="KeyRock")
        await repo.add_trade(ledger, "ETH", "Buy", 10, 2_000.0, "2026-09-03", venue="keyrock")
        assert await repo.list_venues(ledger, "ETH") == ["Binance Futures", "KeyRock"]
        return await perps.build_summary("ETH"), await perps.build_summary("ETH", "KeyRock")

    total, keyrock = asyncio.run(run())
    assert keyrock["net_qty"] == -40 and keyrock["avg_entry"] == pytest.approx(2_200.0)
    assert not keyrock["is_exchange"]
    assert total["net_qty"] == -140
    assert [v["venue"] for v in total["by_venue"]] == ["Binance Futures", "KeyRock"]
    assert total["unrealized_pnl_usd"] == pytest.approx(
        sum(v["unrealized_pnl_usd"] for v in total["by_venue"]))
    # Avg entry of the net reproduces the summed unrealised P&L at the mark.
    assert (2_100.0 - total["avg_entry"]) * total["net_qty"] == pytest.approx(
        total["unrealized_pnl_usd"], abs=0.05)
