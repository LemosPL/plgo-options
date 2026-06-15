"""Optimizer v2 endpoints."""

from __future__ import annotations

import os
import traceback
from datetime import datetime
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


def _resolve_snapshot_root() -> Path:
    """Pick where saved optimizer snapshots live.

    Resolution order:
      1. SNAPSHOT_DIR env var (explicit override — typically a GCS FUSE mount on Cloud Run).
      2. DB_DIR/optimization_snapshots — if DB_DIR is set, piggyback on the same persistent
         mount that backs the SQLite DB so snapshots survive container restarts for free.
      3. Local repo-relative ./data/optimization_snapshots (dev fallback; ephemeral on Cloud Run).
    """
    snap = os.environ.get("SNAPSHOT_DIR")
    if snap:
        return Path(snap)
    db_dir = os.environ.get("DB_DIR")
    if db_dir:
        return Path(db_dir) / "optimization_snapshots"
    return Path("data/optimization_snapshots")


# Resolved at import time — the same root is used by both the save path and the
# list/download endpoints, so anything written ends up listable.
SNAPSHOT_ROOT = _resolve_snapshot_root()


class OptimizationParams(BaseModel):
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
        pnl_data = await portfolio_pnl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    print(params)
    run_params = OptimizerRunParams(
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
            # Save under the same persistent root the list/download endpoints scan,
            # so anything saved here is immediately visible in the snapshots browser.
            save_dir = SNAPSHOT_ROOT / "usecases"
            save_path = usecase.save_auto(save_dir)
            print(f"Saved usecase snapshot to {save_path}")
        result = usecase.run()
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(
            status_code=500,
            detail=f"Optimization save failed: {e}\n\n{tb}",
        )

    return result


@router.get("/snapshots")
async def list_snapshots():
    """List saved optimizer snapshot JSON files under SNAPSHOT_ROOT (recursively).

    Returns each entry's relative path (POSIX-style), size in bytes, and mtime.
    Sorted by mtime descending so the most recent appears first.
    """
    if not SNAPSHOT_ROOT.exists():
        return {"snapshots": [], "root": str(SNAPSHOT_ROOT)}
    items = []
    for p in SNAPSHOT_ROOT.rglob("*.json"):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        items.append({
            "path": p.relative_to(SNAPSHOT_ROOT).as_posix(),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"snapshots": items, "root": str(SNAPSHOT_ROOT)}


@router.get("/snapshots/download")
async def download_snapshot(path: str):
    """Download a single snapshot file by its relative path under SNAPSHOT_ROOT.

    Strict path validation: the resolved target must lie inside SNAPSHOT_ROOT
    and must be an existing .json file. Rejects traversal attempts.
    """
    if not path or "\x00" in path:
        raise HTTPException(400, "Invalid path")
    root = SNAPSHOT_ROOT.resolve()
    candidate = (SNAPSHOT_ROOT / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(400, "Path escapes snapshot root")
    if candidate.suffix.lower() != ".json" or not candidate.is_file():
        raise HTTPException(404, "Snapshot not found")
    return FileResponse(
        candidate,
        media_type="application/json",
        filename=candidate.name,
    )
