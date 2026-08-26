"""Validate the Optimizer v4 chart palette against the dataviz method's gates.

The v4 payoff / target-profile charts draw the book at several time horizons.
Horizons are ORDERED, so they take an *ordinal* ramp (one hue, monotone
lightness) rather than a hue per horizon - a rainbow across time re-encodes
order as identity. This script drives the dataviz skill's own validator so the
ramp is measured, never eyeballed, and it reads the hexes back out of app.js so
the shipped code and this analysis cannot drift apart.

Background - what the measurements forced:
  * The original ramp was seven hand-picked cyans stepping toward near-black.
    On the actual plot surface (#0d1117) its last two steps measured 1.99:1 and
    1.48:1, under the 2:1 ordinal floor: invisible at the far end.
  * Six slots, not seven. Documented ramp steps sit dL~0.047 apart, so a legal
    gap must skip one (0.094), and the 2:1 floor caps the dark end at step 600.
    Eleven usable steps at two-step spacing is six slots. T+16 is therefore not
    drawn on the v4 charts (it remains in the P&L matrix).
  * "Now" takes the ramp's bright end rather than the old separate cyan, since
    it is t=0 of the same family. That cyan measured only dE 7.3 from the ramp.
  * The what-if "excl" line moved off violet (dE 12.1 from the ramp) to aqua.

Run:  .venv\\Scripts\\python.exe scripts/diag_chart_palette.py
"""
import re
import sys
from pathlib import Path

SKILL = Path(
    r"C:\Users\LUCASL~1\AppData\Local\Temp\claude\bundled-skills\2.1.233"
    r"\a5e65a7cdf8e3ba2931ac8b66516b604\dataviz\scripts"
)
sys.path.insert(0, str(SKILL))

import validate_palette as v  # noqa: E402

APP_JS = Path(__file__).resolve().parents[1] / "src/plgo_options/web/static/app.js"

# Documented sequential/ordinal blue ramp (dataviz references/palette.md).
BLUE = {
    100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef",
    300: "#6da7ec", 350: "#5598e7", 400: "#3987e5", 450: "#2a78d6",
    500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281",
    700: "#0d366b",
}

PLOT_SURFACE = "#0d1117"      # plot_bgcolor in optv4ChartLayout
OLD_RAMP = ["#8fe1ff", "#4fc3f7", "#35a3d6", "#2a80ac", "#1f5f83", "#164a68", "#0e3550"]
NORMAL_FLOOR = 15.0           # unsimulated dE: series must not be confusable
LIGHT_FLOOR = 2.0             # validator's ORDINAL_LIGHT_FLOOR


def main() -> int:
    # The validator's own report text contains an arrow; Windows consoles
    # default to cp1252 and would raise on it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    failures: list[str] = []

    print(f"Why the old ramp failed, measured against the plot surface {PLOT_SURFACE}:")
    for hexv in OLD_RAMP:
        L, _ = v.oklch(hexv)
        cr = v.contrast(hexv, PLOT_SURFACE)
        flag = "  <-- below the 2:1 ordinal floor" if cr < LIGHT_FLOOR else ""
        print(f"  {hexv}  L={L:.3f}  contrast={cr:5.2f}{flag}")

    print("\nDocumented blue ramp, usable steps on this surface:")
    print("  step  hex        L      C      contrast")
    for step, hexv in BLUE.items():
        cr = v.contrast(hexv, PLOT_SURFACE)
        L, C = v.oklch(hexv)
        note = "" if cr >= LIGHT_FLOOR else "   (below floor - unusable)"
        print(f"  {step:4d}  {hexv}  {L:.3f}  {C:.3f}  {cr:6.2f}{note}")

    # ── What app.js actually ships ────────────────────────────────────────────
    app = APP_JS.read_text(encoding="utf-8")
    block = re.search(r"const OPTV4_HORIZON_COLOR = \{(.*?)\n\};", app, re.S)
    if not block:
        print("\nFAIL: OPTV4_HORIZON_COLOR not found in app.js")
        return 1
    shipped = re.findall(r"(\d+):\s*\"(#[0-9a-fA-F]{6})\"", block.group(1))
    horizons = [int(k) for k, _ in shipped]
    ramp = [h for _, h in shipped]

    print("\n=== Shipped in app.js ===")
    print(f"  horizons : {horizons}")
    print(f"  ramp     : {ramp}")

    undocumented = [h for h in ramp if h not in BLUE.values()]
    if undocumented:
        failures.append(f"ramp uses undocumented hexes: {undocumented}")
    print(f"  every step from the documented ramp: {not undocumented}")

    report, ok = v.validate_ordinal(ramp, "dark", PLOT_SURFACE)
    print(f"\n  ordinal gates on {PLOT_SURFACE}:")
    for name, passed, detail in report:
        print(f"    [{'PASS' if passed else 'FAIL'}] {name:22s} {detail}")
    if not ok:
        failures.append("shipped ramp fails the ordinal gates")

    # The chart's other fixed colours must not be confusable with any ramp step.
    tail = app[block.end():]
    sem = {}
    for role in ("before", "target", "excl"):
        m = re.search(rf"\b{role}:\s*\"(#[0-9a-fA-F]{{6}})\"", tail)
        if m:
            sem[role] = m.group(1)
    print(f"\n  fixed semantic colours vs the ramp (unsimulated dE, >={NORMAL_FLOOR:.0f}):")
    for role, hexv in sem.items():
        d = min(v.deltaE(hexv, h) for h in ramp)
        cr = v.contrast(hexv, PLOT_SURFACE)
        flag = "" if d >= NORMAL_FLOOR else "  <-- confusable with the ramp"
        if d < NORMAL_FLOOR:
            failures.append(f"{role} {hexv} only dE {d:.1f} from the ramp")
        print(f"    {role:8s} {hexv}  nearest-ramp dE={d:5.1f}  contrast={cr:5.2f}{flag}")
    if len(sem) < 3:
        failures.append(f"could not read all semantic colours from app.js (got {sorted(sem)})")

    # A 7th slot must remain impossible - if this ever passes, the cap moved and
    # the "six slots" comment in app.js is stale.
    usable = [BLUE[s] for s in sorted(BLUE) if v.contrast(BLUE[s], PLOT_SURFACE) >= LIGHT_FLOOR]
    seven = usable[:: max(1, (len(usable) - 1) // 6)][:7]
    seven_ok = len(seven) == 7 and v.validate_ordinal(seven, "dark", PLOT_SURFACE)[1]
    print(f"\n  a 7th one-hue step is still impossible here: {not seven_ok}")
    if seven_ok:
        failures.append("a 7-step ramp now passes - revisit the 6-slot cap in app.js")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
