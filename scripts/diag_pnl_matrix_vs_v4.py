"""Prove the Portfolio P&L matrix == Optimizer v4's "before" matrix.

Builds a synthetic book, values it the way /api/portfolio/pnl now does
(`pnl_by_horizon`, bs_vec_bridge) plus the frontend's anchoring, and compares
against OptimizerV3.build_payoffs' own `before_payoff` for the same book.
Also prints the old /pnl valuation so the "flat far columns" is visible.
"""
import numpy as np

from plgo_options.optimization.math_utils import bs_vec_bridge, bs_vec
from plgo_options.optimization.optimizer_v3 import OptimizerV3
from plgo_options.optimization.models import Position
from plgo_options.web.routes.portfolio import PNL_MATRIX_HORIZONS

SPOT = 2440.0
LADDER = np.array(list(range(500, 7100, 100)), dtype=float)
HZ = PNL_MATRIX_HORIZONS

BOOK = [
    # (strike, opt, dte, iv_pct, signed_qty)
    (3000.0, "C", 20, 62.0, -4000.0),
    (2000.0, "P", 45, 70.0, -3000.0),
    (2800.0, "C", 120, 58.0, 1500.0),
]


def portfolio_side():
    """What routes/portfolio.py now ships as position['pnl_by_horizon']."""
    per_pos = []
    for strike, opt, dte, iv, qty in BOOK:
        curves = {}
        for h in HZ:
            curves[str(h)] = np.round(qty * bs_vec_bridge(SPOT, LADDER, strike, dte, h, iv / 100.0, opt), 2)
        per_pos.append(curves)
    # frontend pfRenderMtmGrid: sum the set, then subtract value at (h=0, spot)
    total = {str(h): sum(c[str(h)] for c in per_pos) for h in HZ}
    anchor = float(np.interp(SPOT, LADDER, total["0"]))
    return {k: v - anchor for k, v in total.items()}


def old_portfolio_side():
    """The pre-change valuation: T_h = max(dte - h, 0), raw MTM."""
    total = {}
    for h in HZ:
        acc = np.zeros(len(LADDER))
        for strike, opt, dte, iv, qty in BOOK:
            acc += qty * bs_vec(LADDER, strike, max(dte - h, 0) / 365.25, 0.0, iv / 100.0, opt)
        total[str(h)] = acc
    return total


def v4_side():
    """OptimizerV3.build_payoffs' own before curves, same book."""
    o = object.__new__(OptimizerV3)
    o.spot = SPOT
    o.positions = [
        Position(id=i, instrument="", opt=opt, counterparty="x", side="", strike=strike,
                 expiry="2026-12-25", days_remaining=dte, net_qty=qty, iv_pct=iv,
                 delta=None, gamma=None, theta=None, vega=None, mark_price_usd=0.0,
                 current_mtm=0.0, payoff_by_horizon={}, mtm_by_horizon=[])
        for i, (strike, opt, dte, iv, qty) in enumerate(BOOK)
    ]
    before, _after, _mtm = o.build_payoffs(HZ, LADDER, [])
    return {k: np.array(v) for k, v in before.items()}


mine, theirs, old = portfolio_side(), v4_side(), old_portfolio_side()

print(f"horizons={HZ}  spot={SPOT}")
worst = 0.0
for h in HZ:
    k = str(h)
    d = float(np.max(np.abs(mine[k] - theirs[k])))
    worst = max(worst, d)
    print(f"  h={h:>4}d  max|portfolio - v4| = {d:.4f}")
print(f"MAX DIFF ACROSS MATRIX: {worst:.4f}  ->", "MATCH" if worst < 0.02 else "MISMATCH")

i = int(np.argmin(np.abs(LADDER - SPOT * 1.25)))
print(f"\nRow at spot {LADDER[i]:.0f} (+25%):")
print("  new (v4 P&L)  : " + "  ".join(f"{h}d={mine[str(h)][i]:>12,.0f}" for h in HZ))
print("  old (raw MTM) : " + "  ".join(f"{h}d={old[str(h)][i]:>12,.0f}" for h in HZ))
print("\nNote the old row repeating past 45d (longest short-dated leg) — that was the flat-columns bug.")
