"""Book state: the agents' shared memory, in the app's own SQLite DB.

Kept in the database rather than in any agent so Lucas, Chris and every agent
read the same Monday numbers, and a restart loses nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from plgo_options.agents.policy import AssetPolicy, default_policy
from plgo_options.data.database import get_db

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agent_policy (
        asset TEXT PRIMARY KEY,
        policy_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS agent_policy_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        policy_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS agent_row_fires (
        asset TEXT NOT NULL,
        week TEXT NOT NULL,
        row_key TEXT NOT NULL,
        fired_at TEXT NOT NULL,
        spot REAL,
        PRIMARY KEY (asset, week, row_key)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        text TEXT DEFAULT '',
        data_json TEXT DEFAULT '{}',
        delivered INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS agent_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        agent TEXT NOT NULL,
        asset TEXT NOT NULL,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        route TEXT NOT NULL,
        reasons_json TEXT NOT NULL DEFAULT '[]',
        proposal_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'open',
        decided_by TEXT DEFAULT '',
        decided_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS agent_flags (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS agent_curves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        run_label TEXT NOT NULL,
        created_at TEXT NOT NULL,
        spot REAL,
        params_json TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )""",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_agent_tables() -> None:
    db = await get_db()
    for stmt in SCHEMA:
        await db.execute(stmt)
    await db.commit()


# ── policy ────────────────────────────────────────────────────────────────

async def get_policy(asset: str) -> AssetPolicy:
    db = await get_db()
    cur = await db.execute("SELECT policy_json FROM agent_policy WHERE asset = ?", (asset.upper(),))
    row = await cur.fetchone()
    if not row:
        return default_policy(asset)
    return AssetPolicy.from_dict(json.loads(row["policy_json"]))


