"""The execution bot's state, in the app's SQLite DB next to the agents' tables.

    exec_orders   one parent order: what to trade, by when, the threshold ramp, progress
    exec_ticks    the order's passes: fair value, threshold, best cost, action; takes as they
                  happen, the routine ones once a minute; what the Execution page charts
    exec_fills    every fill, with its cost against the spline at the time, Deribit's mark and the mid
    exec_lock     a lease so two scheduler calls never work the same orders at once
"""

from __future__ import annotations

import json
import time
from typing import Any

from plgo_options.data.database import get_db

from .threshold import ThresholdOrder

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS exec_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts REAL NOT NULL,
        created_by TEXT DEFAULT '',
        instrument TEXT NOT NULL,
        side TEXT NOT NULL,
        qty REAL NOT NULL,
        deadline_ts REAL NOT NULL,
        start_vp REAL NOT NULL,
        end_vp REAL NOT NULL,
        hard_vp REAL NOT NULL,
        cap REAL NOT NULL,
        mode TEXT NOT NULL DEFAULT 'paper',
        status TEXT NOT NULL DEFAULT 'working',
        filled REAL NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        fees_usd REAL NOT NULL DEFAULT 0,
        note TEXT DEFAULT '',
        finished_ts REAL
    )""",
    """CREATE TABLE IF NOT EXISTS exec_ticks (
        order_id INTEGER NOT NULL REFERENCES exec_orders(id),
        ts REAL NOT NULL,
        action TEXT NOT NULL,
        theta_vp REAL, theta_usd REAL, best_cost REAL,
        fair_usd REAL, iv REAL, vega REAL, S REAL,
        qty REAL NOT NULL DEFAULT 0,
        detail TEXT DEFAULT '',
        PRIMARY KEY (order_id, ts)
    )""",
    """CREATE TABLE IF NOT EXISTS exec_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES exec_orders(id),
        ts REAL NOT NULL,
        intent_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        price_eth REAL NOT NULL,
        price_usd REAL NOT NULL,
        qty REAL NOT NULL,
        fee_usd REAL NOT NULL,
        cost_usd REAL NOT NULL,
        fair_usd REAL NOT NULL,
        mode TEXT NOT NULL,
        mark_usd REAL,
        mid_usd REAL,
        UNIQUE (order_id, trade_id)
    )""",
    """CREATE TABLE IF NOT EXISTS exec_lock (
        name TEXT PRIMARY KEY,
        until REAL NOT NULL,
        last_json TEXT DEFAULT '{}'
    )""",
]

WORKING, PAUSED = "working", "paused"
FINISHED = ("filled", "expired", "cancelled")


async def init_exec_tables() -> None:
    db = await get_db()
    for stmt in SCHEMA:
        await db.execute(stmt)
    # Deribit's mark and the screen mid at each fill (9 Nov 2026); earlier fills have none.
    cur = await db.execute("PRAGMA table_info(exec_fills)")
    have = {r[1] for r in await cur.fetchall()}
    for col in ("mark_usd", "mid_usd"):
        if col not in have:
            await db.execute(f"ALTER TABLE exec_fills ADD COLUMN {col} REAL")
    await db.commit()


def _order(r: Any) -> ThresholdOrder:
    return ThresholdOrder(id=r["id"], instrument=r["instrument"], side=r["side"], qty=r["qty"],
                          filled=r["filled"], created_ts=r["created_ts"], deadline_ts=r["deadline_ts"],
                          start_vp=r["start_vp"], end_vp=r["end_vp"], hard_vp=r["hard_vp"], cap=r["cap"],
                          mode=r["mode"], status=r["status"])


async def create_order(instrument: str, side: str, qty: float, deadline_ts: float, start_vp: float,
                       end_vp: float, hard_vp: float, cap: float, mode: str = "paper",
                       created_by: str = "", note: str = "", now: float | None = None) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO exec_orders (created_ts, created_by, instrument, side, qty, deadline_ts, start_vp,"
        " end_vp, hard_vp, cap, mode, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now or time.time(), created_by, instrument, side, qty, deadline_ts, start_vp, end_vp, hard_vp,
         cap, mode, note))
    await db.commit()
    return cur.lastrowid


async def working_orders() -> list[ThresholdOrder]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM exec_orders WHERE status = ? ORDER BY id", (WORKING,))
    return [_order(r) for r in await cur.fetchall()]


async def get_order(oid: int) -> dict[str, Any] | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM exec_orders WHERE id = ?", (oid,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def list_orders(limit: int = 100) -> list[dict[str, Any]]:
    """Working and paused orders first, then the most recent finished ones; each with its last tick."""
    db = await get_db()
    cur = await db.execute(
        "SELECT o.*, t.action AS last_action, t.detail AS last_detail, t.best_cost AS last_best_cost,"
        " t.theta_usd AS last_theta_usd, t.ts AS last_ts FROM exec_orders o"
        " LEFT JOIN exec_ticks t ON t.order_id = o.id"
        " AND t.ts = (SELECT MAX(ts) FROM exec_ticks WHERE order_id = o.id)"
        " ORDER BY o.status NOT IN ('working','paused'), o.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


async def set_status(oid: int, status: str, note: str | None = None) -> None:
    db = await get_db()
    finished = time.time() if status in FINISHED else None
    await db.execute("UPDATE exec_orders SET status = ?, finished_ts = COALESCE(?, finished_ts),"
                     " note = COALESCE(?, note) WHERE id = ?", (status, finished, note, oid))
    await db.commit()


async def add_tick(oid: int, ts: float, action: str, *, theta_vp=None, theta_usd=None, best_cost=None,
                   fair_usd=None, iv=None, vega=None, S=None, qty: float = 0.0, detail: str = "") -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO exec_ticks (order_id, ts, action, theta_vp, theta_usd, best_cost, fair_usd,"
        " iv, vega, S, qty, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, ts, action, theta_vp, theta_usd, best_cost, fair_usd, iv, vega, S, qty, detail))
    await db.commit()


async def add_fills(oid: int, fills: list[dict[str, Any]]) -> None:
    """Record fills and roll them into the order's filled size, cost and fees."""
    if not fills:
        return
    db = await get_db()
    for f in fills:
        cur = await db.execute(
            "INSERT OR IGNORE INTO exec_fills (order_id, ts, intent_id, trade_id, price_eth, price_usd, qty,"
            " fee_usd, cost_usd, fair_usd, mode, mark_usd, mid_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, f["ts"], f["intent_id"], f["trade_id"], f["price_eth"], f["price_usd"], f["qty"],
             f["fee_usd"], f["cost_usd"], f["fair_usd"], f["mode"], f.get("mark_usd"), f.get("mid_usd")))
        if cur.rowcount:  # a fill seen twice is counted once
            await db.execute("UPDATE exec_orders SET filled = filled + ?, cost_usd = cost_usd + ?,"
                             " fees_usd = fees_usd + ? WHERE id = ?",
                             (f["qty"], f["qty"] * f["cost_usd"], f["fee_usd"], oid))
    await db.commit()


