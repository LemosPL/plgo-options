"""P&L entered on the Collateral Map counts as USD collateral: $1 a unit, no
haircut, may be negative, never repriced with spot."""

import asyncio

import aiosqlite
import pytest
from fastapi import HTTPException

from plgo_options.data.database import COLLATERAL_PRICE_SCHEMA, COLLATERAL_SCHEMA
from plgo_options.web.routes import collateral as col


def _db(monkeypatch):
    async def make():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(COLLATERAL_SCHEMA)
        await db.execute(COLLATERAL_PRICE_SCHEMA)
        return db
    db = asyncio.run(make())

    async def fake_db():
        return db

    async def fake_live(force=False):
        return {"USDC": 1.0, "ETH": 2000.0, "FIL": 0.75, "BTC": 90000.0}

    async def fake_liab():
        return ({"flowdesk": {"ETH": 1_000_000.0, "FIL": 0.0}}, {"flowdesk": "Flowdesk"},
                {"flowdesk": 3}, {"flowdesk": 1_000_000.0})

    monkeypatch.setattr(col, "get_db", fake_db)
    monkeypatch.setattr(col, "_live_prices", fake_live)
    monkeypatch.setattr(col, "_liability_by_cp", fake_liab)
    return db


def test_pnl_rows_are_usd_collateral(monkeypatch):
    db = _db(monkeypatch)
    cells = [col.MapCell(counterparty="Flowdesk", asset="USDC", book="ETH", qty=500_000),
             col.MapCell(counterparty="Flowdesk", asset="PNL_ETH_FWD", book="ETH", qty=400_000),
             col.MapCell(counterparty="Flowdesk", asset="PNL_TRADES", book="ETH", qty=-50_000)]
    asyncio.run(col.upsert_map_cells(col.MapCells(cells=cells)))
    # A price override never applies to a P&L row.
    asyncio.run(col.upsert_price(col.PriceUpdate(asset="PNL_ETH_FWD", price=3.0)))
    m = asyncio.run(col.collateral_map())
    assert {"PNL_TRADES", "PNL_ETH_FWD", "PNL_FIL_FWD"} <= set(m["assets"])
    assert m["prices"]["PNL_ETH_FWD"] == 1.0 and "PNL_ETH_FWD" in m["usd_assets"]
    assert m["asset_labels"]["PNL_FIL_FWD"] == "PnL – FIL Forwards"
    fd = m["counterparties"][0]
    assert fd["usd"]["PNL_TRADES"] == -50_000 and fd["total_usd"] == pytest.approx(850_000)
    assert fd["collateral_haircut_usd"] == pytest.approx(850_000)
    assert fd["balance_usd"] == pytest.approx(-150_000)
    # Negative only for P&L rows.
    with pytest.raises(HTTPException):
        asyncio.run(col.upsert_map_cell(col.MapCell(counterparty="Flowdesk", asset="ETH", qty=-1)))
    asyncio.run(db.close())
