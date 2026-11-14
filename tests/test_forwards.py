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


def test_forward_positions_curves_for_client_side_screens():
    from plgo_options.web.routes.portfolio import _forward_positions
    book = {"carry": 0.0, "trades": [{"id": 7, "counterparty": "Flowdesk", "side": "sell", "signed_qty": -2500.0,
                                      "forward_price": 2346.5, "maturity_date": "2026-11-27", "days_to_maturity": 30,
                                      "delta": -2500.0, "theta": 0.0, "fair_forward": 2400.0, "mtm": -133_750.0}]}
    rows = _forward_positions(book, [2000.0, 2346.5, 2600.0], [0, 30, 60],
                              [{"days": 0}, {"days": 45}], 2400.0, "ETH")
    r = rows[0]
    assert r["is_forward"] and r["opt"] == "F" and r["id"] == -100007 and r["net_qty"] == -2500.0
    assert r["payoff_by_horizon"]["0"] == [pytest.approx(866_250), 0.0, pytest.approx(-633_750)]
    assert r["payoff_by_horizon"]["60"] == r["payoff_by_horizon"]["0"]      # no carry: same past maturity
    assert r["delta"] == 1.0 and r["current_mtm"] == -133_750.0
    assert r["value_path"] == [pytest.approx(-133_750), pytest.approx(-133_750)]


def test_confirmed_forward_is_booked_once(monkeypatch):
    async def go():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await repo.ensure_tables(db)
        n_fwd, n_lv = len(repo.CONFIRMED_FORWARDS), sum(1 for l in repo.CONFIRMED_LEVELS if l.get("external_ref"))
        assert await repo.seed_confirmed(db) == n_fwd + n_lv
        assert await repo.seed_confirmed(db) == 0          # idempotent
        eth = {r["external_ref"]: r for r in await repo.list_forwards(db, "ETH")}
        r = eth["e45b44b1-1592-4201-86ec-bd5a7f71a0b7"]
        assert (r["counterparty"], r["side"], r["qty"], r["forward_price"], r["maturity_date"]) ==             ("Flowdesk", "sell", 2500.0, 2346.5, "2026-11-27")
        r2 = eth["ad76a4c0-0380-4ded-8305-84d8fd4ba3a1"]
        assert (r2["side"], r2["qty"], r2["forward_price"], r2["trade_date"]) == ("sell", 1250.0, 2108.1, "2026-11-09")
        fil = await repo.list_forwards(db, "FIL")
        assert [(f["side"], f["qty"], f["forward_price"]) for f in fil] == [("sell", 5_000_000.0, 0.7544)]
        # Desk orders become levels once: short, so stop above target.
        lv = await repo.get_levels(db)
        assert (lv[f"fwd:{r['id']}"]["stop_loss"], lv[f"fwd:{r['id']}"]["take_profit"]) == (2236.5, 1900.0)
        assert (lv[f"fwd:{fil[0]['id']}"]["stop_loss"], lv[f"fwd:{fil[0]['id']}"]["take_profit"]) == (0.83, 0.67)
        for l in repo.CONFIRMED_LEVELS:
            assert fwd.check_levels_order("sell", l["stop_loss"], l["take_profit"]) is None
        # Clearing levels on the Monitor sticks across restarts.
        await repo.set_levels(db, f"fwd:{r['id']}", "ETH", None, None, "", "desk")
        assert await repo.seed_confirmed(db) == 0
        assert f"fwd:{r['id']}" not in await repo.get_levels(db)
        # A deliberate delete stays deleted.
        await repo.set_status(db, r["id"], "deleted", "desk")
        assert await repo.seed_confirmed(db) == 0
        await db.close()

    asyncio.run(go())


# ── stop-loss / take-profit levels (Forwards Monitor) ─────────────────────

def test_level_watch_follows_the_side():
    # Sold at 1.10: stop above spot, target below.
    w = fwd.level_watch("sell", 1_000_000, 1.10, 1.00, 0.0, 0.1, 1.20, 0.90)
    assert w["status"] == "watching"
    assert w["stop"]["mtm_at"] == pytest.approx(-100_000)      # 1m x (1.10 - 1.20)
    assert w["target"]["mtm_at"] == pytest.approx(200_000)
    assert w["stop"]["dist_pct"] == pytest.approx(20.0)
    assert fwd.level_watch("sell", 1, 1.1, 1.21, 0, 0, 1.20, 0.90)["status"] == "stop_hit"
    assert fwd.level_watch("sell", 1, 1.1, 0.89, 0, 0, 1.20, 0.90)["status"] == "target_hit"
    assert fwd.level_watch("sell", 1, 1.1, 1.19, 0, 0, 1.20, 0.90)["status"] == "near_stop"
    # Bought: stop below, target above.
    assert fwd.level_watch("buy", 1, 1.0, 0.95, 0, 0, 0.96, None)["status"] == "stop_hit"
    assert fwd.level_watch("buy", 1, 1.0, 1.0, 0, 0, None, None)["status"] == "none"
    assert fwd.check_levels_order("sell", 0.9, 1.2) and fwd.check_levels_order("buy", 1.2, 0.9)
    assert fwd.check_levels_order("sell", 1.2, 0.9) is None and fwd.check_levels_order("buy", 0.9, None) is None


