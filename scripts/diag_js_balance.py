"""Syntax check for app.js: a real parse when possible, balance as a fallback.

The balance check alone is NOT sufficient and has already let a real bug ship:
a hand edit put a literal newline inside a double-quoted string, which balances
perfectly but is a hard SyntaxError, so app.js never executed and the whole page
came up dead with only a console message to show for it.

So: if node is available, run `node --check`, which is an actual parse and the
only thing that proves the file loads. Balance counting stays as a fallback for
environments without node, and still usefully localises an unclosed block.

Node is looked up on PATH and then in the usual Windows install locations, since
it is frequently installed but not exported to a Git Bash PATH.

Run:  .venv\\Scripts\\python.exe scripts/diag_js_balance.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src/plgo_options/web/static/app.js"

NODE_CANDIDATES = (
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
)


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in NODE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def node_check(node: str, target: Path) -> bool:
    """True when node parses the file. Prints node's own error if it doesn't."""
    proc = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        print("  node --check   PARSE OK")
        return True
    print("  node --check   SYNTAX ERROR")
    for line in (proc.stderr or proc.stdout or "").splitlines()[:12]:
        print(f"    {line}")
    return False


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

    node = find_node()
    if node:
        if not node_check(node, TARGET):
            bad = True
    else:
        print("  node --check   SKIPPED (node not found) -- balance only, which "
              "does NOT prove the file parses")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
