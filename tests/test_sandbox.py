"""Sandbox: offer pricing, the collar solver, vesting checks, and the routes
(isolated tables, seeded dealer offers)."""

import asyncio
from datetime import date

import aiosqlite
import numpy as np
import pytest
from fastapi import HTTPException

from plgo_options.data import sandbox_repository as repo
from plgo_options.market_data import token_market as tm
from plgo_options.pricing import sandbox as sb
from plgo_options.web.routes import sandbox as route

TODAY = date(2026, 11, 2)
COLLAR = {"expiry": "2027-02-03", "premium_usd": 0.0, "collateral_usd": None, "legs": [
    {"kind": "put", "side": "buy", "qty": 1_000_000, "strike": 0.05},
    {"kind": "call", "side": "sell", "qty": 1_000_000, "strike": 0.10}]}


def test_black_parity_and_skew():
    F, K, T = 0.08, 0.07, 0.25
    c, p = sb.black(F, K, T, 1.2, "call"), sb.black(F, K, T, 1.2, "put")
    assert c - p == pytest.approx(F - K, abs=1e-12)
    # Positive skew: lower strikes get more vol, the forward strike gets ATM.
    assert sb.vol_at(F, F, 1.0, 10) == pytest.approx(1.0)
    assert sb.vol_at(F * 0.9, F, 1.0, 10) > 1.0 > sb.vol_at(F * 1.1, F, 1.0, 10)
    assert sb.vol_at(F * 50, F, 0.2, 50) == sb.MIN_VOL


def test_forward_value_and_implied_carry():
    fwd = {"expiry": "2027-01-29", "legs": [{"kind": "forward", "side": "sell", "qty": 1_000_000, "strike": 0.0754}]}
    v = sb.value_offer(fwd, 0.07, 1.2, 0, 0.0, 0.05, TODAY)
    assert v["net_value"] == pytest.approx(1_000_000 * (0.0754 - 0.07))
    assert v["delta"] == pytest.approx(-1_000_000)
    fc = sb.floor_cap(fwd, 0.0, TODAY)
    assert fc["floor"] == pytest.approx(0.0754) and fc["cap"] == pytest.approx(0.0754)
    T = v["T"]
    assert sb.implied_carry(0.0754, 0.0754, T) == pytest.approx(0.0)
    assert sb.value_offer(fwd, 0.07, 1.2, 0, sb.implied_carry(0.0754, 0.07, T), 0, TODAY)["net_value"] == pytest.approx(0, abs=1e-6)


def test_collar_effective_price_floor_and_cap():
    eff = sb.effective_price(COLLAR, np.array([0.01, 0.07, 0.5]), 0.0, TODAY)
    assert list(np.round(eff, 10)) == [0.05, 0.07, 0.10]
    # Collateral cost lowers every outcome by cost / qty.
    c = {**COLLAR, "collateral_usd": 1_000_000}
    T = sb.years_to(date(2027, 2, 3), TODAY)
    assert sb.floor_cap(c, 0.10, TODAY)["floor"] == pytest.approx(0.05 - 1_000_000 * 0.10 * T / 1_000_000)


def test_solver_zero_cost_and_implied_skew():
    T = sb.years_to(date(2027, 2, 3), TODAY)
    kc = sb.solve_strike("call", 0.05, 0.08, T, 1.2, 0.0, 0.0)
    F = 0.08
    assert sb.black(F, kc, T, 1.2, "call") == pytest.approx(sb.black(F, 0.05, T, 1.2, "put"), rel=1e-6)
    kp = sb.solve_strike("put", kc, 0.08, T, 1.2, 0.0, 0.0)
    assert kp == pytest.approx(0.05, rel=1e-5)
    # A collar fair at our vol is worth ~0, and needs no skew to be fair.
    fair = {**COLLAR, "legs": [{**COLLAR["legs"][0]}, {**COLLAR["legs"][1], "strike": kc}]}
    assert sb.value_offer(fair, 0.08, 1.2, 0, 0, 0, TODAY)["net_value"] == pytest.approx(0, abs=1e-2)
    assert sb.implied_skew(fair, 0.08, 1.2, 0, 0, TODAY) == pytest.approx(0, abs=1e-2)
    # The 0.05/0.10 quote at 0.08 caps us far below fair: only put skew rescues it.
    assert kc > 0.10
    assert sb.implied_skew(COLLAR, 0.08, 1.2, 0, 0, TODAY) > 5
    # 2 calls per put: each call funds half, so the strike is higher.
    assert sb.solve_strike("call", 0.05, 0.08, T, 1.2, 0, 0, call_ratio=2) > kc


