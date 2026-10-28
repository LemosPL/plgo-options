"""Collateral Platform / Greeks report, checked against the counterparty's own
"Protocol Labs Positions" ETH report (Flowdesk, Oct 2026): same book, spot
2451.53, Dec26 at ~50% vol, 66 days out."""

import asyncio

import pytest

from plgo_options.web.routes import risk_report as rr
from plgo_options.web.routes.portfolio import bs_greeks

S, DTE, IV = 2451.53, 66, 50.0
BOOK = [(8000, 1780, "P"), (-9150, 1780, "C"), (1250, 2100, "P"), (1000, 2200, "P"), (1000, 2600, "C"),
        (1250, 2700, "C"), (1500, 2800, "C"), (1750, 2900, "C"), (6000, 3020, "C"), (-9150, 3020, "P")]


def _fake_portfolio():
    positions = []
    for q, k, opt in BOOK:
        d, g, th, vg = bs_greeks(S, k, DTE / 365.25, 0.0, IV / 100, opt)
        positions.append({"counterparty": "Flowdesk", "net_qty": q, "days_remaining": DTE, "opt": opt,
                          "strike": k, "expiry": "2026-12-25", "iv_pct": IV, "current_mtm": 0.0,
                          "delta": d, "gamma": g, "theta": th, "vega": vg})
    positions.append({"counterparty": "Wave", "net_qty": 500, "days_remaining": DTE, "opt": "C",
                      "strike": 3000, "expiry": "2026-12-25", "iv_pct": IV, "delta": 0.3})
    return {"eth_spot": S, "positions": positions}


def _run(monkeypatch, counterparty="Flowdesk", mode="flowdesk"):
    async def fake_gather(asset):
        return _fake_portfolio() if asset == "ETH" else None

    async def fake_posted(cp):
        return {"ETH": 2000.0}

    async def fake_ctx(asset):
        raise RuntimeError("no market data in tests")

    monkeypatch.setattr(rr, "_gather_portfolio", fake_gather)
    monkeypatch.setattr(rr, "_posted_collateral", fake_posted)
    monkeypatch.setattr(rr, "build_market_context", fake_ctx)
    sec = asyncio.run(rr._asset_section("ETH", counterparty, 2000.0, mode))
    return sec


def test_positions_and_greeks_like_the_report(monkeypatch):
    sec = _run(monkeypatch)
    rows = sec["rows"]
    assert rows[0]["symbol"] == "ETH Spot Collateral" and rows[0]["delta"] == 2000
    assert "ETHUSDC-25DEC26-1600-1780-P-USDC" in [r["symbol"] for r in rows]
    assert len(rows) == 1 + len(BOOK)                      # Wave's leg is not Flowdesk's
    t = sec["totals"]
    # Flat 50% approximates their smile: PDF 2,820 / 3.48 / -5,363 / 16,460.
    assert 2600 < t["delta"] < 3000
    assert 2.8 < t["gamma"] < 3.8
    assert -7000 < t["theta"] < -5000
    assert 15000 < t["vega"] < 18500
    assert abs(t["delta_usd"] - t["delta"] * S) < 1
    assert abs(t["gamma_usd"] - t["gamma"] * S * S / 100) < 1
    assert sec["bullets"][0].startswith("Long") and "2,000 ETH collateral" in sec["bullets"][0]


def test_slides_match_the_report_on_spot_moves(monkeypatch):
    s = _run(monkeypatch)["slides"]
    col = s["vol_shocks"].index(0.0)
    at = {ds: s["pnl"][i][col] for i, ds in enumerate(s["spot_shocks"])}
    assert at[0.0] == 0
    # PDF: +10% 782,119 · -10% -577,794 · -50% -1,589,855 · +100% 10,165,902
    assert abs(at[0.10] - 782_119) < 60_000
    assert abs(at[-0.10] - (-577_794)) < 60_000
    assert abs(at[-0.50] - (-1_589_855)) < 40_000
    assert abs(at[1.00] - 10_165_902) < 150_000
    # Long vega: vol up makes money at unchanged spot.
    i0 = s["spot_shocks"].index(0.0)
    assert s["pnl"][i0][-1] > 0 > s["pnl"][i0][0]


def test_all_counterparties_aggregates(monkeypatch):
    sec = _run(monkeypatch, counterparty=None)
    assert len(sec["rows"]) == 1 + len(BOOK) + 1


def test_notes_save_and_load_per_counterparty(monkeypatch):
    import aiosqlite
    from starlette.requests import Request
    from plgo_options.data.database import COUNTERPARTY_NOTES_SCHEMA

    async def go():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(COUNTERPARTY_NOTES_SCHEMA)

        async def fake_db():
            return db

        monkeypatch.setattr(rr, "get_db", fake_db)
        req = Request({"type": "http", "session": {"user": {"email": "desk@example.com"}}})
        assert (await rr.get_notes("Flowdesk"))["notes"] == ""
        saved = await rr.put_notes(rr.NotesUpdate(counterparty="Flowdesk", notes="Need 2m FIL top-up"), req)
        assert saved["notes"] == "Need 2m FIL top-up" and saved["updated_by"] == "desk@example.com"
        await rr.put_notes(rr.NotesUpdate(counterparty="flowdesk", notes="Topped up"), req)
        assert (await rr.get_notes("FLOWDESK"))["notes"] == "Topped up"     # one row per counterparty
        assert (await rr.get_notes("KeyRock"))["notes"] == ""
        await db.close()

    asyncio.run(go())