async def leg_costs(limit: int = 50) -> list[dict[str, Any]]:
    """Each order with fills, most recent first: what its fills cost against the spline, Deribit's
    mark and the screen mid, before the fee, in USD and in vol points (USD over the filled
    contracts' vega at each fill), and the fee. A reference a fill lacks leaves that fill out of
    its column, with the contracts it covers in *_qty."""
    db = await get_db()
    cur = await db.execute(
        "SELECT o.id, o.instrument, o.side, o.qty AS order_qty, o.filled, o.status, o.mode, f.ts, f.qty,"
        " f.price_usd, f.fair_usd, f.mark_usd, f.mid_usd, f.fee_usd, t.vega FROM exec_fills f"
        " JOIN exec_orders o ON o.id = f.order_id"
        " LEFT JOIN exec_ticks t ON t.order_id = f.order_id AND t.ts = f.ts"
        " WHERE o.id IN (SELECT order_id FROM exec_fills GROUP BY order_id ORDER BY MAX(ts) DESC LIMIT ?)",
        (limit,))
    legs: dict[int, dict[str, Any]] = {}
    for r in await cur.fetchall():
        g = legs.setdefault(r["id"], {k: r[k] for k in ("id", "instrument", "side", "order_qty", "filled",
                                                         "status", "mode")} | {"last_ts": 0.0, "fee_usd": 0.0})
        g["last_ts"] = max(g["last_ts"], r["ts"])
        g["fee_usd"] += r["fee_usd"]
        g["fee_vega"] = g.get("fee_vega", 0.0) + (r["qty"] * r["vega"] if r["vega"] else 0.0)
        g["fee_vp_usd"] = g.get("fee_vp_usd", 0.0) + (r["fee_usd"] if r["vega"] else 0.0)
        sign = 1.0 if r["side"] == "buy" else -1.0
        for name, ref in (("spline", r["fair_usd"]), ("mark", r["mark_usd"]), ("mid", r["mid_usd"])):
            if ref is None:
                continue
            g[f"{name}_usd"] = g.get(f"{name}_usd", 0.0) + sign * (r["price_usd"] - ref) * r["qty"]
            g[f"{name}_qty"] = g.get(f"{name}_qty", 0.0) + r["qty"]
            if r["vega"]:
                g[f"{name}_vp_usd"] = g.get(f"{name}_vp_usd", 0.0) + sign * (r["price_usd"] - ref) * r["qty"]
                g[f"{name}_vega"] = g.get(f"{name}_vega", 0.0) + r["qty"] * r["vega"]
    out = []
    for g in sorted(legs.values(), key=lambda g: -g["last_ts"]):
        for name in ("spline", "mark", "mid", "fee"):
            v = g.pop(f"{name}_vega", 0.0)
            g[f"{name}_vp"] = g.pop(f"{name}_vp_usd", 0.0) / v if v else None
            g[f"{name}_vega"] = v
        out.append(g)
    return out


