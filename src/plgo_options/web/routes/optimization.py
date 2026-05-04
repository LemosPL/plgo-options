"""Optimizer v2 endpoints."""

from __future__ import annotations

import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.web.routes.portfolio import portfolio_pnl
from plgo_options.optimization.optim_usecase import (
    OptimizerRunParams,
    OptimizerUseCase,
)

router = APIRouter()


class OptimizationParams(BaseModel):
    risk_aversion: float = 1.0
    brokerage_txn_cost_pct: float = 5.0
    deribit_txn_cost_pct: float = 0.1
    max_collateral: float = 4_000_000.0
    target_expiry: str | None = None
    lambda_delta: float = 1.0
    lambda_gamma: float = 1.0
    lambda_vega: float = 1.0
    vega_cross_expiry_corr: float = 0.0
    save_usecase_snapshot: bool = False


@router.post("/run")
async def run_optimizer(params: OptimizationParams):
    """Gather optimizer inputs, persist a reproducible use case, and run it."""
    print("run_optimizer()")
    try:
        pnl_data = await portfolio_pnl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    run_params = OptimizerRunParams(
        risk_aversion=params.risk_aversion,
        brokerage_txn_cost_pct=params.brokerage_txn_cost_pct,
        deribit_txn_cost_pct=params.deribit_txn_cost_pct,
        max_collateral=params.max_collateral,
        target_expiry=params.target_expiry,
        lambda_delta=params.lambda_delta,
        lambda_gamma=params.lambda_gamma,
        lambda_vega=params.lambda_vega,
        vega_cross_expiry_corr=params.vega_cross_expiry_corr,
    )

    usecase = OptimizerUseCase.from_portfolio_payload(pnl_data, run_params)
    print(params.save_usecase_snapshot)
    try:
        if params.save_usecase_snapshot:
            save_dir = Path("data/optimization_snapshots/usecases")
            save_path = usecase.save_auto(save_dir)
        result = usecase.run()
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(
            status_code=500,
            detail=f"Optimization save failed: {e}\n\n{tb}",
        )

    return result