def _leg(i, typ, side, k=0.7564, q=1_000_000, cp="KeyRock", td="2026-11-10", exp="2026-12-26", prem=0.0):
    return dict(id=i, asset="FIL", counterparty=cp, trade_date=td, side=side, option_type=typ,
                expiry=exp, strike=k, qty=q, premium_usd=prem, status="active", trade_id="")


def test_synthetic_forward_detection():
    legs = [
        _leg(1, "Put", "Sell", prem=12_000), _leg(2, "Call", "Buy", prem=-10_000),   # long fwd 0.7564
        _leg(3, "Call", "Sell", k=1.0), _leg(4, "Put", "Buy", k=1.0),                 # short fwd 1.0
        _leg(5, "Call", "Buy", k=0.9),                                                # lone call
        _leg(6, "Put", "Sell", k=1.2, q=500_000), _leg(7, "Call", "Buy", k=1.2),      # size mismatch
        _leg(8, "Put", "Sell", td="2026-11-09"), _leg(9, "Call", "Buy", td="2026-11-10", k=0.5),
    ]
    syn = {s["key"]: s for s in fwd.find_synthetic_forwards(legs)}
    assert set(syn) == {"syn:2-1", "syn:3-4"}
    long_, short = syn["syn:2-1"], syn["syn:3-4"]
    assert (long_["side"], long_["qty"], long_["strike"]) == ("buy", 1_000_000, 0.7564)
    # +2k net premium received on a long: all-in price 0.002 lower.
    assert long_["effective_price"] == pytest.approx(0.7564 - 0.002)
    assert short["side"] == "sell"
    # Both pairs sit in a bigger KeyRock deal that day: embedded, not standalone.
    assert not long_["standalone"] and not short["standalone"]
    alone = fwd.find_synthetic_forwards([_leg(1, "Put", "Sell"), _leg(2, "Call", "Buy")])
    assert alone[0]["standalone"]


def _with_trades(monkeypatch, legs, spot):
    from plgo_options.data.database import TRADES_SCHEMA
    db = _with_db(monkeypatch, spot=spot)

    async def seed():
        await db.execute(TRADES_SCHEMA)
        for t in legs:
            await db.execute(
                "INSERT INTO trades (id, asset, counterparty, trade_id, trade_date, side, option_type, expiry,"
                " strike, qty, premium_usd, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["asset"], t["counterparty"], t["trade_id"], t["trade_date"], t["side"],
                 t["option_type"], t["expiry"], t["strike"], t["qty"], t["premium_usd"], t["status"]))
        await db.commit()
    asyncio.run(seed())
    return db


