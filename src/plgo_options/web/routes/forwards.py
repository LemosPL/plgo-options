"""Forwards screen: book, amend, settle, reconcile and value cash-settled
forwards (pricing/forwards.py), and the forward book every risk view reads
(build_forward_book: portfolio /pnl, collateral, optimizer, risk report)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from plgo_options.data import forward_repository as repo
from plgo_options.data.database import get_db
from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.pricing import forwards as fwd

router = APIRouter()
log = logging.getLogger(__name__)
_client = DeribitClient()

ASSETS = ("ETH", "FIL")
# Reconciliation tolerances: a field differing by more than this is a break.
QTY_TOL = 1e-6           # relative
PRICE_TOL = 1e-4         # relative (0.01%)
MTM_TOL_USD = 1_000.0    # or 1% of the larger MTM, whichever is bigger


def _user(request: Request | None) -> str:
    if request is None or "session" not in request.scope:
        return ""
    return str((request.session.get("user") or {}).get("email") or "")


def _date(s: str, field: str) -> date:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")


async def spot_for(asset: str) -> float:
    a = asset.upper()
    if a == "FIL":
        return float(await _client.get_fil_spot_price())
    return float(await _client.get_eth_spot_price())


def _recon(row: dict, v: dict) -> dict:
    """Our figures vs the counterparty's, field by field."""
    if row.get("cpty_qty") is None and row.get("cpty_price") is None and row.get("cpty_mtm") is None:
        return {"status": "pending", "breaks": []}
    breaks = []
    if row.get("cpty_qty") is not None:
        d = abs(float(row["cpty_qty"])) - float(row["qty"])
        if abs(d) > QTY_TOL * max(float(row["qty"]), 1.0):
            breaks.append({"field": "qty", "ours": float(row["qty"]), "theirs": abs(float(row["cpty_qty"])), "diff": d})
    if row.get("cpty_price") is not None:
        d = float(row["cpty_price"]) - float(row["forward_price"])
        if abs(d) > PRICE_TOL * max(abs(float(row["forward_price"])), 1e-12):
            breaks.append({"field": "forward_price", "ours": float(row["forward_price"]),
                           "theirs": float(row["cpty_price"]), "diff": d})
    if row.get("cpty_mtm") is not None and v is not None:
        d = float(row["cpty_mtm"]) - v["mtm"]
        if abs(d) > max(MTM_TOL_USD, 0.01 * max(abs(v["mtm"]), abs(float(row["cpty_mtm"])))):
            breaks.append({"field": "mtm", "ours": v["mtm"], "theirs": float(row["cpty_mtm"]), "diff": d})
    return {"status": "break" if breaks else "matched", "breaks": breaks}


def _value_row(row: dict, spot: float, carry: float, today: date) -> dict:
    mat = _date(row["maturity_date"], "maturity_date")
    T = fwd.years_to(mat, today)
    v = fwd.value(row["side"], float(row["qty"]), float(row["forward_price"]), spot, carry, T)
    out = {**row, **v, "days_to_maturity": (mat - today).days, "matured": mat <= today,
           "signed_qty": fwd.side_sign(row["side"]) * float(row["qty"])}
    out["recon"] = _recon(row, v)
    out["_T"] = T
    return out


def _attach_levels(t: dict, lv: dict | None, spot: float, carry: float, T: float, forward_price: float) -> dict:
    """Stop / target from the Forwards Monitor (forward_levels), checked live."""
    lv = lv or {}
    t["stop_loss"] = lv.get("stop_loss")
    t["take_profit"] = lv.get("take_profit")
    t["levels_note"] = lv.get("note") or ""
    t["levels"] = fwd.level_watch(t["side"], float(t["qty"]), forward_price, spot, carry, T,
                                  t["stop_loss"], t["take_profit"])
    return t