def test_unlock_schedule():
    s = sb.unlock_schedule(366_000_000, date(2027, 2, 2), 0.10, 12)
    assert len(s) == 13 and s[0]["qty"] == pytest.approx(36_600_000)
    assert s[1]["date"] == date(2027, 3, 2) and s[-1]["date"] == date(2028, 2, 2)
    assert s[-1]["cumulative"] == pytest.approx(366_000_000)
    assert sb.unlocked_by(s, date(2027, 1, 29)) == 0
    assert sb.unlocked_by(s, date(2027, 2, 3)) == pytest.approx(36_600_000)


def test_consensus_drops_outliers():
    out = tm.consensus([{"source": "A", "price": 0.070}, {"source": "B", "price": 0.0702},
                        {"source": "C", "price": 0.090}, {"source": "D", "price": None, "error": "x"}])
    assert out["n_used"] == 2 and out["spot"] == pytest.approx(0.0701)
    with pytest.raises(RuntimeError):
        tm.consensus([{"source": "A", "price": None, "error": "down"}])
    with pytest.raises(ValueError):
        tm.check_symbol("ZA MA;")


@pytest.fixture
def db(monkeypatch):
    async def make():
        d = await aiosqlite.connect(":memory:")
        d.row_factory = aiosqlite.Row
        await repo.ensure_tables(d)
        await repo.seed(d)
        return d

    d = asyncio.run(make())

    async def get_db():
        return d

    async def spot(asset):
        return {"spot": 0.07, "quotes": [], "n_used": 1}

    async def hist(asset):
        return {"vols": {"90d": 1.2, "all": 1.2}, "dates": [], "closes": []}

    monkeypatch.setattr(route, "get_db", get_db)
    monkeypatch.setattr(tm, "get_spot", spot)
    monkeypatch.setattr(tm, "get_history", hist)
    yield d
    asyncio.run(d.close())


def test_seeded_offers_and_warnings(db):
    out = asyncio.run(route.offers("ZAMA"))
    assert out["inputs"]["atm_vol"] == 1.2 and out["inputs"]["vol_source"] == "realised 90d"
    by = {o["counterparty"] + ":" + o["name"]: o for o in out["offers"]}
    fx = next(o for o in out["offers"] if o["counterparty"] == "FalconX")
    assert fx["live"]["hedge_qty"] == pytest.approx(2_000_000 / 0.0754, rel=1e-6)
    assert any("before the first unlock" in w for w in fx["warnings"])
    fd = by["Flowdesk:Zero-cost collar 0.05P / 0.10C"]
    assert fd["at_ref"]["net_value"] < 0                # costs us at their own reference
    assert any("vs the quote's reference" in w for w in fd["warnings"])
    assert len(fd["curve"]) == len(out["grid"]) and len(fd["scenarios"]) == len(out["scenario_prices"])
    dist = out["distributions"]["2027-02-03"]
    assert len(dist["density"]) == len(dist["cdf"]) == len(out["grid"])
    pc = dist["percentiles"]
    assert pc["p05"] < pc["p25"] < pc["p50"] < pc["p75"] < pc["p95"]
    assert sum(fd["stats"]["zones"][k] for k in ("protected", "between", "capped")) == pytest.approx(1.0)
    # Re-seeding never duplicates, and a delete stays deleted.
    asyncio.run(route.remove_offer(fd["id"]))
    assert asyncio.run(repo.seed(db)) == 0
    assert all(o["id"] != fd["id"] for o in asyncio.run(route.offers("ZAMA"))["offers"])


