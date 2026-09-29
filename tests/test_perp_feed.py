"""Perp feed venue fallback.

Binance answers 451 from Cloud Run, which made every perp reading in
production come back as 0.0 — indistinguishable from a flat hedge. These pin
down that the chain falls through, that a total outage is loud rather than
silent, and that fallback funding events carry the mark at settlement.

No network: every venue is stubbed.
"""

from __future__ import annotations

import httpx
import pytest

from plgo_options.market_data import binance_client, perp_feed


def _451(_asset=None, *a, **k):
    req = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/premiumIndex")
    raise httpx.HTTPStatusError("451", request=req,
                                response=httpx.Response(451, request=req))


@pytest.fixture
def venues(monkeypatch):
    """Let a test set the chain without leaking into the next one."""
    original = list(perp_feed.MARK_VENUES)
    yield perp_feed.MARK_VENUES
    perp_feed.MARK_VENUES[:] = original


def _mark(price: float) -> perp_feed.PerpMark:
    return perp_feed.PerpMark(symbol="ETHUSDT", mark_price=price, index_price=price,
                              last_funding_rate=0.0001, next_funding_time_ms=1)


@pytest.mark.asyncio
async def test_uses_binance_when_it_answers(venues):
    """Binance stays first: the hedge actually sits there, so its mark is the
    right one whenever it is reachable."""
    async def ok(asset):
        return _mark(2692.33)

    venues[:] = [("binance", ok), ("bybit", _451), ("okx", _451)]
    vm = await perp_feed.get_mark_with_venue("ETH")
    assert vm.venue == "binance" and vm.mark.mark_price == 2692.33


@pytest.mark.asyncio
async def test_falls_through_to_bybit_on_451(venues):
    async def bybit(asset):
        return _mark(2692.89)

    venues[:] = [("binance", _451), ("bybit", bybit), ("okx", _451)]
    vm = await perp_feed.get_mark_with_venue("ETH")
    assert vm.venue == "bybit" and vm.mark.mark_price == 2692.89


@pytest.mark.asyncio
async def test_falls_through_again_to_okx(venues):
    async def dead(asset):
        raise RuntimeError("down")

    async def okx(asset):
        return _mark(2692.58)

    venues[:] = [("binance", _451), ("bybit", dead), ("okx", okx)]
    vm = await perp_feed.get_mark_with_venue("ETH")
    assert vm.venue == "okx"


@pytest.mark.asyncio
async def test_total_outage_raises_and_names_every_venue(venues):
    """The old failure mode printed 0.0, which reads as a flat position. A
    dead feed has to be loud."""
    async def dead(asset):
        raise RuntimeError("down")

    venues[:] = [("binance", dead), ("bybit", dead), ("okx", dead)]
    with pytest.raises(RuntimeError) as e:
        await perp_feed.get_mark_with_venue("ETH")
    msg = str(e.value)
    assert "binance" in msg and "bybit" in msg and "okx" in msg


@pytest.mark.asyncio
async def test_status_code_is_kept_in_the_error(venues):
    async def dead(asset):
        raise RuntimeError("down")

    venues[:] = [("binance", _451), ("bybit", dead), ("okx", dead)]
    with pytest.raises(RuntimeError) as e:
        await perp_feed.get_mark_with_venue("ETH")
    assert "451" in str(e.value)


# ── funding history ──────────────────────────────────────────────────────────

BYBIT_RATES = {"result": {"list": [
    {"symbol": "ETHUSDT", "fundingRate": "0.00008702", "fundingRateTimestamp": "1790697600000"},
    {"symbol": "ETHUSDT", "fundingRate": "0.00009032", "fundingRateTimestamp": "1790668800000"},
]}}
BYBIT_KLINES = {"result": {"list": [
    ["1790697600000", "2672.39", "2683.54", "2667.58", "2674.69"],
    ["1790668800000", "2700.00", "2710.00", "2699.00", "2707.85"],
]}}


@pytest.mark.asyncio
async def test_funding_history_falls_back_and_carries_the_settlement_mark(monkeypatch):
    """A payment is -qty x mark x rate, so each event needs the mark AT
    settlement. Today's mark repeated would misstate every historical
    payment, so the rates are joined to Bybit's mark klines on the shared
    timestamp grid."""
    monkeypatch.setattr(binance_client, "get_funding_history", _451)

    async def fake_json(url, params=None):
        return BYBIT_KLINES if "kline" in url else BYBIT_RATES

    monkeypatch.setattr(perp_feed, "_json", fake_json)

    ev = await perp_feed.get_funding_history("ETH", limit=5)
    assert [e.funding_time_ms for e in ev] == [1790668800000, 1790697600000]  # oldest first
    assert [e.mark_price for e in ev] == [2707.85, 2674.69]                   # per settlement
    assert len({e.mark_price for e in ev}) == 2                               # not one repeated


