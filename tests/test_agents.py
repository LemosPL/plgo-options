"""Unit tests for the desk agents' rules: rows, the mandate gate, the sweep ranking."""

from plgo_options.agents import gate as g
from plgo_options.agents import optimizer as opt
from plgo_options.agents.policy import AssetPolicy, OptimizerPreset, default_policy
from plgo_options.agents.rows import build_rows, crossed_rows, next_rows


def ctx(**kw):
    base = dict(policy=default_policy("ETH"), spot=3000.0)
    base.update(kw)
    return g.GateContext(**base)


# ── rows (B1) ─────────────────────────────────────────────────────────────

def test_rows_eth_example():
    rows = build_rows("ETH", 3000, [10, 20, 30])
    assert [round(r.price) for r in rows] == [2100, 2400, 2700, 3300, 3600, 3900]
    assert {r.key for r in crossed_rows(rows, 3000, 3310)} == {"+10"}
    assert crossed_rows(rows, 3000, 3620)[0].key == "+20"          # outer row fires first
    assert crossed_rows(rows, 3000, 3050) == []
    below, above = next_rows(rows, 3050)
    assert below.key == "-10" and above.key == "+10"


def test_rows_fil_wider_steps():
    rows = build_rows("FIL", 2.0, [15, 30, 45])
    assert [round(r.price, 2) for r in rows] == [1.1, 1.4, 1.7, 2.3, 2.6, 2.9]


# ── gate (A2-A5, B1-B3) ───────────────────────────────────────────────────

def perp(side="Sell", qty=800):
    return {"kind": "perp", "side": side, "qty": qty, "counterparty": "Binance Futures", "opt": "F"}


def test_perp_row_inside_mandate_goes_to_lucas():
    p = {"asset": "ETH", "purpose": "direction", "row_key": "+10", "legs": [perp()],
         "notional_usd": 2_500_000, "net_cost_usd": 2_000}
    assert g.evaluate(p, ctx()).route == g.LUCAS


def test_perp_over_limit_is_split():
    p = {"asset": "ETH", "purpose": "direction", "legs": [perp(qty=1700)],
         "notional_usd": 5_000_000, "net_cost_usd": 2_000}
    r = g.evaluate(p, ctx())
    assert r.route == g.CHRIS
    assert r.lucas_notional_usd == 3_000_000 and r.chris_notional_usd == 2_000_000


def test_option_to_change_direction_is_rejected():
    leg = {"kind": "option", "side": "Buy", "opt": "P", "strike": 2700, "expiry": "2026-12-25",
           "qty": 100, "counterparty": "Flowdesk"}
    p = {"asset": "ETH", "purpose": "direction", "legs": [leg], "notional_usd": 300_000}
    assert g.evaluate(p, ctx()).route == g.REJECTED


def test_row_fires_once_per_week():
    p = {"asset": "ETH", "purpose": "direction", "row_key": "+10", "legs": [perp()],
         "notional_usd": 2_500_000}
    assert g.evaluate(p, ctx(fired_this_week={"+10"})).route == g.REJECTED


def test_roll_down_same_expiry_rejected_roll_out_to_chris():
    old = {"kind": "option", "side": "Sell", "opt": "C", "strike": 3200, "expiry": "2026-12-25",
           "qty": 100, "counterparty": "Flowdesk", "is_unwind": True}
    down = {**old, "side": "Buy", "strike": 2700, "is_unwind": False}
    out = {**old, "side": "Buy", "expiry": "2027-03-26", "is_unwind": False}
    base = {"asset": "ETH", "purpose": "shape", "notional_usd": 200_000, "net_cost_usd": -1_000}
    assert g.evaluate({**base, "legs": [old, down]}, ctx()).route == g.REJECTED
    r = g.evaluate({**base, "legs": [old, out]}, ctx())
    assert r.route == g.CHRIS and any("out same strike" in x for x in r.reasons)


def test_floor_lowered_is_rejected():
    ladder = [1800, 2100, 2400, 2700, 3000, 3300]
    before = [-10e6, -9e6, -8e6, -7e6, -6e6, -5e6]
    after = [-10.5e6, -9.4e6, -8e6, -7e6, -6e6, -5e6]      # worse below the floor
    p = {"asset": "ETH", "purpose": "shape", "legs": [], "notional_usd": 0,
         "spot_ladder": ladder, "before_payoff": before, "after_payoff": after, "spot": 3000}
    pol = default_policy("ETH")
    pol.floor_price = 2400
    assert g.evaluate(p, ctx(policy=pol)).route == g.REJECTED


def test_debit_needs_reason_and_budget():
    leg = {"kind": "option", "side": "Buy", "opt": "C", "strike": 3300, "expiry": "2027-03-26",
           "qty": 10, "counterparty": "Flowdesk"}
    p = {"asset": "ETH", "purpose": "shape", "legs": [leg], "notional_usd": 30_000,
         "net_cost_usd": 90_000}
    r = g.evaluate(p, ctx(month_cost_usd=380_000))
    assert r.route == g.CHRIS
    assert any("written reason" in x for x in r.reasons)
    assert any("monthly budget" in x for x in r.reasons)


def test_kill_switch_and_frozen_curves_block_everything():
    p = {"asset": "ETH", "purpose": "direction", "legs": [perp()], "notional_usd": 1_000_000}
    assert g.evaluate(p, ctx(kill_switch=True)).route == g.REJECTED
    assert g.evaluate(p, ctx(curves_frozen=True)).route == g.REJECTED


