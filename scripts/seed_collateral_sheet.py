"""Seed the Collateral Map (counterparty_collateral) from the internal sheet.

Source — "Collateral map - $", USD value of each asset at each counterparty:

    Price (mkt)   Wave         G20          Keyrock        Flowdesk
    USDC   1      1,500,000    0            13,426,168     0
    FIL    0.8    4,791,332    1,600,000    1,600,000      4,320,000
    ETH    1800   0            1,800,000    0              3,600,000
    BTC    64000  0            1,216,000    0              0

Cells in the app are token quantities (USD ÷ price); USD value = qty × price is
recomputed live at the effective price. So we seed quantities derived with the
sheet's reference prices below. Prices themselves are left to default to live
(no manual override seeded) — set overrides in the UI to pin them.

Idempotent: ON CONFLICT(counterparty, asset) updates qty in place.
Run:  ./.venv/Scripts/python.exe scripts/seed_collateral_sheet.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from plgo_options.data.database import get_db, init_db

REF_PRICE = {"USDC": 1.0, "FIL": 0.8, "ETH": 1800.0, "BTC": 64000.0}

# counterparty -> {asset: USD value from the sheet}
SHEET_USD = {
    "Wave":     {"USDC": 1_500_000, "FIL": 4_791_332},
    "G20":      {"FIL": 1_600_000, "ETH": 1_800_000, "BTC": 1_216_000},
    "Keyrock":  {"USDC": 13_426_168, "FIL": 1_600_000},
    "Flowdesk": {"FIL": 4_320_000, "ETH": 3_600_000},
}


async def main() -> None:
    await init_db()
    db = await get_db()
    now = datetime.utcnow().isoformat()
    n = 0
    for cp, by_asset in SHEET_USD.items():
        for asset, usd in by_asset.items():
            qty = usd / REF_PRICE[asset]
            await db.execute(
                """INSERT INTO counterparty_collateral (counterparty, asset, qty, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(counterparty, asset) DO UPDATE SET
                      qty = excluded.qty, updated_at = excluded.updated_at""",
                (cp, asset, qty, now),
            )
            n += 1
    await db.commit()
    print(f"Seeded {n} collateral-map cells across {len(SHEET_USD)} counterparties.")


if __name__ == "__main__":
    asyncio.run(main())
