"""Positions & risk endpoints — reads trades from database."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.data.trades import aggregate_positions, _safe_float

router = APIRouter()


def _db_trades_to_legacy(db_trades: list[dict]) -> list[dict]:
    """Map DB trade dicts to legacy column names for aggregate_positions."""
    result = []
    for t in db_trades:
        result.append({
            "Counterparty": t["counterparty"],
            "ID": t["id"],
            "Initial Trade Date": t["trade_date"],
            "Buy / Sell / Unwind": t["side"],
            "Option Type": t["option_type"],
            "Trade_ID": t.get("trade_id", ""),
            "Option Expiry Date": t["expiry"],
            "Days Remaining to Expiry": 0,
            "Strike": t["strike"],
            "Ref. Spot Price": t["ref_spot"],
            "% OTM": t["pct_otm"],
            "ETH Options": t["qty"],
            "$ Notional (mm)": t["notional_mm"],
            "Premium per Contract": t["premium_per"],
            "Premium USD": t["premium_usd"],
        })
    return result


@router.get("/trades")
async def get_trades():
    """Return raw trade list from database."""
    try:
        db = await get_db()
        trades = await list_trades(db, include_expired=True, include_deleted=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return trades


@router.get("/summary")
async def get_position_summary():
    """Aggregate trades into net positions and compute portfolio totals."""
    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=True, include_deleted=False)
        legacy_trades = _db_trades_to_legacy(db_trades)
        positions = aggregate_positions(legacy_trades)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    totals = {
        "positions_count": len(positions),
        "trades_count": len(db_trades),
        "total_net_qty": sum(p["net_qty"] for p in positions),
        "total_premium_usd": sum(p["total_premium_usd"] for p in positions),
        "total_notional_mm": sum(p["total_notional_mm"] for p in positions),
        "long_count": sum(1 for p in positions if p["side"] == "Long"),
        "short_count": sum(1 for p in positions if p["side"] == "Short"),
        "flat_count": sum(1 for p in positions if p["side"] == "Flat"),
    }

    return {"positions": positions, "totals": totals}
