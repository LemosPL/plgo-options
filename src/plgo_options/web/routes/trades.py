"""Trade CRUD API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.data.database import get_db
from plgo_options.data import trade_repository as repo

router = APIRouter()


# ─── Reconciliation models ──────────────────────────────────

class ReconTrade(BaseModel):
    trade_id: str = ""
    trade_date: str = ""
    side: str = ""
    option_type: str = ""
    strike: float = 0
    expiry: str = ""
    qty: float = 0
    premium_usd: float = 0


class ReconCollateral(BaseModel):
    ETH: float = 0
    FIL: float = 0
    USD: float = 0
    USDC: float = 0


class ReconRequest(BaseModel):
    counterparty: str
    asset: str = "ETH"
    their_trades: list[ReconTrade] = []
    their_collateral: ReconCollateral = ReconCollateral()


class TradeCreate(BaseModel):
    asset: str = "ETH"
    counterparty: str = ""
    trade_id: str = ""
    trade_date: str = ""
    side: str
    option_type: str
    instrument: str = ""
    expiry: str
    strike: float
    ref_spot: float = 0
    pct_otm: float = 0
    qty: float
    notional_mm: float = 0
    premium_per: float = 0
    premium_usd: float = 0


class TradeUpdate(BaseModel):
    counterparty: str | None = None
    trade_id: str | None = None
    trade_date: str | None = None
    side: str | None = None
    option_type: str | None = None
    instrument: str | None = None
    expiry: str | None = None
    strike: float | None = None
    ref_spot: float | None = None
    pct_otm: float | None = None
    qty: float | None = None
    notional_mm: float | None = None
    premium_per: float | None = None
    premium_usd: float | None = None
    is_otc: int | None = None
    last_otc_quote: float | None = None
    otc_settlement_method: str | None = None
    otc_override_price: float | None = None


_OTC_METHODS = {"intrinsic_at_spot", "agreed_mid", "negotiated"}


class TradeOTCUpdate(BaseModel):
    is_otc: bool | None = None
    last_otc_quote: float | None = None
    otc_settlement_method: str | None = None
    otc_override_price: float | None = None


class BulkExpireRequest(BaseModel):
    ids: list[int]


@router.get("/")
async def list_trades(
    include_expired: bool = False,
    include_deleted: bool = False,
    asset: str | None = None,
):
    db = await get_db()
    trades = await repo.list_trades(db, include_expired, include_deleted, asset=asset)
    return {"trades": trades}


@router.post("/")
async def create_trade(body: TradeCreate):
    db = await get_db()
    data = body.model_dump(exclude_none=True)
    trade = await repo.create_trade(db, data)
    return trade


@router.post("/bulk-expire")
async def bulk_expire_trades(body: BulkExpireRequest):
    if not body.ids:
        raise HTTPException(status_code=400, detail="No trade IDs provided")
    db = await get_db()
    results = []
    for tid in body.ids:
        trade = await repo.expire_trade(db, tid)
        if trade:
            results.append(trade)
    return {"expired": len(results), "trades": results}


@router.get("/{trade_id}")
async def get_trade(trade_id: int):
    db = await get_db()
    trade = await repo.get_trade(db, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.put("/{trade_id}")
async def update_trade(trade_id: int, body: TradeUpdate):
    db = await get_db()
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided")
    trade = await repo.update_trade(db, trade_id, changes)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.delete("/{trade_id}")
async def delete_trade(trade_id: int):
    db = await get_db()
    trade = await repo.soft_delete_trade(db, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.post("/{trade_id}/expire")
async def expire_trade(trade_id: int):
    db = await get_db()
    trade = await repo.expire_trade(db, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.put("/{trade_id}/otc")
async def update_trade_otc(trade_id: int, body: TradeOTCUpdate):
    """Set OTC metadata on a trade. Used so close-leg pricing can prefer the
    user's settled OTC price over Deribit/intrinsic fallback."""
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="No OTC fields provided")
    if "is_otc" in payload:
        payload["is_otc"] = 1 if payload["is_otc"] else 0
    if "otc_settlement_method" in payload and payload["otc_settlement_method"] not in _OTC_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"otc_settlement_method must be one of {sorted(_OTC_METHODS)}",
        )
    db = await get_db()
    trade = await repo.update_trade(db, trade_id, payload)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.get("/{trade_id}/history")
async def get_trade_history(trade_id: int):
    db = await get_db()
    history = await repo.get_trade_history(db, trade_id)
    return {"history": history}


# ─── Reconciliation endpoint ────────────────────────────────

import re
from datetime import datetime, date


def _norm_side(s: str) -> str:
    s = s.strip().upper()
    if s in ("BUY", "BUYS", "BOUGHT", "LONG", "B", "L"):
        return "BUY"
    if s in ("SELL", "SELLS", "SOLD", "SHORT", "S"):
        return "SELL"
    return s


def _norm_type(t: str) -> str:
    t = t.strip().upper()
    if t in ("C", "CALL", "CALLS"):
        return "CALL"
    if t in ("P", "PUT", "PUTS"):
        return "PUT"
    return t


