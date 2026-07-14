"""Sync the trades DB from the latest data/positions/*.xlsx (or FIL_positions/).

Upsert only: existing DB rows are matched by (counterparty, expiry, strike,
option_type) and have their mutable fields (qty, premium, notional, etc.)
refreshed in place — their id never changes. Trades in the spreadsheet with
no match are inserted as new. Trades in the DB but absent from the
spreadsheet are left untouched (never deleted or expired here).

Usage:
    python scripts/sync_trades_from_xlsx.py [ETH|FIL] [path/to/file.xlsx]

If no path is given, uses the most recently modified *.xlsx in the asset's
positions directory (skipping Office lock files, e.g. ~$foo.xlsx).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from plgo_options.data.database import (  # noqa: E402
    AUDIT_SCHEMA,
    TRADES_SCHEMA,
    close_db,
    get_db,
    sync_excel_trades,
)
from plgo_options.data.trades import read_eth_trades, read_fil_trades  # noqa: E402


def _latest_xlsx(directory: Path) -> Path | None:
    files = sorted(
        (p for p in directory.glob("*.xlsx") if not p.name.startswith("~$")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


async def main() -> None:
    asset = (sys.argv[1].upper() if len(sys.argv) > 1 else "ETH")
    if asset not in ("ETH", "FIL"):
        raise SystemExit(f"Unsupported asset: {asset}")

    if len(sys.argv) > 2:
        path = Path(sys.argv[2])
    else:
        positions_dir = PROJECT_ROOT / "data" / ("positions" if asset == "ETH" else "FIL_positions")
        path = _latest_xlsx(positions_dir)
        if path is None:
            raise SystemExit(f"No .xlsx files found in {positions_dir}")

    print(f"Syncing {asset} trades from: {path}")

    db = await get_db()
    await db.execute(TRADES_SCHEMA)
    await db.execute(AUDIT_SCHEMA)
    await db.commit()

    excel_trades = read_eth_trades(path) if asset == "ETH" else read_fil_trades(path)
    print(f"Read {len(excel_trades)} rows from Excel")

    result = await sync_excel_trades(db, excel_trades, asset=asset)
    print(f"Matched (unchanged): {result['matched'] - result['updated']}")
    print(f"Matched (updated):   {result['updated']}")
    print(f"Inserted (new):      {result['inserted']}")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
