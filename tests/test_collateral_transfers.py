"""Collateral transfer model (CollateralOptimization._add_collateral_transfers).

Collateral away from the hub costs carry; each counterparty with exposure must
keep what we would owe it at today's spot plus a buffer; the rest moves
directly between counterparties at a fixed fee per transfer, and a
counterparty left short sends nothing. These run the model on a fixed book
(plan_transfers), where the answer can be worked out by hand.
"""

from __future__ import annotations

import numpy as np
import pytest

from plgo_options.optimization.collateral_optimization import CollateralOptimization

LADDER = np.array([1.0, 2.0, 3.0])          # the book's own asset, $ per token
CARRY = 0.001 * 90 / 365                     # 0.1 %/yr over 90 days


def _cfg(posted, buffer=500_000.0, fee=10.0, hub="Flowdesk"):
    assets = sorted({a for by in posted.values() for a in by} | {"USDC"})
    curves = {a: (LADDER if a == "FIL" else np.full(3, 1.0)) for a in assets}
    return {
        "hub": hub,
        "posted": {cp: dict(by) for cp, by in posted.items()},
        "price_today": {a: float(curves[a][1]) for a in assets},   # today = middle of the ladder
        "price_curves": curves,
        "haircuts": {},
        "carry_rate": CARRY,
        "horizon_days": 90,
        "buffer_usd": buffer,
        "fee_usd": fee,
        "shortfall_cost": 0.01,
        "n_spots": 3,
        "spot_index": 1,
    }


def _moves(report):
    return {(t["from"], t["to"], t["asset"]): t["qty"] for t in report["transfers"]}


def test_excess_moves_to_the_hub_and_requirement_plus_buffer_stays():
    cfg = _cfg({"Flowdesk": {}, "KeyRock": {"USDC": 10_000_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": np.full(3, -3_000_000.0)})
    assert _moves(report) == {("KeyRock", "Flowdesk", "USDC"): pytest.approx(6_500_000.0)}
    kr = report["by_counterparty"]["KeyRock"]
    assert kr["posted_after_usd"] == pytest.approx(3_500_000.0)
    assert kr["requirement_today_usd"] == pytest.approx(3_000_000.0)
    assert kr["headroom_usd"] == pytest.approx(0.0, abs=1.0)
    assert report["carry_after_usd"] == pytest.approx(3_500_000.0 * CARRY, abs=0.01)


def test_counterparty_we_are_owed_by_still_keeps_the_buffer():
    cfg = _cfg({"Flowdesk": {}, "KeyRock": {"USDC": 2_000_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": np.full(3, 5_000_000.0)})
    assert _moves(report) == {("KeyRock", "Flowdesk", "USDC"): pytest.approx(1_500_000.0)}


def test_hub_tops_up_a_short_counterparty():
    cfg = _cfg({"Flowdesk": {"USDC": 5_000_000.0}, "Wave": {"USDC": 1_000_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"Wave": np.full(3, -2_000_000.0)})
    assert _moves(report) == {("Flowdesk", "Wave", "USDC"): pytest.approx(1_500_000.0)}
    assert report["by_counterparty"]["Wave"]["shortfall_usd"] == 0.0


def test_fee_leaves_small_excess_where_it_is():
    # 20k above requirement + buffer saves 20k x 0.025% ≈ $5 of carry < $10 fee.
    cfg = _cfg({"Flowdesk": {}, "KeyRock": {"USDC": 3_520_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": np.full(3, -3_000_000.0)})
    assert report["transfers"] == []
    no_fee = CollateralOptimization.plan_transfers(
        _cfg({"Flowdesk": {}, "KeyRock": {"USDC": 3_520_000.0}}, fee=0.0),
        {"KeyRock": np.full(3, -3_000_000.0)})
    assert _moves(no_fee) == {("KeyRock", "Flowdesk", "USDC"): pytest.approx(20_000.0)}


def test_requirement_is_what_is_owed_at_todays_spot_only():
    # Owed 3M only at the LOW spot, nothing today: just the buffer stays,
    # FIL valued at today's $2.
    cfg = _cfg({"Flowdesk": {}, "KeyRock": {"FIL": 5_000_000.0}})
    owed_low = np.array([-3_000_000.0, 0.0, 0.0])
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": owed_low})
    assert _moves(report) == {("KeyRock", "Flowdesk", "FIL"): pytest.approx(4_750_000.0)}
    assert report["by_counterparty"]["KeyRock"]["requirement_today_usd"] == 0.0


def test_a_short_counterparty_never_sends():
    # Both short. KeyRock's FIL counts at 50% there and 100% at Flowdesk, so
    # moving it would cut the TOTAL shortfall — but KeyRock is short, so it
    # keeps what it has: a shortfall is new collateral to post, not to shuffle.
    cfg = _cfg({"Flowdesk": {"USDC": 1_000_000.0}, "KeyRock": {"FIL": 2_000_000.0}})
    cfg["haircuts"] = {"KeyRock": {"FIL": 0.5}}
    report = CollateralOptimization.plan_transfers(
        cfg, {"KeyRock": np.full(3, -5_000_000.0), "Flowdesk": np.full(3, -3_000_000.0)})
    assert report["transfers"] == []
    assert report["by_counterparty"]["KeyRock"]["shortfall_usd"] == pytest.approx(3_500_000.0)
    assert report["by_counterparty"]["Flowdesk"]["shortfall_usd"] == pytest.approx(2_500_000.0)


def test_counterparty_without_exposure_sends_everything():
    cfg = _cfg({"Flowdesk": {}, "G20": {"USDC": 2_000_000.0}, "KeyRock": {"USDC": 3_500_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": np.full(3, -3_000_000.0)})
    assert _moves(report) == {("G20", "Flowdesk", "USDC"): pytest.approx(2_000_000.0)}


def test_spare_collateral_goes_straight_to_the_short_counterparty():
    # G20 has no exposure and spare USDC; KeyRock is short. One direct move,
    # not G20 -> hub -> KeyRock (two fees, two settlements).
    cfg = _cfg({"Flowdesk": {}, "G20": {"USDC": 2_000_000.0}, "KeyRock": {"USDC": 1_000_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"KeyRock": np.full(3, -1_800_000.0)})
    assert _moves(report) == {("G20", "KeyRock", "USDC"): pytest.approx(1_300_000.0),
                              ("G20", "Flowdesk", "USDC"): pytest.approx(700_000.0)}
    assert report["fees_usd"] == 20.0


def test_uncoverable_counterparty_reports_a_shortfall_instead_of_failing():
    cfg = _cfg({"Flowdesk": {"USDC": 1_000_000.0}, "Wave": {"USDC": 1_000_000.0}})
    report = CollateralOptimization.plan_transfers(cfg, {"Wave": np.full(3, -4_000_000.0)})
    assert report is not None
    # The hub has no exposure here, so it sends everything it holds.
    assert _moves(report) == {("Flowdesk", "Wave", "USDC"): pytest.approx(1_000_000.0)}
    # Owed 4M + 500k buffer, holds 2M after the top-up.
    assert report["by_counterparty"]["Wave"]["shortfall_usd"] == pytest.approx(2_500_000.0)
