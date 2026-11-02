"""Sandbox screen: price and compare dealer offers on a token we don't trade
yet (ZAMA first) and keep a what-if book of them.

Isolated on purpose: offers live in sandbox_offers (data/sandbox_repository.py)
and nothing here feeds the real trades, P&L, collateral or optimizer.

Market data comes from the exchanges (market_data/token_market.py); there are
no listed options, so the ATM vol defaults to realised vol and the skew is a
setting (pricing/sandbox.py). Every pricing input can be overridden per
request for what-ifs without saving it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from plgo_options.data import sandbox_repository as repo
from plgo_options.data.database import get_db
from plgo_options.market_data import token_market as tm
from plgo_options.pricing import sandbox as sb

router = APIRouter()
log = logging.getLogger(__name__)

SCENARIO_MULTS = (0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5)
SPOT_MOVE_WARN = 0.05


def _user(request: Request | None) -> str:
    if request is None or "session" not in request.scope:
        return ""
    return str((request.session.get("user") or {}).get("email") or "")


def _asset(asset: str) -> str:
    try:
        return tm.check_symbol(asset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _date(s, field: str) -> date:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")


async def _db():
    db = await get_db()
    await repo.ensure_tables(db)
    return db


def schedule_for(settings: dict) -> list[dict]:
    if not settings.get("holdings") or not settings.get("cliff_date"):
        return []
    return sb.unlock_schedule(float(settings["holdings"]), _date(settings["cliff_date"], "cliff_date"),
                              float(settings.get("cliff_pct") or 0), int(settings.get("linear_months") or 0))


async def _inputs(asset: str, spot: float | None, atm_vol: float | None, skew_pts: float | None,
                  carry: float | None, collateral_rate: float | None) -> dict:
    """Pricing inputs: request overrides > saved settings > live market."""
    db = await _db()
    st = await repo.get_settings(db, asset)
    market = None
    if spot is None:
        try:
            market = await tm.get_spot(asset)
            spot = market["spot"]
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{asset} spot unavailable: {e}")
    vols = {}
    vol_source = "override"
    if atm_vol is None:
        atm_vol = st.get("atm_vol")
        vol_source = "settings"
    if atm_vol is None:
        try:
            vols = (await tm.get_history(asset))["vols"]
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"no {asset} history for realised vol, set a vol: {e}")
        win = st.get("vol_window") or "90d"
        atm_vol = vols.get(win) or vols.get("all")
        vol_source = f"realised {win}"
    return {
        "settings": st, "spot": float(spot), "atm_vol": float(atm_vol), "vol_source": vol_source,
        "skew_pts": float(st["skew_pts"] if skew_pts is None else skew_pts),
        "carry": float(st["carry"] if carry is None else carry),
        "collateral_rate": float(st["collateral_rate"] if collateral_rate is None else collateral_rate),
        "spot_quotes": market,
    }


def _warnings(o: dict, spot: float, schedule: list[dict], cliff: date | None) -> list[str]:
    w = []
    expiry = _date(o["expiry"], "expiry")
    Q = float(o.get("hedge_qty") or 0) or sb.hedge_qty([sb.check_leg(l) for l in o["legs"]])
    if schedule and o["settlement"] in ("physical", "tbc"):
        unlocked = sb.unlocked_by(schedule, expiry)
        if cliff and expiry < cliff:
            w.append(f"Settles {(cliff - expiry).days} day(s) before the first unlock ({cliff.isoformat()}): "
                     f"nothing is unlocked to deliver. Needs cash settlement or a date on/after the unlock.")
        elif Q > unlocked * (1 + 1e-9):
            w.append(f"Hedges {Q / 1e6:,.1f}m tokens but only {unlocked / 1e6:,.1f}m are unlocked by {expiry.isoformat()}.")
    if o["settlement"] == "tbc":
        w.append("Settlement (cash or physical) not confirmed yet.")
    if o.get("ref_spot") and abs(spot / float(o["ref_spot"]) - 1) > SPOT_MOVE_WARN:
        w.append(f"Spot is {spot / float(o['ref_spot']) - 1:+.1%} vs the quote's reference {float(o['ref_spot']):g}: "
                 f"a 'zero-cost' quote at that reference is not zero-cost now; ask for a refresh.")
    if any(l["kind"] == "call" and l["side"] == "sell" for l in o["legs"]) and o.get("collateral_usd") is None:
        w.append("Short calls: collateral amount not stated.")
    return w


def evaluate(o: dict, inp: dict, today: date, schedule: list[dict]) -> dict:
    spot, vol, skew, carry, cr = inp["spot"], inp["atm_vol"], inp["skew_pts"], inp["carry"], inp["collateral_rate"]
    st = inp["settings"]
    cliff = _date(st["cliff_date"], "cliff_date") if st.get("cliff_date") else None
    live = sb.value_offer(o, spot, vol, skew, carry, cr, today)
    out = {**o, "live": live,
           "floor_cap": sb.floor_cap(o, cr, today),
           "stats": sb.distribution_stats(o, spot, vol, carry, cr, today),
           "probabilities": sb.option_probabilities(o, spot, vol, carry, today),
           "implied_skew": sb.implied_skew(o, spot, vol, carry, cr, today),
           "warnings": _warnings(o, spot, schedule, cliff)}
    ref = float(o.get("ref_spot") or 0)
    if ref > 0:
        out["at_ref"] = sb.value_offer(o, ref, vol, skew, carry, cr, today)
        out["implied_skew_at_ref"] = sb.implied_skew(o, ref, vol, carry, cr, today)
    fwd_legs = [l for l in o["legs"] if l["kind"] == "forward"]
    if fwd_legs:
        K = fwd_legs[0]["strike"]
        out["implied_carry"] = sb.implied_carry(K, spot, live["T"])
        if ref > 0:
            out["implied_carry_at_ref"] = sb.implied_carry(K, ref, live["T"])
    return out


def _book(evald: list[dict], schedule: list[dict]) -> dict:
    booked = [e for e in evald if e["status"] == "booked"]
    tot = {k: sum(e["live"][k] for e in booked) for k in ("net_value", "legs_value", "delta", "vega", "hedge_qty", "collateral_cost")}
    tot["collateral_usd"] = sum(float(e.get("collateral_usd") or 0) for e in booked)
    tot["count"] = len(booked)
    # Each booked offer hedges the unlock nearest its expiry.
    coverage = [{"date": t["date"].isoformat(), "unlock": t["qty"], "hedged": 0.0} for t in schedule]
    for e in booked:
        if schedule:
            x = _date(e["expiry"], "expiry")
            i = min(range(len(schedule)), key=lambda j: abs((schedule[j]["date"] - x).days))
            coverage[i]["hedged"] += e["live"]["hedge_qty"]
    return {**tot, "coverage": coverage}


@router.get("/market")
async def market(asset: str = "ZAMA"):
    """Live spot (every venue), daily history, realised vols, perp funding,
    the saved settings and the vesting schedule."""
    a = _asset(asset)
    db = await _db()
    st = await repo.get_settings(db, a)
    try:
        spot = await tm.get_spot(a)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{a} spot unavailable: {e}")
    try:
        hist = await tm.get_history(a)
    except Exception as e:
        log.warning("sandbox history for %s unavailable: %s", a, e)
        hist = {"asset": a, "dates": [], "closes": [], "vols": {}, "error": str(e)}
    funding = await tm.get_funding(a)
    sched = schedule_for(st)
    return {"asset": a, "spot": spot, "history": hist, "funding": funding, "settings": st,
            "schedule": [{**t, "date": t["date"].isoformat()} for t in sched]}


@router.get("/offers")
async def offers(asset: str = "ZAMA", spot: float | None = None, atm_vol: float | None = None,
                 skew_pts: float | None = None, carry: float | None = None, collateral_rate: float | None = None):
    """Every offer valued at live spot (and at its quote reference), with its
    payoff curve, scenarios and warnings; plus the sandbox book."""
    a = _asset(asset)
    inp = await _inputs(a, spot, atm_vol, skew_pts, carry, collateral_rate)
    db = await _db()
    rows = await repo.list_offers(db, a)
    today = date.today()
    sched = schedule_for(inp["settings"])
    evald = []
    for o in rows:
        try:
            evald.append(evaluate(o, inp, today, sched))
        except Exception as e:  # one malformed offer must not hide the others
            evald.append({**o, "error": str(e)})
    S = inp["spot"]
    grid = np.linspace(0.15 * S, 2.6 * S, 141)
    scen = np.array([m * S for m in SCENARIO_MULTS])
    for e in evald:
        if "error" in e:
            continue
        e["curve"] = sb.effective_price(e, grid, inp["collateral_rate"], today).tolist()
        e["scenarios"] = sb.effective_price(e, scen, inp["collateral_rate"], today).tolist()
    return {"asset": a, "as_of": datetime.now(timezone.utc).isoformat(),
            "inputs": {k: inp[k] for k in ("spot", "atm_vol", "vol_source", "skew_pts", "carry", "collateral_rate")},
            "spot_quotes": inp["spot_quotes"], "grid": grid.tolist(), "scenario_prices": scen.tolist(),
            "offers": evald, "book": _book([e for e in evald if "error" not in e], sched)}


class LegIn(BaseModel):
    kind: str
    side: str
    qty: float = Field(gt=0)
    strike: float = Field(gt=0)


class OfferIn(BaseModel):
    asset: str = "ZAMA"
    name: str
    counterparty: str = ""
    quote_date: str | None = None
    ref_spot: float | None = Field(default=None, gt=0)
    expiry: str
    settlement: str = "tbc"
    premium_usd: float = 0.0
    collateral_usd: float | None = Field(default=None, ge=0)
    collateral_note: str = ""
    hedge_qty: float | None = Field(default=None, gt=0)
    legs: list[LegIn]
    status: str = "indicative"
    notes: str = ""


def _clean(o: dict) -> dict:
    o["asset"] = _asset(o.get("asset") or "ZAMA")
    o["name"] = (o.get("name") or "").strip()
    if not o["name"]:
        raise HTTPException(status_code=400, detail="name is required")
    o["expiry"] = _date(o["expiry"], "expiry").isoformat()
    if o.get("quote_date"):
        o["quote_date"] = _date(o["quote_date"], "quote_date").isoformat()
    if o.get("settlement") not in repo.SETTLEMENTS:
        raise HTTPException(status_code=400, detail=f"settlement must be one of {repo.SETTLEMENTS}")
    if o.get("status") not in repo.STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {repo.STATUSES}")
    if not o.get("legs"):
        raise HTTPException(status_code=400, detail="an offer needs at least one leg")
    try:
        o["legs"] = [sb.check_leg(l) for l in o["legs"]]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return o


@router.post("/offers")
async def create_offer(body: OfferIn, request: Request = None):
    o = _clean(body.model_dump())
    db = await _db()
    oid = await repo.insert_offer(db, o, _user(request))
    return {"id": oid}


@router.put("/offers/{oid}")
async def edit_offer(oid: int, body: OfferIn, request: Request = None):
    db = await _db()
    if not await repo.get_offer(db, oid):
        raise HTTPException(status_code=404, detail="offer not found")
    await repo.update_offer(db, oid, _clean(body.model_dump()), _user(request))
    return {"id": oid}


class StatusIn(BaseModel):
    status: str


@router.post("/offers/{oid}/status")
async def set_status(oid: int, body: StatusIn, request: Request = None):
    if body.status not in repo.STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {repo.STATUSES}")
    db = await _db()
    if not await repo.get_offer(db, oid):
        raise HTTPException(status_code=404, detail="offer not found")
    await repo.update_offer(db, oid, {"status": body.status}, _user(request))
    return {"id": oid, "status": body.status}


@router.delete("/offers/{oid}")
async def remove_offer(oid: int, request: Request = None):
    db = await _db()
    if not await repo.get_offer(db, oid):
        raise HTTPException(status_code=404, detail="offer not found")
    await repo.delete_offer(db, oid, _user(request))
    return {"deleted": oid}


class SettingsIn(BaseModel):
    atm_vol: float | None = Field(default=None, gt=0, le=10)
    vol_window: str = "90d"
    skew_pts: float = Field(default=0.0, ge=-150, le=150)
    carry: float = Field(default=0.0, ge=-5, le=5)
    collateral_rate: float = Field(default=0.05, ge=0, le=1)
    holdings: float = Field(default=0.0, ge=0)
    cliff_date: str | None = None
    cliff_pct: float = Field(default=0.10, ge=0, le=1)
    linear_months: int = Field(default=12, ge=0, le=120)


@router.put("/settings")
async def put_settings(body: SettingsIn, asset: str = "ZAMA", request: Request = None):
    a = _asset(asset)
    d = body.model_dump()
    if d["vol_window"] not in [f"{n}d" for n in tm.VOL_WINDOWS] + ["all"]:
        raise HTTPException(status_code=400, detail="vol_window must be 30d, 60d, 90d, 180d or all")
    if d.get("cliff_date"):
        d["cliff_date"] = _date(d["cliff_date"], "cliff_date").isoformat()
    db = await _db()
    await repo.save_settings(db, a, d, _user(request))
    return await repo.get_settings(db, a)


class SolveIn(BaseModel):
    asset: str = "ZAMA"
    solve_for: str = "call"
    fixed_strike: float = Field(gt=0)
    expiry: str
    spot: float | None = Field(default=None, gt=0)
    atm_vol: float | None = Field(default=None, gt=0)
    skew_pts: float | None = None
    carry: float | None = None
    target_premium_per_token: float = 0.0
    call_ratio: float = Field(default=1.0, gt=0)


@router.post("/solve")
async def solve(body: SolveIn):
    """Collar strike solver: the call (or put) strike that makes the collar
    cost exactly the target premium (0 = zero cost) at our vol and skew."""
    a = _asset(body.asset)
    if body.solve_for not in ("call", "put"):
        raise HTTPException(status_code=400, detail="solve_for must be call or put")
    inp = await _inputs(a, body.spot, body.atm_vol, body.skew_pts, body.carry, None)
    T = sb.years_to(_date(body.expiry, "expiry"), date.today())
    K = sb.solve_strike(body.solve_for, body.fixed_strike, inp["spot"], T, inp["atm_vol"], inp["skew_pts"],
                        inp["carry"], body.target_premium_per_token, body.call_ratio)
    F = sb.forward_price(inp["spot"], inp["carry"], T)
    res = {"solve_for": body.solve_for, "strike": K, "forward": F, "T": T,
           "inputs": {k: inp[k] for k in ("spot", "atm_vol", "vol_source", "skew_pts", "carry")}}
    if K:
        kp, kc = (body.fixed_strike, K) if body.solve_for == "call" else (K, body.fixed_strike)
        res.update({"put_strike": kp, "call_strike": kc,
                    "put_price": sb.black(F, kp, T, sb.vol_at(kp, F, inp["atm_vol"], inp["skew_pts"]), "put"),
                    "call_price": sb.black(F, kc, T, sb.vol_at(kc, F, inp["atm_vol"], inp["skew_pts"]), "call"),
                    "put_pct_spot": kp / inp["spot"], "call_pct_spot": kc / inp["spot"]})
    return res


@router.get("/program")
async def program(asset: str = "ZAMA", put_pct: float = 0.625, spot: float | None = None,
                  atm_vol: float | None = None, skew_pts: float | None = None, carry: float | None = None):
    """A zero-cost collar for every unlock in the vesting schedule: put at
    put_pct x spot, call solved at our vol. What the whole program should
    look like before dealers quote it."""
    a = _asset(asset)
    if not 0 < put_pct < 1.5:
        raise HTTPException(status_code=400, detail="put_pct must be between 0 and 1.5")
    inp = await _inputs(a, spot, atm_vol, skew_pts, carry, None)
    sched = schedule_for(inp["settings"])
    if not sched:
        raise HTTPException(status_code=400, detail="set holdings and the cliff date first")
    today = date.today()
    S, out = inp["spot"], []
    for t in sched:
        T = sb.years_to(t["date"], today)
        kp = put_pct * S
        kc = sb.solve_strike("call", kp, S, T, inp["atm_vol"], inp["skew_pts"], inp["carry"])
        out.append({"date": t["date"].isoformat(), "days": (t["date"] - today).days, "qty": t["qty"],
                    "put_strike": kp, "call_strike": kc, "floor_usd": t["qty"] * kp,
                    "cap_usd": t["qty"] * kc if kc else None})
    return {"asset": a, "put_pct": put_pct,
            "inputs": {k: inp[k] for k in ("spot", "atm_vol", "vol_source", "skew_pts", "carry")},
            "tranches": out, "floor_total": sum(r["floor_usd"] for r in out)}
