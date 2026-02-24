"""Positions & risk endpoints — reads trades from Excel for now."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from plgo_options.data.trades import read_eth_trades, aggregate_positions, _safe_float

router = APIRouter()


# ── GET /api/positions/trades ────────────────────────────────
@router.get("/trades")
async def get_trades():
    """Return raw trade list from Excel."""
    try:
        trades = read_eth_trades()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return trades


@router.get("/summary")
async def get_position_summary():
    """Aggregate trades into net positions and compute portfolio totals.

    Returns
    -------
    {
      positions: [ { option_type, strike, expiry, net_qty, side,
                      avg_premium_per_contract, total_premium_usd,
                      total_notional_mm, days_remaining, pct_otm,
                      ref_spot, trade_count, counterparties } ],
      totals:    { positions_count, total_net_qty, total_premium_usd,
                   total_notional_mm, long_count, short_count, flat_count }
    }
    """
    try:
        trades = read_eth_trades()
        positions = aggregate_positions(trades)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    totals = {
        "positions_count": len(positions),
        "trades_count": len(trades),
        "total_net_qty": sum(p["net_qty"] for p in positions),
        "total_premium_usd": sum(p["total_premium_usd"] for p in positions),
        "total_notional_mm": sum(p["total_notional_mm"] for p in positions),
        "long_count": sum(1 for p in positions if p["side"] == "Long"),
        "short_count": sum(1 for p in positions if p["side"] == "Short"),
        "flat_count": sum(1 for p in positions if p["side"] == "Flat"),
    }

    return {"positions": positions, "totals": totals}