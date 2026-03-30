"""Deribit order execution API — authenticated trading endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

from plgo_options.config import (
    DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET,
    DERIBIT_EXEC_URL, DERIBIT_TESTNET, REQUEST_TIMEOUT,
)

router = APIRouter()

# In-memory token cache
_auth_token: str | None = None
_token_expiry: float = 0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    instrument_name: str          # e.g. "ETH-29MAY26-3000-C" or "ETH-PERPETUAL"
    side: str                     # "buy" or "sell"
    amount: float                 # quantity in contracts
    order_type: str = "limit"     # "limit" or "market"
    price: float | None = None    # required for limit orders (in ETH for options, USD for perps)
    reduce_only: bool = False
    label: str = "plgo"


class CancelRequest(BaseModel):
    order_id: str


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _authenticate() -> str:
    """Get or refresh Deribit access token."""
    global _auth_token, _token_expiry

    if _auth_token and datetime.utcnow().timestamp() < _token_expiry - 30:
        return _auth_token

    if not DERIBIT_CLIENT_ID or not DERIBIT_CLIENT_SECRET:
        raise HTTPException(
            status_code=400,
            detail="Deribit API credentials not configured. "
                   "Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET environment variables.",
        )

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{DERIBIT_EXEC_URL}/public/auth", params={
            "grant_type": "client_credentials",
            "client_id": DERIBIT_CLIENT_ID,
            "client_secret": DERIBIT_CLIENT_SECRET,
        })
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise HTTPException(502, f"Deribit auth failed: {data['error']}")

    result = data["result"]
    _auth_token = result["access_token"]
    _token_expiry = datetime.utcnow().timestamp() + result.get("expires_in", 900)
    return _auth_token


async def _private_get(method: str, params: dict[str, Any]) -> Any:
    """Make authenticated Deribit private API call."""
    token = await _authenticate()
    url = f"{DERIBIT_EXEC_URL}/private/{method}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers={
            "Authorization": f"Bearer {token}",
        })
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise HTTPException(502, f"Deribit API error: {data['error']}")
    return data["result"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
async def execution_status():
    """Check if execution is configured and return connection info."""
    configured = bool(DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET)
    env = "testnet" if DERIBIT_TESTNET else "PRODUCTION"
    result = {
        "configured": configured,
        "environment": env,
        "url": DERIBIT_EXEC_URL,
    }

    if configured:
        try:
            token = await _authenticate()
            result["authenticated"] = True
            # Get account summary
            summary = await _private_get("get_account_summary", {"currency": "ETH"})
            result["account"] = {
                "equity": summary.get("equity"),
                "balance": summary.get("balance"),
                "available_funds": summary.get("available_withdrawal_funds"),
                "margin_balance": summary.get("margin_balance"),
                "currency": "ETH",
            }
        except Exception as e:
            result["authenticated"] = False
            result["error"] = str(e)
    else:
        result["authenticated"] = False
        result["error"] = "Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET env vars"

    return result


@router.post("/order")
async def place_order(req: OrderRequest):
    """Place a single order on Deribit."""
    params = {
        "instrument_name": req.instrument_name,
        "amount": req.amount,
        "type": req.order_type,
        "label": req.label,
    }
    if req.reduce_only:
        params["reduce_only"] = True
    if req.order_type == "limit":
        if req.price is None:
            raise HTTPException(400, "Price required for limit orders")
        params["price"] = req.price

    method = "buy" if req.side.lower() == "buy" else "sell"
    result = await _private_get(method, params)

    order = result.get("order", {})
    trades = result.get("trades", [])

    return {
        "success": True,
        "order_id": order.get("order_id"),
        "instrument": req.instrument_name,
        "side": req.side,
        "amount": req.amount,
        "order_type": req.order_type,
        "price": req.price,
        "order_state": order.get("order_state"),
        "filled_amount": order.get("filled_amount", 0),
        "average_price": order.get("average_price"),
        "trades_count": len(trades),
        "order": order,
    }


@router.post("/cancel")
async def cancel_order(req: CancelRequest):
    """Cancel an open order."""
    result = await _private_get("cancel", {"order_id": req.order_id})
    return {"success": True, "order": result}


@router.get("/open-orders")
async def get_open_orders():
    """Get all open orders for ETH."""
    result = await _private_get("get_open_orders_by_currency", {
        "currency": "ETH",
    })
    return {"orders": result}


@router.get("/positions")
async def get_positions():
    """Get current Deribit positions."""
    result = await _private_get("get_positions", {
        "currency": "ETH",
    })
    return {"positions": result}


@router.get("/orderbook/{instrument_name:path}")
async def get_orderbook(instrument_name: str, depth: int = 5):
    """Get live order book for an instrument (public endpoint)."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(f"{DERIBIT_EXEC_URL}/public/get_order_book", params={
            "instrument_name": instrument_name,
            "depth": depth,
        })
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise HTTPException(502, f"Deribit error: {data['error']}")

    book = data["result"]
    return {
        "instrument": instrument_name,
        "best_bid": book.get("best_bid_price"),
        "best_ask": book.get("best_ask_price"),
        "mark_price": book.get("mark_price"),
        "mark_iv": book.get("mark_iv"),
        "bids": book.get("bids", [])[:depth],
        "asks": book.get("asks", [])[:depth],
        "underlying_price": book.get("underlying_price"),
    }
