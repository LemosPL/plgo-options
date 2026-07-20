"""Optimizer v2 endpoints."""

from __future__ import annotations

import json
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
    asset: str = "ETH"
    lam_factor: float = 0.2
    mu_factor: float = 0.0
    target_expiry: str | None = None
    unwind_discount: float = 0.2
    new_position_penalty: float = 0.04
    roll_dte_threshold: int | None = None
    roll_itm_only: bool = False
    collateral_budget_pct: float | None = None
    save_usecase_snapshot: bool = False
    is_replay: bool = False
    counterparties: list[str] | None = None
    collateral_tier_free_pct: float | dict[str, float] = 0.0
    collateral_tier_mu: float | dict[str, float] | None = None
    forced_roll_ids: list[int] | None = None
    cash_neutrality_factor: float | dict[str, float] = 0.0
    max_qty: float | None = None
    max_trades: int | None = None
    enable_box_neutralizer: bool = True
    downside_factor: float = 1.0
    t90_weight: float = 0.0
    # Optional allow-list of DB trade ids: when set, the optimizer's *input book*
    # is scoped to exactly these trades (the "current portfolio" it optimizes
    # against becomes only this subset), instead of the whole asset book. Sourced
    # from a selection of deals on the Deals / Risk screen. None/empty = full book.
    base_trade_ids: list[int] | None = None
    # Optional user-drawn target payoff the LP fits to, replacing the parametric
    # target. List of {"x": spot, "y": payoff} control points (>=2), interpolated
    # onto the optimizer's spot ladder. None = use the built-in parametric target.
    manual_target: list[dict] | None = None

@router.post("/run")
async def run_optimizer(params: OptimizationParams):
    """Gather optimizer inputs, persist a reproducible use case, and run it."""
    print("run_optimizer()")
    try:
        # Matches "Load Risk Profile"'s own /pnl fetch (include_expired defaults to
        # False there) — this used to be harmless since the optimizer discarded
        # whatever positions it was given in favor of a fresh xlsx re-read; now
        # that it uses these positions directly, True here would flood the book
        # with every historically-expired trade as if it were still live.
        pnl_data = await portfolio_pnl(asset=params.asset.upper(), include_expired=False)
    except HTTPException as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Failed to gather portfolio data for asset {params.asset.upper()}: {e.detail}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    # Scope the input book to a caller-selected subset of trades, if requested.
    # Positions in the /pnl payload carry the DB trade id under "id" (same id the
    # forced_roll_ids / Deals-screen leg_ids use), so we filter by it here — the
    # LP engine then treats this subset as the entire current portfolio.
    if params.base_trade_ids:
        wanted = {int(i) for i in params.base_trade_ids}
        all_positions = pnl_data.get("positions", []) or []
        filtered = [p for p in all_positions if int(p.get("id", -1)) in wanted]
        if not filtered:
            raise HTTPException(
                status_code=400,
                detail=("None of the selected trades were found in the current "
                        f"{params.asset.upper()} book (they may be expired or from "
                        "a different asset)."),
            )
        pnl_data["positions"] = filtered
        print(f"base_trade_ids: scoped book to {len(filtered)}/{len(all_positions)} positions")

    print(params)
    run_params = OptimizerRunParams(
        asset=params.asset.upper(),
        lam_factor=params.lam_factor,
        mu_factor=params.mu_factor,
        target_expiry=params.target_expiry,
        unwind_discount=params.unwind_discount,
        new_position_penalty=params.new_position_penalty,
        roll_dte_threshold=params.roll_dte_threshold,
        roll_itm_only=params.roll_itm_only,
        collateral_budget_pct=params.collateral_budget_pct,
        is_replay=False,
        counterparties=params.counterparties,
        collateral_tier_free_pct=params.collateral_tier_free_pct,
        collateral_tier_mu=params.collateral_tier_mu,
        forced_roll_ids=params.forced_roll_ids,
        cash_neutrality_factor=params.cash_neutrality_factor,
        max_qty=params.max_qty,
        max_trades=params.max_trades,
        enable_box_neutralizer=params.enable_box_neutralizer,
        downside_factor=params.downside_factor,
        t90_weight=params.t90_weight,
        manual_target=params.manual_target,
    )

    usecase = OptimizerUseCase.from_portfolio_payload(pnl_data, run_params)
    try:
        result = usecase.run()
        if params.save_usecase_snapshot:
            # Save AFTER run() so the snapshot captures the result, not just the
            # inputs. Written under the same persistent root the list/download
            # endpoints scan, so it's immediately visible in the snapshots browser.
            save_dir = SNAPSHOT_ROOT / "usecases"
            save_path = usecase.save_auto(save_dir)
            print(f"Saved usecase snapshot (with result) to {save_path}")
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(
            status_code=500,
            detail=f"Optimization failed: {e}\n\n{tb}",
        )

    return result


class TargetProfileRequest(BaseModel):
    asset: str = "ETH"
    spot_ladder: list[float]
    current_spot: float


@router.post("/target-profile")
async def target_profile(req: TargetProfileRequest):
    """Return the built-in parametric target payoff aligned to the given spot
    ladder, so the UI can show/seed the default target before any optimizer run.
    Mirrors exactly what run_lp fits to when no manual target is supplied."""
    import numpy as np
    from plgo_options.optimization.misc_utils import build_parametric_target_profile

    asset = (req.asset or "ETH").upper()
    if not req.spot_ladder or req.current_spot <= 0:
        raise HTTPException(400, "spot_ladder and a positive current_spot are required.")
    try:
        df = build_parametric_target_profile(
            asset, spot_ladder=req.spot_ladder, current_spot=req.current_spot,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to build target profile: {e}")

    strikes = np.asarray(df.index, dtype=float)
    payoff = np.asarray(df["Payoff($)"], dtype=float)
    ladder = np.asarray(req.spot_ladder, dtype=float)
    # FIL's parametric profile uses its own strike grid; interpolate onto the
    # caller's ladder so the returned payoff is index-aligned to spot_ladder.
    aligned = np.interp(ladder, strikes, payoff)
    return {
        "asset": asset,
        "spot_ladder": [float(x) for x in ladder],
        "payoff": [float(v) for v in aligned],
    }


# Listing/download read from the same Cloud-Run-aware root the optimizer saves to
# (SNAPSHOT_ROOT/usecases), so snapshots persist on the GCS FUSE mount in prod.
SNAPSHOT_DIR = SNAPSHOT_ROOT / "usecases"


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