def test_vol_columns_match_flowdesk_slides(monkeypatch):
    # Their ETH report, spot unchanged: -15..+15 -> -405,099 .. 400,466. Our
    # book uses one flat 50% vol where theirs has a skew, hence the tolerance.
    pdf = [-405_099, -275_123, -137_701, 0, 135_818, 269_284, 400_466]
    s = _run(monkeypatch)["slides"]
    ours = s["pnl"][s["spot_shocks"].index(0.0)]
    for o, p in zip(ours, pdf):
        assert abs(o - p) <= max(0.05 * abs(p), 1), (ours, pdf)
    # A plain vol-point shift is ~40% short of theirs.
    plain = _run(monkeypatch, mode="points")["slides"]["pnl"][s["spot_shocks"].index(0.0)]
    assert plain[-1] < 0.7 * pdf[-1]


def test_optimizer_ladder_before_matches_the_platform_and_trades_move_after(monkeypatch):
    positions = _fake_portfolio()["positions"]
    sec = _run(monkeypatch)
    trades = [
        # Full unwind of a held leg (at a different mark): nets to nothing.
        {"counterparty": "flowdesk", "opt": "P", "strike": 1780, "expiry": "2026-12-25", "dte": DTE,
         "qty": -8000, "iv_pct": 70.0},
        # New perp hedge.
        {"counterparty": "Flowdesk", "opt": "F", "strike": S, "expiry": "2099-12-31", "dte": 0,
         "qty": -100, "iv_pct": 0.0},
    ]
    lad = rr.optimizer_ladder(positions, trades, S, "ETH", {"Flowdesk": {"ETH": 2000.0, "USDC": 1e6}},
                              {"Flowdesk": {"ETH": 1500.0}}, ["Flowdesk"])
    assert [c["counterparty"] for c in lad["counterparties"]] == ["Flowdesk"]
    fd = lad["counterparties"][0]
    assert (fd["collateral_before"], fd["collateral_after"]) == (2000.0, 1500.0)
    g = fd["grids"]["flowdesk"]
    assert g["before"] == sec["slides"]["pnl"]

    legs = [{"net_qty": q, "strike": k, "opt": o, "T": DTE / 365.25, "sigma": IV / 100}
            for q, k, o in BOOK if (q, k, o) != (8000, 1780, "P")]
    legs.append({"net_qty": -100, "strike": 0.0, "opt": "PERP", "T": 0.0, "sigma": 0.0})
    assert g["after"] == rr.slides_grid(legs, S, 1500.0, "flowdesk")


def test_optimizer_ladder_without_a_selection_lists_every_counterparty():
    positions = _fake_portfolio()["positions"]
    lad = rr.optimizer_ladder(positions, [], S, "ETH", {"G20": {"ETH": 10.0}, "KeyRock": {"USDC": 5.0}})
    names = [c["counterparty"] for c in lad["counterparties"]]
    # KeyRock: no legs and no collateral in this asset, nothing to show.
    assert names == ["Flowdesk", "G20", "Wave"]
    for c in lad["counterparties"]:
        assert set(c["grids"]) == set(rr.VOL_MODES)
        assert c["grids"]["points"]["before"] == c["grids"]["points"]["after"]


def test_forwards_join_the_report(monkeypatch):
    port = _fake_portfolio()
    port["forwards"] = {"trades": [{"counterparty": "Flowdesk", "signed_qty": -1000.0, "forward_price": 2500.0,
                                    "maturity_date": "2026-12-31", "mtm": 48_470.0, "delta": -1000.0,
                                    "delta_usd": -1000.0 * S, "theta": 0.0}]}

    async def fake_gather(asset):
        return port if asset == "ETH" else None

    async def fake_ctx(asset):
        raise RuntimeError("no market data in tests")

    monkeypatch.setattr(rr, "_gather_portfolio", fake_gather)
    monkeypatch.setattr(rr, "build_market_context", fake_ctx)
    with_fwd = asyncio.run(rr._asset_section("ETH", "Flowdesk", 2000.0, "points"))
    fwd_rows = [r for r in with_fwd["rows"] if r["kind"] == "forward"]
    assert len(fwd_rows) == 1 and fwd_rows[0]["symbol"].startswith("ETHUSDC-31DEC26-FWD")
    port.pop("forwards")
    without = asyncio.run(rr._asset_section("ETH", "Flowdesk", 2000.0, "points"))
    assert with_fwd["totals"]["delta"] == pytest.approx(without["totals"]["delta"] - 1000.0)
    # Short 1,000 ETH forward: at +10% spot the ladder loses 1,000 x 10% of spot more.
    i = with_fwd["slides"]["spot_shocks"].index(0.10)
    j = with_fwd["slides"]["vol_shocks"].index(0.0)
    assert with_fwd["slides"]["pnl"][i][j] - without["slides"]["pnl"][i][j] == pytest.approx(-1000.0 * S * 0.10, abs=1)
