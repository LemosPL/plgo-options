"""Diagnostic for the Action Radar signal engine (web/signal_engine.py).

The local dev DB has almost no live legs, so this drives the engine against
hand-built deals instead: it stubs out ``build_deals_payload`` with synthetic
structures chosen to trip each signal kind, then checks the sign conventions,
the economic gate, and the trigger-level detection.

Run:  .venv/Scripts/python.exe scripts/diag_signals.py
"""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import date, timedelta

import numpy as np

# The engine's rationale strings use em-dashes / arrows; a cp1252 Windows
# console can't encode those, so force UTF-8 on stdout before importing.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import plgo_options.web.signal_engine as se  # noqa: E402

GRID = np.linspace(500.0, 12000.0, 161)
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def build_deal(deal_id: str, cpty: str, strategy: str, legs_spec: list[dict],
               dte: int = 60) -> dict:
    """Build a deals-payload-shaped deal from leg specs.

    Mirrors ``deals._build_deal``'s conventions exactly: ``premium_usd`` is
    signed (+ received / - paid) and each leg's payoff already includes it.
    """
    expiry = (date.today() + timedelta(days=dte)).isoformat()
    legs, total = [], np.zeros_like(GRID)
    net_credit = 0.0
    for i, spec in enumerate(legs_spec):
        sign = 1.0 if spec["side"] == "Long" else -1.0
        intrinsic = (np.maximum(GRID - spec["strike"], 0.0) if spec["opt"] == "C"
                     else np.maximum(spec["strike"] - GRID, 0.0))
        payoff = sign * spec["qty"] * intrinsic + spec["premium_usd"]
        total = total + payoff
        net_credit += spec["premium_usd"]
        legs.append({
            "id": 1000 + i, "side": spec["side"], "opt": spec["opt"],
            "qty": spec["qty"], "strike": spec["strike"], "expiry": expiry,
            "premium_usd": spec["premium_usd"],
            "payoff": [float(x) for x in payoff],
        })
    return {
        "id": deal_id, "counterparty": cpty, "strategy": strategy,
        "group_id": deal_id, "leg_ids": [l["id"] for l in legs],
        "expiry": expiry, "days_to_expiry": dte, "n_legs": len(legs),
        "net_credit": net_credit, "legs": legs,
        "payoff": [float(x) for x in total],
        "max_profit": float(np.max(total)), "max_loss": float(np.min(total)),
    }


async def run(deals: list[dict], spot: float, **kw) -> dict:
    """Run a scan against synthetic deals with a flat DEFAULT_IV surface."""
    payload = {"asset": "ETH", "spot": spot, "deals": deals,
               "grid": [float(x) for x in GRID]}
    ctx = {"smiles": {}, "deribit_dates": {}, "spot": spot}   # -> flat DEFAULT_IV

    async def stub(asset, include_expired, overrides=None):
        return payload, ctx

    real, se.build_deals_payload = se.build_deals_payload, stub
    try:
        return await se.scan_deals(asset="ETH", **kw)
    finally:
        se.build_deals_payload = real


def show(sig: dict) -> None:
    p = sig["package"]
    trig = "now" if sig["trigger_spot"] is None else \
        f"{sig['direction']} @ {sig['trigger_spot']:,.0f} ({sig['distance_pct']}% away)"
    print(f"      {sig['kind']:9} {sig['state']:12} {trig:28} "
          f"net={p['net_cash']:>11,.0f} margin_freed={-p['d_margin']:>11,.0f} "
          f"dMaxLoss={p['d_max_loss']:>11,.0f} "
          f"dPoP={'  n/a' if p['d_prob_profit'] is None else format(p['d_prob_profit'] * 100, '5.1f') + 'pp'} "
          f"gate={'OK ' if p['passes_gate'] else 'NO '}")
    for l in p["close"]:
        print(f"         CLOSE {l['qty']:g}x {l['side']} {l['opt']} {l['strike']:g} @ {l['price']:,.2f} -> {l['cash']:>+12,.0f}")
    for l in p["open"]:
        print(f"         OPEN  {l['qty']:g}x {l['side']} {l['opt']} {l['strike']:g} @ {l['price']:,.2f} -> {l['cash']:>+12,.0f} ({l['delta']:+.3f}d)")
    print(f"         why: {p['rationale']}")
    print(f"         gate: {p['gate_reason']}")


