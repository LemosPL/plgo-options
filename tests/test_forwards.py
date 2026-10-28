"""Cash-settled forwards: pricing, the booking lifecycle, reconciliation, and
how the forward book feeds collateral and the optimizer."""

import asyncio
from datetime import date

import aiosqlite
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from plgo_options.data import forward_repository as repo
from plgo_options.pricing import forwards as fwd
from plgo_options.web.routes import forwards as route

REQ = Request({"type": "http", "session": {"user": {"email": "desk@example.com"}}})


def test_pricing_signs_and_carry():
    # Sold 1m FIL forward at 1.10; spot 1.00, no carry: we gain 100k.
    v = fwd.value("sell", 1_000_000, 1.10, 1.00, 0.0, 0.25)
    assert v["fair_forward"] == 1.00 and round(v["mtm"]) == 100_000 and v["delta"] == -1_000_000
    # 10% carry, 3 months: fair forward 1.025, delta scaled by the carry factor.
    v = fwd.value("buy", 1_000, 2_000, 2_000, 0.10, 0.25)
    assert round(v["fair_forward"], 6) == 2_050 and round(v["mtm"]) == 50_000
    assert round(v["delta"], 6) == 1_025 and v["theta"] < 0
    assert fwd.settlement_pnl("sell", 1_000_000, 1.10, 1.30) == pytest.approx(-200_000)
    with pytest.raises(ValueError):
        fwd.side_sign("hold")


def _with_db(monkeypatch, spot=1.0):
    async def make():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await repo.ensure_tables(db)
        return db

    db = asyncio.run(make())

    async def fake_db():
        return db

    async def fake_spot(asset):
        return spot

    monkeypatch.setattr(route, "get_db", fake_db)
    monkeypatch.setattr(route, "spot_for", fake_spot)
    return db


def _book(**kw):
    body = dict(asset="FIL", counterparty="Flowdesk", side="sell", qty=1_000_000, forward_price=1.10,
                trade_date="2026-10-27", maturity_date="2026-12-31")
    body.update(kw)
    return asyncio.run(route.book_forward(route.ForwardIn(**body), REQ))


def test_lifecycle_book_amend_recon_settle_delete(monkeypatch):
    _with_db(monkeypatch, spot=1.0)
    a = _book()
    b = _book(counterparty="KeyRock", side="buy", qty=500_000, forward_price=0.95)
    assert a["created_by"] == "desk@example.com" and a["status"] == "open"

    book = asyncio.run(route.build_forward_book("FIL", 1.0, today=date(2026, 10, 28)))
    t = book["totals"]
    assert t["count"] == 2 and t["net_qty"] == -500_000
    assert round(t["mtm"]) == 100_000 + 25_000          # sold 1.10 / bought 0.95, spot 1.00
    assert t["unreconciled"] == 2

    # Amend qty, then reconcile: matching figures, then a price break.
    asyncio.run(route.amend_forward(a["id"], route.ForwardEdit(qty=2_000_000), REQ))
    asyncio.run(route.reconcile_forward(a["id"], route.ReconIn(cpty_qty=-2_000_000, cpty_price=1.10, cpty_mtm=200_400), REQ))
    asyncio.run(route.reconcile_forward(b["id"], route.ReconIn(cpty_qty=500_000, cpty_price=0.96), REQ))
    book = asyncio.run(route.build_forward_book("FIL", 1.0, today=date(2026, 10, 28)))
    rec = {x["id"]: x["recon"] for x in book["trades"]}
    assert rec[a["id"]]["status"] == "matched"
    assert rec[b["id"]]["status"] == "break" and rec[b["id"]]["breaks"][0]["field"] == "forward_price"

    # Settle the first at 1.05: sold 2m at 1.10 -> +100k realised; delete the second.
    s = asyncio.run(route.settle_forward(a["id"], route.SettleIn(fixing_price=1.05), REQ))
    assert s["status"] == "settled" and s["settled_pnl_usd"] == pytest.approx(100_000)
    asyncio.run(route.delete_forward(b["id"], REQ))
    book = asyncio.run(route.get_book("FIL"))
    assert book["totals"]["count"] == 0 and book["realised_pnl_usd"] == pytest.approx(100_000)
    with pytest.raises(HTTPException):
        asyncio.run(route.settle_forward(a["id"], route.SettleIn(fixing_price=1.0), REQ))


def test_validation(monkeypatch):
    _with_db(monkeypatch)
    with pytest.raises(HTTPException):
        _book(maturity_date="2026-10-01")                  # before trade date
    with pytest.raises(HTTPException):
        _book(asset="BTC")
    with pytest.raises(HTTPException):
        _book(side="hold")


def test_carry_moves_the_mark(monkeypatch):
    _with_db(monkeypatch, spot=1.0)
    _book(maturity_date="2027-10-28", trade_date="2026-10-28")
    asyncio.run(route.set_carry(route.CarryIn(asset="FIL", rate_pct=10), REQ))
    book = asyncio.run(route.build_forward_book("FIL", 1.0, today=date(2026, 10, 28)))
    t = book["trades"][0]
    assert book["carry"] == pytest.approx(0.10)
    assert t["fair_forward"] == pytest.approx(1.10, rel=1e-3) and abs(t["mtm"]) < 1_000


def test_collateral_nets_forward_mtm():
    from plgo_options.web.routes.collateral import _compute_liabilities, _net_mtm_by_cp
    data = {"positions": [{"counterparty": "Flowdesk", "current_mtm": -300_000.0}],
            "forwards": {"by_counterparty": [{"counterparty": "Flowdesk", "net_qty": -1e6, "mtm": 100_000.0}]}}
    assert _net_mtm_by_cp(data)["flowdesk"] == pytest.approx(-200_000)
    assert _compute_liabilities(data)[0]["flowdesk"] == pytest.approx(200_000)


def test_optimizer_gets_forward_legs(monkeypatch):
    from plgo_options.web.routes import optimization as opt

    async def no_perps(asset):
        return {"by_venue": []}

    monkeypatch.setattr(opt, "perp_summary", no_perps)
    pnl = {"spot": 1.0, "spot_ladder": [0.5, 1.0, 1.5], "chart_horizons": [0, 30],
           "positions": [{"counterparty": "Flowdesk"}],
           "forwards": {"by_counterparty": [{"counterparty": "flowdesk", "net_qty": -1e6,
                                             "avg_forward_price": 1.1, "mtm": 100_000.0}]}}
    legs = asyncio.run(opt._build_perp_positions("FIL", pnl))
    assert len(legs) == 1
    leg = legs[0]
    assert leg["opt"] == "F" and leg["is_forward"] and leg["counterparty"] == "Flowdesk"
    assert leg["strike"] == 1.1 and leg["net_qty"] == -1e6
    assert leg["payoff_by_horizon"]["0"] == [pytest.approx(600_000), pytest.approx(100_000), pytest.approx(-400_000)]
