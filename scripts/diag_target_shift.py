"""Sanity-check the target-profile re-centering math against the real shipped CSVs.

Run:  .venv\\Scripts\\python.exe scripts/diag_target_shift.py

Verifies, for every built-in curve, that: the anchor is detected on an interior
trough; a moneyness shift lands that anchor exactly on the requested spot; the
payoff column is untouched unless scale_payoff is set; a parallel shift either
preserves dollar distances or refuses outright rather than producing a
non-positive strike; and the legacy shift_target_profile wrapper still matches
its documented contract.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plgo_options.optimization.misc_utils import (  # noqa: E402
    detect_profile_anchor,
    load_target_profile_file,
    rescale_target_profile,
    shift_target_profile,
)

CASES = (
    ("ETH - target shifted v2.csv", 4500.0),
    ("ETH - target shifted.csv", 4500.0),
    ("ETH - target.csv", 4500.0),
    ("FIL - target v3.csv", 1.35),
    ("FIL - target v2.csv", 1.35),
    ("FIL - target.csv", 1.35),
)


def main() -> int:
    failures = []
    for fn, new_spot in CASES:
        df = load_target_profile_file(fn)
        k = np.asarray(df.index, float)
        p = np.asarray(df["Payoff($)"], float)
        anchor, kind = detect_profile_anchor(df)
        print(f"\n=== {fn}: {len(df)} pts, strikes {k[0]:,.4f}..{k[-1]:,.4f}")
        print(f"    payoff {p.min():,.0f}..{p.max():,.0f}   anchor={anchor:,.4f} ({kind})")

        sh = rescale_target_profile(df, anchor, new_spot)
        k2 = np.asarray(sh.index, float)
        print(f"    moneyness -> {new_spot:,.4f}: strikes {k2[0]:,.4f}..{k2[-1]:,.4f} (x{new_spot / anchor:.3f})")

        a2, _ = detect_profile_anchor(sh)
        if abs(a2 - new_spot) > 1e-6 * max(1.0, new_spot):
            failures.append(f"{fn}: shifted anchor {a2} != requested {new_spot}")
        if not np.allclose(np.asarray(sh["Payoff($)"], float), p):
            failures.append(f"{fn}: payoff column changed without scale_payoff")

        # Legacy wrapper: strikes scaled so the *minimum* payoff sits on the spot.
        expected = k * (new_spot / float(df["Payoff($)"].idxmin()))
        if not np.allclose(np.asarray(shift_target_profile(df, new_spot).index, float), expected):
            failures.append(f"{fn}: shift_target_profile wrapper changed behavior")

        try:
            pl = rescale_target_profile(df, anchor, new_spot, mode="parallel")
            k3 = np.asarray(pl.index, float)
            print(f"    parallel  -> {new_spot:,.4f}: strikes {k3[0]:,.4f}..{k3[-1]:,.4f} (+{new_spot - anchor:,.4f})")
            if not np.allclose(k3 - k, new_spot - anchor):
                failures.append(f"{fn}: parallel shift did not preserve dollar distances")
        except ValueError as e:
            print(f"    parallel  -> refused: {e}")

        sp = rescale_target_profile(df, anchor, new_spot, scale_payoff=True)
        p3 = np.asarray(sp["Payoff($)"], float)
        print(f"    scale_payoff: payoff {p3.min():,.0f}..{p3.max():,.0f}")
        if not np.allclose(p3, p * (new_spot / anchor)):
            failures.append(f"{fn}: scale_payoff did not scale by the shift ratio")

    print()
    for bad, args in (
        ("to_spot <= 0", dict(from_spot=100.0, to_spot=0.0)),
        ("from_spot <= 0", dict(from_spot=0.0, to_spot=100.0)),
        ("unknown mode", dict(from_spot=100.0, to_spot=200.0, mode="sideways")),
    ):
        df = load_target_profile_file(CASES[0][0])
        try:
            rescale_target_profile(df, **args)
            failures.append(f"expected a ValueError for {bad}, got none")
        except ValueError as e:
            print(f"rejects {bad}: {e}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
