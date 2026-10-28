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
    return out


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
        if spot is None:
            spot = await spot_for(a) if rows else 0.0
    except Exception as e:
        log.warning("forward book unavailable for %s: %s", a, e)
        return {"asset": a, "spot": spot or 0.0, "carry": 0.0, "trades": [], "by_counterparty": [], "totals": _totals([])}
    trades = [_value_row(r, spot, carry, today) for r in rows]
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
