"""Crude JS structural smoke test for app.js.

There is no node/JS runtime in this environment, so this is the cheapest way to
catch the failure mode a hand edit actually causes: an unclosed block. It strips
comments, strings and template literals, then checks that braces, parens and
brackets balance. It is NOT a parser -- balanced delimiters do not prove the file
is valid JS, only that no block was left hanging.

Run:  .venv\\Scripts\\python.exe scripts/diag_js_balance.py
"""
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src/plgo_options/web/static/app.js"


def strip_literals(src: str) -> str:
    """Remove // and /* */ comments and the contents of ' " ` literals."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'`":
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                # Template literals can nest real code in ${...}; keep that code
                # so its delimiters still count toward the balance. Both the
                # opening and closing brace must be emitted -- dropping either
                # one makes every interpolation look like an unclosed block.
                # (Strings *inside* ${...} aren't re-stripped, so a literal brace
                # in one would skew the count; rare enough for a smoke test.)
                if quote == "`" and src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    i += 1          # skip the '$', leaving i on the '{'
                    depth = 0
                    while i < n:
                        ch = src[i]
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                        out.append(ch)
                        i += 1
                        if depth == 0:
                            break
                    continue
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    stripped = strip_literals(src)
    bad = False
    for op, cl, name in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
        o, c = stripped.count(op), stripped.count(cl)
        status = "OK" if o == c else "UNBALANCED"
        if o != c:
            bad = True
        print(f"  {name:9s} open={o:5d} close={c:5d} delta={o - c:+d}  {status}")
    print(f"  lines={src.count(chr(10)) + 1}  bytes={len(src.encode('utf-8'))}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
