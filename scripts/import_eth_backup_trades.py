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
    DB_PATH,
    TRADES_SCHEMA,
    _auto_expire_trades,
    _import_excel_trades,
    close_db,
    get_db,
)
from plgo_options.data.trades import read_eth_trades  # noqa: E402


BACKUP_XLSX = PROJECT_ROOT / "data" / "positions" / "PLGO_Trades_2026-05-26.xlsx"


async def main() -> None:
    if not BACKUP_XLSX.exists():
        raise FileNotFoundError(f"Backup Excel file not found: {BACKUP_XLSX}")

    print(f"Using database: {DB_PATH}")
    print(f"Reading backup trades from: {BACKUP_XLSX}")

    db = await get_db()

    await db.execute(TRADES_SCHEMA)
    await db.execute(AUDIT_SCHEMA)
    await db.commit()

    excel_trades = read_eth_trades(BACKUP_XLSX)
    print(f"Read {len(excel_trades)} rows from Excel")

    cursor = await db.execute("SELECT COUNT(*) FROM trades WHERE asset = 'ETH'")
    row = await cursor.fetchone()
    existing_eth_count = row[0] if row else 0
    print(f"Existing ETH trades in DB: {existing_eth_count}")

    if existing_eth_count:
        answer = input("Delete existing ETH trades before import? [y/N]: ").strip().lower()
        if answer == "y":
            await db.execute(
                """
                DELETE FROM trade_audit_log
                WHERE trade_id IN (SELECT id FROM trades WHERE asset = 'ETH')
                """
            )
            await db.execute("DELETE FROM trades WHERE asset = 'ETH'")
            await db.commit()
            print("Deleted existing ETH trades")
        else:
            print("Keeping existing ETH trades; new rows may create duplicates")

    imported = await _import_excel_trades(db, excel_trades, asset="ETH")
    print(f"Imported {imported} ETH trades")

    await _auto_expire_trades(db)

    cursor = await db.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM trades
        WHERE asset = 'ETH'
        GROUP BY status
        ORDER BY status
        """
    )
    status_rows = await cursor.fetchall()

    print("ETH trade counts by status:")
    for status_row in status_rows:
        print(f"  {status_row['status']}: {status_row['count']}")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())