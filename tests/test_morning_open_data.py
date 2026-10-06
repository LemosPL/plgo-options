"""morning-open returns the figures as data for the Agents page panel."""

from __future__ import annotations

import pytest

from plgo_options.agents import decisions, desk
from plgo_options.agents.policy import default_policy


@pytest.mark.asyncio
async def test_morning_open_returns_panel_data(monkeypatch):
    pols = {a: decisions.apply_sheet(default_policy(a), decisions.SHEET_2026_10_05[a]) for a in ("ETH", "FIL")}

    async def get_policy(a):
        return pols[a]

    async def book(a):
        return {"spot": 2695.0 if a == "ETH" else 1.08, "mtm": -1e6, "delta": 100.0, "theta": -1e4,
                "vega": 5e4, "positions": []}

    async def perp(a):
        return {"venue": "Flowdesk", "net_qty": -10, "notional_usd": -27_000, "funding": {"last_30d_usd": -50}}

    async def empty(*a, **k):
        return set() if "fired" in str(k) else []

    async def fired(*a, **k):
        return {"+5"}

    async def props(*a, **k):
        return [{"id": 1, "asset": "ETH", "kind": "x", "route": "rejected"}]

    async def orders():
        return {"orders": [1, 2]}

    async def coll():
        return {"totals": {"shortfall_haircut": 0, "liability_usd": 3e6}}

    monkeypatch.setattr(desk.store, "get_policy", get_policy)
    monkeypatch.setattr(desk, "get_book", book)
    monkeypatch.setattr(desk, "get_perp", perp)
    monkeypatch.setattr(desk.store, "fired_rows", fired)
    monkeypatch.setattr(desk.store, "list_proposals", props)
    monkeypatch.setattr(desk, "get_open_orders", orders)
    monkeypatch.setattr(desk, "get_collateral", coll)
    res = await desk.morning_open({})
    d = res["data"]
    assert d["ETH"]["spot"] == 2695.0 and d["ETH"]["reference"] == 2700
    assert d["ETH"]["row_up"]["key"] == "+5" and d["ETH"]["fired"] == ["+5"]
    assert d["ETH"]["open_proposals"] == {"lucas": 0, "chris": 0, "rejected": 1}
    assert d["FIL"]["stop"] == 0.89 and d["FIL"]["perp_venue"] == "Flowdesk"
    assert d["_all"]["resting_orders"] == 2 and d["_all"]["liability"] == 3e6
    assert len(d["ETH"]["targets"]) == 3
