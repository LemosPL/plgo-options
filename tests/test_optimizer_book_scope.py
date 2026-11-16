"""Optimizer v4 'Book' switch: selected counterparties' book only, with the
parametric target's max loss scaled to that book."""

import pytest

from plgo_options.web.routes.optimization import OptimizationParams, _scope_counterparties, _scope_target


def _p(**kw):
    base = dict(asset="FIL", counterparties=["G20"], book_scope="counterparties", parametric_trough_payoff=-15_750_000.0)
    base.update(kw)
    return OptimizationParams(**base)


def test_scope_needs_both_the_switch_and_a_selection():
    assert _scope_counterparties(_p()) == {"g20"}
    assert _scope_counterparties(_p(book_scope="portfolio")) is None
    assert _scope_counterparties(_p(counterparties=None)) is None
    assert _scope_counterparties(_p(counterparties=["ALL"])) is None


def test_target_max_loss_is_the_scoped_books_value_at_spot():
    params = _p()
    info = _scope_target(params, {"positions": [{"opt": "P", "net_qty": 1e6, "current_mtm": -900_000.0},
                                                {"opt": "F", "net_qty": -5e5, "current_mtm": -100_000.0}]})
    assert params.parametric_trough_payoff == pytest.approx(-1_000_000.0)
    assert info["target"] == "scaled" and info["basis"] == "book value at spot"


def test_a_book_worth_more_than_zero_scales_by_its_share_of_gross_quantity():
    params = _p()
    scoped = [{"opt": "C", "net_qty": -1e6, "current_mtm": 50_000.0}]
    everything = scoped + [{"opt": "P", "net_qty": 3e6, "current_mtm": -2e7}]
    info = _scope_target(params, {"positions": scoped, "_all_option_positions": everything})
    assert params.parametric_trough_payoff == pytest.approx(-15_750_000.0 * 0.25)
    assert "25.0%" in info["basis"]


def test_a_manual_target_is_left_alone():
    params = _p(manual_target=[{"x": 0.5, "y": -1}, {"x": 1.0, "y": 0}])
    info = _scope_target(params, {"positions": [{"opt": "P", "net_qty": 1, "current_mtm": -5.0}]})
    assert params.parametric_trough_payoff == -15_750_000.0 and info["target"].startswith("manual")
