"""SQLite database for trade persistence and audit logging."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import aiosqlite

from plgo_options.data.trades import read_eth_trades, read_fil_trades

logger = logging.getLogger(__name__)

# Use DB_DIR env var for persistent storage (e.g. GCS FUSE mount on Cloud Run).
# Falls back to local data/ directory for development.
_db_dir = os.environ.get("DB_DIR")
if _db_dir:
    DB_PATH = Path(_db_dir) / "plgo_options.db"
else:
    DB_PATH = Path(__file__).resolve().parents[3] / "data" / "plgo_options.db"

_db: aiosqlite.Connection | None = None

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL DEFAULT 'ETH',
    counterparty TEXT NOT NULL DEFAULT '',
    trade_id TEXT DEFAULT '',
    trade_date TEXT DEFAULT '',
    side TEXT NOT NULL,
    option_type TEXT NOT NULL,
    instrument TEXT DEFAULT '',
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    ref_spot REAL DEFAULT 0,
    pct_otm REAL DEFAULT 0,
    qty REAL NOT NULL,
    notional_mm REAL DEFAULT 0,
    premium_per REAL DEFAULT 0,
    premium_usd REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,
    field_changed TEXT,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT DEFAULT 'system'
);
"""


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        # Use DELETE journal mode for GCS FUSE compatibility (WAL doesn't sync reliably)
        await _db.execute("PRAGMA journal_mode=DELETE")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def init_db():
    """Create tables, run migrations, and auto-import from Excel if DB is empty."""
    db = await get_db()
    await db.execute(TRADES_SCHEMA)
    await db.execute(AUDIT_SCHEMA)
    await db.commit()

    # Migration: add 'asset' column if missing (existing DBs)
    cursor = await db.execute("PRAGMA table_info(trades)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "asset" not in columns:
        logger.info("Migrating: adding 'asset' column to trades table...")
        await db.execute("ALTER TABLE trades ADD COLUMN asset TEXT NOT NULL DEFAULT 'ETH'")
        await db.commit()

    # Auto-import from Excel on first run
    cursor = await db.execute("SELECT COUNT(*) FROM trades")
    row = await cursor.fetchone()
    count = row[0] if row else 0

    if count == 0:
        logger.info("Empty database — importing trades from Excel...")
        try:
            excel_trades = read_eth_trades()
            imported = await _import_excel_trades(db, excel_trades, asset="ETH")
            logger.info("Imported %d ETH trades from Excel", imported)
        except Exception as e:
            logger.warning("ETH Excel import failed: %s", e)

    # Auto-import FIL trades if none exist yet
    cursor = await db.execute("SELECT COUNT(*) FROM trades WHERE asset = 'FIL'")
    row = await cursor.fetchone()
    fil_count = row[0] if row else 0

    if fil_count == 0:
        logger.info("No FIL trades — importing from FIL Excel...")
        try:
            fil_trades = read_fil_trades()
            imported = await _import_excel_trades(db, fil_trades, asset="FIL")
            logger.info("Imported %d FIL trades from Excel", imported)
        except Exception as e:
            logger.warning("FIL Excel import failed: %s", e)


    # Auto-expire trades past their expiry date (runs every startup)
    await _auto_expire_trades(db)


async def _auto_expire_trades(db: aiosqlite.Connection):
    """Mark active trades with expiry < today as expired (expire the day after expiry)."""
    from datetime import date
    today = date.today().isoformat()
    cursor = await db.execute(
        "SELECT id FROM trades WHERE status = 'active' AND expiry < ? AND expiry != ''",
        (today,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return
    ids = [row[0] for row in rows]
    await db.execute(
        f"UPDATE trades SET status = 'expired', updated_at = datetime('now') "
        f"WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    for tid in ids:
        await db.execute(
            "INSERT INTO trade_audit_log (trade_id, action, field_changed, old_value, new_value, changed_by) "
            "VALUES (?, 'update', 'status', 'active', 'expired', 'auto_expiry')",
            (tid,),
        )
    await db.commit()
    logger.info("Auto-expired %d trades (expiry < %s)", len(ids), today)


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _import_excel_trades(
    db: aiosqlite.Connection, excel_trades: list[dict], asset: str = "ETH",
) -> int:
    """Map Excel column names to DB columns and insert."""
    # Detect qty column name — ETH uses "ETH Options", FIL may use "FIL Options" or "Options"
    qty_col = "ETH Options"
    if asset != "ETH":
        for candidate in [f"{asset} Options", "FIL Options", "Options", "Qty", "Quantity"]:
            if any(candidate in t for t in excel_trades[:1]):
                qty_col = candidate
                break

    count = 0
    for t in excel_trades:
        counterparty = str(t.get("Counterparty") or "").strip()
        trade_id = str(t.get("Trade_ID") or t.get("ID") or "").strip()
        trade_date = str(t.get("Initial Trade Date") or "").strip()
        if "T" in trade_date:
            trade_date = trade_date.split("T")[0]
        side = str(t.get("Buy / Sell / Unwind") or "").strip()
        option_type = str(t.get("Option Type") or "").strip()
        expiry = str(t.get("Option Expiry Date") or "").strip()
        if "T" in expiry:
            expiry = expiry.split("T")[0]
        strike = _safe_float(t.get("Strike"))
        ref_spot = _safe_float(t.get("Ref. Spot Price"))
        pct_otm = _safe_float(t.get("% OTM"))
        qty = _safe_float(t.get(qty_col) or t.get("ETH Options"))
        notional_mm = _safe_float(t.get("$ Notional (mm)"))
        premium_per = _safe_float(t.get("Premium per Contract"))
        premium_usd = _safe_float(t.get("Premium USD"))

        if strike <= 0 or qty <= 0:
            continue

        cursor = await db.execute(
            """INSERT INTO trades
               (asset, counterparty, trade_id, trade_date, side, option_type,
                expiry, strike, ref_spot, pct_otm, qty,
                notional_mm, premium_per, premium_usd, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            (asset, counterparty, trade_id, trade_date, side, option_type,
             expiry, strike, ref_spot, pct_otm, qty,
             notional_mm, premium_per, premium_usd),
        )
        new_id = cursor.lastrowid

        await db.execute(
            """INSERT INTO trade_audit_log (trade_id, action, changed_by)
               VALUES (?, 'create', 'excel_import')""",
            (new_id,),
        )
        count += 1

    await db.commit()
    return count