async def main() -> None:
    print("=" * 100)
    print("A. RECYCLE — short 2000P decayed to pennies, spot 3800")
    print("=" * 100)
    deal = build_deal("cp|A", "Wave", "Short Put", [
        {"side": "Short", "opt": "P", "strike": 2000.0, "qty": 10.0, "premium_usd": 15000.0},
    ])
    r = await run([deal], 3800.0, kinds=["recycle"], include_rejected=True)
    for s in r["signals"]:
        show(s)
    rec = [s for s in r["signals"] if s["kind"] == "recycle"]
    check("recycle fires", len(rec) == 1)
    if rec:
        p = rec[0]["package"]
        check("state is live", rec[0]["state"] == "live", rec[0]["state"])
        check("closes 1 leg, opens 1 leg", len(p["close"]) == 1 and len(p["open"]) == 1)
        check("buying back the short costs money", p["close"][0]["cash"] < 0,
              f"{p['close'][0]['cash']:,.0f}")
        check("selling the replacement brings money in", p["open"][0]["cash"] > 0,
              f"{p['open'][0]['cash']:,.0f}")
        check("replacement is nearer spot than 2000", p["open"][0]["strike"] > 2000)
        check("net cash strongly positive", p["net_cash"] > 1000, f"{p['net_cash']:,.0f}")
        check("gate passes", p["passes_gate"])
        # Selling a 3050 put instead of a 2000 put adds exactly 10 x 1050 of
        # tail risk. The structural figure must equal that, with the trade's
        # cash backed out.
        expect = 10 * (2000 - p["open"][0]["strike"])
        check("structural risk change == qty x strike distance",
              abs(p["structural_risk_cut"] - expect) < 1.0,
              f"got {p['structural_risk_cut']:,.0f}, expected {expect:,.0f}")

    print()
    print("=" * 100)
    print("B. TIGHTEN — short 3000C / long 3600C, deep ITM at spot 4500")
    print("=" * 100)
    deal = build_deal("cp|B", "Flowdesk", "Call Spread", [
        {"side": "Short", "opt": "C", "strike": 3000.0, "qty": 10.0, "premium_usd": 40000.0},
        {"side": "Long", "opt": "C", "strike": 3600.0, "qty": 10.0, "premium_usd": -18000.0},
    ])
    r = await run([deal], 4500.0, kinds=["tighten"], include_rejected=True)
    for s in r["signals"]:
        show(s)
    tig = [s for s in r["signals"] if s["kind"] == "tighten"]
    check("tighten fires", len(tig) == 1)
    if tig:
        p = tig[0]["package"]
        check("state is live", tig[0]["state"] == "live", tig[0]["state"])
        check("narrows the spread", abs(p["open"][0]["strike"] - 3600) < 600,
              f"reopened at {p['open'][0]['strike']:g}")
        check("max loss improves", p["d_max_loss"] > 0, f"{p['d_max_loss']:,.0f}")
        check("frees collateral", p["d_margin"] <= 0, f"d_margin {p['d_margin']:,.0f}")
        # Width goes 600 -> 300 on 10 lots, so the structural risk removed is
        # exactly 3,000. This is the invariant that catches the "cost counted
        # twice" class of bug in the gate.
        expect = 10 * (600 - abs(p["open"][0]["strike"] - 3600))
        check("structural risk cut == qty x width removed",
              abs(p["structural_risk_cut"] - expect) < 1.0,
              f"got {p['structural_risk_cut']:,.0f}, expected {expect:,.0f}")

    print()
    print("=" * 100)
    print("C. TIGHTEN TRIGGER — same spread at spot 3800 (not yet deep enough ITM)")
    print("   3600 needs to be 15% ITM => trigger should sit near 3600/0.85 = 4235")
    print("=" * 100)
    r = await run([deal], 3800.0, kinds=["tighten"], include_rejected=True)
    for s in r["signals"]:
        show(s)
    tig = [s for s in r["signals"] if s["kind"] == "tighten"]
    check("not live at 3800", all(s["state"] != "live" for s in tig))
    ups = [s for s in tig if s["direction"] == "up"]
    check("an upside trigger is found", len(ups) == 1)
    if ups:
        lvl = ups[0]["trigger_spot"]
        check("trigger is near 4235", abs(lvl - 4235) < 120, f"got {lvl:,.0f}")
        check("no downside trigger", not [s for s in tig if s["direction"] == "down"])

    print()
    print("=" * 100)
    print("D. REDUCE — short 4000P x30 deep ITM at spot 3000, only 20k collected")
    print("=" * 100)
    deal = build_deal("cp|D", "Galaxy", "Short Put", [
        {"side": "Short", "opt": "P", "strike": 4000.0, "qty": 30.0, "premium_usd": 20000.0},
    ])
    r = await run([deal], 3000.0, kinds=["reduce"], include_rejected=True)
    for s in r["signals"]:
        show(s)
    red = [s for s in r["signals"] if s["kind"] == "reduce"]
    check("reduce fires", len(red) == 1)
    if red:
        p = red[0]["package"]
        check("closes part of the short only", len(p["close"]) == 1
              and p["close"][0]["qty"] < 30)
        check("re-strikes further OTM (lower put strike)",
              p["open"][0]["strike"] < 4000, f"{p['open'][0]['strike']:g}")
        check("max loss improves", p["d_max_loss"] > 0, f"{p['d_max_loss']:,.0f}")
        check("costs cash (de-risking is not free)", p["net_cash"] < 0,
              f"{p['net_cash']:,.0f}")
        qty, k_new = p["close"][0]["qty"], p["open"][0]["strike"]
        expect = qty * (4000 - k_new)
        check("structural risk cut == qty x strike distance",
              abs(p["structural_risk_cut"] - expect) < 1.0,
              f"got {p['structural_risk_cut']:,.0f}, expected {expect:,.0f}")
        check("efficiency is measured on structural risk, not net-of-cost",
              p["efficiency"] is None
              or abs(p["efficiency"] - expect / abs(p["net_cash"])) < 0.05,
              f"efficiency {p['efficiency']}")

    print()
    print("=" * 100)
    print("E. INCREASE — far-OTM strangle sitting on unrealised profit, spot 3800")
    print("=" * 100)
    deal = build_deal("cp|E", "KeyRock", "Strangle", [
        {"side": "Short", "opt": "P", "strike": 1800.0, "qty": 20.0, "premium_usd": 30000.0},
        {"side": "Short", "opt": "C", "strike": 7000.0, "qty": 20.0, "premium_usd": 25000.0},
    ])
    r = await run([deal], 3800.0, kinds=["increase"], include_rejected=True)
    for s in r["signals"]:
        show(s)
    inc = [s for s in r["signals"] if s["kind"] == "increase"]
    check("increase fires", len(inc) == 1)
    if inc:
        p = inc[0]["package"]
        check("closes nothing", not p["close"])
        check("adds every leg", len(p["open"]) == 2)
        check("adds 25% of qty", all(abs(l["qty"] - 5.0) < 1e-6 for l in p["open"]),
              str([l["qty"] for l in p["open"]]))
        check("brings premium in", p["net_cash"] > 0, f"{p['net_cash']:,.0f}")

    print()
    print("=" * 100)
    print("F. GATE — the same recycle with min_net_usd raised above its net cash")
    print("=" * 100)
    deal = build_deal("cp|F", "Wave", "Short Put", [
        {"side": "Short", "opt": "P", "strike": 2000.0, "qty": 10.0, "premium_usd": 15000.0},
    ])
    r = await run([deal], 3800.0, kinds=["recycle"], include_rejected=True,
                  cfg=se.SignalConfig(min_net_usd=10_000_000.0))
    check("rejected packages are surfaced when asked",
          len(r["signals"]) == 1 and not r["signals"][0]["package"]["passes_gate"])
    if r["signals"]:
        print(f"      reason: {r['signals'][0]['package']['gate_reason']}")
    r2 = await run([deal], 3800.0, kinds=["recycle"], include_rejected=False,
                   cfg=se.SignalConfig(min_net_usd=10_000_000.0))
    check("and hidden by default", len(r2["signals"]) == 0)

    print()
    print("=" * 100)
    print("G. NO-OP — a plain ATM long call should produce nothing")
    print("=" * 100)
    deal = build_deal("cp|G", "Wave", "Long Call", [
        {"side": "Long", "opt": "C", "strike": 3800.0, "qty": 5.0, "premium_usd": -30000.0},
    ])
    r = await run([deal], 3800.0, include_rejected=True)
    check("no signals on a bought ATM call", len(r["signals"]) == 0,
          f"{len(r['signals'])} signals")

    print()
    print("=" * 100)
    print(f"{len(FAILURES)} failure(s)" + (": " + ", ".join(FAILURES) if FAILURES else ""))
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
