"""Storage for cash-settled forwards (Forwards screen).

forward_trades: one row per forward. status: open -> settled (fixing price and
realised P&L recorded at maturity) or deleted (soft delete; the row is kept).
The counterparty's own figures (cpty_*) are stored on the trade for the
reconciliation view. forward_carry: the annual carry rate per asset used to
mark open forwards (see pricing/forwards.py).
"""
from __future__ import annotations

FORWARD_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL COLLATE NOCASE,
    counterparty TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    forward_price REAL NOT NULL,
    trade_date TEXT NOT NULL,
    maturity_date TEXT NOT NULL,
    settlement TEXT NOT NULL DEFAULT 'cash',
    external_ref TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    fixing_price REAL,
    settled_pnl_usd REAL,
    settled_at TEXT,
    cpty_qty REAL,
    cpty_price REAL,
    cpty_mtm REAL,
    recon_note TEXT NOT NULL DEFAULT '',
    recon_at TEXT,
    recon_by TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

FORWARD_CARRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_carry (
    asset TEXT PRIMARY KEY COLLATE NOCASE,
    rate REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

EDITABLE = ("asset", "counterparty", "side", "qty", "forward_price", "trade_date",
            "maturity_date", "external_ref", "note")


async def ensure_tables(db) -> None:
    await db.execute(FORWARD_TRADES_SCHEMA)
    await db.execute(FORWARD_CARRY_SCHEMA)


async def list_forwards(db, asset: str | None = None, statuses: tuple[str, ...] = ("open",)) -> list[dict]:
    q = "SELECT * FROM forward_trades WHERE status IN (%s)" % ",".join("?" * len(statuses))
    args: list = list(statuses)
    if asset:
        q += " AND asset = ?"
        args.append(asset.upper())
    q += " ORDER BY maturity_date, counterparty, id"
    cur = await db.execute(q, args)
    return [dict(r) for r in await cur.fetchall()]


async def get_forward(db, fid: int) -> dict | None:
    cur = await db.execute("SELECT * FROM forward_trades WHERE id = ?", (fid,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def insert_forward(db, f: dict, user: str) -> int:
    cur = await db.execute(
        """INSERT INTO forward_trades (asset, counterparty, side, qty, forward_price, trade_date,
               maturity_date, external_ref, note, created_by, updated_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f["asset"].upper(), f["counterparty"], f["side"], f["qty"], f["forward_price"],
         f["trade_date"], f["maturity_date"], f.get("external_ref") or "", f.get("note") or "", user, user))
    await db.commit()
    return cur.lastrowid


async def update_forward(db, fid: int, f: dict, user: str) -> None:
    sets = [f"{k} = ?" for k in EDITABLE if k in f]
    args = [f[k].upper() if k == "asset" else f[k] for k in EDITABLE if k in f]
    if not sets:
        return
    await db.execute(
        f"UPDATE forward_trades SET {', '.join(sets)}, updated_by = ?, updated_at = datetime('now') WHERE id = ?",
        (*args, user, fid))
    await db.commit()


async def set_status(db, fid: int, status: str, user: str, **extra) -> None:
    cols = ["status = ?"] + [f"{k} = ?" for k in extra]
    await db.execute(
        f"UPDATE forward_trades SET {', '.join(cols)}, updated_by = ?, updated_at = datetime('now') WHERE id = ?",
        (status, *extra.values(), user, fid))
    await db.commit()


async def set_recon(db, fid: int, cpty_qty, cpty_price, cpty_mtm, note: str, user: str) -> None:
    await db.execute(
        """UPDATE forward_trades SET cpty_qty = ?, cpty_price = ?, cpty_mtm = ?, recon_note = ?,
               recon_at = datetime('now'), recon_by = ? WHERE id = ?""",
        (cpty_qty, cpty_price, cpty_mtm, note or "", user, fid))
    await db.commit()


async def get_carry(db, asset: str) -> dict:
    cur = await db.execute("SELECT rate, updated_by, updated_at FROM forward_carry WHERE asset = ?", (asset.upper(),))
    row = await cur.fetchone()
    return dict(row) if row else {"rate": 0.0, "updated_by": "", "updated_at": None}


async def set_carry(db, asset: str, rate: float, user: str) -> None:
    await db.execute(
        """INSERT INTO forward_carry (asset, rate, updated_by, updated_at) VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(asset) DO UPDATE SET rate = excluded.rate, updated_by = excluded.updated_by,
             updated_at = excluded.updated_at""",
        (asset.upper(), rate, user))
    await db.commit()
