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
    asset: str = "ETH"
    lam_factor: float = 1.0
    target_expiry: str | None = None
    unwind_discount: float = 0.2
    new_position_penalty: float = 0.04
    roll_dte_threshold: int | None = None
    save_usecase_snapshot: bool = False
    is_replay: bool = False
    counterparties: list[str] | None = None

@router.post("/run")
async def run_optimizer(params: OptimizationParams):
    """Gather optimizer inputs, persist a reproducible use case, and run it."""
    print("run_optimizer()")
    try:
        pnl_data = await portfolio_pnl(asset=params.asset.upper())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    print(params)
    run_params = OptimizerRunParams(
        asset=params.asset.upper(),
        lam_factor=params.lam_factor,
        target_expiry=params.target_expiry,
        unwind_discount=params.unwind_discount,
        new_position_penalty=params.new_position_penalty,
        roll_dte_threshold=params.roll_dte_threshold,
        is_replay=False,
        counterparties=params.counterparties,
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
