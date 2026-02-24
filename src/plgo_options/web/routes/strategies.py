"""Strategy builder endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from plgo_options.strategies.builder import (
    bull_call_spread,
    iron_condor,
    long_call,
    long_put,
    straddle,
    strangle,
)

router = APIRouter()


class StraddleRequest(BaseModel):
    strike: float
    call_premium: float
    put_premium: float
    quantity: float = 1.0
    is_long: bool = True


@router.get("/templates")
async def list_templates() -> list[str]:
    return [
        "long_call", "long_put",
        "bull_call_spread", "bear_put_spread",
        "straddle", "strangle",
        "iron_condor", "butterfly",
    ]


@router.post("/straddle")
async def build_straddle(req: StraddleRequest) -> dict:
    legs = straddle(
        strike=req.strike,
        call_premium=req.call_premium,
        put_premium=req.put_premium,
        qty=req.quantity,
        is_long=req.is_long,
    )
    return {"strategy": "straddle", "legs": legs}