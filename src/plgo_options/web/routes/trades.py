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

def _norm_side(s: str) -> str:
    s = s.strip().upper()
    if s in ("BUY", "LONG", "B"):
        return "BUY"
    if s in ("SELL", "SHORT", "S"):
        return "SELL"
    return s


def _norm_type(t: str) -> str:
    t = t.strip().upper()
    if t in ("C", "CALL"):
        return "CALL"
    if t in ("P", "PUT"):
        return "PUT"
    return t


def _match_key(t: dict) -> tuple:
    """Build a matching key from a trade dict: (side, type, strike, expiry, abs_qty)."""
    return (
        _norm_side(str(t.get("side", ""))),
        _norm_type(str(t.get("option_type", ""))),
        float(t.get("strike", 0)),
        str(t.get("expiry", "")).strip().upper(),
        abs(float(t.get("qty", 0))),
    )


@router.post("/reconcile")
async def reconcile_trades(body: ReconRequest):
    db = await get_db()
    our_all = await repo.list_trades(db, include_expired=False, include_deleted=False, asset=body.asset)
    our_trades = [t for t in our_all if (t.get("counterparty", "") or "").lower() == body.counterparty.lower()]

    # Build keyed lookups
    our_by_key: dict[tuple, list[dict]] = {}
    for t in our_trades:
        k = _match_key(t)
        our_by_key.setdefault(k, []).append(t)

    their_by_key: dict[tuple, list[dict]] = {}
    for t in body.their_trades:
        td = t.model_dump()
        k = _match_key(td)
        their_by_key.setdefault(k, []).append(td)

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
            o_prem = float(o.get("premium_usd", 0) or 0)
            th_prem = float(th.get("premium_usd", 0) or 0)
            if abs(o_prem - th_prem) > 0.01:
                diffs["premium_usd"] = {"ours": o_prem, "theirs": th_prem}
            o_tid = str(o.get("trade_id", "") or "")
            th_tid = str(th.get("trade_id", "") or "")
            if o_tid and th_tid and o_tid != th_tid:
                diffs["trade_id"] = {"ours": o_tid, "theirs": th_tid}
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