def test_at_the_stop_no_new_options():
    leg = {"kind": "option", "side": "Buy", "opt": "C", "strike": 2300, "expiry": "2027-03-26",
           "qty": 10, "counterparty": "Flowdesk"}
    p = {"asset": "ETH", "purpose": "shape", "legs": [leg], "notional_usd": 20_000}
    assert g.evaluate(p, ctx(spot=2050)).route == g.REJECTED


def test_unknown_counterparty_rejected():
    leg = {"kind": "option", "side": "Buy", "opt": "C", "strike": 3300, "expiry": "2027-03-26",
           "qty": 10, "counterparty": "NewDealer"}
    p = {"asset": "ETH", "purpose": "shape", "legs": [leg], "notional_usd": 30_000}
    assert g.evaluate(p, ctx()).route == g.REJECTED


def test_box_legs_are_not_option_trades():
    box = {"kind": "option", "side": "Buy", "opt": "C", "strike": 2000, "expiry": "2026-12-25",
           "qty": 40, "counterparty": "Flowdesk", "strategy": "BOX_NEUTRALIZER"}
    p = {"asset": "ETH", "purpose": "shape", "legs": [box], "notional_usd": 0}
    assert g.evaluate(p, ctx()).route == g.LUCAS


# ── optimizer sweep ranking ───────────────────────────────────────────────

def fake_result(fb, fa, cost, n_opt, deltas_m):
    spot = 2684.0
    ladder = [spot * (1 + m) for m in opt.KEY_MONEYNESS]
    before = [0.0] * len(ladder)
    after = [d * 1e6 for d in deltas_m]
    trades = [{"strategy": "PUT_SPREAD", "qty": 1000, "side": "Buy", "opt": "P", "strike": 2500,
               "expiry": "2026-12-25", "counterparty": "Flowdesk"}] * n_opt
    trades += [{"strategy": "BOX_NEUTRALIZER", "qty": 40, "counterparty": "Flowdesk"}] * 4
    return {"status": "ok", "optimizer_converged": True, "spot": spot, "spot_ladder": ladder,
            "before_payoff": before, "after_payoff": after, "fit_error_before": fb,
            "fit_error_after": fa, "total_cost_usd": cost, "net_premium_generated": 0, "trades": trades}


def test_ranking_prefers_fewer_cheaper_lines_like_the_28_sep_sweep():
    preset = OptimizerPreset()
    five = opt.summarize(fake_result(100, 41.0, 107_697, 5, [2.7, 1.1, 0, 0.8, 5.5]),
                         {"lam_factor": .5, "downside_factor": 1.1, "t90_weight": .5, "max_trades": 5,
                          "max_qty": 5000}, preset)
    seven = opt.summarize(fake_result(100, 36.5, 153_486, 7, [3.9, 1.4, 0, 0.5, 5.0]),
                          {"lam_factor": .5, "downside_factor": 1.1, "t90_weight": .5, "max_trades": 7,
                           "max_qty": 5000}, preset)
    lam03 = opt.summarize(fake_result(100, 96.2, 20_815, 3, [1.2, 0.6, 0, -0.7, -1.0]),
                          {"lam_factor": .3, "downside_factor": 1, "t90_weight": .2, "max_trades": 7,
                           "max_qty": 5000}, preset)
    ranked = opt.rank([seven, lam03, five])
    assert ranked[0] is five
    assert lam03["disqualified"]                    # gives back $1M at +85%
    assert five in opt.pareto([five, seven, lam03])


def test_proposal_from_sweep_passes_gate_to_chris():
    preset = OptimizerPreset()
    s = opt.summarize(fake_result(100, 41.0, 107_697, 5, [2.7, 1.1, 0, 0.8, 5.5]),
                      {"lam_factor": .5, "downside_factor": 1.1, "t90_weight": .5, "max_trades": 5,
                       "max_qty": 5000}, preset)
    prop = opt.to_proposal("ETH", s)
    r = g.evaluate(prop, ctx(spot=2684.0))
    assert r.route == g.CHRIS                       # options change shape: Chris decides


def test_two_curve_check():
    a = {"trades": [{"counterparty": "Flowdesk", "opt": "P", "strike": 2500, "expiry": "x", "side": "Buy"}]}
    b = {"trades": [{"counterparty": "Flowdesk", "opt": "P", "strike": 2500, "expiry": "x", "side": "Sell"}]}
    assert opt.compare_curves(a, a, 0.5)["verdict"] == "agree"
    assert opt.compare_curves(a, b, 0.5)["verdict"] == "disagree"


def test_policy_roundtrip():
    p = default_policy("FIL")
    q = AssetPolicy.from_dict(p.to_dict())
    assert q == p


def test_gate_keeps_rejections_apart_from_chris_items():
    leg = {"kind": "option", "side": "Buy", "opt": "P", "strike": 2700, "expiry": "2026-12-25",
           "qty": 100, "counterparty": "Nobody Capital"}
    r = g.evaluate({"asset": "ETH", "purpose": "shape", "legs": [leg], "notional_usd": 300_000}, ctx())
    d = r.to_dict()
    assert r.route == g.REJECTED
    assert any("Unknown counterparty" in x for x in d["rejected_by"])
    assert not any("Unknown counterparty" in x for x in d["needs_chris"])
    assert any("shape" in x for x in d["needs_chris"])
