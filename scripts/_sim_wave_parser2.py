"""Validate: OPEN = 'Days Remaining to Expiry' is a plain (small) number."""
import openpyxl, datetime
path = r'C:/Users/Lucas Lemos/Downloads/Filecoin - Wave.xlsx'
wb = openpyxl.load_workbook(path, data_only=True)

def norm_side(s):
    s = str(s or "").strip().upper()
    if "BUY" in s: return "Buy"
    if "SELL" in s: return "Sell"
    return None

def norm_type(t):
    t = str(t or "").strip().upper()
    return "Call" if t.startswith("C") else ("Put" if t.startswith("P") else "")

def norm_date(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return (v.date() if isinstance(v, datetime.datetime) else v).isoformat()
    return str(v or "").split(" ")[0]

def is_open_days(v):
    # numeric day-count shown in the column (excludes 'Expired', blank, and date-serials)
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 3650

ws = wb['FIL Option Positions']
aoa = [[c.value for c in row] for row in ws.iter_rows()]
hdr = next(i for i, r in enumerate(aoa) if any(str(c or "").strip().lower() == "counterparty" for c in r))
header = [str(c or "").strip().lower() for c in aoa[hdr]]
def find(pred):
    return next((i for i, h in enumerate(header) if pred(h)), -1)
cp   = find(lambda h: h == "counterparty")
bs   = find(lambda h: "buy/sell" in h)
days = find(lambda h: "days remaining" in h)
exp  = find(lambda h: "expiry date" in h or "expiry" in h)
strk = find(lambda h: "strike" in h)
qty  = find(lambda h: "options" in h)
typ  = cp + 1 if bs - cp == 2 else -1
print(f"idx cp={cp} type={typ} bs={bs} days={days} exp={exp} strike={strk} qty={qty}")

out = []
for r in aoa[hdr+1:]:
    def g(i): return r[i] if 0 <= i < len(r) else None
    if not is_open_days(g(days)):      # <-- OPEN filter = numeric days remaining
        continue
    side = norm_side(g(bs))
    if side not in ("Buy", "Sell"): continue
    try: k = float(g(strk))
    except (TypeError, ValueError): k = 0
    e = norm_date(g(exp))
    if not e or k <= 0: continue
    out.append((side, norm_type(g(typ)) if typ >= 0 else "Call", k, e, g(days), abs(float(g(qty) or 0))))

print(f"OPEN rows: {len(out)}")
for o in out:
    print(f"  {o[0]:4} {o[1]:4} K={o[2]:<6g} exp={o[3]} days={o[4]:>6.1f} qty={o[5]:>12,.0f}")
