"""Diagnostic for the brief composer + proximity latch (phases 2-4).

Drives the pipeline against synthetic deals under a throwaway asset code
(``TSTETH``) so nothing touches a real book, then verifies the part that is
easy to get wrong: that a latched band does NOT re-fire on the next poll, and
that it re-arms only after spot retreats past the band edge plus hysteresis.

Only ever writes to ``signal_alert_state`` / ``signal_journal`` (both new
tables) under TSTETH, and deletes exactly those rows at the end. The trades
table is never touched.

Run:  .venv/Scripts/python.exe scripts/diag_brief.py
"""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import date, datetime, time, timedelta

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import plgo_options.web.signal_engine as se            # noqa: E402
from plgo_options.data.database import get_db, init_db  # noqa: E402
from plgo_options.web import alerting                   # noqa: E402
from plgo_options.web.brief_composer import (           # noqa: E402
    build_brief_data, render_brief,
)

TEST_ASSET = "TSTETH"
GRID = np.linspace(500.0, 12000.0, 161)
FAILURES: list[str] = []
_spot = 3800.0


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"   {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make_payload(spot: float) -> tuple[dict, dict]:
    """One recycle-able short put and one deep-ITM call spread."""
    expiry = (date.today() + timedelta(days=60)).isoformat()

    def leg(i, side, opt, strike, qty, prem):
        sign = 1.0 if side == "Long" else -1.0
        intr = (np.maximum(GRID - strike, 0.0) if opt == "C"
                else np.maximum(strike - GRID, 0.0))
        return {"id": i, "side": side, "opt": opt, "qty": qty, "strike": strike,
                "expiry": expiry, "premium_usd": prem,
                "payoff": [float(x) for x in (sign * qty * intr + prem)]}

    def deal(did, cpty, strategy, legs):
        total = np.zeros_like(GRID)
        for l in legs:
            total = total + np.asarray(l["payoff"])
        return {"id": did, "counterparty": cpty, "strategy": strategy,
                "group_id": did, "leg_ids": [l["id"] for l in legs],
                "expiry": expiry, "days_to_expiry": 60, "n_legs": len(legs),
                "net_credit": sum(l["premium_usd"] for l in legs), "legs": legs,
                "payoff": [float(x) for x in total],
                "max_profit": float(np.max(total)), "max_loss": float(np.min(total))}

    deals = [
        deal("Wave|A", "Wave", "Short Put",
             [leg(1, "Short", "P", 2000.0, 10.0, 15000.0)]),
        deal("Flowdesk|B", "Flowdesk", "Call Spread",
             [leg(2, "Short", "C", 3000.0, 10.0, 40000.0),
              leg(3, "Long", "C", 3600.0, 10.0, -18000.0)]),
    ]
    payload = {"asset": TEST_ASSET, "spot": spot, "deals": deals,
               "grid": [float(x) for x in GRID]}
    return payload, {"smiles": {}, "deribit_dates": {}, "spot": spot}


async def _stub(asset, include_expired, overrides=None):
    return make_payload(_spot)


async def cleanup() -> None:
    db = await get_db()
    await db.execute("DELETE FROM signal_alert_state WHERE asset = ?", (TEST_ASSET,))
    await db.execute("DELETE FROM signal_journal WHERE asset = ?", (TEST_ASSET,))
    await db.commit()


