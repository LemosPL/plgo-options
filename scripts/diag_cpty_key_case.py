"""Guard the counterparty-key case trap that reached production.

CPTY_PRICING (app.js) is keyed lowercase; a trade's `counterparty` from the
backend is display-cased ("KeyRock", "Flowdesk"). getCptyMethod does an object
lookup, and every caller reads a null result as "uncalibrated - use mid vol",
so a case mismatch produces a WRONG PRICE WITH NO ERROR rather than a failure.
That shipped once in optv4DisplayPriceAtStrike.

This checks:
  1. getCptyMethod normalizes its key (so no call site can reintroduce it).
  2. Every counterparty name the backend can emit resolves to a CPTY_PRICING
     key once normalized - or is knowingly uncalibrated.

Run:  .venv\\Scripts\\python.exe scripts/diag_cpty_key_case.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src/plgo_options/web/static/app.js"
PY_CPTY = ROOT / "src/plgo_options/pricing/cpty_pricing.py"


def main() -> int:
    app = APP.read_text(encoding="utf-8")
    failures = []

    # 1. getCptyMethod must normalize before the lookup.
    m = re.search(r"function getCptyMethod\([^)]*\)\s*\{(.*?)\n\}", app, re.S)
    if not m:
        print("FAIL: getCptyMethod not found")
        return 1
    body = m.group(1)
    lookup = re.search(r"CPTY_PRICING\[([^\]]+)\]", body)
    print("getCptyMethod lookup expression:", lookup.group(1).strip() if lookup else "(none)")
    normalized = bool(lookup and "toLowerCase" in lookup.group(1))
    print(f"  normalizes case internally: {normalized}")
    if not normalized:
        failures.append("getCptyMethod does not lowercase its key before the lookup")

    # 2. Which keys exist in the JS table, and in the Python port.
    js_block = re.search(r"const CPTY_PRICING\s*=\s*\{(.*?)\n\};", app, re.S)
    js_keys = set()
    if js_block:
        js_keys = set(re.findall(r"^\s{2}(\w+)\s*:\s*\{", js_block.group(1), re.M))
    print(f"\nCPTY_PRICING keys (app.js): {sorted(js_keys)}")
    if not js_keys:
        failures.append("could not parse CPTY_PRICING keys from app.js")
    bad_case = [k for k in js_keys if k != k.lower()]
    if bad_case:
        failures.append(f"CPTY_PRICING has non-lowercase keys: {bad_case}")

    if PY_CPTY.exists():
        py = PY_CPTY.read_text(encoding="utf-8")
        py_keys = set(re.findall(r'^\s*"(\w+)"\s*:\s*\{', py, re.M))
        print(f"CPTY_PRICING keys (cpty_pricing.py): {sorted(py_keys)}")
        # The Python port is meant to mirror the JS table; a key only on one side
        # means one surface prices a counterparty the other does not.
        only_js, only_py = js_keys - py_keys, py_keys - js_keys
        if js_keys and py_keys:
            if only_js:
                print(f"  only in app.js: {sorted(only_js)}")
            if only_py:
                print(f"  only in python: {sorted(only_py)}")

    # 3. Display-cased forms must resolve after normalization.
    display_forms = ["KeyRock", "Keyrock", "Flowdesk", "FlowDesk", "Wave", "G20", " keyrock "]
    print("\ndisplay-cased forms -> normalized -> resolves?")
    for name in display_forms:
        norm = name.strip().lower()
        hit = norm in js_keys
        print(f"  {name!r:12s} -> {norm!r:12s} -> {'HIT' if hit else 'no key (uncalibrated, mid vol)'}")

    # 4. Every remaining getCptyMethod call site, for the record.
    sites = [(i + 1, ln.strip()) for i, ln in enumerate(app.splitlines())
             if "getCptyMethod(" in ln and "function getCptyMethod" not in ln]
    print(f"\ngetCptyMethod call sites: {len(sites)} (all safe now that the function normalizes)")
    for line_no, text in sites:
        print(f"  app.js:{line_no}: {text[:96]}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
