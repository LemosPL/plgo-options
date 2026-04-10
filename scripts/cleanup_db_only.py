"""Soft-delete the 59 DB-only FIL trades that have no match in the new Excel.
These are either duplicates (wrong premiums from old import) or trades
from old Excel versions that were restructured in the new spreadsheet.
"""
import sqlite3

DB_PATH = r"C:\Users\Lucas Lemos\PycharmProjects\plgo_options\data\plgo_options.db"

# The 59 DB-only trade IDs identified by the reconciliation
DB_ONLY_IDS = [
    # Wave - qty mismatch duplicates (old 400K, correct is 500K already in DB)
    71, 72,
    # KeyRock - old structures not in new Excel
    116, 117, 118, 119,
    # Wave - orphan
    135,
    # Flowdesk - old structure
    136, 138,
    # KeyRock - old structures
    150, 151, 152, 153,
    # KeyRock - duplicates with wrong premiums (correct version already matched)
    173, 174, 175, 176,
    # GSR - old expired structures not in new Excel
    185, 186, 187, 188, 189, 190,
    # Wave - old structures not in new Excel
    201, 202, 203,
    # GSR - old expired
    204, 205, 206,
    # Wave - orphan active (not in new Excel)
    238,
    # Flowdesk - duplicates with fractional premiums (correct version already matched)
    249, 250, 251, 252, 253, 254, 255, 256,
    # G20 - duplicates with fractional premiums
    278, 279, 280, 281,
    # GSR - all active GSR (new Excel shows 0 active GSR FIL trades)
    282, 283, 284, 285, 286, 287, 288, 289, 290, 291, 292, 293, 294,
    # Wave - orphan active (not in new Excel)
    331, 332, 333, 334,
]

conn = sqlite3.connect(DB_PATH)

# Verify these exist and show what we're deleting
print(f"Soft-deleting {len(DB_ONLY_IDS)} DB-only trades...\n")

for tid in DB_ONLY_IDS:
    row = conn.execute(
        "SELECT id, counterparty, trade_date, side, option_type, strike, expiry, qty, premium_usd, status "
        "FROM trades WHERE id = ?", (tid,)
    ).fetchone()
    if row:
        print(f"  #{row[0]} | {row[1]} | {row[2]} | {row[3]} {row[4]} | K={row[5]} | exp={row[6]} | qty={row[7]} | prem=${row[8]} | {row[9]} -> deleted")
    else:
        print(f"  #{tid} NOT FOUND (already deleted?)")

# Soft-delete: set status='deleted' + audit log
for tid in DB_ONLY_IDS:
    cur = conn.execute("SELECT status FROM trades WHERE id = ?", (tid,))
    row = cur.fetchone()
    if not row:
        continue
    old_status = row[0]
    conn.execute(
        "UPDATE trades SET status = 'deleted', updated_at = datetime('now') WHERE id = ?",
        (tid,),
    )
    conn.execute(
        "INSERT INTO trade_audit_log (trade_id, action, field_changed, old_value, new_value, changed_by) "
        "VALUES (?, 'update', 'status', ?, 'deleted', 'recon_cleanup_20260331')",
        (tid, old_status),
    )

conn.commit()

# Final counts
total = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status != 'deleted'").fetchone()[0]
active = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status='active'").fetchone()[0]
expired = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status='expired'").fetchone()[0]
deleted = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status='deleted'").fetchone()[0]
conn.close()

print(f"\n=== FINAL DB STATE ===")
print(f"FIL trades (non-deleted): {total}")
print(f"  Active: {active}")
print(f"  Expired: {expired}")
print(f"  Soft-deleted: {deleted}")
print(f"\nAudit log tag: 'recon_cleanup_20260331'")
