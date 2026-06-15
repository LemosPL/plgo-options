#!/usr/bin/env python3
"""Bilateral OTC variation-margin classifier for crypto options.

Single file, standard library only (math + csv + argparse + datetime).

For a book of options it tells you, per leg and for the whole portfolio, whether
each position CALLS margin (you post collateral) or PAYS margin (it is an asset /
collateral credit), the spot direction that triggers a call, and the size of the
variation-margin swing per spot move.

Perspective = MINE (the holder of this book). Black-Scholes spot model, q=0,
no skew adjustment. Variation margin is approximated to first order (delta), which
is exactly what the netted/gross sensitivity rollups in the spec are built on.

Usage examples
--------------
  # Run the built-in default book:
  python scripts/margin_tool.py --valuation-date 2026-06-04

  # Same book but tell it your real FIL holdings (covers the short call):
  python scripts/margin_tool.py --valuation-date 2026-06-04 --holdings 750000

  # Load positions from a CSV (columns: counterparty,side,type,strike,expiry_date,quantity,iv):
  python scripts/margin_tool.py --csv mybook.csv --spot 0.90
"""
from __future__ import annotations

import argparse
import csv
import math
from datetime import date, datetime

# ---------------------------------------------------------------------------
# CONFIG BLOCK — edit here, or override any of it on the command line.
# ---------------------------------------------------------------------------
SPOT = 0.90              # current underlying price
RATE = 0.0              # risk-free rate r
VALUATION_DATE = None    # None -> today()
HOLDINGS = 0.0          # physical underlying units I own (covered-call logic)
FLAT_IV = 0.80          # implied vol used for any leg that does not specify its own
MOVE_SIZES = [0.05, 0.10, 0.20]   # absolute spot moves ($) to stress
REPORT_PATH = "margin_report.md"

# Default book to report on. iv=None -> use FLAT_IV.
#   buy  1,250,000  FIL 31JUL26  0.50 P
#   sell   500,000  FIL 31JUL26  2.50 P
#   sell   750,000  FIL 31JUL26  0.50 C
#   buy    500,000  FIL 31JUL26  2.50 C
DEFAULT_POSITIONS = [
    dict(counterparty="OTC", side="buy",  type="P", strike=0.50, expiry="2026-07-31", quantity=1_250_000, iv=None),
    dict(counterparty="OTC", side="sell", type="P", strike=2.50, expiry="2026-07-31", quantity=500_000,   iv=None),
    dict(counterparty="OTC", side="sell", type="C", strike=0.50, expiry="2026-07-31", quantity=750_000,   iv=None),
    dict(counterparty="OTC", side="buy",  type="C", strike=2.50, expiry="2026-07-31", quantity=500_000,   iv=None),
]


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price_delta(spot, strike, T, r, sigma, opt):
    """Return (per-unit price, per-unit delta) for a European option.

    call delta = N(d1); put delta = N(d1) - 1. At/past expiry -> intrinsic.
    """
    opt = opt.upper()
    if T <= 0 or sigma <= 0:
        if opt == "C":
            return max(spot - strike, 0.0), (1.0 if spot > strike else 0.0)
        return max(strike - spot, 0.0), (-1.0 if spot < strike else 0.0)

    sq = sigma * math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = math.exp(-r * T)
    if opt == "C":
        price = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
    return price, delta


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------
def _as_date(v) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def classify(positions, spot, r, val_date, holdings, move_sizes, flat_iv):
    """Price + classify every leg. Returns a list of enriched result dicts."""
    results = []
    for p in positions:
        sigma = float(p["iv"]) if p.get("iv") not in (None, "", 0, 0.0) else flat_iv
        exp = _as_date(p["expiry"])
        days = (exp - val_date).days
        T = max(days, 0) / 365.0
        price, du = bs_price_delta(spot, float(p["strike"]), T, r, sigma, p["type"])
        sign = 1.0 if str(p["side"]).lower() in ("buy", "long") else -1.0
        qty = float(p["quantity"])
        results.append(dict(
            p=p, counterparty=p.get("counterparty", "OTC"), typ=p["type"].upper(),
            strike=float(p["strike"]), exp=exp, days=days, T=T, sigma=sigma,
            price=price, du=du, sign=sign, qty=qty,
            pos_delta=du * qty * sign,
            mtm=sign * price * qty,           # + asset (long) / - liability (short)
        ))

    # Covered-call allocation: pledge holdings to short calls, largest-quantity first.
    for r_ in results:
        r_["covered"] = False
        r_["covered_qty"] = 0.0
    short_calls = sorted(
        [r_ for r_ in results if r_["sign"] < 0 and r_["typ"] == "C"],
        key=lambda r_: -r_["qty"],
    )
    remaining = float(holdings)
    for r_ in short_calls:
        cov = min(max(remaining, 0.0), r_["qty"])
        r_["covered_qty"] = cov
        r_["covered"] = cov >= r_["qty"] and r_["qty"] > 0
        remaining -= cov

    # Margin verdict, cover label, cash requirement, VM swings.
    for r_ in results:
        if r_["sign"] > 0:
            # Long option: prepaid premium, an asset / collateral credit. Never calls.
            r_["verdict"], r_["dir"] = "PAYS (asset)", "PAYS"
            r_["cover"], r_["cash"], r_["pledge_qty"] = "asset (credit)", 0.0, 0.0
        elif r_["typ"] == "C":
            # Short call: liability grows as spot RISES.
            r_["verdict"], r_["dir"] = "CALLS on UP", "UP"
            if r_["covered"]:
                r_["cover"], r_["cash"], r_["pledge_qty"] = "covered (pledge)", 0.0, r_["qty"]
            else:
                # Naked (or partially covered) -> uncovered units need cash margin.
                naked_qty = r_["qty"] - r_["covered_qty"]
                r_["cover"] = "cash margin" if r_["covered_qty"] == 0 else "part-pledge"
                r_["cash"] = spot * naked_qty       # notional of the naked portion (unbounded risk)
                r_["pledge_qty"] = r_["covered_qty"]
        else:
            # Short put: obligates me to BUY -> never covered by holdings, always cash.
            r_["verdict"], r_["dir"] = "CALLS on DOWN", "DOWN"
            r_["cover"] = "cash (CSP)"
            r_["cash"] = r_["strike"] * r_["qty"]   # cash-secured put amount = strike * qty
            r_["pledge_qty"] = 0.0
        # VM swing for a +move (spot up). Negative => I post; positive => released.
        r_["vm"] = {m: r_["pos_delta"] * m for m in move_sizes}

    return results