async def last_fill_ts(instruments: list[str]) -> dict[str, float]:
    """{instrument: time of its last fill, any order}, for the instruments that have one."""
    if not instruments:
        return {}
    db = await get_db()
    cur = await db.execute(
        "SELECT o.instrument, MAX(f.ts) AS ts FROM exec_fills f JOIN exec_orders o ON o.id = f.order_id"
        f" WHERE o.instrument IN ({','.join('?' * len(set(instruments)))}) GROUP BY o.instrument",
        tuple(set(instruments)))
    return {r["instrument"]: r["ts"] for r in await cur.fetchall()}


async def order_detail(oid: int, ticks: int = 2000) -> dict[str, Any] | None:
    o = await get_order(oid)
    if o is None:
        return None
    db = await get_db()
    cur = await db.execute("SELECT * FROM exec_ticks WHERE order_id = ? ORDER BY ts DESC LIMIT ?", (oid, ticks))
    t = [dict(r) for r in await cur.fetchall()][::-1]
    cur = await db.execute("SELECT * FROM exec_fills WHERE order_id = ? ORDER BY ts, id", (oid,))
    return {"order": o, "ticks": t, "fills": [dict(r) for r in await cur.fetchall()]}


async def acquire_lock(name: str, ttl: float, now: float | None = None) -> bool:
    """Take the lease unless someone holds an unexpired one (atomic in SQLite)."""
    now = now or time.time()
    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO exec_lock (name, until) VALUES (?, 0)", (name,))
    cur = await db.execute("UPDATE exec_lock SET until = ? WHERE name = ? AND until < ?", (now + ttl, name, now))
    await db.commit()
    return cur.rowcount == 1


async def release_lock(name: str, last: dict[str, Any] | None = None) -> None:
    db = await get_db()
    await db.execute("UPDATE exec_lock SET until = 0, last_json = COALESCE(?, last_json) WHERE name = ?",
                     (json.dumps(last) if last is not None else None, name))
    await db.commit()


async def last_run(name: str) -> dict[str, Any]:
    db = await get_db()
    cur = await db.execute("SELECT last_json FROM exec_lock WHERE name = ?", (name,))
    r = await cur.fetchone()
    return json.loads(r["last_json"]) if r and r["last_json"] else {}
