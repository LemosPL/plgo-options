"""Load production data, saved as JSON from the live app, into a local DB.

No gcloud access needed: while signed in to production in a browser, open
each URL below and save the page (Cmd-S, or copy the JSON into a file) into
one folder, under these names:

    trades.json        <prod>/api/trades/?include_expired=true&include_deleted=true
    collateral.json    <prod>/api/collateral/map
    perps_ETH.json     <prod>/api/perps/trades?asset=ETH&include_closed=true
    perps_FIL.json     <prod>/api/perps/trades?asset=FIL&include_closed=true

Any file can be left out; its table is then left as it is locally.

Usage:
    python scripts/import_prod_json.py <folder>            # dry run: counts only
    python scripts/import_prod_json.py <folder> --apply    # write data/plgo_options.db

--apply first copies the local DB to data/plgo_options.<timestamp>.bak.db, then
REPLACES the imported tables wholesale (trades keep production's ids, so deal
grouping, forced-roll ids and the audit trail line up with production):

    trades                    <- trades.json
    counterparty_collateral   <- collateral.json (per counterparty, asset, book;
                                 collar-loan counterparties are not on the map,
                                 so their local rows are kept)
    collateral_price          <- collateral.json price overrides
    perp_trades               <- perps_*.json (perp funding is cleared for
                                 those assets; it re-accrues on the next
                                 "Accrue funding" or page load)

Everything else (agents' proposals and policies, MTM history, users) stays
local. Restart the dev server afterwards.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from plgo_options.data.collar_loans import COLLAR_LOAN_COUNTERPARTIES  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "plgo_options.db"


def _load(folder: Path, name: str):
    path = folder / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def _replace_rows(db: sqlite3.Connection, table: str, rows: list[dict],
                  where: str = "", params: tuple = ()) -> int:
    """Delete the table's rows (or the WHERE subset) and insert ``rows``,
    keeping only the keys the local schema has — so a production column that
    does not exist locally yet is skipped instead of failing the import."""
    cols = _columns(db, table)
    db.execute(f"DELETE FROM {table} {where}", params)
    n = 0
    for row in rows:
        keys = [k for k in row if k in cols]
        if not keys:
            continue
        db.execute(
            f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
            [row[k] for k in keys],
        )
        n += 1
    return n


def _collateral_rows(payload: dict) -> list[dict]:
    """/api/collateral/map -> counterparty_collateral rows (one per
    counterparty, asset, book with a non-zero quantity)."""
    rows = []
    now = datetime.now().isoformat(timespec="seconds")
    for cp in payload.get("counterparties", []):
        for book, by_asset in (cp.get("books") or {}).items():
            for asset, qty in (by_asset or {}).items():
                if qty:
                    rows.append({"counterparty": cp["counterparty"], "asset": asset,
                                 "book": book, "qty": float(qty), "updated_at": now})
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    folder = Path(sys.argv[1]).expanduser()
    apply = "--apply" in sys.argv[2:]
    if not folder.is_dir():
        print(f"No such folder: {folder}")
        return 2

    trades = _load(folder, "trades.json")
    collateral = _load(folder, "collateral.json")
    perps = {a: _load(folder, f"perps_{a}.json") for a in ("ETH", "FIL")}

    plan = []
    if trades is not None:
        rows = trades.get("trades", trades) if isinstance(trades, dict) else trades
        plan.append(("trades", rows, "", ()))
    if collateral is not None:
        # The map leaves the collar loans out, so their rows are kept, not wiped.
        keep = tuple(sorted(COLLAR_LOAN_COUNTERPARTIES))
        where = (f"WHERE lower(trim(counterparty)) NOT IN ({', '.join('?' for _ in keep)})"
                 if keep else "")
        plan.append(("counterparty_collateral", _collateral_rows(collateral), where, keep))
        overrides = [{"asset": a, "price": float(p)}
                     for a, p in (collateral.get("price_overrides") or {}).items()]
        plan.append(("collateral_price", overrides, "", ()))
    for asset, payload in perps.items():
        if payload is not None:
            plan.append(("perp_trades", payload.get("trades", []),
                         "WHERE asset = ? COLLATE NOCASE", (asset,)))

    if not plan:
        print(f"Nothing to import in {folder} (expected trades.json, collateral.json, perps_ETH.json, perps_FIL.json).")
        return 1

    db = sqlite3.connect(DB_PATH)
    try:
        for table, rows, where, params in plan:
            before = db.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
            scope = f" ({params[0]})" if table == "perp_trades" else ""
            print(f"{table}{scope}: {before} local rows -> {len(rows)} from production")
        if not apply:
            print("\nDry run. Re-run with --apply to write (the local DB is backed up first).")
            return 0
    finally:
        db.close()

    backup = DB_PATH.with_name(f"plgo_options.{datetime.now():%Y%m%d_%H%M%S}.bak.db")
    shutil.copy2(DB_PATH, backup)
    print(f"\nBacked up local DB to {backup.name}")

    db = sqlite3.connect(DB_PATH)
    try:
        with db:
            for table, rows, where, params in plan:
                n = _replace_rows(db, table, rows, where, params)
                print(f"  {table}: wrote {n}")
                if table == "perp_trades":
                    db.execute(f"DELETE FROM perp_funding {where}", params)
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {ok}")
    finally:
        db.close()
    print("Done. Restart the dev server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