@pytest.mark.asyncio
async def test_funding_history_prefers_binance(monkeypatch):
    async def ok(asset, start_ms=None, end_ms=None, limit=1000):
        return [perp_feed.FundingEvent(funding_time_ms=1, rate=0.0001, mark_price=2600.0)]

    monkeypatch.setattr(binance_client, "get_funding_history", ok)

    async def boom(url, params=None):
        raise AssertionError("should not have reached Bybit")

    monkeypatch.setattr(perp_feed, "_json", boom)
    assert len(await perp_feed.get_funding_history("ETH")) == 1


@pytest.mark.asyncio
async def test_missing_klines_leave_mark_zero_rather_than_guessing(monkeypatch):
    """If the mark series is unavailable the rate is still recorded, but the
    mark stays 0 instead of being invented from the current price."""
    monkeypatch.setattr(binance_client, "get_funding_history", _451)

    async def fake_json(url, params=None):
        if "kline" in url:
            raise RuntimeError("klines down")
        return BYBIT_RATES

    monkeypatch.setattr(perp_feed, "_json", fake_json)
    ev = await perp_feed.get_funding_history("ETH")
    assert len(ev) == 2 and all(e.mark_price == 0.0 for e in ev)


# ── the OKX history path ─────────────────────────────────────────────────────
# Cloud Run gets 451 from Binance AND 403 from Bybit, so OKX is the venue that
# actually has to carry funding history in production.

OKX_RATES = {"data": [
    {"fundingTime": "1790697600000", "fundingRate": "0.0000598415868892"},
    {"fundingTime": "1790668800000", "fundingRate": "0.0000572899115064"},
]}
OKX_CANDLES = {"data": [
    ["1790697600000", "2673", "2683.54", "2667.81", "2674.79", "1"],
    ["1790668800000", "2700", "2710.00", "2699.00", "2707.69", "1"],
]}


@pytest.fixture
def history_venues():
    original = list(perp_feed.HISTORY_VENUES)
    yield perp_feed.HISTORY_VENUES
    perp_feed.HISTORY_VENUES[:] = original


@pytest.mark.asyncio
async def test_okx_carries_history_when_binance_451_and_bybit_403(monkeypatch, history_venues):
    """The exact production condition."""
    async def b403(*a, **k):
        req = httpx.Request("GET", "https://api.bybit.com/x")
        raise httpx.HTTPStatusError("403", request=req,
                                    response=httpx.Response(403, request=req))

    history_venues[0] = ("binance", _451)
    history_venues[1] = ("bybit", b403)

    async def fake_json(url, params=None):
        return OKX_CANDLES if "candles" in url else OKX_RATES

    monkeypatch.setattr(perp_feed, "_json", fake_json)

    ev = await perp_feed.get_funding_history("ETH", limit=30)
    assert [e.funding_time_ms for e in ev] == [1790668800000, 1790697600000]
    assert [e.mark_price for e in ev] == [2707.69, 2674.79]
    assert len({e.mark_price for e in ev}) == 2


@pytest.mark.asyncio
async def test_history_outage_raises_naming_every_venue(history_venues):
    async def dead(*a, **k):
        raise RuntimeError("down")

    history_venues[:] = [("binance", dead), ("bybit", dead), ("okx", dead)]
    with pytest.raises(RuntimeError) as e:
        await perp_feed.get_funding_history("ETH")
    msg = str(e.value)
    assert "binance" in msg and "bybit" in msg and "okx" in msg


@pytest.mark.asyncio
async def test_okx_mark_series_pages_backwards(monkeypatch):
    """One OKX page is 100 hourly candles (~4 days); a 90-event history needs
    several, so the walk-back has to actually advance and then stop."""
    pages, calls = [], {"n": 0}

    def page(base_ts):
        return {"data": [[str(base_ts - i * 3_600_000), "1", "1", "1",
                          str(2600 + i), "1"] for i in range(100)]}

    async def fake_json(url, params=None):
        calls["n"] += 1
        after = int(params["after"])
        pages.append(after)
        return page(after - 3_600_000)

    monkeypatch.setattr(perp_feed, "_json", fake_json)
    t_max = 1790697600000
    t_min = t_max - 300 * 3_600_000            # needs ~3 pages
    marks = await perp_feed._okx_mark_series("ETH-USDT-SWAP", t_min, t_max)

    assert calls["n"] >= 3, "should have paged back more than once"
    assert calls["n"] <= 12, "must respect the page cap"
    assert pages == sorted(pages, reverse=True), "cursor must move backwards"
    assert len(marks) > 100