async def build_forward_book(asset: str, spot: float | None = None, today: date | None = None) -> dict:
    """Open forwards of one asset, valued, with totals per counterparty.
    Never raises: an empty book on failure, so risk views degrade gracefully."""
    a = asset.upper()
    today = today or date.today()
    try:
        db = await get_db()
        await repo.ensure_tables(db)
        rows = await repo.list_forwards(db, a, ("open",))
        carry = float((await repo.get_carry(db, a))["rate"] or 0.0)
        levels = await repo.get_levels(db, a)
        if spot is None:
            spot = await spot_for(a) if rows else 0.0
    except Exception as e:
        log.warning("forward book unavailable for %s: %s", a, e)
        return {"asset": a, "spot": spot or 0.0, "carry": 0.0, "trades": [], "by_counterparty": [], "totals": _totals([])}
    trades = []
    for r in rows:
        t = _value_row(r, spot, carry, today)
        _attach_levels(t, levels.get(f"fwd:{r['id']}"), spot, carry, t.pop("_T"), float(r["forward_price"]))
        trades.append(t)
    by_cp: dict[str, dict] = {}
    for t in trades:
        k = t["counterparty"].strip().lower()
        b = by_cp.setdefault(k, {"counterparty": t["counterparty"], "trades": []})
        b["trades"].append(t)
    cps = []
    for b in by_cp.values():
        tot = _totals(b["trades"])
        cps.append({"counterparty": b["counterparty"], **tot,
                    # Single equivalent forward for the optimizer: net qty at the
                    # qty-weighted forward price (exact for a linear payoff).
                    "avg_forward_price": (sum(t["signed_qty"] * t["forward_price"] for t in b["trades"]) / tot["net_qty"])
                    if abs(tot["net_qty"]) > 1e-12 else 0.0})
    cps.sort(key=lambda c: c["counterparty"].lower())
    return {"asset": a, "spot": spot, "carry": carry, "trades": trades, "by_counterparty": cps, "totals": _totals(trades)}


def _totals(trades: list[dict]) -> dict:
    return {
        "count": len(trades),
        "net_qty": sum(t["signed_qty"] for t in trades),
        "mtm": sum(t["mtm"] for t in trades),
        "delta": sum(t["delta"] for t in trades),
        "delta_usd": sum(t["delta_usd"] for t in trades),
        "theta": sum(t["theta"] for t in trades),
        "notional_usd": sum(t["notional_usd"] for t in trades),
        "breaks": sum(1 for t in trades if t["recon"]["status"] == "break"),
        "unreconciled": sum(1 for t in trades if t["recon"]["status"] == "pending"),
        "matured": sum(1 for t in trades if t["matured"]),
    }


# ── API ──────────────────────────────────────────────────────────

class ForwardIn(BaseModel):
    asset: str
    counterparty: str
    side: str
    qty: float = Field(gt=0)
    forward_price: float = Field(gt=0)
    trade_date: str
    maturity_date: str
    external_ref: str = ""
    note: str = ""


class ForwardEdit(BaseModel):
    asset: str | None = None
    counterparty: str | None = None
    side: str | None = None
    qty: float | None = Field(default=None, gt=0)
    forward_price: float | None = Field(default=None, gt=0)
    trade_date: str | None = None
    maturity_date: str | None = None
    external_ref: str | None = None
    note: str | None = None


class SettleIn(BaseModel):
    fixing_price: float = Field(gt=0)


class ReconIn(BaseModel):
    cpty_qty: float | None = None
    cpty_price: float | None = None
    cpty_mtm: float | None = None
    note: str = ""


class CarryIn(BaseModel):
    asset: str
    rate_pct: float = Field(ge=-100, le=1000)


def _check(f: dict) -> dict:
    if "asset" in f and f["asset"].upper() not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    if "side" in f:
        try:
            f["side"] = "buy" if fwd.side_sign(f["side"]) > 0 else "sell"
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "counterparty" in f:
        f["counterparty"] = f["counterparty"].strip()
        if not f["counterparty"]:
            raise HTTPException(status_code=400, detail="counterparty is required")
    for k in ("trade_date", "maturity_date"):
        if k in f:
            f[k] = _date(f[k], k).isoformat()
    if "trade_date" in f and "maturity_date" in f and f["maturity_date"] <= f["trade_date"]:
        raise HTTPException(status_code=400, detail="maturity_date must be after trade_date")
    return f


@router.get("/book")
async def get_book(asset: str = "ETH"):
    """Open forwards valued at live spot + carry, totals and per counterparty."""
    if asset.upper() not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    try:
        spot = await spot_for(asset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"spot unavailable: {e}")
    book = await build_forward_book(asset, spot)
    db = await get_db()
    book["carry_info"] = await repo.get_carry(db, asset)
    settled = await repo.list_forwards(db, asset, ("settled",))
    book["settled"] = settled
    book["realised_pnl_usd"] = sum(float(s["settled_pnl_usd"] or 0.0) for s in settled)
    return book


