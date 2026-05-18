"""Insert the 3 extra trades flagged during reconciliation:
  - ETH FlowDesk active Buy Put K=2000 exp=2026-04-27 (trade_date 2026-02-20)
  - 2 FIL G20 expired trades on 2026-03-27 (expired 2026-04-10)

Re-checks that each key is still missing before inserting (defensive).
"""
import sqlite3
from datetime import datetime, date
from collections import defaultdict
import openpyxl

TODAY = date.today().isoformat()
AUDIT_TAG = "excel_import_recon_20260420_extras"
EXCEL_PATH = r"C:\Users\Lucas Lemos\Downloads\Copy of FIL, ETH & BTC Option Strategies (2).xlsx"
DB_PATH = r"C:\Users\Lucas Lemos\PycharmProjects\plgo_options\data\plgo_options.db"

# Specific extras to insert (asset, cpty, sheet, trade_date, side_raw, option_type_raw, expiry, strike, qty)
EXTRAS_KEYS = [
    ("ETH", "FlowDesk", "Flowdesk - ETH Option Positions", "2026-02-20", "Buy", "Puts", "2026-04-27", 2000.0, 1500.0),
    ("FIL", "G20",      "G20 - FIL Option Positions",      "2026-03-27", "Buy", "Puts", "2026-04-10", 0.76,   1000000.0),
    ("FIL", "G20",      "G20 - FIL Option Positions",      "2026-03-27", "Sell","Puts", "2026-04-10", 1.343,  1000000.0),
]


def to_date_str(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).split("T")[0] if v else ""


def to_float(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


def norm_side(s):
    return str(s or "").strip().lower()


def norm_opt(s):
    return str(s or "").strip().lower().rstrip("s")


def fmt_side(asset, side):
    s = norm_side(side)
    return s.capitalize() if asset == "ETH" else s


def fmt_opt(asset, ot):
    s = str(ot or "").strip().lower()
    if asset == "ETH":
        return s.rstrip("s").capitalize()
    return s if s.endswith("s") else s + "s"


def match_key(asset, cpty, td_, side, ot, exp, strike, qty):
    return (asset, cpty, td_.split("T")[0], norm_side(side), norm_opt(ot),
            exp.split("T")[0], round(float(strike), 4), round(float(qty), 0))


# ── Load DB keys ──
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
db_rows = conn.execute(
    "SELECT asset, counterparty, trade_date, side, option_type, expiry, strike, qty "
    "FROM trades WHERE status != 'deleted'"
).fetchall()
db_key_count = defaultdict(int)
for r in db_rows:
    k = match_key(r["asset"], r["counterparty"], r["trade_date"], r["side"],
                  r["option_type"], r["expiry"], r["strike"], r["qty"])
    db_key_count[k] += 1

# ── Parse only the sheets we need and collect matching rows ──
wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
# Map (asset, cpty, td, side, ot, exp, strike, qty) -> full excel record
by_key = defaultdict(list)
needed_sheets = {extra[2] for extra in EXTRAS_KEYS}
sheet_meta = {extra[2]: (extra[0], extra[1]) for extra in EXTRAS_KEYS}

for sname in needed_sheets:
    asset, cpty = sheet_meta[sname]
    ws = wb[sname]
    rows = list(ws.iter_rows(values_only=True))
    hi = None
    for i, r in enumerate(rows):
        if r and r[0] and str(r[0]).strip() == "Initial Trade Date":
            hi = i
            break
    headers = [str(h).strip() if h else f"c{j}" for j, h in enumerate(rows[hi])]
    qty_col = f"# of {asset} Options"
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        if not r or all(c is None for c in r):
            continue
        rec = dict(zip(headers, r))
        td_val = rec.get("Initial Trade Date")
        side = rec.get("Buy / Sell / Unwind")
        ot = rec.get("Option Type")
        if not td_val or not side or not ot:
            continue
        if norm_side(side) in ("", "total", "totals"):
            continue
        strike = to_float(rec.get("$ Strike"))
        qty = to_float(rec.get(qty_col))
        if strike <= 0 or qty <= 0:
            continue
        t = {
            "asset": asset,
            "counterparty": cpty,
            "trade_date": to_date_str(td_val),
            "side_raw": side,
            "option_type_raw": ot,
            "expiry": to_date_str(rec.get("Option Expiry Date")),
            "strike": strike,
            "qty": qty,
            "ref_spot": to_float(rec.get("Ref. Spot Price")),
            "pct_otm": to_float(rec.get("% OTM")),
            "notional_mm": to_float(rec.get("$ Notional (mm)")),
            "premium_per": to_float(rec.get("Premium/ option ($)")),
            "premium_usd": to_float(rec.get("Total $ Premium Received/(Paid)")),
        }
        k = match_key(asset, cpty, t["trade_date"], side, ot, t["expiry"], strike, qty)
        by_key[k].append(t)
wb.close()

# ── For each requested extra, find its excel record (respecting DB multiset count) ──
inserted = 0
for (asset, cpty, sheet, td_, side, ot, exp, strike, qty) in EXTRAS_KEYS:
    k = match_key(asset, cpty, td_, side, ot, exp, strike, qty)
    xl_list = by_key.get(k, [])
    db_cnt = db_key_count.get(k, 0)
    available = xl_list[db_cnt:]
    if not available:
        print(f"SKIP (already in DB or not in Excel): {asset}/{cpty} {td_} {side} {ot} K={strike} qty={qty}")
        continue
    t = available[0]
    status = "active" if t["expiry"] >= TODAY else "expired"
    side_db = fmt_side(asset, t["side_raw"])
    opt_db = fmt_opt(asset, t["option_type_raw"])
    cur = conn.execute(
        """INSERT INTO trades
           (asset, counterparty, trade_id, trade_date, side, option_type,
            expiry, strike, ref_spot, pct_otm, qty,
            notional_mm, premium_per, premium_usd, status)
           VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset, t["counterparty"], t["trade_date"], side_db, opt_db,
         t["expiry"], t["strike"], t["ref_spot"], t["pct_otm"], t["qty"],
         t["notional_mm"], t["premium_per"], t["premium_usd"], status),
    )
    new_id = cur.lastrowid
    conn.execute(
        "INSERT INTO trade_audit_log (trade_id, action, changed_by) VALUES (?, 'create', ?)",
        (new_id, AUDIT_TAG),
    )
    # Account for this insert so duplicate keys across extras stay correct
    db_key_count[k] += 1
    inserted += 1
    print(f"INSERTED #{new_id}: {asset} {cpty} {t['trade_date']} {side_db} {opt_db} "
          f"K={t['strike']} exp={t['expiry']} qty={t['qty']} status={status}")

conn.commit()

for asset in ("FIL", "ETH"):
    r = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE asset=? AND status!='deleted'", (asset,)
    ).fetchone()[0]
    print(f"DB {asset} total now: {r}")

conn.close()
print(f"\n=== DONE: inserted {inserted} extra trades, audit tag '{AUDIT_TAG}' ===")
