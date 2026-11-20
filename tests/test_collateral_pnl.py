"""P&L entered on the Collateral Map counts as USD collateral: $1 a unit, no
haircut, may be negative, never repriced with spot."""

import asyncio

import aiosqlite
import pytest
from fastapi import HTTPException

from plgo_options.data.database import COLLATERAL_PRICE_SCHEMA, COLLATERAL_SCHEMA
from plgo_options.web.routes import collateral as col


FWD_MTM: dict = {}


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

    async def fake_liab(fwd_mtm=None):
        if fwd_mtm is not None:          # open forwards' MTM per book, as _liability_by_cp fills it
            fwd_mtm.clear()
            fwd_mtm.update(FWD_MTM)
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


def test_open_forwards_unrealised_pnl_counts_automatically_under_pnl_trades(monkeypatch):
    db = _db(monkeypatch)
    FWD_MTM.clear()
    FWD_MTM.update({
        "ETH": {"flowdesk": {"counterparty": "flowdesk", "net_qty": -2500, "mtm": 1_256_250.0}},
        "FIL": {"keyrock": {"counterparty": "KeyRock", "net_qty": -1.5e6, "mtm": 102_075.0}},
    })
    asyncio.run(col.upsert_map_cells(col.MapCells(cells=[
        col.MapCell(counterparty="Flowdesk", asset="PNL_TRADES", book="ETH", qty=-100_000),   # e.g. closed P&L, by hand
        col.MapCell(counterparty="Flowdesk", asset="USDC", book="ETH", qty=1_000_000)])))
    m = asyncio.run(col.collateral_map())
    by = {c["counterparty"].lower(): c for c in m["counterparties"]}
    fd, kr = by["flowdesk"], by["keyrock"]
    assert fd["auto_pnl"] == {"ETH": pytest.approx(1_256_250.0)}
    assert fd["manual_books"]["ETH"]["PNL_TRADES"] == -100_000            # input keeps what was typed
    assert fd["books"]["ETH"]["PNL_TRADES"] == pytest.approx(1_156_250.0)  # typed + open forwards' MTM
    assert fd["total_usd"] == pytest.approx(2_156_250.0)
    assert kr["auto_pnl"] == {"FIL": pytest.approx(102_075.0)} and kr["usd"]["PNL_TRADES"] == pytest.approx(102_075.0)
    assert col.forward_pnl_rows({"ETH": {"x": {"counterparty": "X", "mtm": 0.0}}}) == []
    FWD_MTM.clear()
    asyncio.run(db.close())


def test_collateral_map_liabilities_leave_forwards_out():
    port = {"positions": [{"counterparty": "Flowdesk", "current_mtm": -3_000_000.0}],
            "forwards": {"by_counterparty": [{"counterparty": "Flowdesk", "net_qty": -2500, "mtm": 1_000_000.0}]}}
    with_f, _, _ = col._compute_liabilities(port)
    without, _, _ = col._compute_liabilities(port, include_forwards=False)
    assert with_f["flowdesk"] == pytest.approx(2_000_000.0) and without["flowdesk"] == pytest.approx(3_000_000.0)
    assert col._net_mtm_by_cp(port, include_forwards=False)["flowdesk"] == pytest.approx(-3_000_000.0)
