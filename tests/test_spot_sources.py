"""ETH / BTC spot: Deribit index first, a locked perp is never used, external
venues and the last good price when Deribit can't answer."""

import asyncio

import pytest

from plgo_options.market_data import deribit_client as dc


def _client(monkeypatch, index=None, ticker=None, external=None):
    dc._cache.clear()
    dc._last_good_spot.clear()

    async def fake_get(self, method, params=None):
        if method == "get_index_price":
            if index is None:
                raise RuntimeError("503 system_maintenance")
            return {"index_price": index}
        if method == "ticker":
            if ticker is None:
                raise RuntimeError("429 Too Many Requests")
            return ticker
        raise AssertionError(method)

    monkeypatch.setattr(dc.DeribitClient, "_get", fake_get)
    monkeypatch.setattr(dc, "_external_spot_sources", lambda a: external or [])
    return dc.DeribitClient()


def test_index_is_the_spot_not_a_stale_perp_last_trade(monkeypatch):
    c = _client(monkeypatch, index=1639.16, ticker={"state": "locked", "last_price": 1963.0, "mark_price": 1639.0})
    assert asyncio.run(c.get_eth_spot_price()) == pytest.approx(1639.16)


def test_locked_perp_is_skipped_and_an_open_perp_uses_its_mark(monkeypatch):
    c = _client(monkeypatch, index=None, ticker={"state": "locked", "last_price": 1963.0, "mark_price": 1639.0})
    with pytest.raises(RuntimeError):
        asyncio.run(c.get_eth_spot_price())
    c = _client(monkeypatch, index=None, ticker={"state": "open", "last_price": 1600.0, "mark_price": 1601.5})
    assert asyncio.run(c.get_eth_spot_price()) == pytest.approx(1601.5)


def test_last_good_price_bridges_an_outage(monkeypatch):
    c = _client(monkeypatch, index=1597.44)
    assert asyncio.run(c.get_eth_spot_price()) == pytest.approx(1597.44)
    dc._cache.clear()                                    # cache expired, Deribit now down

    async def down(self, method, params=None):
        raise RuntimeError("503")
    monkeypatch.setattr(dc.DeribitClient, "_get", down)
    assert asyncio.run(c.get_eth_spot_price()) == pytest.approx(1597.44)


def test_implausible_quotes_are_rejected(monkeypatch):
    c = _client(monkeypatch, index=0.0, ticker=None)
    with pytest.raises(RuntimeError):
        asyncio.run(c.get_eth_spot_price())
