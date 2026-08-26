"""Check the target-table control grid (port of app.js optv4TargetGrid).

The FIL target table used to snap a $0.25 step onto whatever the spot ladder
happened to be, which gave rows 2-3 ladder points apart pre-run and unrounded
log-moneyness values (0.74 / 0.83 / 0.94 ...) after a run. The grid is now a
fixed price step, independent of ladder spacing, with values interpolated onto
it. This verifies the row prices come out as round $0.10 steps in both cases.

Run:  .venv\\Scripts\\python.exe scripts/diag_target_grid.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STEP = {"FIL": 0.10, "ETH": 250.0}
DP = {"FIL": 2, "ETH": 0}


def target_grid(spots: list[float], asset: str) -> list[float]:
    """Direct port of optv4TargetGrid."""
    if not spots:
        return []
    step = STEP[asset]
    dp = DP[asset]
    lo, hi = spots[0], spots[-1]
    rnd = lambda v: round(v, dp)
    out: list[float] = []
    g = math.ceil(lo / step - 1e-9) * step
    while g <= hi + 1e-9:
        v = rnd(g)
        if not out or v > out[-1]:
            out.append(v)
        g += step
    if not out or out[0] > rnd(lo):
        out.insert(0, rnd(lo))
    if out[-1] < rnd(hi):
        out.append(rnd(hi))
    return out


def interp_at(spots, series, x):
    """Direct port of optv4InterpAt (flat extrapolation past either end)."""
    if not series or not spots:
        return None
    if x <= spots[0]:
        return series[0]
    if x >= spots[-1]:
        return series[-1]
    i = 1
    while i < len(spots) and spots[i] < x:
        i += 1
    x0, x1, y0, y1 = spots[i - 1], spots[i], series[i - 1], series[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def log_ladder(spot, lo, hi, n):
    """The optimizer's post-run ladder (optim_usecase._log_moneyness_ladder shape):
    geometric spacing, NOT rounded for FIL."""
    import numpy as np
    return [float(v) for v in np.exp(np.linspace(math.log(lo), math.log(hi), n))]


def main() -> int:
    failures = []

    # Pre-run FIL ladder from portfolio.py: 0.20 .. 3.00 at 0.10
    fil_pre = [round(0.2 + i * 0.1, 2) for i in range(29)]
    g = target_grid(fil_pre, "FIL")
    print(f"FIL pre-run  ladder: {len(fil_pre)} pts {fil_pre[0]}..{fil_pre[-1]}")
    print(f"  grid: {len(g)} rows  {g[:6]} ... {g[-3:]}")
    steps = {round(b - a, 2) for a, b in zip(g, g[1:])}
    print(f"  distinct row spacings: {sorted(steps)}")
    if steps != {0.10}:
        failures.append(f"pre-run FIL grid spacing {sorted(steps)} != [0.1]")
    if len(g) != 29:
        failures.append(f"pre-run FIL grid has {len(g)} rows, expected 29")

    # Post-run FIL ladder: log-moneyness, unrounded
    fil_post = log_ladder(0.73, 0.20, 3.00, 29)
    print(f"\nFIL post-run ladder (log-moneyness, unrounded):")
    print(f"  raw: {[round(v, 4) for v in fil_post[:6]]} ... (these were the old row labels)")
    g2 = target_grid(fil_post, "FIL")
    print(f"  grid: {len(g2)} rows  {g2[:6]} ... {g2[-3:]}")
    steps2 = {round(b - a, 2) for a, b in zip(g2, g2[1:])}
    print(f"  distinct row spacings: {sorted(steps2)}")
    if not steps2 <= {0.10}:
        failures.append(f"post-run FIL grid spacing {sorted(steps2)} not all 0.1")
    # every row must be a clean 10-cent multiple
    ragged = [v for v in g2 if abs(round(v * 10) - v * 10) > 1e-9]
    if ragged:
        failures.append(f"post-run FIL grid has non-10c rows: {ragged[:5]}")

    # Interpolation must reproduce the series exactly at shared points and stay
    # monotone between them.
    series = [(-15_750_000 + 10_000_000 * (s - 0.2)) for s in fil_pre]
    exact = all(abs(interp_at(fil_pre, series, s) - v) < 1e-6 for s, v in zip(fil_pre, series))
    print(f"\ninterp reproduces the ladder's own points exactly: {exact}")
    if not exact:
        failures.append("interp_at does not reproduce ladder points")
    mid = interp_at(fil_pre, series, 0.25)
    expect = (series[0] + series[1]) / 2
    print(f"interp at a midpoint 0.25: {mid:,.1f} (expected {expect:,.1f})")
    if abs(mid - expect) > 1e-6:
        failures.append("interp_at midpoint wrong")

    # ETH sanity
    eth = list(range(500, 7100, 100))
    ge = target_grid([float(v) for v in eth], "ETH")
    print(f"\nETH grid: {len(ge)} rows  {ge[:4]} ... {ge[-2:]}")
    if {b - a for a, b in zip(ge, ge[1:])} != {250.0}:
        failures.append("ETH grid spacing != 250")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
