"""Reconcile FIL trades: DB vs new Excel file."""
import sqlite3
from datetime import datetime, date
from collections import Counter, defaultdict
import openpyxl

TODAY = date.today().isoformat()
EXCEL_PATH = r"C:\Users\Lucas Lemos\Downloads\Copy of FIL, ETH & BTC Option Strategies.xlsx"
DB_PATH = r"C:\Users\Lucas Lemos\PycharmProjects\plgo_options\data\plgo_options.db"

# ── DB trades ──
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
db_trades = [dict(r) for r in conn.execute(
    "SELECT * FROM trades WHERE asset='FIL' AND status != 'deleted' ORDER BY id"
).fetchall()]
conn.close()

db_cpty = Counter(t["counterparty"] for t in db_trades)
print("=== DATABASE ===")
print(f"Total FIL trades: {len(db_trades)}")
for c, n in sorted(db_cpty.items()):
    act = sum(1 for t in db_trades if t["counterparty"] == c and t["status"] == "active")
    exp = sum(1 for t in db_trades if t["counterparty"] == c and t["status"] == "expired")
    print(f"  {c}: {n} total ({act} active, {exp} expired)")
print(f"\nActive: {sum(1 for t in db_trades if t['status']=='active')}, "
      f"Expired: {sum(1 for t in db_trades if t['status']=='expired')}")

# ── Excel trades ──
wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
fil_sheets = {
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

excel_trades = []
for sname, cpty in fil_sheets.items():
    ws = wb[sname]
    rows = list(ws.iter_rows(values_only=True))
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0] and str(r[0]).strip() == "Initial Trade Date":
            header_idx = i
            break
    if header_idx is None:
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

        if not td or not side or not opt_type:
            continue
        side_s = str(side).strip().lower()
        if side_s in ("", "total", "totals"):
            continue

        excel_trades.append({
            "counterparty": cpty,
            "trade_date": to_date_str(td),
            "side": str(side).strip().capitalize(),
            "option_type": str(opt_type).strip().lower(),
            "expiry": to_date_str(expiry),
            "strike": to_float(strike),
            "qty": to_float(qty),
            "premium_usd": to_float(premium),
        })
wb.close()

xl_cpty = Counter(t["counterparty"] for t in excel_trades)
print(f"\n=== EXCEL (New File) ===")
print(f"Total FIL trades: {len(excel_trades)}")
for c, n in sorted(xl_cpty.items()):
    act = sum(1 for t in excel_trades if t["counterparty"] == c and t["expiry"] >= TODAY)
    exp = sum(1 for t in excel_trades if t["counterparty"] == c and t["expiry"] < TODAY)
    print(f"  {c}: {n} total ({act} active, {exp} expired)")
xl_active = [t for t in excel_trades if t["expiry"] >= TODAY]
xl_expired = [t for t in excel_trades if t["expiry"] < TODAY]
print(f"\nActive (expiry >= {TODAY}): {len(xl_active)}, Expired: {len(xl_expired)}")

# ── Reconciliation ──
def make_key(t, src="db"):
    if src == "db":
        side = "Buy" if t["side"].lower() == "buy" else ("Sell" if t["side"].lower() == "sell" else t["side"])
        opt = t["option_type"].lower().rstrip("s")  # puts -> put
        expiry = t["expiry"].split("T")[0]
        return (t["counterparty"], t["trade_date"].split("T")[0], side, opt, expiry,
                round(float(t["strike"]), 2), round(float(t["qty"]), 0))
    else:
        opt = t["option_type"].lower().rstrip("s")
        return (t["counterparty"], t["trade_date"], t["side"], opt, t["expiry"],
                round(t["strike"], 2), round(t["qty"], 0))

db_keys = defaultdict(list)
for t in db_trades:
    db_keys[make_key(t, "db")].append(t)

xl_keys = defaultdict(list)
for t in excel_trades:
    xl_keys[make_key(t, "xl")].append(t)

all_keys = set(db_keys.keys()) | set(xl_keys.keys())
matched = 0
in_db_only = []
in_xl_only = []

for k in all_keys:
    db_list = db_keys.get(k, [])
    xl_list = xl_keys.get(k, [])
    n_match = min(len(db_list), len(xl_list))
    matched += n_match
    for t in db_list[n_match:]:
        in_db_only.append(t)
    for t in xl_list[n_match:]:
        in_xl_only.append(t)

print(f"\n=== RECONCILIATION ===")
print(f"Matched: {matched}")
print(f"In DB only (not in Excel): {len(in_db_only)}")
print(f"In Excel only (missing from DB): {len(in_xl_only)}")

if in_db_only:
    print(f"\n--- IN DB ONLY ({len(in_db_only)}) ---")
    for t in sorted(in_db_only, key=lambda x: (x["counterparty"], x["expiry"])):
        print(f"  DB#{t['id']} | {t['counterparty']} | {t['trade_date']} | "
              f"{t['side']} {t['option_type']} | K={t['strike']} | exp={t['expiry']} | "
              f"qty={t['qty']} | prem=${t['premium_usd']} | {t['status']}")

if in_xl_only:
    xl_only_active = [t for t in in_xl_only if t["expiry"] >= TODAY]
    xl_only_expired = [t for t in in_xl_only if t["expiry"] < TODAY]

    print(f"\n--- IN EXCEL ONLY - ACTIVE ({len(xl_only_active)}) --- NEED TO ADD TO DB ---")
    for t in sorted(xl_only_active, key=lambda x: (x["counterparty"], x["expiry"])):
        print(f"  {t['counterparty']} | {t['trade_date']} | {t['side']} {t['option_type']} | "
              f"K={t['strike']} | exp={t['expiry']} | qty={t['qty']} | prem=${t['premium_usd']}")

    print(f"\n--- IN EXCEL ONLY - EXPIRED ({len(xl_only_expired)}) ---")
    for t in sorted(xl_only_expired, key=lambda x: (x["counterparty"], x["expiry"]))[:50]:
        print(f"  {t['counterparty']} | {t['trade_date']} | {t['side']} {t['option_type']} | "
              f"K={t['strike']} | exp={t['expiry']} | qty={t['qty']} | prem=${t['premium_usd']}")
    if len(xl_only_expired) > 50:
        print(f"  ... and {len(xl_only_expired) - 50} more expired trades")
