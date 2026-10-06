"""The agent prices a trade and tests it against the policy before anyone sees it."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from plgo_options.agents import decisions, gate as g, validate as v
from plgo_options.agents.policy import default_policy
from plgo_options.optimization.math_utils import bs_vec_bridge

EXP = date.today() + timedelta(days=80)


class FakeEngine:
    """Enough of OptimizerV3 for validate(): spot, surface, ladder, build_payoffs."""
    def __init__(self, spot=3000.0):
        self.spot = spot
        self.vol_surface = [{"expiry_code": EXP.strftime("%d%b%y").upper(), "expiry_date": EXP.isoformat(),
                             "dte": 80, "strikes": [1000, 2000, 3000, 4000, 6000], "ivs": [80, 75, 70, 72, 78]}]
        self.spot_ladder = list(np.arange(1000.0, 6001.0, 50.0))

    def build_payoffs(self, horizons, ladder, trades):
        before = {str(h): np.zeros(len(ladder)) for h in set(horizons) | {0}}
        after = {}
        for h in before:
            tot = np.zeros(len(ladder))
            for t in trades:
                vals = (ladder - t["strike"]) if t["opt"] == "F" else bs_vec_bridge(
                    self.spot, ladder, t["strike"], t["dte"], int(h), t["iv_pct"] / 100, t["opt"])
                tot += t["qty"] * vals
            today = sum(t["qty"] * (self.spot - t["strike"]) if t["opt"] == "F" else t["qty"] * t["bs_price_usd"]
                        for t in trades)
            after[h] = (tot - today).tolist()
            before[h] = before[h].tolist()
        return before, after, 0.0


def pol():
    return decisions.apply_sheet(default_policy("ETH"), decisions.SHEET_2026_10_05["ETH"])


def leg(side, opt, k, qty, cp="Flowdesk"):
    return {"kind": "option", "side": side, "opt": opt, "strike": k, "expiry": EXP.isoformat(), "qty": qty,
            "counterparty": cp}


def run(monkeypatch, legs):
    monkeypatch.setattr(v, "_engine", lambda pnl, asset: FakeEngine())
    return v.validate({}, pol(), legs)


def test_selling_downside_puts_pushes_the_floor_down_and_is_rejected(monkeypatch):
    r = run(monkeypatch, [leg("Sell", "P", 2400, 2000)])
    kinds = [(k, rule) for k, _, rule in r["findings"]]
    assert ("fail", "A2") in kinds
    assert r["floor_worst_change_usd"] < -50_000 and r["net_premium_usd"] < 0       # we receive premium
    prop = v.apply_to_proposal({"asset": "ETH", "purpose": "shape", "legs": [leg("Sell", "P", 2400, 2000)],
                                "notional_usd": 100_000}, r)
    res = g.evaluate(prop, g.GateContext(policy=pol(), spot=3000.0))
    assert res.route == g.REJECTED and any("floor" in x.lower() for x in res.rejected_by)


def test_buying_protection_holds_the_floor_and_reports_cost_and_pnl_at_spot(monkeypatch):
    r = run(monkeypatch, [leg("Buy", "P", 2700, 100)])
    assert not [f for f in r["findings"] if f[0] == "fail"]
    assert r["net_premium_usd"] > 0 and r["dealing_cost_usd"] > 0
    assert abs(r["pnl_at_spot_now_usd"] + r["dealing_cost_usd"]) < 1      # at mid, we lose the spread at spot
    assert r["horizon_days"] in (79, 80, 81)
    assert any("Costs $" in t for k, t, _ in r["findings"] if k == "info")


def test_unpriceable_leg_is_a_failure(monkeypatch):
    r = run(monkeypatch, [{"kind": "option", "side": "Buy", "opt": "P", "strike": 2700, "expiry": "", "qty": 1}])
    assert any(k == "fail" and rule == "B3" for k, _, rule in r["findings"])
