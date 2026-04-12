"""Import missing FIL trades from the new Excel into the DB.

- Imports all 1,472 trades missing from DB (47 active + 1,425 expired)
- Marks trades with expiry < today as 'expired'
- Does NOT touch existing DB trades
- Creates audit log entries for all imports
"""
import sqlite3
import openpyxl
from datetime import datetime, date
from collections import defaultdict

TODAY = date.today().isoformat()
EXCEL_PATH = r"C:\Users\Lucas Lemos\Downloads\Copy of FIL, ETH & BTC Option Strategies.xlsx"
DB_PATH = r"C:\Users\Lucas Lemos\PycharmProjects\plgo_options\data\plgo_options.db"

FIL_SHEETS = {
    "GSR - FIL Option Positions (New": "GSR",
    "Wave - FIL Option Positions (Ne": "Wave",
    "G20 - FIL Option Positions": "G20",
    "Flowdesk - FIL Option Positions": "Flowdesk",
    "Keyrock - FIL Option Positions": "KeyRock",
}


def to_date_str(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).split("T")[0] if v else ""


def to_float(v):
    try:
        return float(v) if v else 0
    except (ValueError, TypeError):
        return 0


def make_key(t):
    """Create matching key: (cpty, trade_date, side, opt_type_norm, expiry, strike_r2, qty_r0)"""
    opt = t["option_type"].lower().rstrip("s")  # puts -> put, calls -> call
    return (
        t["counterparty"],
        t["trade_date"].split("T")[0],
        t["side"].strip().capitalize(),
        opt,
        t["expiry"].split("T")[0],
        round(float(t["strike"]), 2),
        round(float(t["qty"]), 0),
    )


# ── Load existing DB trades ──
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
db_trades = [dict(r) for r in conn.execute(
    "SELECT * FROM trades WHERE asset='FIL' AND status != 'deleted' ORDER BY id"
).fetchall()]
conn.close()

db_keys = defaultdict(list)
for t in db_trades:
    side = "Buy" if t["side"].lower() == "buy" else ("Sell" if t["side"].lower() == "sell" else t["side"])
    opt = t["option_type"].lower().rstrip("s")
    expiry = t["expiry"].split("T")[0]
    k = (t["counterparty"], t["trade_date"].split("T")[0], side, opt, expiry,
         round(float(t["strike"]), 2), round(float(t["qty"]), 0))
    db_keys[k].append(t)

print(f"Existing DB FIL trades: {len(db_trades)}")

# ── Parse Excel trades ──
wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
excel_trades = []

for sname, cpty in FIL_SHEETS.items():
    ws = wb[sname]
    rows = list(ws.iter_rows(values_only=True))
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0] and str(r[0]).strip() == "Initial Trade Date":
            header_idx = i
            break
    if header_idx is None:
        print(f"WARNING: No header in {sname}")
        continue
    headers = [str(h).strip() if h else f"col_{j}" for j, h in enumerate(rows[header_idx])]

    for i in range(header_idx + 1, len(rows)):
        r = rows[i]
        if not r or all(c is None for c in r):
            continue
        rec = dict(zip(headers, r))
        td = rec.get("Initial Trade Date")
        side = rec.get("Buy / Sell / Unwind")
        opt_type = rec.get("Option Type")
        expiry = rec.get("Option Expiry Date")
        strike = rec.get("$ Strike")
        qty = rec.get("# of FIL Options")
        premium = rec.get("Total $ Premium Received/(Paid)")
        ref_spot = rec.get("Ref. Spot Price")
        pct_otm = rec.get("% OTM")
        notional = rec.get("$ Notional (mm)")

        if not td or not side or not opt_type:
            continue
        side_s = str(side).strip().lower()
        if side_s in ("", "total", "totals"):
            continue

        strike_val = to_float(strike)
        qty_val = to_float(qty)
        if strike_val <= 0 or qty_val <= 0:
            continue

        excel_trades.append({
            "counterparty": cpty,
            "trade_date": to_date_str(td),
            "side": str(side).strip().lower(),
            "option_type": str(opt_type).strip().lower(),
            "expiry": to_date_str(expiry),
            "strike": strike_val,
            "qty": qty_val,
            "premium_usd": to_float(premium),
            "ref_spot": to_float(ref_spot),
            "pct_otm": to_float(pct_otm),
            "notional_mm": to_float(notional),
        })
wb.close()

print(f"Excel FIL trades parsed: {len(excel_trades)}")

# ── Find missing trades ──
# Track how many times each key is used in DB (multiset matching)
db_key_usage = defaultdict(int)
for k in db_keys:
    db_key_usage[k] = len(db_keys[k])

xl_by_key = defaultdict(list)
for t in excel_trades:
    side_cap = t["side"].strip().capitalize()
    opt = t["option_type"].lower().rstrip("s")
    k = (t["counterparty"], t["trade_date"], side_cap, opt, t["expiry"],
         round(t["strike"], 2), round(t["qty"], 0))
    xl_by_key[k].append(t)

missing = []
for k, xl_list in xl_by_key.items():
    db_count = db_key_usage.get(k, 0)
    for t in xl_list[db_count:]:
        missing.append(t)

active_missing = [t for t in missing if t["expiry"] >= TODAY]
expired_missing = [t for t in missing if t["expiry"] < TODAY]
print(f"\nMissing from DB: {len(missing)} ({len(active_missing)} active, {len(expired_missing)} expired)")

# ── Import ──
conn = sqlite3.connect(DB_PATH)
imported = 0
imported_active = 0
imported_expired = 0

for t in missing:
    status = "active" if t["expiry"] >= TODAY else "expired"
    cur = conn.execute(
        """INSERT INTO trades
           (asset, counterparty, trade_id, trade_date, side, option_type,
            expiry, strike, ref_spot, pct_otm, qty,
            notional_mm, premium_per, premium_usd, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("FIL", t["counterparty"], "", t["trade_date"], t["side"], t["option_type"],
         t["expiry"], t["strike"], t["ref_spot"], t["pct_otm"], t["qty"],
         t["notional_mm"], 0, t["premium_usd"], status),
    )
    new_id = cur.lastrowid
    conn.execute(
        """INSERT INTO trade_audit_log (trade_id, action, changed_by)
           VALUES (?, 'create', 'excel_import_recon_20260331')""",
        (new_id,),
    )
    imported += 1
    if status == "active":
        imported_active += 1
    else:
        imported_expired += 1

conn.commit()
conn.close()

print(f"\n=== IMPORT COMPLETE ===")
print(f"Imported: {imported} trades ({imported_active} active, {imported_expired} expired)")
print(f"Audit log tag: 'excel_import_recon_20260331'")

# Verify final counts
conn = sqlite3.connect(DB_PATH)
total = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status != 'deleted'").fetchone()[0]
active = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status='active'").fetchone()[0]
expired = conn.execute("SELECT COUNT(*) FROM trades WHERE asset='FIL' AND status='expired'").fetchone()[0]
conn.close()

print(f"\n=== FINAL DB STATE ===")
print(f"Total FIL trades: {total} (was {len(db_trades)})")
print(f"Active: {active}")
print(f"Expired: {expired}")
