"""Collar-loan counterparties are out of the managed book by default.

FalconX and Galaxy are borrowing structures, not risk the desk steers. The
important properties are that the exclusion is ON unless a caller asks
otherwise, that it matches names the way they actually arrive (spacing, case),
and that it reaches every shape of row the app passes around.
"""

from __future__ import annotations

import importlib

import pytest

from plgo_options.data import collar_loans as cl


# ── matching ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "FalconX", "falconx", "FALCONX", " FalconX ", "Falcon X", "falcon x",
    "Galaxy", "galaxy", " GALAXY ",
])
def test_matches_the_loan_counterparties_however_they_are_written(name):
    """These names arrive from a spreadsheet, so spacing and case vary."""
    assert cl.is_collar_loan(name)


@pytest.mark.parametrize("name", [
    "Flowdesk", "KeyRock", "G20", "Binance Futures", "Deribit",
    "", None, "Falcon", "FalconX Capital Partners", "Galaxy Digital Ltd",
])
def test_does_not_match_anyone_else(name):
    """Substring matching would be dangerous: a real counterparty whose name
    merely contains "Galaxy" must not silently leave the book."""
    assert not cl.is_collar_loan(name)


# ── filtering across row shapes ──────────────────────────────────────────────

class _Obj:
    def __init__(self, counterparty):
        self.counterparty = counterparty


def test_filters_dict_rows():
    rows = [{"counterparty": "Flowdesk"}, {"counterparty": "FalconX"},
            {"counterparty": "KeyRock"}, {"counterparty": "Galaxy"}]
    kept = cl.drop_collar_loans(rows)
    assert [r["counterparty"] for r in kept] == ["Flowdesk", "KeyRock"]


def test_filters_objects_too():
    """Optimizer Positions are objects, not dicts."""
    rows = [_Obj("Flowdesk"), _Obj("Galaxy"), _Obj("G20")]
    assert [r.counterparty for r in cl.drop_collar_loans(rows)] == ["Flowdesk", "G20"]


def test_split_reports_what_it_removed():
    rows = [{"counterparty": "Flowdesk"}, {"counterparty": "FalconX"},
            {"counterparty": "Galaxy"}]
    kept, dropped = cl.split_collar_loans(rows)
    assert len(kept) == 1 and len(dropped) == 2
    assert {r["counterparty"] for r in dropped} == {"FalconX", "Galaxy"}


def test_split_is_one_pass_and_order_preserving():
    rows = [{"counterparty": c} for c in
            ["A", "FalconX", "B", "Galaxy", "C"]]
    kept, dropped = cl.split_collar_loans(rows)
    assert [r["counterparty"] for r in kept] == ["A", "B", "C"]
    assert [r["counterparty"] for r in dropped] == ["FalconX", "Galaxy"]


def test_a_missing_counterparty_field_is_kept():
    """Dropping rows we cannot classify would hide real positions."""
    assert len(cl.drop_collar_loans([{"id": 1}, _Obj(None)])) == 2


# ── configuration ────────────────────────────────────────────────────────────

def test_the_list_is_env_overridable(monkeypatch):
    monkeypatch.setenv("COLLAR_LOAN_COUNTERPARTIES", "someoneelse")
    importlib.reload(cl)
    try:
        assert cl.is_collar_loan("SomeoneElse")
        assert not cl.is_collar_loan("FalconX")      # no longer excluded
    finally:
        monkeypatch.delenv("COLLAR_LOAN_COUNTERPARTIES")
        importlib.reload(cl)


def test_empty_env_turns_the_feature_off(monkeypatch):
    """An escape hatch that needs no deploy: clear the list and nothing is
    excluded anywhere."""
    monkeypatch.setenv("COLLAR_LOAN_COUNTERPARTIES", "")
    importlib.reload(cl)
    try:
        assert cl.COLLAR_LOAN_COUNTERPARTIES == frozenset()
        assert not cl.is_collar_loan("FalconX")
        rows = [{"counterparty": "FalconX"}, {"counterparty": "Galaxy"}]
        assert cl.drop_collar_loans(rows) == rows
        assert cl.split_collar_loans(rows) == (rows, [])
    finally:
        monkeypatch.delenv("COLLAR_LOAN_COUNTERPARTIES")
        importlib.reload(cl)


def test_defaults_to_falconx_and_galaxy():
    importlib.reload(cl)
    assert cl.COLLAR_LOAN_COUNTERPARTIES == frozenset({"falconx", "galaxy"})


# ── the default direction ────────────────────────────────────────────────────

def test_portfolio_pnl_excludes_by_default():
    """The signature is the contract: anything that forgets to pass the flag
    gets the managed book, not the whole book. That is the safe direction —
    a new collar loan is caught without anyone noticing it arrived."""
    import inspect

    from plgo_options.web.routes.portfolio import portfolio_pnl
    sig = inspect.signature(portfolio_pnl)
    assert sig.parameters["include_collar_loans"].default is False


def test_optimizer_params_default_to_excluding():
    from plgo_options.web.routes.optimization import OptimizationParams
    assert OptimizationParams().include_collar_loans is False
