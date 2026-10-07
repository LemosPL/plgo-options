"""The 5 Oct 2026 decision sheet: the new gate rules and the one-time policy patch."""

from __future__ import annotations

from datetime import date

import pytest

from plgo_options.agents import decisions
from plgo_options.agents import gate as g
from plgo_options.agents.policy import AssetPolicy, OptimizerPreset, default_policy


def agreed(asset="ETH"):
    return decisions.apply_sheet(default_policy(asset), decisions.SHEET_2026_10_05[asset])


def ctx(pol, **kw):
    return g.GateContext(**{"policy": pol, "spot": 2700.0, **kw})


OLD = {"kind": "option", "side": "Sell", "opt": "C", "strike": 3200, "expiry": "2026-12-25",
       "qty": 100, "counterparty": "Flowdesk", "is_unwind": True}
BASE = {"asset": "ETH", "purpose": "shape", "notional_usd": 200_000, "net_cost_usd": -1_000}


def test_sheet_writes_the_agreed_numbers_and_keeps_the_rest():
    stored = default_policy("ETH")
    stored.optimizer.target_down_ratio = 0.9          # tuned on prod, not on the sheet
    pol = decisions.apply_sheet(stored, decisions.SHEET_2026_10_05["ETH"])
    assert (pol.reference_price, pol.row_steps_pct, pol.stop_price) == (2700, [5, 10, 20], 2300)
    assert (pol.max_single_trade_usd, pol.max_cost_per_trade_usd, pol.book_notional_usd) == \
        (1_000_000, 250_000, 25_000_000)
    assert pol.optimizer.target_trough_payoff == -20_000_000
    assert pol.optimizer.counterparties == []
    assert pol.optimizer.target_down_ratio == 0.9
    assert not pol.is_example and not pol.rolls_need_chris
    fil = agreed("FIL")
    assert (fil.reference_price, fil.stop_price, fil.max_single_trade_usd) == (1.00, 0.89, 2_500_000)


def test_roll_out_goes_to_lucas_when_rolls_are_his():
    out = {**OLD, "side": "Buy", "expiry": "2027-03-26", "is_unwind": False}
    down = {**out, "strike": 2800}
    for new in (out, down):
        r = g.evaluate({**BASE, "legs": [OLD, new]}, ctx(agreed()))
        assert r.route == g.LUCAS, r.reasons
    # Old rule still holds when the switch is on.
    assert g.evaluate({**BASE, "legs": [OLD, out]}, ctx(default_policy("ETH"))).route == g.CHRIS


def test_roll_plus_a_new_line_is_still_a_shape_change():
    out = {**OLD, "side": "Buy", "expiry": "2027-03-26", "is_unwind": False}
    extra = {**out, "opt": "P", "strike": 2000}
    assert g.evaluate({**BASE, "legs": [OLD, out, extra]}, ctx(agreed())).route == g.CHRIS


def test_shortening_or_same_expiry_strike_move_stays_rejected():
    same = {**OLD, "side": "Buy", "strike": 2700, "is_unwind": False}
    shorter = {**OLD, "side": "Buy", "expiry": "2026-11-27", "is_unwind": False}
    for new in (same, shorter):
        assert g.evaluate({**BASE, "legs": [OLD, new]}, ctx(agreed())).route == g.REJECTED


def test_perp_cap_grows_to_full_delta():
    pol = agreed()
    perp = {"kind": "perp", "side": "Sell", "qty": 2000, "counterparty": "Flowdesk", "opt": "F"}
    p = {"asset": "ETH", "purpose": "direction", "legs": [perp], "notional_usd": 5_400_000,
         "net_cost_usd": 1_000}
    assert g.evaluate(p, ctx(pol, book_delta_usd=8_000_000)).route == g.LUCAS
    r = g.evaluate(p, ctx(pol))                          # delta unknown -> fixed caps
    assert r.route == g.CHRIS


def test_no_funding_budget_means_no_funding_flag():
    pol = agreed()
    perp = {"kind": "perp", "side": "Sell", "qty": 100, "counterparty": "Flowdesk", "opt": "F"}
    p = {"asset": "ETH", "purpose": "direction", "legs": [perp], "notional_usd": 270_000,
         "net_cost_usd": 100}
    assert g.evaluate(p, ctx(pol, perp_funding_month_usd=90_000)).route == g.LUCAS


def test_maturity_switches_a_month_out():
    o = OptimizerPreset(target_expiry="25DEC26", next_expiry="26MAR27", switch_days_before=30)
    assert o.effective_expiry(date(2026, 10, 5)) == "25DEC26"
    assert o.effective_expiry(date(2026, 11, 25)) == "26MAR27"
    assert OptimizerPreset().effective_expiry(date(2026, 12, 20)) == "25DEC26"


def test_old_stored_policy_without_new_fields_loads():
    d = default_policy("ETH").to_dict()
    for k in ("perp_cap_full_delta", "perp_venue", "rolls_need_chris"):
        d.pop(k)
    pol = AssetPolicy.from_dict(d)
    assert pol.rolls_need_chris and not pol.perp_cap_full_delta


@pytest.mark.asyncio
async def test_apply_pending_runs_once(monkeypatch):
    flags, saved = {}, []

    async def get_flag(k, default=""):
        return flags.get(k, default)

    async def set_flag(k, v, by=""):
        flags[k] = v

    async def get_policy(a):
        return default_policy(a)

    async def save_policy(p, by=""):
        saved.append((p.asset, by))
        return p

    for name, fn in (("get_flag", get_flag), ("set_flag", set_flag),
                     ("get_policy", get_policy), ("save_policy", save_policy)):
        monkeypatch.setattr(decisions.store, name, fn)
    assert await decisions.apply_pending() == ["2026-10-05", "2026-10-06-sweep", "2026-10-06-giveback",
                                              "2026-10-06-size", "2026-10-07-shape"]
    assert saved[:2] == [("ETH", "decision-sheet 2026-10-05"), ("FIL", "decision-sheet 2026-10-05")]
    assert await decisions.apply_pending() == []


@pytest.mark.asyncio
async def test_get_book_reads_spot_from_eth_spot(monkeypatch):
    """portfolio_pnl returns the spot as eth_spot (FIL too); a 0 spot blanked the Monday pack."""
    from plgo_options.agents import desk
    from plgo_options.web.routes import portfolio

    async def fake_pnl(asset, include_expired=False):
        return {"eth_spot": 1.02, "totals": {"current_total_mtm": -1.0}, "positions": []}

    monkeypatch.setattr(portfolio, "portfolio_pnl", fake_pnl)
    assert (await desk.get_book("FIL"))["spot"] == 1.02