def test_create_book_and_validate(db):
    body = route.OfferIn(name="Put", counterparty="X", expiry="2027-02-03", settlement="cash",
                         legs=[route.LegIn(kind="put", side="buy", qty=1e6, strike=0.05)], premium_usd=-5000)
    oid = asyncio.run(route.create_offer(body))["id"]
    asyncio.run(route.set_status(oid, route.StatusIn(status="booked")))
    out = asyncio.run(route.offers("ZAMA"))
    assert out["book"]["count"] == 1 and out["book"]["hedge_qty"] == 1e6
    assert out["book"]["coverage"][0]["hedged"] == 1e6
    with pytest.raises(HTTPException):
        asyncio.run(route.create_offer(route.OfferIn(name="bad", expiry="2027-02-03", legs=[
            route.LegIn(kind="swap", side="buy", qty=1, strike=1)])))
    with pytest.raises(HTTPException):
        asyncio.run(route.set_status(oid, route.StatusIn(status="live")))


def test_settings_overrides_and_program(db):
    asyncio.run(route.put_settings(route.SettingsIn(atm_vol=0.9, skew_pts=5, holdings=366e6,
                                                    cliff_date="2027-02-02", cliff_pct=0.1, linear_months=12)))
    out = asyncio.run(route.offers("ZAMA"))
    assert out["inputs"]["atm_vol"] == 0.9 and out["inputs"]["skew_pts"] == 5
    # A request override wins over the saved settings and isn't saved.
    assert asyncio.run(route.offers("ZAMA", atm_vol=1.5))["inputs"]["atm_vol"] == 1.5
    assert asyncio.run(route.offers("ZAMA"))["inputs"]["atm_vol"] == 0.9
    p = asyncio.run(route.program("ZAMA", put_pct=0.7))
    assert len(p["tranches"]) == 13 and all(t["call_strike"] > 0.07 for t in p["tranches"])
    assert p["floor_total"] == pytest.approx(366e6 * 0.049)
    s = asyncio.run(route.solve(route.SolveIn(fixed_strike=0.05, expiry="2027-02-03")))
    assert s["call_strike"] > s["put_strike"]


def test_terminal_distribution_matches_pricing():
    T = sb.years_to(date(2027, 2, 3), TODAY)
    flat = sb.TerminalDistribution(0.07, 1.3, 0.0, 0.0, T)
    sd = 1.3 * T ** 0.5
    from scipy.stats import norm
    for K in (0.03, 0.05, 0.1):
        assert float(flat.cdf(K)) == pytest.approx(norm.cdf((np.log(K / 0.07) + 0.5 * sd * sd) / sd), abs=1e-4)
    # With skew the distribution stays centred on the forward (no smile arbitrage)
    for skew in (-5, 5, 10):
        d = sb.TerminalDistribution(0.07, 1.3, skew, 0.0, T)
        assert d.samples().mean() == pytest.approx(0.07, rel=2e-3)
        # and a put's price equals its expected payoff under it
        st = d.samples(20000)
        put = sb.black(d.F, 0.05, T, sb.vol_at(0.05, d.F, 1.3, skew), "put")
        assert np.maximum(0.05 - st, 0).mean() == pytest.approx(put, rel=5e-3)
    z = sb.landing_zones(COLLAR, flat)
    assert z["protected"] + z["between"] + z["capped"] == pytest.approx(1.0)
    assert z["protected"] == pytest.approx(float(flat.cdf(0.05)))
    fz = sb.landing_zones({"legs": [{"kind": "forward", "side": "sell", "qty": 1, "strike": 0.0754}]}, flat)
    assert fz["below"] + fz["above"] == pytest.approx(1.0)
