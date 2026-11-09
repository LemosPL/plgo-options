"""Deribit's measured costs fill in for ETH wherever a caller leaves Deribit out."""

from __future__ import annotations

from plgo_options.optimization.collateral_optimization import CollateralOptimization
from plgo_options.optimization.optimizer_v3 import (DEFAULT_BID_ASK_VOL_PTS_BY_ASSET, _structure_leg_costs_usd,
                                                    _with_exchange_costs)

R = CollateralOptimization._resolve


def test_defaults_give_deribit_its_cost_and_everyone_else_theirs():
    vp = _with_exchange_costs(None, DEFAULT_BID_ASK_VOL_PTS_BY_ASSET["ETH"], "bid_ask_vol_pts", "ETH")
    assert R(vp, "Deribit", 0.75) == 0.6 and R(vp, "Flowdesk", 0.75) == 5.0
    assert R(_with_exchange_costs(None, {"KeyRock": 1.0}, "netting_pct", "ETH"), "Deribit", 1.0) == 0.0
    assert R(_with_exchange_costs(None, {}, "box_fee_bps", "ETH"), "Deribit", 0.0) == 160.0


def test_the_page_values_win_and_fil_has_no_deribit():
    ui = {"Flowdesk": 3.0}                                                # Deribit not on the page
    assert _with_exchange_costs(ui, 5.0, "bid_ask_vol_pts", "ETH") == {"Deribit": 0.6, "Flowdesk": 3.0}
    assert _with_exchange_costs({"Deribit": 1.0}, 5.0, "bid_ask_vol_pts", "ETH") == {"Deribit": 1.0}
    assert _with_exchange_costs(2.0, 5.0, "bid_ask_vol_pts", "ETH") == 2.0     # a scalar: everyone
    assert _with_exchange_costs(None, 40.0, "bid_ask_vol_pts", "FIL") == 40.0


def test_a_deribit_spread_pays_each_leg_in_full():
    class Leg:
        def __init__(self, vega):
            self.vega, self.counterparty, self.opt = vega, "Deribit", "C"
    legs = [(Leg(3.0), 100), (Leg(2.5), -100)]                           # a call spread: net vega 50
    netting = _with_exchange_costs(None, {"KeyRock": 1.0}, "netting_pct", "ETH")
    vp = _with_exchange_costs(None, 5.0, "bid_ask_vol_pts", "ETH")
    assert sum(_structure_leg_costs_usd(legs, vp, netting_pct=netting)) == 0.6 * (300 + 250)
