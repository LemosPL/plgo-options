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
    is_otc INTEGER NOT NULL DEFAULT 0,
    last_otc_quote REAL,
    otc_settlement_method TEXT,
    otc_override_price REAL,
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

RECON_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL DEFAULT (datetime('now')),
    counterparty TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT 'ETH',
    our_count INTEGER NOT NULL DEFAULT 0,
    their_count INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    breaks INTEGER NOT NULL DEFAULT 0,
    only_ours INTEGER NOT NULL DEFAULT 0,
    only_theirs INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'clean',
    notes TEXT DEFAULT '',
    created_by TEXT DEFAULT 'user'
);
"""

MTM_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_mtm_history (
    snapshot_date TEXT NOT NULL,
    asset TEXT NOT NULL,
    spot REAL NOT NULL DEFAULT 0,
    mtm_usd REAL NOT NULL DEFAULT 0,
    position_count INTEGER NOT NULL DEFAULT 0,
    delta REAL DEFAULT 0,
    gamma REAL DEFAULT 0,
    theta REAL DEFAULT 0,
    vega REAL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (snapshot_date, asset)
);
"""

COLLATERAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS counterparty_collateral (
    counterparty TEXT NOT NULL COLLATE NOCASE,
    asset TEXT NOT NULL COLLATE NOCASE,
    qty REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (counterparty, asset)
);
"""

# Manual per-asset price overrides for the collateral map. When a row exists,
# it overrides the live market price for that asset; otherwise the live feed
# (or 1.0 for USDC) is used.
COLLATERAL_PRICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS collateral_price (
    asset TEXT PRIMARY KEY COLLATE NOCASE,
    price REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One row per (counterparty, portfolio_asset). Tracks collateral posted (in
# ETH and FIL) against that asset's options book, plus the USD figure the
# counterparty themselves are asking for (for side-by-side comparison).
MARGIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS counterparty_margin (
    counterparty TEXT NOT NULL COLLATE NOCASE,
    portfolio_asset TEXT NOT NULL COLLATE NOCASE,
    eth_qty REAL NOT NULL DEFAULT 0,
    fil_qty REAL NOT NULL DEFAULT 0,
    usdc_usd REAL NOT NULL DEFAULT 0,
    btc_qty REAL NOT NULL DEFAULT 0,
    wave_qty REAL NOT NULL DEFAULT 0,
    wave_price REAL NOT NULL DEFAULT 0,
    margin_req_tokens REAL NOT NULL DEFAULT 0,
    requested_usd REAL,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (counterparty, portfolio_asset)
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
        # Wait up to 5s for a write lock instead of failing immediately with
        # "database is locked" (can happen if another process/connection is
        # mid-write, e.g. a stray dev server or a seed script).
        await _db.execute("PRAGMA busy_timeout=5000")
    return _db


async def init_db():
    """Create tables, run migrations, and auto-import from Excel if DB is empty."""
    db = await get_db()
    await db.execute(TRADES_SCHEMA)
    await db.execute(AUDIT_SCHEMA)
    await db.execute(MTM_HISTORY_SCHEMA)
    await db.execute(COLLATERAL_SCHEMA)
    await db.execute(COLLATERAL_PRICE_SCHEMA)
    await db.execute(MARGIN_SCHEMA)
    await db.execute(RECON_HISTORY_SCHEMA)
    await db.commit()

    # Migration: add 'asset' column if missing (existing DBs)
    cursor = await db.execute("PRAGMA table_info(trades)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "asset" not in columns:
        logger.info("Migrating: adding 'asset' column to trades table...")
        await db.execute("ALTER TABLE trades ADD COLUMN asset TEXT NOT NULL DEFAULT 'ETH'")
        await db.commit()

    # Migration: add OTC metadata columns if missing
    otc_migrations = [
        ("is_otc", "INTEGER NOT NULL DEFAULT 0"),
        ("last_otc_quote", "REAL"),
        ("otc_settlement_method", "TEXT"),
        ("otc_override_price", "REAL"),
    ]
    cursor = await db.execute("PRAGMA table_info(trades)")
    columns = {row[1] for row in await cursor.fetchall()}
    for col_name, col_type in otc_migrations:
        if col_name not in columns:
            logger.info("Migrating: adding %r column to trades table...", col_name)
            await db.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
    await db.commit()

    # Migration: richer collateral on counterparty_margin (USD/USDC cash, BTC,
    # WAVE token + manual price, and the counterparty's margin requirement in
    # the book's native token). Existing DBs only had eth_qty/fil_qty.
    margin_migrations = [
        ("usdc_usd", "REAL NOT NULL DEFAULT 0"),
        ("btc_qty", "REAL NOT NULL DEFAULT 0"),
        ("wave_qty", "REAL NOT NULL DEFAULT 0"),
        ("wave_price", "REAL NOT NULL DEFAULT 0"),
        ("margin_req_tokens", "REAL NOT NULL DEFAULT 0"),
    ]
    cursor = await db.execute("PRAGMA table_info(counterparty_margin)")
    columns = {row[1] for row in await cursor.fetchall()}
    for col_name, col_type in margin_migrations:
        if col_name not in columns:
            logger.info("Migrating: adding %r column to counterparty_margin...", col_name)
            await db.execute(f"ALTER TABLE counterparty_margin ADD COLUMN {col_name} {col_type}")
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


    # Normalize trade fields (side, option_type, counterparty, trade_id) on startup
    await _normalize_trade_fields(db)

    # Auto-expire trades past their expiry date (runs every startup)
    await _auto_expire_trades(db)


async def _normalize_trade_fields(db: aiosqlite.Connection):
    """Normalize side, option_type, counterparty case, clear UUID trade_ids, fix expiry dates, build instruments."""
    import re
    from datetime import datetime as _dt
    _UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
    _DDMMMYY_RE = re.compile(r'^(\d{1,2})([A-Z]{3})(\d{2})$', re.I)

    cursor = await db.execute("SELECT id, asset, side, option_type, counterparty, trade_id, expiry, strike, instrument FROM trades")
    rows = await cursor.fetchall()

    # Build canonical counterparty casing: use the most frequent casing per lowercase name
    cp_counts: dict[str, dict[str, int]] = {}
    for r in rows:
        cp = (r["counterparty"] or "").strip()
        if cp:
            cp_counts.setdefault(cp.lower(), {})
            cp_counts[cp.lower()][cp] = cp_counts[cp.lower()].get(cp, 0) + 1
    canonical_cp: dict[str, str] = {}
    for lower, variants in cp_counts.items():
        canonical_cp[lower] = max(variants, key=variants.get)

    changes = 0
    for r in rows:
        updates = {}
        # Normalize side to title case
        side = (r["side"] or "").strip()
        side_lower = side.lower()
        if side_lower in ("buy", "buys", "bought", "long", "b", "l"):
            if side != "Buy":
                updates["side"] = "Buy"
        elif side_lower in ("sell", "sells", "sold", "short", "s"):
            if side != "Sell":
                updates["side"] = "Sell"

        # Normalize option_type to title case
        otype = (r["option_type"] or "").strip()
        otype_lower = otype.lower()
        if otype_lower in ("c", "call", "calls"):
            if otype != "Call":
                updates["option_type"] = "Call"
        elif otype_lower in ("p", "put", "puts"):
            if otype != "Put":
                updates["option_type"] = "Put"

        # Normalize counterparty to canonical casing
        cp = (r["counterparty"] or "").strip()
        if cp and cp.lower() in canonical_cp and cp != canonical_cp[cp.lower()]:
            updates["counterparty"] = canonical_cp[cp.lower()]

        # Clear UUID-like trade_ids (from counterparty recon imports)
        tid = (r["trade_id"] or "").strip()
        if tid and _UUID_RE.match(tid):
            updates["trade_id"] = ""

        # Normalize expiry dates: DDMMMYY → YYYY-MM-DD
        expiry = (r["expiry"] or "").strip()
        m = _DDMMMYY_RE.match(expiry)
        if m:
            try:
                dt = _dt.strptime(f"{m.group(1)}{m.group(2).upper()}{m.group(3)}", "%d%b%y")
                updates["expiry"] = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

        # Build missing instrument names
        instrument = (r["instrument"] or "").strip()
        final_expiry = updates.get("expiry", expiry)
        strike = r["strike"] or 0
        final_otype = updates.get("option_type", otype)
        if not instrument and final_expiry and strike > 0:
            try:
                ed = _dt.strptime(final_expiry, "%Y-%m-%d")
                prefix = r["asset"] or "ETH"
                opt_code = "P" if final_otype.lower() == "put" else "C"
                strike_str = str(int(strike)) if strike == int(strike) else str(strike)
                updates["instrument"] = f"{prefix}-{ed.day}{ed.strftime('%b').upper()}{ed.strftime('%y')}-{strike_str}-{opt_code}"
            except ValueError:
                pass

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [r["id"]]
            await db.execute(f"UPDATE trades SET {set_clause}, updated_at = datetime('now') WHERE id = ?", values)
            changes += 1

    if changes:
        await db.commit()
        logger.info("Normalized %d trades (side/option_type/counterparty/trade_id)", changes)


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