def rollup(results, spot, move_sizes):
    """Portfolio-level aggregates."""
    net_delta = sum(r["pos_delta"] for r in results)
    net_mtm = sum(r["mtm"] for r in results)
    short = [r for r in results if r["sign"] < 0]
    long_ = [r for r in results if r["sign"] > 0]
    short_liab_gross = sum(-r["mtm"] for r in short)     # positive magnitude
    long_asset = sum(r["mtm"] for r in long_)            # positive magnitude

    # Per-move sensitivities.
    netted = {}     # signed netted swing for an UP move (all legs in one netting set)
    gross_up = {}   # cash I must POST on an UP move if legs margin separately (no netting)
    gross_down = {} # cash I must POST on a DOWN move with no netting
    for m in move_sizes:
        netted[m] = net_delta * m
        gross_up[m] = sum(max(0.0, -(r["pos_delta"] * m)) for r in results)
        gross_down[m] = sum(max(0.0, (r["pos_delta"] * m)) for r in results)

    calls_up = any(r["dir"] == "UP" for r in short)
    calls_down = any(r["dir"] == "DOWN" for r in short)

    cash_required = sum(r["cash"] for r in short)
    pledged_qty = sum(r["pledge_qty"] for r in results)

    return dict(
        net_delta=net_delta, dollar_delta=net_delta * spot, net_mtm=net_mtm,
        short_liab_gross=short_liab_gross, long_asset=long_asset,
        netted=netted, gross_up=gross_up, gross_down=gross_down,
        two_sided=(calls_up and calls_down), calls_up=calls_up, calls_down=calls_down,
        cash_required=cash_required, pledged_qty=pledged_qty, pledged_value=pledged_qty * spot,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def m0(x):   # signed whole dollars
    return f"{x:+,.0f}"

def u0(x):   # unsigned whole dollars
    return f"{x:,.0f}"

def d3(x):   # per-unit delta, 3 dp
    return f"{x:+.3f}"

def qn(x):   # quantity / position-delta, whole with commas
    return f"{x:+,.0f}"

DIR_TAG = {"UP": "UP", "DOWN": "DN", "PAYS": "--"}
COVER_SHORT = {
    "asset (credit)": "asset", "covered (pledge)": "pledge",
    "cash margin": "cash", "cash (CSP)": "cash", "part-pledge": "part",
}


def _aligned_table(headers, rows, aligns):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(c.ljust(widths[i]) if aligns[i] == "l" else c.rjust(widths[i]))
        return "  ".join(out)
    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------
def print_console(results, roll, spot, r, val_date, holdings, move_sizes, flat_iv):
    print()
    print("=" * 64)
    print("  OTC VARIATION-MARGIN REPORT  (my perspective)")
    print("=" * 64)
    print(f"  spot {spot:.4f} | r {r:.3f} | val {val_date.isoformat()} | "
          f"holdings {holdings:,.0f} | flat iv {flat_iv:.0%}")
    print()

    # --- Position table (compact, aligned) ---
    headers = ["#", "Leg", "Qty", "Px", "MTM $", "Pos d", "Call", "Cover"]
    aligns =  ["r", "l",   "r",   "r",  "r",     "r",     "l",    "l"]
    rows = []
    for i, x in enumerate(results, 1):
        leg = f"{x['p']['side'][:1].upper()}{x['p']['side'][1:].lower():<3} {x['typ']} {x['strike']:.2f}"
        rows.append([
            str(i), leg, f"{x['qty']:,.0f}", f"{x['price']:.4f}",
            m0(x["mtm"]), qn(x["pos_delta"]), DIR_TAG[x["dir"]], COVER_SHORT[x["cover"]],
        ])
    print("POSITIONS")
    print(_aligned_table(headers, rows, aligns))
    print("  Call: UP = liability grows on rise | DN = on fall | -- = pays/asset")
    print(f"  (all legs expire {results[0]['exp'].strftime('%d%b%y').upper()}, "
          f"{results[0]['days']}d)")
    print()

    # --- VM swing table ---
    headers = ["#"] + [f"+{m:g}" for m in move_sizes]
    aligns = ["r"] + ["r"] * len(move_sizes)
    rows = []
    for i, x in enumerate(results, 1):
        rows.append([str(i)] + [m0(x["vm"][m]) for m in move_sizes])
    print("VM SWING on a +move  ($ as spot RISES; negative = I POST, positive = released)")
    print(_aligned_table(headers, rows, aligns))
    print()

    # --- Portfolio summary ---
    print("PORTFOLIO SUMMARY")
    print("-" * 64)
    print(f"  Net delta            : {roll['net_delta']:+,.3f} FIL")
    print(f"  Dollar delta         : {m0(roll['dollar_delta'])}  (net delta x spot)")
    print(f"  Net MTM              : {m0(roll['net_mtm'])}  (+ asset / - liability)")
    print(f"  Short-leg liability  : {u0(roll['short_liab_gross'])} gross "
          f"| {m0(roll['net_mtm'])} net of long assets")
    print()
    print("  VM sensitivity per move:")
    for m in move_sizes:
        print(f"    move {m:g}:  netted {m0(roll['netted'][m])} (one netting set)"
              f"  |  gross POST  up {u0(roll['gross_up'][m])} / down {u0(roll['gross_down'][m])}")
    print()
    print(f"  Cash collateral req. : {u0(roll['cash_required'])}  (cash-margin short legs)")
    print(f"  Underlying pledged   : {roll['pledged_qty']:,.0f} FIL "
          f"(~{u0(roll['pledged_value'])})")
    print()
    if roll["two_sided"]:
        print("  ** TWO-SIDED MARGIN WARNING **")
        print("     Some legs call on UP and others on DOWN -> you can be called in")
        print("     BOTH directions. A single hedge will not neutralise both ends.")
    else:
        side = "UP" if roll["calls_up"] else ("DOWN" if roll["calls_down"] else "neither")
        print(f"  Margin-call exposure : one-sided ({side}).")
    print("=" * 64)
    print()


# ---------------------------------------------------------------------------
# Markdown report (full detail, no width constraint)
# ---------------------------------------------------------------------------
def write_markdown(path, results, roll, spot, r, val_date, holdings, move_sizes, flat_iv):
    lines = []
    lines.append("# OTC Variation-Margin Report")
    lines.append("")
    lines.append(f"- **Spot**: {spot:.4f}")
    lines.append(f"- **r**: {r:.4f}")
    lines.append(f"- **Valuation date**: {val_date.isoformat()}")
    lines.append(f"- **Holdings**: {holdings:,.0f} FIL")
    lines.append(f"- **Flat IV**: {flat_iv:.2%} (legs without their own iv)")
    lines.append(f"- **Move sizes ($)**: {', '.join(f'{m:g}' for m in move_sizes)}")
    lines.append("")
    lines.append("## Positions")
    lines.append("")
    hdr = ["CP", "Side", "Type", "Strike", "Expiry", "DTE", "IV", "Qty",
           "Per-unit Px", "MTM $ (+asset/-liab)", "Per-unit d", "Position d",
           "Verdict", "Cover"]
    lines.append("| " + " | ".join(hdr) + " |")
    lines.append("|" + "|".join(["---"] * len(hdr)) + "|")
    for x in results:
        lines.append("| " + " | ".join([
            str(x["counterparty"]),
            x["p"]["side"],
            x["typ"],
            f"{x['strike']:.2f}",
            x["exp"].strftime("%d%b%y").upper(),
            f"{x['days']}",
            f"{x['sigma']:.0%}",
            f"{x['qty']:,.0f}",
            f"{x['price']:.4f}",
            m0(x["mtm"]),
            d3(x["du"]),
            qn(x["pos_delta"]),
            x["verdict"],
            x["cover"],
        ]) + " |")
    lines.append("")
    lines.append("### VM swing per move ($, signed for a spot RISE; negative = I post)")
    lines.append("")
    vmh = ["#", "Leg"] + [f"+{m:g}" for m in move_sizes]
    lines.append("| " + " | ".join(vmh) + " |")
    lines.append("|" + "|".join(["---"] * len(vmh)) + "|")
    for i, x in enumerate(results, 1):
        leg = f"{x['p']['side']} {x['typ']} {x['strike']:.2f}"
        lines.append("| " + " | ".join([str(i), leg] + [m0(x["vm"][m]) for m in move_sizes]) + " |")
    lines.append("")
    lines.append("## Portfolio rollup")
    lines.append("")
    lines.append(f"- **Net delta**: {roll['net_delta']:+,.3f} FIL")
    lines.append(f"- **Dollar delta**: {m0(roll['dollar_delta'])} (net delta x spot)")
    lines.append(f"- **Net MTM**: {m0(roll['net_mtm'])} (+ asset / - liability)")
    lines.append(f"- **Standing short-leg liability**: {u0(roll['short_liab_gross'])} gross, "
                 f"{m0(roll['net_mtm'])} net of long assets")
    lines.append("")
    lines.append("### VM sensitivity")
    lines.append("")
    lines.append("| Move ($) | Netted swing (UP, one set) | Gross POST on UP | Gross POST on DOWN |")
    lines.append("|---|---|---|---|")
    for m in move_sizes:
        lines.append(f"| {m:g} | {m0(roll['netted'][m])} | {u0(roll['gross_up'][m])} "
                     f"| {u0(roll['gross_down'][m])} |")
    lines.append("")
    lines.append(f"- **Cash collateral required**: {u0(roll['cash_required'])} "
                 f"(sum of cash-margin short legs)")
    lines.append(f"- **Underlying pledged**: {roll['pledged_qty']:,.0f} FIL "
                 f"(~{u0(roll['pledged_value'])})")
    lines.append("")
    if roll["two_sided"]:
        lines.append("> **TWO-SIDED MARGIN WARNING** — some legs call on UP and others on "
                     "DOWN. You can be called in both directions; a single directional hedge "
                     "will not neutralise both ends.")
    else:
        side = "UP" if roll["calls_up"] else ("DOWN" if roll["calls_down"] else "neither")
        lines.append(f"> Margin-call exposure is one-sided ({side}).")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CSV input
# ---------------------------------------------------------------------------
def load_csv(path):
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            iv = row.get("iv", "").strip()
            out.append(dict(
                counterparty=row.get("counterparty", "OTC").strip() or "OTC",
                side=row["side"].strip(),
                type=row["type"].strip(),
                strike=float(row["strike"]),
                expiry=row["expiry_date"].strip(),
                quantity=float(str(row["quantity"]).replace(",", "")),
                iv=float(iv) if iv else None,
            ))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="OTC variation-margin classifier for options.")
    ap.add_argument("--spot", type=float, default=SPOT)
    ap.add_argument("--rate", type=float, default=RATE)
    ap.add_argument("--valuation-date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--holdings", type=float, default=HOLDINGS, help="physical underlying units owned")
    ap.add_argument("--iv", type=float, default=FLAT_IV, help="flat IV for legs without their own")
    ap.add_argument("--moves", default=None, help="comma list of absolute spot moves, e.g. 0.05,0.10,0.20")
    ap.add_argument("--csv", default=None, help="positions CSV (counterparty,side,type,strike,expiry_date,quantity,iv)")
    ap.add_argument("--out", default=REPORT_PATH, help="markdown output path")
    args = ap.parse_args(argv)

    val_date = _as_date(args.valuation_date) if args.valuation_date else date.today()
    move_sizes = ([float(x) for x in args.moves.split(",")] if args.moves else list(MOVE_SIZES))
    positions = load_csv(args.csv) if args.csv else DEFAULT_POSITIONS

    results = classify(positions, args.spot, args.rate, val_date, args.holdings, move_sizes, args.iv)
    roll = rollup(results, args.spot, move_sizes)

    print_console(results, roll, args.spot, args.rate, val_date, args.holdings, move_sizes, args.iv)
    write_markdown(args.out, results, roll, args.spot, args.rate, val_date, args.holdings, move_sizes, args.iv)
    print(f"Markdown report written to {args.out}")


if __name__ == "__main__":
    main()
