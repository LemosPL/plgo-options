"""Optimizer v2 endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.web.routes.portfolio import portfolio_pnl
from plgo_options.optimization.snapshot import save_snapshot
from plgo_options.optimization.optimizer import OptimizerV2

router = APIRouter()


class OptimizationParams(BaseModel):
    risk_aversion: float = 1.0
    txn_cost_pct: float = 5.0
    max_collateral: float = 4_000_000.0
    target_expiry: str | None = None
    lambda_delta: float = 1.0
    lambda_vega: float = 100.0


@router.post("/run")
async def run_optimizer(params: OptimizationParams):
    """Snapshot all optimizer inputs, load into OptimizerV2, and run."""
    # 1. Gather the full risk profile (same data as Load Risk Profile)
    try:
        pnl_data = await portfolio_pnl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    # 2. Save snapshot (include params for reproducibility)
    pnl_data["optimization_params"] = params.model_dump()
    try:
        path = save_snapshot(pnl_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save snapshot: {e}")

    # 3. Load snapshot into optimizer and run
    try:
        optimizer = OptimizerV2.from_snapshot(path)
        result = optimizer.run(
            risk_aversion=params.risk_aversion,
            txn_cost_pct=params.txn_cost_pct,
            max_collateral=params.max_collateral,
            target_expiry=params.target_expiry,
            lambda_delta=params.lambda_delta,
            lambda_vega=params.lambda_vega,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Optimization failed: {e}")

    return result
