#!/usr/bin/env python3
"""Perp hedge diagnostics — ledger math, funding signs, and accrual invalidation.

Runs without a live market feed or a populated DB: the ledger walk and the
funding arithmetic are pure functions, and the DB half uses a throwaway asset
("TSTPERP") in a temp file, so this works anywhere.

Two things it is specifically here to catch:

* **Funding booked with the wrong sign.** Longs pay shorts when the rate is
  positive, so the payment to the holder is the negative of `qty*mark*rate`.
  Getting this backwards silently turns a cost into income of exactly the same
  magnitude, which no total would flag.
* **Stale accrual after a back-dated fill.** The accrual walks forward from the
  newest stored row, so a fill inserted behind that watermark would never be
  re-priced unless the affected rows are cleared first.

    .venv/bin/python scripts/diag_perp_hedge.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiosqlite  # noqa: E402

from plgo_options.data import perp_repository as repo  # noqa: E402
from plgo_options.data.database import (  # noqa: E402
    PERP_FUNDING_SCHEMA,
    PERP_TRADES_SCHEMA,
)

ASSET = "TSTPERP"
VENUE = "Binance Futures"


def fill(side: str, qty: float, price: float, when: str) -> dict:
    return {"side": side, "qty": qty, "price": price, "traded_at": when, "fee_usd": 0.0}


def check(label: str, got, want, tol: float = 1e-6) -> None:
    ok = abs(float(got) - float(want)) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got:,.6f}, want {want:,.6f}")
    if not ok:
        raise AssertionError(label)


def test_ledger_walk() -> None:
    print("\nLedger — signed weighted-average cost")

    p = repo.summarize([fill("Sell", 100, 1.00, "2026-01-01"),
                        fill("Sell", 100, 0.90, "2026-02-01")])
    check("adding to a short moves the average", p.avg_entry, 0.95)
    check("  net qty", p.net_qty, -200)
    check("  nothing realized yet", p.realized_pnl_usd, 0)

    p = repo.summarize([fill("Sell", 100, 1.00, "2026-01-01"),
                        fill("Buy", 100, 0.80, "2026-02-01")])
    check("covering a short at a lower price profits", p.realized_pnl_usd, 20)
    check("  flat afterwards", p.net_qty, 0)
    check("  average resets", p.avg_entry, 0)

    # Flipping through zero must not carry the old side's average into the new
    # one — that would misstate every subsequent unrealized figure.
    p = repo.summarize([fill("Sell", 100, 1.00, "2026-01-01"),
                        fill("Buy", 300, 0.80, "2026-02-01")])
    check("flip realizes only the closed portion", p.realized_pnl_usd, 20)
    check("  remainder is long", p.net_qty, 200)
    check("  re-opened at the fill price", p.avg_entry, 0.80)

    # Unrealized P&L is the other half of the same identity: a position closed
    # at price X must realize exactly what it was showing unrealized at X.
    p = repo.summarize([fill("Sell", 100, 1.00, "2026-01-01")])
    check("unrealized at 0.80 equals the realized above", p.unrealized_pnl_usd(0.80), 20)


def test_qty_at() -> None:
    print("\nPosition as of a past timestamp")
    trades = [fill("Sell", 100, 1.0, "2026-01-01"),
              fill("Sell", 50, 1.0, "2026-03-01")]
    check("before the second fill", repo.qty_at(trades, repo.to_epoch_ms("2026-02-01")), -100)
    check("after it", repo.qty_at(trades, repo.to_epoch_ms("2026-04-01")), -150)
    # Inclusive at the boundary: a fill stamped exactly at a funding settlement
    # was held when it settled.
    check("at the exact fill time", repo.qty_at(trades, repo.to_epoch_ms("2026-03-01")), -150)


def test_funding_signs() -> None:
    print("\nFunding sign convention (longs pay shorts on a positive rate)")
    check("short + positive rate = income", repo._payment_usd(-100, 1.0, 0.0001), 0.01)
    check("long + positive rate = cost", repo._payment_usd(100, 1.0, 0.0001), -0.01)
    check("short + negative rate = cost", repo._payment_usd(-100, 1.0, -0.0001), -0.01)
    check("flat pays nothing", repo._payment_usd(0, 1.0, 0.0001), 0.0)


async def test_invalidation() -> None:
    print("\nAccrual invalidation after a back-dated fill")
    with tempfile.TemporaryDirectory() as tmp:
        async with aiosqlite.connect(f"{tmp}/diag.db") as db:
            db.row_factory = aiosqlite.Row
            await db.execute(PERP_TRADES_SCHEMA)
            await db.execute(PERP_FUNDING_SCHEMA)

            # A hedge opened on the 10th, with funding booked through the 20th.
            await repo.add_trade(db, ASSET, "Sell", 1000, 1.0,
                                 traded_at="2026-01-10T00:00:00Z", venue=VENUE)
            for day in (12, 16, 20):
                ms = repo.to_epoch_ms(f"2026-01-{day}T00:00:00Z")
                await db.execute(
                    """INSERT INTO perp_funding (asset, venue, funding_time,
                         funding_time_ms, funding_rate, mark_price, position_qty, payment_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ASSET, VENUE, f"2026-01-{day}T00:00:00+00:00", ms,
                     0.0001, 1.0, -1000.0, 0.1),
                )
            await db.commit()
            check("watermark before", await repo.last_accrued_ms(db, ASSET, VENUE),
                  repo.to_epoch_ms("2026-01-20T00:00:00Z"))

            # A second fill surfaces late, dated the 15th — behind the watermark.
            # The 16th and 20th were priced on the wrong position; the 12th was not.
            await repo.add_trade(db, ASSET, "Sell", 500, 1.0,
                                 traded_at="2026-01-15T00:00:00Z", venue=VENUE)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM perp_funding WHERE asset = ?", (ASSET,))
            remaining = (await cursor.fetchone())[0]
            check("rows at/after the back-dated fill are cleared", remaining, 1)
            check("watermark rolled back so they get re-booked",
                  await repo.last_accrued_ms(db, ASSET, VENUE),
                  repo.to_epoch_ms("2026-01-12T00:00:00Z"))

            trades = await repo.list_trades(db, asset=ASSET, venue=VENUE)
            check("position on the 16th now reflects both fills",
                  repo.qty_at(trades, repo.to_epoch_ms("2026-01-16T00:00:00Z")), -1500)

            # Deleting the late fill must restore the earlier state, not compound
            # the invalidation into the untouched window.
            late = [t for t in trades if t["traded_at"].startswith("2026-01-15")][0]
            await repo.delete_trade(db, late["id"])
            cursor = await db.execute(
                "SELECT COUNT(*) FROM perp_funding WHERE asset = ?", (ASSET,))
            check("the pre-fill row survives the delete too",
                  (await cursor.fetchone())[0], 1)


def main() -> int:
    try:
        test_ledger_walk()
        test_qty_at()
        test_funding_signs()
        asyncio.run(test_invalidation())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1
    print("\nAll perp hedge diagnostics passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
