"""Deribit as a counterparty: the exchange_book pricing model.

No network: book-summary rows are built by hand from known vols, so the
implied vols the parser recovers can be checked exactly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from plgo_options.optimization.math_utils import bs_price
from plgo_options.pricing import cpty_pricing, exchange_quotes

S = 2500.0


def _row(expiry: datetime, strike: float, opt: str, bid_vol: float | None,
         ask_vol: float | None, now: datetime) -> dict:
    T = (expiry - now).total_seconds() / (365.25 * 86400)
    eth = lambda v: None if v is None else bs_price(S, strike, T, 0.0, v, opt) / S  # noqa: E731
    return {"instrument_name": f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(strike)}-{opt}",
            "bid_price": eth(bid_vol), "ask_price": eth(ask_vol), "underlying_price": S}


@pytest.fixture
def loaded(monkeypatch):
    """Quotes for an expiry 30 calendar days out, as if loaded today."""
    now = datetime.now(timezone.utc)
    exp_day = date.today() + timedelta(days=30)
    expiry = datetime(exp_day.year, exp_day.month, exp_day.day, 8, tzinfo=timezone.utc)
    rows = [_row(expiry, 2800, "C", 0.60, 0.64, now),
            _row(expiry, 2200, "P", None, 0.70, now)]  # one-sided: no bid
    key = ("deribit", "ETH")
    monkeypatch.setitem(exchange_quotes._QUOTES, key, exchange_quotes.parse(rows, now))
    monkeypatch.setitem(exchange_quotes._LOADED_ON, key, date.today())
    # The optimizer's dte floors (expiry midnight - datetime.today()): 29 during the day.
    return 29 / 365.25


def test_parse_recovers_bid_and_ask_vols(loaded):
    exp_day = date.today() + timedelta(days=30)
    bid, ask = exchange_quotes._QUOTES[("deribit", "ETH")][(exp_day, 2800.0, "C")]
    assert bid == pytest.approx(60.0, abs=1e-3) and ask == pytest.approx(64.0, abs=1e-3)


def test_one_sided_instruments_are_dropped(loaded):
    exp_day = date.today() + timedelta(days=30)
    assert (exp_day, 2200.0, "P") not in exchange_quotes._QUOTES[("deribit", "ETH")]


def test_buy_at_ask_vol_plus_fee_sell_at_bid_vol_minus_fee(loaded):
    T = loaded
    buy, buy_vol = cpty_pricing.resolve_price("Deribit", "ETH", S, 2800, T, "C", "buy")
    sell, sell_vol = cpty_pricing.resolve_price("Deribit", "ETH", S, 2800, T, "C", "sell")
    assert buy_vol == pytest.approx(64.0, abs=1e-3) and sell_vol == pytest.approx(60.0, abs=1e-3)
    ask_px = bs_price(S, 2800, T, 0.0, buy_vol / 100, "C")
    bid_px = bs_price(S, 2800, T, 0.0, sell_vol / 100, "C")
    fee = min(0.0003 * S, 0.125 * ask_px)
    assert buy == pytest.approx(ask_px + fee) and sell < bid_px < ask_px < buy


def test_reprices_at_the_optimizer_spot(loaded):
    lo, _ = cpty_pricing.resolve_price("Deribit", "ETH", 2400.0, 2800, loaded, "C", "buy")
    hi, _ = cpty_pricing.resolve_price("Deribit", "ETH", 2600.0, 2800, loaded, "C", "buy")
    assert lo < hi  # sticky strike: same vol, spot moves the call


def test_unquoted_or_unloaded_falls_back_to_mid(loaded, monkeypatch):
    assert cpty_pricing.resolve_price("Deribit", "ETH", S, 2200, loaded, "P", "buy") is None
    assert cpty_pricing.resolve_price("Deribit", "ETH", S, 2800, loaded + 7 / 365.25, "C",
                                      "buy") is None
    monkeypatch.setitem(exchange_quotes._LOADED_ON, ("deribit", "ETH"),
                        date.today() - timedelta(days=1))  # yesterday's book
    assert cpty_pricing.resolve_price("Deribit", "ETH", S, 2800, loaded, "C", "buy") is None


def test_other_counterparties_unchanged():
    assert cpty_pricing.resolve_price("Flowdesk", "ETH", S, 2800, 0.1, "C", "buy") is None
    assert cpty_pricing.resolve_price("KeyRock", "FIL", 1.0, 1.2, 0.1, "C", "buy") is not None
