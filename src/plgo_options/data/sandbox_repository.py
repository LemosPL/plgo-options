"""Storage for the Sandbox screen: dealer offers on tokens we don't trade yet
(ZAMA first) and the per-token pricing settings.

Deliberately its own tables: nothing here is read by the trades screen, the
P&L, collateral, the optimizer or any risk view. "Booked" in the sandbox
means "in the sandbox book", not a real trade.

sandbox_offers: one row per offer. legs is JSON, our side (pricing/sandbox.py).
status: indicative -> booked | rejected; delete is soft (status 'deleted',
row kept) so a deleted seed offer is not re-seeded.
sandbox_settings: one JSON blob per token (vol, skew, carry, vesting...).
"""
from __future__ import annotations

import json

OFFERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    quote_date TEXT,
    ref_spot REAL,
    expiry TEXT NOT NULL,
    settlement TEXT NOT NULL DEFAULT 'tbc',
    premium_usd REAL NOT NULL DEFAULT 0,
    collateral_usd REAL,
    collateral_note TEXT NOT NULL DEFAULT '',
    hedge_qty REAL,
    legs TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'indicative',
    notes TEXT NOT NULL DEFAULT '',
    seed_key TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_settings (
    asset TEXT PRIMARY KEY COLLATE NOCASE,
    data TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

FIELDS = ("asset", "name", "counterparty", "quote_date", "ref_spot", "expiry", "settlement",
          "premium_usd", "collateral_usd", "collateral_note", "hedge_qty", "legs", "status", "notes")
STATUSES = ("indicative", "booked", "rejected")
SETTLEMENTS = ("physical", "cash", "tbc")

# Defaults per token. vol None = use realised vol (vol_window) at load time.
DEFAULT_SETTINGS = {
    "atm_vol": None,
    "vol_window": "90d",
    "skew_pts": 0.0,
    "carry": 0.0,
    "collateral_rate": 0.05,
    "holdings": 0.0,
    "cliff_date": None,
    "cliff_pct": 0.10,
    "linear_months": 12,
}
SEED_SETTINGS = {
    # Chris Brocoum: ~366m ZAMA, 10% vesting 2 Feb 2027, the rest linearly
    # over the following 12 months.
    "ZAMA": {"holdings": 366_000_000.0, "cliff_date": "2027-02-02", "cliff_pct": 0.10, "linear_months": 12},
}

# The offers on the table when the sandbox was built, from the Slack threads.
# Inserted once each (seed_key), so a deliberate delete stays deleted.
SEED_OFFERS = [
    {
        "seed_key": "falconx-fwd-29jan27", "asset": "ZAMA", "name": "Forward sale 29Jan27 @ 0.0754",
        "counterparty": "FalconX", "expiry": "2027-01-29", "settlement": "tbc",
        "legs": [{"kind": "forward", "side": "sell", "qty": round(2_000_000 / 0.0754), "strike": 0.0754}],
        "notes": "FalconX (Eesa Ahmad): we sell a forward settling 29Jan27, price locked at 0.0754, "
                 "2m USD nominal (= 26.5m ZAMA). Open questions: reference spot at quote, "
                 "cash or physical, collateral required?",
    },
    {
        "seed_key": "flowdesk-collar-5-10", "asset": "ZAMA", "name": "Zero-cost collar 0.05P / 0.10C",
        "counterparty": "Flowdesk", "ref_spot": 0.08, "expiry": "2027-02-03", "settlement": "physical",
        "collateral_usd": 5_000_000.0, "collateral_note": "ETH / stablecoin, returned when the ZAMA is delivered",
        "legs": [{"kind": "put", "side": "buy", "qty": 36_600_000, "strike": 0.05},
                 {"kind": "call", "side": "sell", "qty": 36_600_000, "strike": 0.10}],
        "notes": "Flowdesk (Pravin Pathmanathan): hedges the 2 Feb 2027 cliff (10% = 36.6m). Spot ref 0.08, "
                 "their hedge = sell perps, needs a fill at 0.08; strikes adjustable. Physical: we send the "
                 "tokens when they unlock 2 Feb. Roll into a new collar for each monthly unlock.",
    },
    {
        "seed_key": "flowdesk-collar-4-115", "asset": "ZAMA", "name": "Zero-cost collar 0.04P / 0.115C",
        "counterparty": "Flowdesk", "ref_spot": 0.08, "expiry": "2027-02-03", "settlement": "physical",
        "collateral_usd": 5_000_000.0, "collateral_note": "ETH / stablecoin",
        "legs": [{"kind": "put", "side": "buy", "qty": 36_600_000, "strike": 0.04},
                 {"kind": "call", "side": "sell", "qty": 36_600_000, "strike": 0.115}],
        "notes": "Flowdesk variant: 'only worried about Zama dropping below 0.04: buy 0.04 puts, "
                 "to zero cost this your call sell moves up to 0.115'.",
    },
    {
        "seed_key": "flowdesk-split-collar", "asset": "ZAMA", "name": "Split zero-cost collar 0.04/0.05P - 0.10/0.115C",
        "counterparty": "Flowdesk", "ref_spot": 0.08, "expiry": "2027-02-03", "settlement": "physical",
        "collateral_usd": 5_000_000.0, "collateral_note": "ETH / stablecoin",
        "legs": [{"kind": "put", "side": "buy", "qty": 18_000_000, "strike": 0.04},
                 {"kind": "put", "side": "buy", "qty": 18_000_000, "strike": 0.05},
                 {"kind": "call", "side": "sell", "qty": 18_000_000, "strike": 0.10},
                 {"kind": "call", "side": "sell", "qty": 18_000_000, "strike": 0.115}],
        "notes": "Flowdesk split: buy 18m 0.04P + 18m 0.05P, sell 18m 0.10C + 18m 0.115C, zero cost.",
    },
]


async def ensure_tables(db) -> None:
    await db.execute(OFFERS_SCHEMA)
    await db.execute(SETTINGS_SCHEMA)


async def seed(db) -> int:
    added = 0
    for o in SEED_OFFERS:
        cur = await db.execute("SELECT 1 FROM sandbox_offers WHERE seed_key = ?", (o["seed_key"],))
        if await cur.fetchone():
            continue
        await insert_offer(db, o, "seed", seed_key=o["seed_key"])
        added += 1
    for asset, data in SEED_SETTINGS.items():
        await db.execute("INSERT OR IGNORE INTO sandbox_settings (asset, data, updated_by) VALUES (?, ?, 'seed')",
                         (asset, json.dumps({**DEFAULT_SETTINGS, **data})))
    return added


def _row(r) -> dict:
    d = dict(r)
    d["legs"] = json.loads(d["legs"] or "[]")
    return d


async def list_offers(db, asset: str) -> list[dict]:
    cur = await db.execute("SELECT * FROM sandbox_offers WHERE asset = ? AND status != 'deleted' ORDER BY id", (asset,))
    return [_row(r) for r in await cur.fetchall()]


async def get_offer(db, oid: int) -> dict | None:
    cur = await db.execute("SELECT * FROM sandbox_offers WHERE id = ? AND status != 'deleted'", (oid,))
    r = await cur.fetchone()
    return _row(r) if r else None


async def insert_offer(db, o: dict, user: str, seed_key: str | None = None) -> int:
    vals = {k: o.get(k) for k in FIELDS}
    vals["legs"] = json.dumps(o.get("legs") or [])
    vals["status"] = o.get("status") or "indicative"
    vals["settlement"] = o.get("settlement") or "tbc"
    vals["premium_usd"] = o.get("premium_usd") or 0.0
    for k in ("counterparty", "collateral_note", "notes"):
        vals[k] = vals[k] or ""
    cols = list(vals) + ["seed_key", "created_by", "updated_by"]
    cur = await db.execute(
        f"INSERT INTO sandbox_offers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [*vals.values(), seed_key, user, user])
    await db.commit()
    return cur.lastrowid


async def update_offer(db, oid: int, changes: dict, user: str) -> None:
    sets = {k: (json.dumps(v) if k == "legs" else v) for k, v in changes.items() if k in FIELDS}
    if not sets:
        return
    assign = ", ".join(f"{k} = ?" for k in sets)
    await db.execute(f"UPDATE sandbox_offers SET {assign}, updated_by = ?, updated_at = datetime('now') WHERE id = ?",
                     [*sets.values(), user, oid])
    await db.commit()


async def delete_offer(db, oid: int, user: str = "") -> None:
    await db.execute("UPDATE sandbox_offers SET status = 'deleted', updated_by = ?, updated_at = datetime('now') WHERE id = ?",
                     (user, oid))
    await db.commit()


async def get_settings(db, asset: str) -> dict:
    cur = await db.execute("SELECT data, updated_by, updated_at FROM sandbox_settings WHERE asset = ?", (asset,))
    r = await cur.fetchone()
    data = {**DEFAULT_SETTINGS, **(json.loads(r["data"]) if r else {})}
    return {**data, "updated_by": r["updated_by"] if r else "", "updated_at": r["updated_at"] if r else None}


async def save_settings(db, asset: str, data: dict, user: str) -> None:
    clean = {k: data[k] for k in DEFAULT_SETTINGS if k in data}
    cur_data = {k: v for k, v in (await get_settings(db, asset)).items() if k in DEFAULT_SETTINGS}
    await db.execute(
        "INSERT INTO sandbox_settings (asset, data, updated_by, updated_at) VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(asset) DO UPDATE SET data = excluded.data, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (asset, json.dumps({**cur_data, **clean}), user))
    await db.commit()
