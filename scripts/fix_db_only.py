"""Investigate the 59 DB-only trades: why didn't they match Excel?
Find their likely Excel counterparts and fix or remove duplicates.
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
    if isinstance(v, datetime): return v.strftime("%Y-%m-%d")
    if isinstance(v, date): return v.isoformat()
    return str(v).split("T")[0] if v else ""

def to_float(v):
    try: return float(v) if v else 0
    except: return 0

# ── Load DB trades ──
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
db_trades = [dict(r) for r in conn.execute(
    "SELECT * FROM trades WHERE asset='FIL' AND status != 'deleted' ORDER BY id"
).fetchall()]
conn.close()

# ── Parse Excel ──
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
    if header_idx is None: continue
    headers = [str(h).strip() if h else f"col_{j}" for j, h in enumerate(rows[header_idx])]
    for i in range(header_idx + 1, len(rows)):
        r = rows[i]
        if not r or all(c is None for c in r): continue
        rec = dict(zip(headers, r))
        td = rec.get("Initial Trade Date")
        side = rec.get("Buy / Sell / Unwind")
        opt_type = rec.get("Option Type")
        expiry = rec.get("Option Expiry Date")
        strike = rec.get("$ Strike")
        qty = rec.get("# of FIL Options")
        premium = rec.get("Total $ Premium Received/(Paid)")
        if not td or not side or not opt_type: continue
        side_s = str(side).strip().lower()
        if side_s in ("", "total", "totals"): continue
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

# ── Build matching keys (same as recon) ──
def make_db_key(t):
    side = "Buy" if t["side"].lower() == "buy" else ("Sell" if t["side"].lower() == "sell" else t["side"])
    opt = t["option_type"].lower().rstrip("s")
    expiry = t["expiry"].split("T")[0]
    return (t["counterparty"], t["trade_date"].split("T")[0], side, opt, expiry,
            round(float(t["strike"]), 2), round(float(t["qty"]), 0))

def make_xl_key(t):
    opt = t["option_type"].lower().rstrip("s")
    return (t["counterparty"], t["trade_date"], t["side"], opt, t["expiry"],
            round(t["strike"], 2), round(t["qty"], 0))

db_by_key = defaultdict(list)
for t in db_trades:
    db_by_key[make_db_key(t)].append(t)

xl_by_key = defaultdict(list)
for t in excel_trades:
    xl_by_key[make_xl_key(t)].append(t)

# Find DB-only (excess DB entries per key)
db_only = []
for k, db_list in db_by_key.items():
    xl_count = len(xl_by_key.get(k, []))
    for t in db_list[xl_count:]:
        db_only.append(t)

print(f"DB-only trades: {len(db_only)}\n")

# ── Investigate: try relaxed matching ──
# Maybe the side capitalization is different (e.g., "Sell Calls" vs "Sell Call")
# Or the option_type has "Puts" vs "Put", or "Calls" vs "Call"

def make_relaxed_key(cpty, trade_date, side, opt_type, expiry, strike):
    """Match without qty, normalize more aggressively."""
    side_norm = side.strip().lower().split()[0]  # "sell calls" -> "sell"
    opt_norm = opt_type.strip().lower().rstrip("s")  # "puts" -> "put", "calls" -> "call"
    if opt_norm not in ("put", "call"):
        # might be "calls" -> "call" already handled, or something else
        if "put" in opt_norm: opt_norm = "put"
        elif "call" in opt_norm: opt_norm = "call"
    return (cpty, trade_date.split("T")[0], side_norm, opt_norm, expiry.split("T")[0], round(float(strike), 2))

xl_relaxed = defaultdict(list)
for t in excel_trades:
    k = make_relaxed_key(t["counterparty"], t["trade_date"], t["side"], t["option_type"], t["expiry"], t["strike"])
    xl_relaxed[k].append(t)

print("=== INVESTIGATING DB-ONLY TRADES ===\n")
matched_relaxed = []
truly_orphan = []

for t in db_only:
    db_k = make_relaxed_key(t["counterparty"], t["trade_date"], t["side"], t["option_type"], t["expiry"], t["strike"])
    xl_matches = xl_relaxed.get(db_k, [])

    if xl_matches:
        # Found relaxed match — the qty or exact key was different
        best = xl_matches[0]
        print(f"  DB#{t['id']} RELAXED MATCH:")
        print(f"    DB:  {t['counterparty']} | {t['trade_date']} | {t['side']} {t['option_type']} | K={t['strike']} | exp={t['expiry']} | qty={t['qty']} | prem=${t['premium_usd']}")
        print(f"    XL:  {best['counterparty']} | {best['trade_date']} | {best['side']} {best['option_type']} | K={best['strike']} | exp={best['expiry']} | qty={best['qty']} | prem=${best['premium_usd']}")
        print(f"    DIFF: qty DB={t['qty']} vs XL={best['qty']}, prem DB=${t['premium_usd']} vs XL=${best['premium_usd']}")
        print()
        matched_relaxed.append((t, xl_matches))
    else:
        truly_orphan.append(t)
        print(f"  DB#{t['id']} NO MATCH AT ALL:")
        print(f"    {t['counterparty']} | {t['trade_date']} | {t['side']} {t['option_type']} | K={t['strike']} | exp={t['expiry']} | qty={t['qty']} | prem=${t['premium_usd']} | {t['status']}")
        print()

print(f"\n=== SUMMARY ===")
print(f"Relaxed matches (different qty or key normalization): {len(matched_relaxed)}")
print(f"True orphans (no Excel match at all): {len(truly_orphan)}")