async def main() -> None:
    global _spot
    await init_db()
    se.build_deals_payload = _stub                  # synthetic book everywhere
    await cleanup()

    print("=" * 100)
    print("1. QUIET HOURS parsing")
    print("=" * 100)
    from plgo_options.web.alerting import _parse_quiet_hours, in_quiet_hours
    check("22:00-07:00 parses", _parse_quiet_hours("22:00-07:00") == (time(22), time(7)))
    check("'off' disables", _parse_quiet_hours("off") is None)
    check("empty disables", _parse_quiet_hours("") is None)
    check("garbage disables (fails open, never blocks alerts)",
          _parse_quiet_hours("later") is None)
    # Wrap-around window: 23:00 is inside 22:00-07:00, 12:00 is not.
    import plgo_options.web.alerting as al
    orig = al.QUIET_HOURS
    al.QUIET_HOURS = "22:00-07:00"
    tz = __import__("zoneinfo").ZoneInfo(al.BRIEF_TIMEZONE)
    check("23:00 is quiet", in_quiet_hours(datetime(2026, 8, 31, 23, 0, tzinfo=tz)))
    check("03:00 is quiet", in_quiet_hours(datetime(2026, 8, 31, 3, 0, tzinfo=tz)))
    check("12:00 is not quiet", not in_quiet_hours(datetime(2026, 8, 31, 12, 0, tzinfo=tz)))
    al.QUIET_HOURS = "off"                          # keep the rest deterministic

    print()
    print("=" * 100)
    print("2. BRIEF renders with sparse history (no IV log, no MTM rows for TSTETH)")
    print("=" * 100)
    data = await build_brief_data([TEST_ASSET])
    text = render_brief(data, None)
    print("\n".join("      " + l for l in text.splitlines()))
    check("brief renders", bool(text) and "morning brief" in text)
    check("no crash on missing IV history", "IV history not started yet" in text)
    check("DO TODAY section present", "*DO TODAY" in text)
    check("IF PRICE MOVES section present", "*IF PRICE MOVES*" in text)
    check("threshold footer present", "recycle <=" in text)
    check("no naked None leaked into the text", "None" not in text,
          [l for l in text.splitlines() if "None" in l][:1])

    print()
    print("=" * 100)
    print("3. PROXIMITY LATCH — first poll fires, second poll is silent")
    print("=" * 100)
    r1 = await alerting.proximity_check([TEST_ASSET], deliver=False)
    print(f"      poll 1: fired={r1['fired']} rearmed={r1['rearmed']} "
          f"bands={[f['band'] for f in r1['fires']]}")
    check("first poll fires at least one band", r1["fired"] >= 1)

    r2 = await alerting.proximity_check([TEST_ASSET], deliver=False)
    print(f"      poll 2: fired={r2['fired']} rearmed={r2['rearmed']}")
    check("second poll fires nothing (latched)", r2["fired"] == 0)

    r3 = await alerting.proximity_check([TEST_ASSET], deliver=False)
    check("third poll still silent", r3["fired"] == 0)

    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) c FROM signal_journal WHERE asset = ?", (TEST_ASSET,))
    n_journal = (await cur.fetchone())["c"]
    check("every fire was journalled", n_journal == r1["fired"],
          f"{n_journal} rows vs {r1['fired']} fires")

    print()
    print("=" * 100)
    print("4. ESCALATION — one signal must climb approaching -> imminent -> live")
    print("   Tracks the Flowdesk tighten key specifically. Its 3x efficiency gate")
    print("   rejects it at defaults, so the gate is relaxed to 1x here — otherwise")
    print("   the signal never reaches the alerter and this would silently test")
    print("   nothing (which is exactly what an earlier version of it did).")
    print("=" * 100)
    await cleanup()
    cfg = se.SignalConfig(tighten_min_efficiency=1.0)
    TIGHTEN_KEY = "Flowdesk|B|tighten|C 3000/3600"

    async def poll(spot: float):
        global _spot
        _spot = spot
        r = await alerting.proximity_check([TEST_ASSET], cfg=cfg, deliver=False)
        got = {f"{f['kind']}|{f['counterparty']}": f["band"] for f in r["fires"]}
        print(f"      spot {spot:>7,.0f}: fired={r['fired']} rearmed={r['rearmed']} {got}")
        return r

    async def latched_bands(key: str) -> set[str]:
        cur = await db.execute(
            "SELECT band FROM signal_alert_state WHERE asset = ? AND signal_key = ?",
            (TEST_ASSET, key))
        return {r["band"] for r in await cur.fetchall()}

    # Trigger sits at ~4,237 (both strikes 15% ITM). Walk spot in from below.
    r_app = await poll(3900.0)      # ~8.6% away  -> approaching
    check("approaching band fires for the tighten signal",
          any(f["band"] == "approaching" and f["kind"] == "tighten"
              for f in r_app["fires"]),
          str([(f["kind"], f["band"]) for f in r_app["fires"]]))
    check("latch records 'approaching'", "approaching" in await latched_bands(TIGHTEN_KEY),
          str(await latched_bands(TIGHTEN_KEY)))

    r_imm = await poll(4080.0)      # ~3.8% away  -> imminent
    check("imminent band escalates same key",
          any(f["band"] == "imminent" and f["kind"] == "tighten"
              for f in r_imm["fires"]))
    bands = await latched_bands(TIGHTEN_KEY)
    check("both bands now latched", {"approaching", "imminent"} <= bands, str(bands))

    r_same = await poll(4080.0)
    check("no re-fire at the same level", r_same["fired"] == 0)

    r_live = await poll(4300.0)     # past the trigger -> live
    check("live band escalates same key",
          any(f["band"] == "live" and f["kind"] == "tighten" for f in r_live["fires"]))
    bands = await latched_bands(TIGHTEN_KEY)
    check("all three bands latched", {"approaching", "imminent", "live"} <= bands,
          str(bands))

    r_deeper = await poll(4600.0)   # further past — nothing left to escalate to
    check("no alert once fully escalated", r_deeper["fired"] == 0)

    print()
    print("=" * 100)
    print("5. RE-ARM — retreating past the band edge + hysteresis clears the latch")
    print("=" * 100)
    r_back = await poll(3600.0)     # ~17% away: past 10% + 1.5pp hysteresis
    check("retreat re-arms every band", r_back["rearmed"] >= 3,
          f"rearmed {r_back['rearmed']}")
    check("latch is empty again", not await latched_bands(TIGHTEN_KEY),
          str(await latched_bands(TIGHTEN_KEY)))

    r_again = await poll(4080.0)
    check("re-armed signal can alert again", r_again["fired"] >= 1)

    print()
    print("=" * 100)
    print("6. HYSTERESIS — wobbling just outside a band edge must NOT re-fire")
    print("   Trigger ~4,237, imminent edge 5% => 4,025. Wobble across it.")
    print("=" * 100)
    fired_total = 0
    for wobble in (4010.0, 4030.0, 4005.0, 4035.0, 4020.0):
        fired_total += (await poll(wobble))["fired"]
    print(f"      5 polls straddling the imminent edge: extra fires = {fired_total}")
    check("straddling a band edge produces no new alerts", fired_total == 0,
          f"{fired_total} spurious alerts")

    print()
    print("=" * 100)
    print("7. OUTCOME — marking executed anchors the deal's last-action spot")
    print("=" * 100)
    cur = await db.execute(
        "SELECT id, deal_id FROM signal_journal WHERE asset = ? ORDER BY id LIMIT 1",
        (TEST_ASSET,))
    row = await cur.fetchone()
    ok = await alerting.set_outcome(row["id"], "executed", spot=4180.0, note="diag")
    check("outcome recorded", ok)
    from plgo_options.web.market_trend import last_action_spot
    anchor = await last_action_spot(TEST_ASSET, row["deal_id"])
    check("last-action spot readable", anchor == 4180.0, str(anchor))
    try:
        await alerting.set_outcome(row["id"], "nonsense")
        check("bad outcome rejected", False)
    except ValueError:
        check("bad outcome rejected", True)

    await cleanup()
    cur = await db.execute(
        "SELECT (SELECT COUNT(*) FROM signal_journal WHERE asset=?) j, "
        "(SELECT COUNT(*) FROM signal_alert_state WHERE asset=?) s",
        (TEST_ASSET, TEST_ASSET))
    left = await cur.fetchone()
    check("test rows cleaned up", left["j"] == 0 and left["s"] == 0)
    al.QUIET_HOURS = orig

    print()
    print("=" * 100)
    print(f"{len(FAILURES)} failure(s)" + (": " + ", ".join(FAILURES) if FAILURES else ""))
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
