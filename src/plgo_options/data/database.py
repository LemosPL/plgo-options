"""SQLite database for trade persistence and audit logging."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from plgo_options.data.trades import read_eth_trades

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "plgo_options.db"

_db: aiosqlite.Connection | None = None

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def init_db():
    """Create tables and auto-import from Excel if DB is empty."""
    db = await get_db()
    await db.execute(TRADES_SCHEMA)
    await db.execute(AUDIT_SCHEMA)
    await db.commit()

    # Auto-import from Excel on first run
    cursor = await db.execute("SELECT COUNT(*) FROM trades")
    row = await cursor.fetchone()
    count = row[0] if row else 0

    if count == 0:
        logger.info("Empty database — importing trades from Excel...")
        try:
            excel_trades = read_eth_trades()
            imported = await _import_excel_trades(db, excel_trades)
            logger.info("Imported %d trades from Excel", imported)
        except Exception as e:
            logger.warning("Excel import failed: %s", e)


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


async def _import_excel_trades(db: aiosqlite.Connection, excel_trades: list[dict]) -> int:
    """Map Excel column names to DB columns and insert."""
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
        qty = _safe_float(t.get("ETH Options"))
        notional_mm = _safe_float(t.get("$ Notional (mm)"))
        premium_per = _safe_float(t.get("Premium per Contract"))
        premium_usd = _safe_float(t.get("Premium USD"))

        if strike <= 0 or qty <= 0:
            continue

        cursor = await db.execute(
            """INSERT INTO trades
               (counterparty, trade_id, trade_date, side, option_type,
                expiry, strike, ref_spot, pct_otm, qty,
                notional_mm, premium_per, premium_usd, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            (counterparty, trade_id, trade_date, side, option_type,
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