def test_monitor_lists_forwards_and_synthetics_and_manages_levels(monkeypatch):
    exp = "2099-12-31"
    db = _with_trades(monkeypatch, [_leg(1, "Put", "Sell", exp=exp), _leg(2, "Call", "Buy", exp=exp)], spot=0.75)
    f = _book(maturity_date="2099-12-31")                       # booked: sell 1m FIL @ 1.10

    mon = asyncio.run(route.get_monitor("FIL"))
    keys = {p["key"]: p for p in mon["positions"]}
    assert set(keys) == {f"fwd:{f['id']}", "syn:2-1"}
    syn = keys["syn:2-1"]
    assert syn["kind"] == "synthetic" and syn["side"] == "buy" and len(syn["legs"]) == 2
    assert syn["mtm"] == pytest.approx(1_000_000 * (0.75 - 0.7564))
    assert syn["levels"]["status"] == "none"

    # Levels on the synthetic: long, so stop below, target above.
    p = asyncio.run(route.put_levels(route.LevelsIn(asset="FIL", key="syn:2-1", stop_loss=0.7, take_profit=0.9), REQ))
    assert p["levels"]["status"] == "watching" and p["levels"]["stop"]["mtm_at"] == pytest.approx(-56_400)
    with pytest.raises(HTTPException):                          # wrong way round for a long
        asyncio.run(route.put_levels(route.LevelsIn(asset="FIL", key="syn:2-1", stop_loss=0.9, take_profit=0.7), REQ))
    with pytest.raises(HTTPException):                          # unknown position
        asyncio.run(route.put_levels(route.LevelsIn(asset="FIL", key="syn:9-9", stop_loss=0.7), REQ))

    # Levels on the booked forward reach the forward book (trades table, /pnl).
    asyncio.run(route.put_levels(route.LevelsIn(asset="FIL", key=f"fwd:{f['id']}", stop_loss=0.76), REQ))
    book = asyncio.run(route.build_forward_book("FIL", 0.75))
    assert book["trades"][0]["levels"]["status"] == "near_stop"   # 0.76 is 1.3% above 0.75

    # The option legs are untouched; clearing removes the row.
    cur = asyncio.run(db.execute("SELECT COUNT(*) FROM trades WHERE status = 'active'"))
    assert asyncio.run(cur.fetchone())[0] == 2
    asyncio.run(route.put_levels(route.LevelsIn(asset="FIL", key="syn:2-1"), REQ))
    assert "syn:2-1" not in asyncio.run(repo.get_levels(db, "FIL"))
    mon = asyncio.run(route.get_monitor("FIL"))
    assert mon["positions"][0]["key"] == f"fwd:{f['id']}"       # near-stop sorts first
    asyncio.run(db.close())


def test_desk_levels_attach_to_the_matching_synthetic(monkeypatch):
    # KeyRock 10 Nov: PL buys the 11DEC26 0.7564 put, sells the call, 4m each = short forward.
    legs = [_leg(11, "Put", "Buy", q=4_000_000, exp="2026-12-11", prem=-40_000),
            _leg(12, "Call", "Sell", q=4_000_000, exp="2026-12-11", prem=40_000)]
    db = _with_trades(monkeypatch, legs, spot=0.75)
    mon = asyncio.run(route.build_monitor("FIL", 0.75, today=date(2026, 11, 13)))
    p = next(p for p in mon["positions"] if p["key"] == "syn:12-11")
    assert (p["side"], p["qty"], p["effective_price"]) == ("sell", 4_000_000, pytest.approx(0.7564))
    assert (p["stop_loss"], p["take_profit"]) == (0.83, 0.67) and p["levels"]["status"] == "watching"
    # Once only: a cleared level is not re-applied.
    asyncio.run(repo.set_levels(db, "syn:12-11", "FIL", None, None, "", "desk"))
    mon = asyncio.run(route.build_monitor("FIL", 0.75, today=date(2026, 11, 13)))
    assert next(p for p in mon["positions"] if p["key"] == "syn:12-11")["stop_loss"] is None
    asyncio.run(db.close())


def test_confirmation_adopts_a_hand_booked_copy_and_heals_a_duplicate():
    async def go():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await repo.ensure_tables(db)
        fil = next(f for f in repo.CONFIRMED_FORWARDS if f["asset"] == "FIL")
        eth = next(f for f in repo.CONFIRMED_FORWARDS if f["external_ref"].startswith("ad76"))
        # Hand-booked FIL before the import: adopted, not duplicated.
        hand_fil = await repo.insert_forward(db, {**fil, "external_ref": "", "trade_date": "2026-11-09", "note": ""}, "desk")
        # ETH: an earlier import already duplicated a hand-booked copy (prod, 13 Nov).
        hand_eth = await repo.insert_forward(db, {**eth, "external_ref": "", "note": ""}, "desk")
        dup = await repo.insert_forward(db, eth, "confirmation import")
        await repo.set_levels(db, f"fwd:{dup}", "ETH", 2300.0, 1950.0, "old", "desk orders")
        # Hand-booked 55 ETH sale gets the desk levels by economics.
        e55 = await repo.insert_forward(db, dict(asset="ETH", counterparty="Flowdesk", side="sell", qty=55.0,
                                                 forward_price=1961.0, trade_date="2026-11-13",
                                                 maturity_date="2026-11-27"), "desk")
        await repo.seed_confirmed(db)
        assert await repo.seed_confirmed(db) == 0
        open_ = {r["id"]: r for r in await repo.list_forwards(db)}
        assert dup not in open_ and hand_fil in open_ and hand_eth in open_
        assert open_[hand_fil]["external_ref"] == fil["external_ref"] and open_[hand_fil]["trade_date"] == "2026-11-10"
        assert sum(1 for r in open_.values() if r["external_ref"] == fil["external_ref"]) == 1
        lv = await repo.get_levels(db)
        assert f"fwd:{dup}" not in lv
        assert (lv[f"fwd:{hand_eth}"]["stop_loss"], lv[f"fwd:{hand_eth}"]["take_profit"]) == (2236.5, 1900.0)
        assert (lv[f"fwd:{hand_fil}"]["stop_loss"], lv[f"fwd:{hand_fil}"]["take_profit"]) == (0.83, 0.67)
        assert (lv[f"fwd:{e55}"]["stop_loss"], lv[f"fwd:{e55}"]["take_profit"]) == (2236.5, 1900.0)
        await db.close()

    asyncio.run(go())


