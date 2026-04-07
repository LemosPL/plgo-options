from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from plgo_options.optimization.models import Position


SNAPSHOT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "optimization_snapshots"

def save_snapshot(data: dict) -> Path:
    """Write *data* to a timestamped JSON file and return the path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOT_DIR / f"snapshot_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path

def load_snapshot_dict(data: dict) -> tuple[dict, list[Position]]:
    positions = [
        Position(
            id=p["id"],
            instrument=p["instrument"],
            opt=p["opt"],
            side=p["side"],
            strike=p["strike"],
            expiry=p["expiry"],
            days_remaining=p["days_remaining"],
            net_qty=p["net_qty"],
            iv_pct=p["iv_pct"],
            delta=p.get("delta"),
            gamma=p.get("gamma"),
            theta=p.get("theta"),
            vega=p.get("vega"),
            mark_price_usd=p["mark_price_usd"],
            current_mtm=p["current_mtm"],
            payoff_by_horizon=p["payoff_by_horizon"],
            mtm_by_horizon=p["mtm_by_horizon"],
            counterparty=p.get("counterparty", "brokerage"),
        )
        for p in data["positions"]
    ]
    return data, positions


def load_snapshot(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)
