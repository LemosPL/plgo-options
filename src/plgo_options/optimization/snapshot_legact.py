"""Dump all optimizer inputs to a timestamped JSON snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "optimization_snapshots"


def save_snapshot(data: dict) -> Path:
    """Write *data* to a timestamped JSON file and return the path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOT_DIR / f"snapshot_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path