@router.post("/trades")
async def book_forward(body: ForwardIn, request: Request):
    f = _check(body.model_dump())
    db = await get_db()
    await repo.ensure_tables(db)
    fid = await repo.insert_forward(db, f, _user(request))
    return await repo.get_forward(db, fid)


@router.put("/trades/{fid}")
async def amend_forward(fid: int, body: ForwardEdit, request: Request):
    db = await get_db()
    row = await repo.get_forward(db, fid)
    if not row or row["status"] != "open":
        raise HTTPException(status_code=404, detail="No open forward with that id")
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = _check({**{k: row[k] for k in ("trade_date", "maturity_date")}, **f})
    f.update({k: merged[k] for k in f})
    await repo.update_forward(db, fid, f, _user(request))
    return await repo.get_forward(db, fid)


@router.delete("/trades/{fid}")
async def delete_forward(fid: int, request: Request):
    """Soft delete: the row stays in the table with status 'deleted'."""
    db = await get_db()
    row = await repo.get_forward(db, fid)
    if not row or row["status"] != "open":
        raise HTTPException(status_code=404, detail="No open forward with that id")
    await repo.set_status(db, fid, "deleted", _user(request))
    return {"ok": True}


@router.post("/trades/{fid}/settle")
async def settle_forward(fid: int, body: SettleIn, request: Request):
    """Cash settlement: record the fixing and the realised P&L, close it."""
    db = await get_db()
    row = await repo.get_forward(db, fid)
    if not row or row["status"] != "open":
        raise HTTPException(status_code=404, detail="No open forward with that id")
    pnl = fwd.settlement_pnl(row["side"], float(row["qty"]), float(row["forward_price"]), body.fixing_price)
    await repo.set_status(db, fid, "settled", _user(request), fixing_price=body.fixing_price,
                          settled_pnl_usd=pnl, settled_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    return await repo.get_forward(db, fid)


@router.put("/trades/{fid}/recon")
async def reconcile_forward(fid: int, body: ReconIn, request: Request):
    """Store the counterparty's own figures for this forward."""
    db = await get_db()
    row = await repo.get_forward(db, fid)
    if not row or row["status"] != "open":
        raise HTTPException(status_code=404, detail="No open forward with that id")
    await repo.set_recon(db, fid, body.cpty_qty, body.cpty_price, body.cpty_mtm, body.note, _user(request))
    return await repo.get_forward(db, fid)


@router.put("/carry")
async def set_carry(body: CarryIn, request: Request):
    if body.asset.upper() not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    db = await get_db()
    await repo.ensure_tables(db)
    await repo.set_carry(db, body.asset, body.rate_pct / 100.0, _user(request))
    return await repo.get_carry(db, body.asset)


# ── Forwards Monitor ─────────────────────────────────────────────
# Every forward-like position of one asset, with its stop / target levels:
# booked forwards (forward_trades) and synthetic forwards (a long call + short
# put at one strike, or the reverse, booked as options, e.g. the KeyRock FIL
# structure). Levels live in forward_levels, not on the trades, so the option
# legs are never touched. Synthetic MTM is qty x (S - K) by put-call parity at
# zero rate, the same assumption the options book uses.

async def _active_option_legs(db, asset: str, today: date) -> list[dict]:
    cur = await db.execute(
        "SELECT * FROM trades WHERE asset = ? AND status = 'active' AND substr(expiry, 1, 10) >= ?",
        (asset.upper(), today.isoformat()))
    return [dict(r) for r in await cur.fetchall()]


def _leg_text(t: dict) -> str:
    return f"#{t['id']} {t['side']} {t['option_type']} {float(t['strike']):g} x {float(t['qty']):,.0f}"


async def build_monitor(asset: str, spot: float, today: date | None = None,
                        include_embedded: bool = False) -> dict:
    a = asset.upper()
    today = today or date.today()
    db = await get_db()
    await repo.ensure_tables(db)
    book = await build_forward_book(a, spot, today)
    levels = await repo.get_levels(db, a)
    # Closed round trips (settlement buys / sells offsetting earlier legs):
    # matched quantity leaves the open view; its gain is locked (realised).
    manual = await repo.list_closes(db, a)
    open_qty, trips, invalid = fwd.match_round_trips(book["trades"], manual, levels)
    out = []
    for t in book["trades"]:
        q0 = float(t["qty"])
        q = open_qty.get(int(t["id"]), q0)
        if q <= 1e-9:
            continue
        k = q / q0 if q0 else 0.0
        lv = dict(t["levels"])
        for side in ("stop", "target"):
            if lv.get(side):
                lv[side] = {**lv[side], "mtm_at": lv[side]["mtm_at"] * k}
        out.append({
            "key": f"fwd:{t['id']}", "kind": "forward", "forward_id": t["id"],
            "counterparty": t["counterparty"], "side": t["side"], "qty": q, "booked_qty": q0,
            "forward_price": float(t["forward_price"]), "effective_price": float(t["forward_price"]),
            "net_premium_usd": 0.0, "trade_date": t["trade_date"], "maturity_date": t["maturity_date"],
            "days_to_maturity": t["days_to_maturity"], "mtm": t["mtm"] * k, "delta": t["delta"] * k,
            "fair_forward": t["fair_forward"], "external_ref": t.get("external_ref") or "",
            "group": fwd.group_key({**t, "asset": a}),
            "legs": [], "leg_ids": [], "stop_loss": t["stop_loss"], "take_profit": t["take_profit"],
            "note": t["levels_note"], "levels": lv,
        })
    try:
        legs = await _active_option_legs(db, a, today)
    except Exception as e:  # noqa: BLE001 - forwards still show if the options read fails
        log.warning("option legs unavailable for monitor %s: %s", a, e)
        legs = []
    synthetics = fwd.find_synthetic_forwards(legs)
    try:
        if await repo.seed_synthetic_levels(db, a, synthetics):
            levels = await repo.get_levels(db, a)
    except Exception as e:  # noqa: BLE001 - the monitor still shows without the seeded levels
        log.warning("synthetic level seed failed for %s: %s", a, e)
    embedded_count = sum(1 for s in synthetics if not s["standalone"])
    for s in synthetics:
        if not s["standalone"] and not include_embedded:
            continue
        mat = _date(s["expiry"], "expiry")
        sgn = fwd.side_sign(s["side"])
        lv = levels.get(s["key"]) or {}
        watch = fwd.level_watch(s["side"], s["qty"], s["strike"], spot, 0.0, 0.0,
                                lv.get("stop_loss"), lv.get("take_profit"))
        out.append({
            "key": s["key"], "kind": "synthetic", "forward_id": None, "embedded": not s["standalone"],
            "counterparty": s["counterparty"], "side": s["side"], "qty": s["qty"],
            "forward_price": s["strike"], "effective_price": s["effective_price"],
            "net_premium_usd": s["net_premium_usd"], "trade_date": s["trade_date"],
            "maturity_date": s["expiry"], "days_to_maturity": (mat - today).days,
            "mtm": sgn * s["qty"] * (spot - s["strike"]), "delta": sgn * s["qty"],
            "fair_forward": spot, "external_ref": s["call"].get("trade_id") or "",
            "legs": [_leg_text(s["call"]), _leg_text(s["put"])],
            "leg_ids": [s["call"]["id"], s["put"]["id"]],
            "stop_loss": lv.get("stop_loss"), "take_profit": lv.get("take_profit"),
            "note": lv.get("note") or "", "levels": watch,
        })
    rank = {"stop_hit": 0, "target_hit": 1, "near_stop": 2, "near_target": 3, "watching": 4, "none": 5}
    out.sort(key=lambda p: (rank.get(p["levels"]["status"], 9), p["maturity_date"], p["counterparty"].lower()))
    st = [p["levels"]["status"] for p in out]
    summary = {k: st.count(k) for k in rank}
    summary.update(count=len(out), net_qty=sum(fwd.side_sign(p["side"]) * p["qty"] for p in out),
                   mtm=sum(p["mtm"] for p in out))
    settled = await repo.list_forwards(db, a, ("settled",))
    realised_closed = sum(t["realised_usd"] for t in trips)
    realised_settled = sum(float(r["settled_pnl_usd"] or 0.0) for r in settled)
    summary.update(open_mtm=summary["mtm"], realised_closed=realised_closed, realised_settled=realised_settled,
                   pnl_total=summary["mtm"] + realised_closed + realised_settled)
    open_legs = [{"id": t["id"], "counterparty": t["counterparty"], "side": t["side"], "qty": float(t["qty"]),
                  "open_qty": open_qty.get(int(t["id"]), float(t["qty"])), "forward_price": float(t["forward_price"]),
                  "trade_date": t["trade_date"], "maturity_date": t["maturity_date"]} for t in book["trades"]]
    return {"asset": a, "spot": spot, "near_pct": fwd.NEAR_PCT, "positions": out, "summary": summary,
            "closed": trips, "invalid_closes": invalid, "settled": settled, "forward_legs": open_legs,
            "embedded_synthetics": embedded_count, "include_embedded": include_embedded,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


class LevelsIn(BaseModel):
    asset: str
    key: str
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    note: str = ""


@router.get("/monitor")
async def get_monitor(asset: str = "FIL", include_embedded: bool = False):
    """Booked + synthetic forwards with stop / target levels, at live spot.
    include_embedded adds call/put pairs that sit inside a larger deal."""
    if asset.upper() not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    try:
        spot = await spot_for(asset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"spot unavailable: {e}")
    return await build_monitor(asset, spot, include_embedded=include_embedded)


@router.put("/levels")
async def put_levels(body: LevelsIn, request: Request):
    """Set / clear one position's stop loss and take profit (null clears)."""
    a = body.asset.upper()
    if a not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    try:
        spot = await spot_for(a)
    except Exception:
        raise HTTPException(status_code=502, detail="spot unavailable, levels not saved")
    mon = await build_monitor(a, spot, include_embedded=True)
    pos = next((p for p in mon["positions"] if p["key"] == body.key), None)
    if pos is None:
        raise HTTPException(status_code=404, detail=f"no open {a} forward position {body.key!r}")
    err = fwd.check_levels_order(pos["side"], body.stop_loss, body.take_profit)
    if err:
        raise HTTPException(status_code=400, detail=err)
    db = await get_db()
    await repo.set_levels(db, body.key, a, body.stop_loss, body.take_profit, body.note, _user(request))
    mon = await build_monitor(a, spot, include_embedded=True)
    return next((p for p in mon["positions"] if p["key"] == body.key), pos)


class CloseIn(BaseModel):
    asset: str
    open_id: int
    close_id: int
    qty: float | None = Field(default=None, gt=0)
    reason: str = "closed"
    note: str = ""


@router.post("/closes")
async def pair_close(body: CloseIn, request: Request):
    """Pick by hand which forward closes which (the rest is matched FIFO)."""
    a = body.asset.upper()
    if a not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    if body.reason not in fwd.CLOSE_REASONS:
        raise HTTPException(status_code=400, detail=f"reason must be one of {fwd.CLOSE_REASONS}")
    db = await get_db()
    await repo.ensure_tables(db)
    x, y = await repo.get_forward(db, body.open_id), await repo.get_forward(db, body.close_id)
    if not x or not y or x["status"] != "open" or y["status"] != "open" or x["id"] == y["id"]:
        raise HTTPException(status_code=404, detail="both legs must be different open forwards")
    if str(x["asset"]).upper() != a or str(y["asset"]).upper() != a:
        raise HTTPException(status_code=400, detail=f"both legs must be {a} forwards")
    if fwd.side_sign(x["side"]) == fwd.side_sign(y["side"]):
        raise HTTPException(status_code=400, detail="a close needs one buy and one sell")
    if fwd.group_key(x) != fwd.group_key(y):
        raise HTTPException(status_code=400, detail="legs must share counterparty and maturity to offset")
    # Replace an existing manual pair of the same two legs (e.g. retagging the reason).
    for m in await repo.list_closes(db, a):
        if {int(m["open_id"]), int(m["close_id"])} == {x["id"], y["id"]}:
            await repo.delete_close(db, m["id"])
    qty = body.qty or min(float(x["qty"]), float(y["qty"]))
    cid = await repo.add_close(db, a, x["id"], y["id"], qty, body.reason, body.note, _user(request))
    return {"id": cid}


@router.delete("/closes/{cid}")
async def unpair_close(cid: int):
    db = await get_db()
    if not await repo.delete_close(db, cid):
        raise HTTPException(status_code=404, detail="no such pair")
    return {"ok": True}


@router.get("/history")
async def get_history(asset: str = "FIL"):
    """Hourly spot closes (last ~30 days) for the monitor chart."""
    from plgo_options.market_data import token_market
    if asset.upper() not in ASSETS:
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS}")
    try:
        return await token_market.get_hourly(asset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"history unavailable: {e}")