def _norm_date(d) -> str:
    """Normalize date to YYYY-MM-DD regardless of input format."""
    if d is None:
        return ""
    # Handle numeric values (Excel serial dates: days since 1899-12-30)
    if isinstance(d, (int, float)):
        serial = int(d)
        if 30000 < serial < 60000:  # plausible Excel date range (~1982–2064)
            from datetime import timedelta
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=serial)).strftime("%Y-%m-%d")
        return str(d)
    d = str(d).strip()
    if not d:
        return ""
    # Check if it's a numeric string (Excel serial date)
    try:
        serial = int(float(d))
        if 30000 < serial < 60000:
            from datetime import timedelta
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=serial)).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", d):
        return d[:10]
    # DDMMMYY e.g. 04AUG26
    m = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})$", d.upper())
    if m:
        day, mon, yr = m.groups()
        try:
            dt = datetime.strptime(f"{day}{mon}{yr}", "%d%b%y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    # DD/MM/YYYY or MM/DD/YYYY — try both
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y",
                "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d


def _is_expired(expiry_str: str) -> bool:
    """Check if a trade's expiry is in the past."""
    nd = _norm_date(expiry_str)
    if not nd:
        return False
    try:
        return datetime.strptime(nd, "%Y-%m-%d").date() < date.today()
    except ValueError:
        return False


def _match_key(t: dict) -> tuple:
    """Build a matching key: (side, type, strike, expiry_normalized, abs_qty)."""
    return (
        _norm_side(str(t.get("side", ""))),
        _norm_type(str(t.get("option_type", ""))),
        float(t.get("strike", 0)),
        _norm_date(str(t.get("expiry", ""))),
        abs(float(t.get("qty", 0))),
    )


@router.post("/reconcile")
async def reconcile_trades(body: ReconRequest):
    db = await get_db()
    our_all = await repo.list_trades(db, include_expired=False, include_deleted=False, asset=body.asset)
    our_trades = [t for t in our_all if (t.get("counterparty", "") or "").lower() == body.counterparty.lower()]

    # Filter out expired trades from both sides
    our_trades = [t for t in our_trades if not _is_expired(str(t.get("expiry", "")))]
    their_active = [t.model_dump() for t in body.their_trades if not _is_expired(t.expiry)]

    # Add normalized dates to trade dicts for display
    for t in our_trades:
        t["expiry_norm"] = _norm_date(str(t.get("expiry", "")))
        t["trade_date_norm"] = _norm_date(str(t.get("trade_date", "")))
    for t in their_active:
        t["expiry_norm"] = _norm_date(str(t.get("expiry", "")))
        t["trade_date_norm"] = _norm_date(str(t.get("trade_date", "")))

    # Build keyed lookups
    our_by_key: dict[tuple, list[dict]] = {}
    for t in our_trades:
        k = _match_key(t)
        our_by_key.setdefault(k, []).append(t)

    their_by_key: dict[tuple, list[dict]] = {}
    for t in their_active:
        k = _match_key(t)
        their_by_key.setdefault(k, []).append(t)

    all_keys = set(our_by_key.keys()) | set(their_by_key.keys())

    matched = []
    breaks = []
    only_ours = []
    only_theirs = []

    for k in all_keys:
        ours = our_by_key.get(k, [])
        theirs = their_by_key.get(k, [])
        if ours and theirs:
            o = ours[0]
            th = theirs[0]
            diffs = {}
            # Compare premiums by absolute value (sign convention differs)
            o_prem = abs(float(o.get("premium_usd", 0) or 0))
            th_prem = abs(float(th.get("premium_usd", 0) or 0))
            if abs(o_prem - th_prem) > 0.01:
                diffs["premium_usd"] = {"ours": o_prem, "theirs": th_prem}
            o_tid = str(o.get("trade_id", "") or "")
            th_tid = str(th.get("trade_id", "") or "")
            if o_tid and th_tid and o_tid != th_tid:
                diffs["trade_id"] = {"ours": o_tid, "theirs": th_tid}
            o_td = _norm_date(str(o.get("trade_date", "")))
            th_td = _norm_date(str(th.get("trade_date", "")))
            if o_td and th_td and o_td != th_td:
                diffs["trade_date"] = {"ours": o_td, "theirs": th_td}
            if diffs:
                breaks.append({
                    "key": f"{k[0]} {k[1]} {k[2]} {k[3]} x{k[4]}",
                    "ours": o, "theirs": th, "diffs": diffs,
                })
            else:
                matched.append({
                    "key": f"{k[0]} {k[1]} {k[2]} {k[3]} x{k[4]}",
                    "ours": o, "theirs": th,
                })
            # Handle extras if multiple trades on same key
            for extra in ours[1:]:
                only_ours.append(extra)
            for extra in theirs[1:]:
                only_theirs.append(extra)
        elif ours and not theirs:
            only_ours.extend(ours)
        else:
            only_theirs.extend(theirs)

    return {
        "counterparty": body.counterparty,
        "asset": body.asset,
        "summary": {
            "our_count": len(our_trades),
            "their_count": len(body.their_trades),
            "matched": len(matched),
            "breaks": len(breaks),
            "only_ours": len(only_ours),
            "only_theirs": len(only_theirs),
        },
        "matched": matched,
        "breaks": breaks,
        "only_ours": only_ours,
        "only_theirs": only_theirs,
        "collateral": body.their_collateral.model_dump(),
    }
