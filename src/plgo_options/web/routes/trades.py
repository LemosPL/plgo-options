"""Trade CRUD API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.data.database import get_db
from plgo_options.data import trade_repository as repo

router = APIRouter()


class TradeCreate(BaseModel):
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


@router.get("/")
async def list_trades(
    include_expired: bool = False,
    include_deleted: bool = False,
):
    db = await get_db()
    trades = await repo.list_trades(db, include_expired, include_deleted)
    return {"trades": trades}


@router.get("/{trade_id}")
async def get_trade(trade_id: int):
    db = await get_db()
    trade = await repo.get_trade(db, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.post("/")
async def create_trade(body: TradeCreate):
    db = await get_db()
    data = body.model_dump(exclude_none=True)
    trade = await repo.create_trade(db, data)
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


@router.get("/{trade_id}/history")
async def get_trade_history(trade_id: int):
    db = await get_db()
    history = await repo.get_trade_history(db, trade_id)
    return {"history": history}
