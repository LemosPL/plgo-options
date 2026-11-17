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


def test_closed_forwards_realised_pnl_counts_automatically_under_pnl_trades(monkeypatch):
    from plgo_options.data import forward_repository as frepo
    db = _db(monkeypatch)

    async def book():
        await frepo.ensure_tables(db)
        for asset, cp, side, q, k, status, pnl in [
            ("ETH", "flowdesk", "sell", 2500, 2346.5, "closed", 1_348_000.0),   # cpty spelled differently
            ("ETH", "Flowdesk", "buy", 1250, 2100.0, "closed", -363_812.0),
            ("FIL", "KeyRock", "sell", 4e6, 0.7564, "closed", 305_600.0),
            ("FIL", "Flowdesk", "sell", 5e6, 0.7544, "settled", 737_500.0),     # cash moved: not counted
            ("FIL", "Flowdesk", "sell", 5e6, 0.61, "open", None)]:
            fid = await frepo.insert_forward(db, dict(asset=asset, counterparty=cp, side=side, qty=q, forward_price=k,
                                                      trade_date="2026-11-01", maturity_date="2026-11-27"), "t")
            if status != "open":
                await frepo.set_status(db, fid, status, "t", fixing_price=1.0, settled_pnl_usd=pnl)
    asyncio.run(book())
    asyncio.run(col.upsert_map_cells(col.MapCells(cells=[
        col.MapCell(counterparty="Flowdesk", asset="PNL_TRADES", book="ETH", qty=-100_000),
        col.MapCell(counterparty="Flowdesk", asset="USDC", book="ETH", qty=1_000_000)])))
    m = asyncio.run(col.collateral_map())
    by = {c["counterparty"].lower(): c for c in m["counterparties"]}
    fd, kr = by["flowdesk"], by["keyrock"]
    assert fd["auto_pnl"] == {"ETH": pytest.approx(984_188.0)}
    assert fd["manual_books"]["ETH"]["PNL_TRADES"] == -100_000            # input keeps what was typed
    assert fd["books"]["ETH"]["PNL_TRADES"] == pytest.approx(884_188.0)   # typed + auto
    assert fd["total_usd"] == pytest.approx(1_884_188.0)
    assert kr["auto_pnl"] == {"FIL": pytest.approx(305_600.0)} and kr["usd"]["PNL_TRADES"] == pytest.approx(305_600.0)
    assert "FIL" not in fd["auto_pnl"]                                     # settled + open left out
    rows = asyncio.run(col.realised_forward_pnl_rows("FIL"))
    assert [(r["counterparty"], r["qty"]) for r in rows] == [("KeyRock", 305_600.0)]
    asyncio.run(db.close())
