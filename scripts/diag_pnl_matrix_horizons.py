"""Diagnose flat P&L-matrix columns.

Fetches /api/portfolio/pnl (same payload the Portfolio P&L screen and Optimizer
v4 both render) and prints, per horizon, the summed book curve at a few spot
levels plus the DTE distribution — so we can tell a rendering bug from a book
that is simply all-intrinsic past the longest expiry.
"""
import sys
import urllib.request
import json
from collections import Counter

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
ASSET = sys.argv[2] if len(sys.argv) > 2 else "ETH"
INCLUDE_EXPIRED = sys.argv[3] if len(sys.argv) > 3 else "false"

url = f"{BASE}/api/portfolio/pnl?asset={ASSET}&include_expired={INCLUDE_EXPIRED}"
with urllib.request.urlopen(url, timeout=180) as r:
    d = json.loads(r.read())

spots = d["spot_ladder"]
pos = d["positions"]
spot = d["eth_spot"]
print(f"asset={ASSET} include_expired={INCLUDE_EXPIRED} spot={spot} positions={len(pos)}")
print(f"ladder: {spots[0]} .. {spots[-1]} ({len(spots)} pts)")
print(f"matrix_horizons={d['matrix_horizons']}")
print(f"chart_horizons={d['chart_horizons']}")
print("payoff keys:", sorted(int(k) for k in pos[0]["payoff_by_horizon"]))

dtes = Counter()
for p in pos:
    dte = p.get("days_remaining")
    dtes[dte] += 1
print("DTE histogram (dte: count):", dict(sorted(dtes.items())))
print("max DTE:", max(dtes))

HZ = [0, 16, 30, 60, 90, 120, 150]
# nearest ladder index to a few interesting spot levels
def idx_of(x):
    return min(range(len(spots)), key=lambda i: abs(spots[i] - x))

levels = [spots[0], spot * 0.8, spot, spot * 1.25, spots[-1]]


def book(field, h):
    return [sum(p[field].get(str(h), [0] * len(spots))[i] for p in pos) for i in range(len(spots))]


def _lin(series, x):
    """Linear interp on the ladder — same convention as optv4InterpAt."""
    import bisect
    if x <= spots[0]:
        return series[0]
    if x >= spots[-1]:
        return series[-1]
    j = bisect.bisect_left(spots, x)
    x0, x1, y0, y1 = spots[j - 1], spots[j], series[j - 1], series[j]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


has_new = "pnl_by_horizon" in pos[0]
print("pnl_by_horizon present:", has_new)

print("\nRAW payoff_by_horizon (old matrix valuation):")
for lv in levels:
    i = idx_of(lv)
    row = [f"{h}d={book('payoff_by_horizon', h)[i]:,.0f}" for h in HZ]
    print(f"  spot {spots[i]:>8}: " + "  ".join(row))

if has_new:
    curves = {h: book("pnl_by_horizon", h) for h in HZ}
    anchor = _lin(curves[0], spot)
    print(f"\nANCHORED pnl_by_horizon (new matrix == v4 before), anchor={anchor:,.0f}:")
    for lv in levels:
        i = idx_of(lv)
        row = [f"{h}d={curves[h][i] - anchor:,.0f}" for h in HZ]
        print(f"  spot {spots[i]:>8}: " + "  ".join(row))
    at_spot = _lin([v - anchor for v in curves[0]], spot)
    print(f"\ninvariant: (Now, spot) cell = {at_spot:.4f} (must be 0)")