def test_round_trips_fifo_manual_and_triggers():
    L = lambda i, side, q, K, td: dict(id=i, asset="ETH", counterparty="Flowdesk", side=side, qty=q,
                                       forward_price=K, trade_date=td, maturity_date="2026-11-27")
    legs = [L(1, "sell", 2500, 2346.5, "2026-10-27"), L(2, "buy", 1250, 2100, "2026-11-09"),
            L(3, "sell", 1250, 2108.1, "2026-11-09"), L(5, "buy", 55, 1910, "2026-11-13"),
            L(7, "sell", 55, 1961, "2026-11-13")]
    lv = {k: {"stop_loss": 2236.5, "take_profit": 1900.0} for k in ("fwd:1", "fwd:3", "fwd:7")}
    rem, trips, inv = fwd.match_round_trips(legs, [], lv)
    assert rem == {1: 1195.0, 2: 0.0, 3: 1250.0, 5: 0.0, 7: 55.0} and not inv
    t12, t15 = trips
    assert (t12["open_id"], t12["close_id"], t12["qty"], t12["realised_usd"], t12["reason"]) == (1, 2, 1250, 308_125, "closed")
    assert t15["reason"] == "take_profit" and t15["realised_usd"] == pytest.approx(55 * 436.5)  # 1910 within 1% of TP
    # A hand-picked pair goes first: the 55 buy and the 55 sell close each other.
    rem, trips, inv = fwd.match_round_trips(legs, [dict(id=9, open_id=7, close_id=5, qty=55, reason="roll")], lv)
    m = next(t for t in trips if t["source"] == "manual")
    assert (m["open_id"], m["close_id"], m["realised_usd"], m["reason"]) == (5, 7, pytest.approx(55 * 51), "roll")
    assert rem[1] == 1250.0
    # Stale pairs are reported, not applied.
    _, _, inv = fwd.match_round_trips(legs, [dict(id=10, open_id=1, close_id=3, qty=10, reason="closed")], lv)
    assert inv and inv[0]["id"] == 10
    assert fwd.close_reason("sell", 2240, 2236.5, 1900) == "stop_loss"
    assert fwd.close_reason("buy", 0.95, 0.70, 0.90) == "take_profit"


def test_monitor_shows_open_residual_closed_trips_and_pnl(monkeypatch):
    db = _with_db(monkeypatch, spot=2000.0)
    mk = lambda **kw: _book(asset="ETH", maturity_date="2099-12-31", **kw)
    a = mk(side="sell", qty=2500, forward_price=2346.5, trade_date="2026-10-27")
    b = mk(side="buy", qty=1250, forward_price=2100, trade_date="2026-11-09")
    mon = asyncio.run(route.get_monitor("ETH"))
    assert [(p["forward_id"], p["qty"]) for p in mon["positions"]] == [(a["id"], 1250.0)]
    assert mon["positions"][0]["mtm"] == pytest.approx(-1250 * (2000 - 2346.5))
    assert mon["closed"][0]["realised_usd"] == pytest.approx(308_125)
    s = mon["summary"]
    assert s["pnl_total"] == pytest.approx(s["open_mtm"] + 308_125)
    # Pair by hand with a trigger; validation rejects same-side pairs.
    asyncio.run(route.pair_close(route.CloseIn(asset="ETH", open_id=a["id"], close_id=b["id"], reason="take_profit"), REQ))
    mon = asyncio.run(route.get_monitor("ETH"))
    assert mon["closed"][0]["source"] == "manual" and mon["closed"][0]["reason"] == "take_profit"
    c = mk(side="sell", qty=10, forward_price=2000, trade_date="2026-11-10")
    with pytest.raises(HTTPException):
        asyncio.run(route.pair_close(route.CloseIn(asset="ETH", open_id=a["id"], close_id=c["id"]), REQ))
    asyncio.run(route.unpair_close(mon["closed"][0]["close_row_id"]))
    asyncio.run(db.close())
