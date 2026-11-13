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

# Stop-loss / take-profit levels, managed on the Forwards Monitor and kept OFF
# the trade: one row per monitored position, keyed by the position, so the same
# store covers a booked forward ("fwd:<forward id>") and a synthetic forward
# built from options ("syn:<call trade id>-<put trade id>"). Levels are prices
# of the underlying spot. Deleting the row clears the levels.
FORWARD_LEVELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_levels (
    key TEXT PRIMARY KEY,
    asset TEXT NOT NULL COLLATE NOCASE,
    stop_loss REAL,
    take_profit REAL,
    note TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def ensure_tables(db) -> None:
    await db.execute(FORWARD_TRADES_SCHEMA)
    await db.execute(FORWARD_CARRY_SCHEMA)
    await db.execute(FORWARD_LEVELS_SCHEMA)


async def get_levels(db, asset: str | None = None) -> dict[str, dict]:
    """Levels by position key."""
    q, args = "SELECT * FROM forward_levels", []
    if asset:
        q += " WHERE asset = ?"
        args.append(asset.upper())
    cur = await db.execute(q, args)
    return {r["key"]: dict(r) for r in await cur.fetchall()}


async def set_levels(db, key: str, asset: str, stop_loss, take_profit, note: str, user: str) -> None:
    """Upsert one position's levels; with no stop, no target and no note the row goes."""
    if stop_loss is None and take_profit is None and not (note or "").strip():
        await db.execute("DELETE FROM forward_levels WHERE key = ?", (key,))
    else:
        await db.execute(
            """INSERT INTO forward_levels (key, asset, stop_loss, take_profit, note, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET asset = excluded.asset, stop_loss = excluded.stop_loss,
                   take_profit = excluded.take_profit, note = excluded.note,
                   updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
            (key, asset.upper(), stop_loss, take_profit, (note or "").strip(), user))
    await db.commit()


# Forwards booked from a counterparty confirmation, inserted once at startup
# (init_db). Keyed on the confirmation's trade id: skipped if any row (open,
# settled or deleted) already carries it, so it is never booked twice and a
# deliberate delete stays deleted.
CONFIRMED_FORWARDS = [
    {   # Flowdesk confirmation, 27 Oct 2026: we sold 2,500 ETH/USDC, cash-settled
        # against the Deribit 08:00 UTC fixing on 27 Nov 2026.
        "asset": "ETH", "counterparty": "Flowdesk", "side": "sell", "qty": 2500.0,
        "forward_price": 2346.5, "trade_date": "2026-10-27", "maturity_date": "2026-11-27",
        "external_ref": "e45b44b1-1592-4201-86ec-bd5a7f71a0b7",
        "note": "Cash settlement vs Deribit 0800 UTC, 27Nov26 08:00 UTC (from Flowdesk confirmation)",
    },
    {   # Confirmation, trade date 9 Nov 2026: we sold 1,250 ETH/USDC, cash-settled
        # against the Deribit 08:00 UTC fixing on 27 Nov 2026. Counterparty not
        # named on the confirmation (same template as Flowdesk's) - edit if not Flowdesk.
        "asset": "ETH", "counterparty": "Flowdesk", "side": "sell", "qty": 1250.0,
        "forward_price": 2108.1, "trade_date": "2026-11-09", "maturity_date": "2026-11-27",
        "external_ref": "ad76a4c0-0380-4ded-8305-84d8fd4ba3a1",
        "note": "Cash settlement vs Deribit 0800 UTC, 27Nov26 08:00 UTC (from confirmation)",
    },
    {   # Confirmation, trade date 10 Nov 2026: we sold 5,000,000 FIL/USDC, cash-settled
        # against the Binance 08:00 UTC fixing on 27 Nov 2026. Counterparty not
        # named on the confirmation (same template as Flowdesk's) - edit if not Flowdesk.
        "asset": "FIL", "counterparty": "Flowdesk", "side": "sell", "qty": 5_000_000.0,
        "forward_price": 0.7544, "trade_date": "2026-11-10", "maturity_date": "2026-11-27",
        "external_ref": "5e93d282-3a26-4e97-9357-4c6ccfb5f45b",
        "note": "Cash settlement vs Binance 0800 UTC, 27Nov26 08:00 UTC (from confirmation)",
    },
]

# Stop / target orders given by the desk, applied ONCE per (tag, version);
# later edits on the Monitor stick. A level targets the forward carrying a
# confirmation id ("external_ref") or every open forward matching "match"
# (asset, side, qty, forward_price, maturity_date) for hand-booked ones.
# Short forwards: stop above, target below.
_ETH_NOV27 = {"stop_loss": 2236.5, "take_profit": 1900.0}
CONFIRMED_LEVELS = [
    {"external_ref": "e45b44b1-1592-4201-86ec-bd5a7f71a0b7", "version": 1, **_ETH_NOV27,
     "note": "Orders update: TP 2,500 ETH @ 1900, SL 2,500 ETH @ 2236.5"},
    {"external_ref": "ad76a4c0-0380-4ded-8305-84d8fd4ba3a1", "version": 1, **_ETH_NOV27,
     "note": "Same TP 1900 / SL 2236.5 as the 2,500 ETH"},
    {"tag": "eth-sell-55-1961-27nov", "version": 1, **_ETH_NOV27,
     "match": {"asset": "ETH", "side": "sell", "qty": 55.0, "forward_price": 1961.0, "maturity_date": "2026-11-27"},
     "note": "Same TP 1900 / SL 2236.5 as the 2,500 ETH"},
    {"external_ref": "5e93d282-3a26-4e97-9357-4c6ccfb5f45b", "version": 1,
     "stop_loss": 0.83, "take_profit": 0.67,
     "note": "SL / TP ~10% around the 0.7544 fill"},
]

# Same, for synthetic forwards booked as options: matched on the monitor (the
# option legs may be booked after startup) by asset / side / strike / qty /
# expiry, since the trade ids are only known on the live book.
CONFIRMED_SYNTHETIC_LEVELS = [
    {   # KeyRock 10 Nov 2026 1-month forward as options: PL buys 4M FIL 11DEC26
        # 0.7564 P and sells 4M 0.7564 C, zero net premium = short 4M @ 0.7564.
        "tag": "keyrock-fil-11dec26-0.7564", "version": 1, "asset": "FIL", "side": "sell",
        "strike": 0.7564, "qty": 4_000_000.0, "expiry": "2026-12-11",
        "stop_loss": 0.83, "take_profit": 0.67,
        "note": "SL / TP ~10% around the 0.7564 strike",
    },
]

FORWARD_SEED_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_seed_log (
    tag TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _same_economics(row, f) -> bool:
    return (str(row["asset"]).upper() == f["asset"].upper() and row["side"] == f["side"]
            and abs(float(row["qty"]) - float(f["qty"])) < 1e-9
            and abs(float(row["forward_price"]) - float(f["forward_price"])) <= 1e-9 * max(1.0, float(f["forward_price"]))
            and str(row["maturity_date"])[:10] == f["maturity_date"])


async def _adopt_or_insert(db, f: dict) -> int:
    """Book a confirmed forward without double-counting a hand-booked copy.

    A hand-booked open forward with the same economics and no confirmation
    id is ADOPTED (stamped with the id, trade date from the confirmation)
    instead of inserting another. If an earlier import already duplicated
    one, the imported copy is soft-deleted and its levels move across."""
    cur = await db.execute("SELECT * FROM forward_trades WHERE status = 'open' AND external_ref = ''")
    manual = next((dict(r) for r in await cur.fetchall() if _same_economics(r, f)), None)
    cur = await db.execute("SELECT * FROM forward_trades WHERE external_ref = ?", (f["external_ref"],))
    tagged = [dict(r) for r in await cur.fetchall()]
    if tagged and not manual:
        return 0
    if not tagged and not manual:
        await insert_forward(db, f, "confirmation import")
        return 1
    note = (manual["note"] + " · " if manual["note"] else "") + f"confirmation {f['external_ref']}"
    await db.execute(
        """UPDATE forward_trades SET external_ref = ?, trade_date = ?, note = ?,
               updated_by = 'confirmation import', updated_at = datetime('now') WHERE id = ?""",
        (f["external_ref"], f["trade_date"], note, manual["id"]))
    for t in tagged:
        if t["status"] != "open" or t["created_by"] != "confirmation import":
            continue
        await db.execute(
            """UPDATE forward_trades SET status = 'deleted', note = ?, updated_by = 'confirmation import',
                   updated_at = datetime('now') WHERE id = ?""",
            (f"duplicate of hand-booked #{manual['id']}, removed", t["id"]))
        old, new = f"fwd:{t['id']}", f"fwd:{manual['id']}"
        cur = await db.execute("SELECT 1 FROM forward_levels WHERE key = ?", (new,))
        if await cur.fetchone():
            await db.execute("DELETE FROM forward_levels WHERE key = ?", (old,))
        else:
            await db.execute("UPDATE forward_levels SET key = ? WHERE key = ?", (new, old))
    await db.commit()
    return 1


async def seed_confirmed(db) -> int:
    added = 0
    for f in CONFIRMED_FORWARDS:
        added += await _adopt_or_insert(db, f)
    await db.execute(FORWARD_SEED_LOG_SCHEMA)
    for lv in CONFIRMED_LEVELS:
        tag = f"levels:{lv.get('external_ref') or lv['tag']}:v{lv['version']}"
        cur = await db.execute("SELECT 1 FROM forward_seed_log WHERE tag = ?", (tag,))
        if await cur.fetchone():
            continue
        if lv.get("external_ref"):
            cur = await db.execute(
                "SELECT * FROM forward_trades WHERE external_ref = ? AND status = 'open'", (lv["external_ref"],))
            rows = [dict(r) for r in await cur.fetchall()]
        else:
            cur = await db.execute("SELECT * FROM forward_trades WHERE status = 'open'")
            rows = [dict(r) for r in await cur.fetchall() if _same_economics(r, lv["match"])]
        if not rows:
            continue
        for row in rows:
            await set_levels(db, f"fwd:{row['id']}", row["asset"], lv["stop_loss"], lv["take_profit"],
                             lv["note"], "desk orders")
        await db.execute("INSERT INTO forward_seed_log (tag) VALUES (?)", (tag,))
        await db.commit()
        added += 1
    return added


async def seed_synthetic_levels(db, asset: str, synthetics: list[dict]) -> int:
    """Apply CONFIRMED_SYNTHETIC_LEVELS once to the matching synthetic forward."""
    added = 0
    await db.execute(FORWARD_SEED_LOG_SCHEMA)
    for lv in CONFIRMED_SYNTHETIC_LEVELS:
        if lv["asset"] != asset.upper():
            continue
        tag = f"synlevels:{lv['tag']}:v{lv['version']}"
        cur = await db.execute("SELECT 1 FROM forward_seed_log WHERE tag = ?", (tag,))
        if await cur.fetchone():
            continue
        s = next((s for s in synthetics if s["side"] == lv["side"] and s["expiry"][:10] == lv["expiry"]
                  and abs(s["strike"] - lv["strike"]) < 1e-9 and abs(s["qty"] - lv["qty"]) < 1e-6), None)
        if not s:
            continue
        await set_levels(db, s["key"], asset, lv["stop_loss"], lv["take_profit"], lv["note"], "desk orders")
        await db.execute("INSERT INTO forward_seed_log (tag) VALUES (?)", (tag,))
        await db.commit()
        added += 1
    return added


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