async def save_policy(policy: AssetPolicy, by: str = "") -> AssetPolicy:
    """Save Monday's settings. Enforces "the floor only goes up" (A2)."""
    current = await get_policy(policy.asset)
    if (current.floor_price is not None and policy.floor_price is not None
            and policy.floor_price < current.floor_price and not current.is_example):
        raise ValueError(
            f"Floor can only go up: {current.floor_price:g} -> {policy.floor_price:g} refused (A2).")
    db = await get_db()
    blob = json.dumps(policy.to_dict())
    ts = now_iso()
    await db.execute(
        "INSERT INTO agent_policy (asset, policy_json, updated_at, updated_by) VALUES (?,?,?,?) "
        "ON CONFLICT(asset) DO UPDATE SET policy_json=excluded.policy_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (policy.asset.upper(), blob, ts, by))
    await db.execute(
        "INSERT INTO agent_policy_history (asset, policy_json, updated_at, updated_by) VALUES (?,?,?,?)",
        (policy.asset.upper(), blob, ts, by))
    await db.commit()
    return policy


# ── flags: kill switch, curve freeze ───────────────────────────────────────

async def get_flag(key: str, default: str = "") -> str:
    db = await get_db()
    cur = await db.execute("SELECT value FROM agent_flags WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default


async def set_flag(key: str, value: str, by: str = "") -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO agent_flags (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by", (key, value, now_iso(), by))
    await db.commit()


async def kill_switch_on() -> bool:
    return (await get_flag("kill_switch", "off")) == "on"


# ── row fires ─────────────────────────────────────────────────────────────

async def fired_rows(asset: str, week: str) -> set[str]:
    db = await get_db()
    cur = await db.execute("SELECT row_key FROM agent_row_fires WHERE asset=? AND week=?",
                           (asset.upper(), week))
    return {r["row_key"] for r in await cur.fetchall()}


async def mark_fired(asset: str, week: str, row_key: str, spot: float) -> bool:
    """True if this is the first fire this week (B1: a row fires once per week)."""
    db = await get_db()
    cur = await db.execute(
        "INSERT OR IGNORE INTO agent_row_fires (asset, week, row_key, fired_at, spot) VALUES (?,?,?,?,?)",
        (asset.upper(), week, row_key, now_iso(), spot))
    await db.commit()
    return cur.rowcount == 1


# ── runs and proposals ────────────────────────────────────────────────────

async def start_run(agent: str) -> int:
    db = await get_db()
    cur = await db.execute("INSERT INTO agent_runs (agent, started_at) VALUES (?, ?)",
                           (agent, now_iso()))
    await db.commit()
    return cur.lastrowid


async def finish_run(run_id: int, status: str, text: str, data: dict, delivered: bool) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE agent_runs SET finished_at=?, status=?, text=?, data_json=?, delivered=? WHERE id=?",
        (now_iso(), status, text, json.dumps(data, default=str), int(delivered), run_id))
    await db.commit()


async def list_runs(agent: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    db = await get_db()
    if agent:
        cur = await db.execute("SELECT * FROM agent_runs WHERE agent=? ORDER BY id DESC LIMIT ?",
                               (agent, limit))
    else:
        cur = await db.execute("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


async def last_run(agent: str) -> dict[str, Any] | None:
    runs = await list_runs(agent, 1)
    return runs[0] if runs else None


async def add_proposal(agent: str, asset: str, kind: str, summary: str,
                       route: str, reasons: list[str], proposal: dict) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO agent_proposals (created_at, agent, asset, kind, summary, route, reasons_json, "
        "proposal_json, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso(), agent, asset.upper(), kind, summary, route, json.dumps(reasons),
         json.dumps(proposal, default=str), "rejected" if route == "rejected" else "open"))
    await db.commit()
    return cur.lastrowid


async def list_proposals(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    db = await get_db()
    if status:
        cur = await db.execute(
            "SELECT * FROM agent_proposals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        cur = await db.execute("SELECT * FROM agent_proposals ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in await cur.fetchall():
        d = dict(r)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["proposal"] = json.loads(d.pop("proposal_json") or "{}")
        out.append(d)
    return out


async def get_proposal(pid: int) -> dict[str, Any] | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM agent_proposals WHERE id=?", (pid,))
    r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
    d["proposal"] = json.loads(d.pop("proposal_json") or "{}")
    return d


async def decide_proposal(pid: int, status: str, by: str) -> None:
    db = await get_db()
    await db.execute("UPDATE agent_proposals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                     (status, by, now_iso(), pid))
    await db.commit()


async def supersede_open(asset: str, agent: str, keep_ids: list[int]) -> int:
    """Close the agent's still-open proposals for ``asset`` that a newer run
    replaced, so the screen only carries the latest sweep. Returns how many."""
    db = await get_db()
    marks = ",".join("?" * len(keep_ids)) or "NULL"
    cur = await db.execute(
        f"UPDATE agent_proposals SET status='superseded', decided_by='newer run', decided_at=? "
        f"WHERE asset=? AND agent=? AND status='open' AND id NOT IN ({marks})",
        (now_iso(), asset, agent, *keep_ids))
    await db.commit()
    return cur.rowcount or 0


async def month_to_date_cost(asset: str, month: str) -> float:
    """Net premium paid this month on proposals people marked executed."""
    db = await get_db()
    cur = await db.execute(
        "SELECT proposal_json FROM agent_proposals WHERE asset=? AND status='executed' "
        "AND substr(decided_at,1,7)=?", (asset.upper(), month))
    total = 0.0
    for r in await cur.fetchall():
        total += max(0.0, float(json.loads(r["proposal_json"]).get("net_cost_usd") or 0))
    return total


# ── optimizer curves (for the two-curve check) ─────────────────────────────

async def save_curve(asset: str, label: str, spot: float, params: dict, summary: dict) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO agent_curves (asset, run_label, created_at, spot, params_json, summary_json) "
        "VALUES (?,?,?,?,?,?)",
        (asset.upper(), label, now_iso(), spot, json.dumps(params, default=str),
         json.dumps(summary, default=str)))
    await db.commit()
    return cur.lastrowid


async def recent_curves(asset: str, limit: int = 5) -> list[dict[str, Any]]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM agent_curves WHERE asset=? ORDER BY id DESC LIMIT ?",
                           (asset.upper(), limit))
    out = []
    for r in await cur.fetchall():
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json"))
        d["summary"] = json.loads(d.pop("summary_json"))
        out.append(d)
    return out
