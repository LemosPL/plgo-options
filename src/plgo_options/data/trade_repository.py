"""Trade CRUD operations with audit logging."""

from __future__ import annotations

from datetime import datetime

import aiosqlite


TRADE_FIELDS = [
    "asset", "counterparty", "trade_id", "trade_date", "side", "option_type",
    "instrument", "expiry", "strike", "ref_spot", "pct_otm", "qty",
    "notional_mm", "premium_per", "premium_usd", "status",
]


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


async def list_trades(
    db: aiosqlite.Connection,
    include_expired: bool = False,
    include_deleted: bool = False,
    asset: str | None = None,
) -> list[dict]:
    conditions = []
    params = []
    if not include_deleted:
        conditions.append("status != 'deleted'")
    if not include_expired:
        conditions.append("status != 'expired'")
    if asset:
        conditions.append("asset = ?")
        params.append(asset)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await db.execute(
        f"SELECT * FROM trades {where} ORDER BY id", params
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_trade(db: aiosqlite.Connection, trade_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def create_trade(
    db: aiosqlite.Connection,
    data: dict,
    changed_by: str = "user",
) -> dict:
    cols = [f for f in TRADE_FIELDS if f in data]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [data[f] for f in cols]

    cursor = await db.execute(
        f"INSERT INTO trades ({col_names}) VALUES ({placeholders})",
        values,
    )
    new_id = cursor.lastrowid

    await db.execute(
        """INSERT INTO trade_audit_log (trade_id, action, changed_by)
           VALUES (?, 'create', ?)""",
        (new_id, changed_by),
    )
    await db.commit()

    return await get_trade(db, new_id)


async def update_trade(
    db: aiosqlite.Connection,
    trade_id: int,
    changes: dict,
    changed_by: str = "user",
) -> dict | None:
    current = await get_trade(db, trade_id)
    if current is None:
        return None

    editable = {k: v for k, v in changes.items() if k in TRADE_FIELDS}
    if not editable:
        return current

    now = datetime.utcnow().isoformat()

    for field, new_val in editable.items():
        old_val = current.get(field)
        if str(old_val) != str(new_val):
            await db.execute(
                """INSERT INTO trade_audit_log
                   (trade_id, action, field_changed, old_value, new_value, changed_by)
                   VALUES (?, 'edit', ?, ?, ?, ?)""",
                (trade_id, field, str(old_val), str(new_val), changed_by),
            )

    set_clause = ", ".join(f"{f} = ?" for f in editable)
    values = list(editable.values()) + [now, trade_id]
    await db.execute(
        f"UPDATE trades SET {set_clause}, updated_at = ? WHERE id = ?",
        values,
    )
    await db.commit()
    return await get_trade(db, trade_id)


async def soft_delete_trade(
    db: aiosqlite.Connection,
    trade_id: int,
    changed_by: str = "user",
) -> dict | None:
    current = await get_trade(db, trade_id)
    if current is None:
        return None

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE trades SET status = 'deleted', updated_at = ? WHERE id = ?",
        (now, trade_id),
    )
    await db.execute(
        """INSERT INTO trade_audit_log
           (trade_id, action, field_changed, old_value, new_value, changed_by)
           VALUES (?, 'delete', 'status', ?, 'deleted', ?)""",
        (trade_id, current["status"], changed_by),
    )
    await db.commit()
    return await get_trade(db, trade_id)


async def expire_trade(
    db: aiosqlite.Connection,
    trade_id: int,
    changed_by: str = "user",
) -> dict | None:
    current = await get_trade(db, trade_id)
    if current is None:
        return None

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE trades SET status = 'expired', updated_at = ? WHERE id = ?",
        (now, trade_id),
    )
    await db.execute(
        """INSERT INTO trade_audit_log
           (trade_id, action, field_changed, old_value, new_value, changed_by)
           VALUES (?, 'expire', 'status', ?, 'expired', ?)""",
        (trade_id, current["status"], changed_by),
    )
    await db.commit()
    return await get_trade(db, trade_id)


async def get_trade_history(
    db: aiosqlite.Connection,
    trade_id: int,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM trade_audit_log
           WHERE trade_id = ?
           ORDER BY timestamp DESC""",
        (trade_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]
