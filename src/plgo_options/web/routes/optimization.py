"""Optimizer v2 endpoints."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
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


SNAPSHOT_DIR = Path("data/optimization_snapshots/usecases")


@router.get("/snapshots")
async def list_snapshots():
    """List saved usecase snapshot files."""
    if not SNAPSHOT_DIR.exists():
        return {"snapshots": []}
    files = sorted(SNAPSHOT_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    snapshots = []
    for f in files[:50]:
        try:
            with f.open() as fh:
                data = json.load(fh)
            params = data.get("run_params", {})
            inp = data.get("optimizer_input", {})
            result = data.get("result", {})
            snapshots.append({
                "filename": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "modified": f.stat().st_mtime,
                "asset": params.get("asset", inp.get("asset", "ETH")),
                "target_expiry": params.get("target_expiry", ""),
                "lam_factor": params.get("lam_factor", ""),
                "status": result.get("status", ""),
                "trades_count": len(result.get("replacement_trades", result.get("trades", []))),
            })
        except Exception:
            snapshots.append({"filename": f.name, "size_kb": round(f.stat().st_size / 1024, 1)})
    return {"snapshots": snapshots}


@router.get("/snapshots/{filename}")
async def download_snapshot(filename: str):
    """Download a saved usecase snapshot file."""
    path = SNAPSHOT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    # Security: ensure the resolved path is inside SNAPSHOT_DIR
    if not path.resolve().is_relative_to(SNAPSHOT_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(path, media_type="application/json", filename=filename)
