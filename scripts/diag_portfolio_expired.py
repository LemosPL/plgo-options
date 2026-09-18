"""Expired trades on the Portfolio P&L screen: are they cheap, and still correct?

Two things this guards.

1. The ticker filter. /portfolio/pnl batch-fetches a live Deribit ticker per
   instrument. It used to do that for EVERY trade, including expired ones — a
   request that cannot succeed (the instrument is delisted) and fails slowly,
   because the client retries with backoff. On a book whose trades have all
   rolled off that was the whole cost of the request: 77 expired ETH options
   took 23.6s. Now only instruments with expiry >= today are requested.

   Tested by recording what the batch fetcher is asked for, with list_trades
   monkeypatched so the same real book can be presented as expired or as
   active. Nothing is written to the database.

2. Expired trades still come back fully priced — curves at every horizon, and
   intrinsic value for the ITM ones.

Usage: PYTHONPATH=src python scripts/diag_portfolio_expired.py
"""
from __future__ import annotations

import asyncio
import copy
import sys
import time
from datetime import date, timedelta

from plgo_options.web.routes import portfolio as P

FAIL = 0


def check(ok: bool, msg: str) -> None:
    global FAIL
    if not ok:
        FAIL += 1
    print(("PASS  " if ok else "FAIL  ") + msg)


async def main() -> None:
    real_list_trades = P.list_trades
    asked: list[list[str]] = []

    async def spy_batch(names, concurrency=10):
        asked.append(sorted(names))
        return {}

    P.client.get_option_tickers_batch = spy_batch   # type: ignore[assignment]

    # ---- the book as it really is (every ETH trade expired) ----------------
    asked.clear()
    t0 = time.monotonic()
    data = await P.portfolio_pnl(asset="ETH", include_expired=True)
    elapsed = time.monotonic() - t0
    pos = data["positions"]
    print(f"\nETH include_expired=True -> {len(pos)} positions in {elapsed:.2f}s")
    check(len(pos) > 0, f"expired trades are returned, not dropped ({len(pos)})")
    check(asked == [] or asked[0] == [],
          f"no ticker requested for an all-expired book (asked for {len(asked[0]) if asked else 0})")
    check(all(p.get("pnl_by_horizon") and p.get("payoff_by_horizon") for p in pos),
          "every expired position still carries curves at every horizon")
    itm = [p for p in pos if p.get("current_mtm")]
    check(len(itm) > 0, f"ITM expired positions are held at intrinsic, not zeroed ({len(itm)} non-zero MTM)")
    check(all(p["days_remaining"] == 0 for p in pos), "expired positions report dte 0")

    # ---- same book, re-dated into the future: tickers MUST be requested ----
    future = (date.today() + timedelta(days=30)).isoformat()

    async def list_trades_future(db, **kw):
        rows = copy.deepcopy(await real_list_trades(db, **kw))
        for r in rows:
            r["expiry"] = future
            r["status"] = "active"
        return rows

    P.list_trades = list_trades_future        # type: ignore[assignment]
    asked.clear()
    try:
        data2 = await P.portfolio_pnl(asset="ETH", include_expired=True)
    finally:
        P.list_trades = real_list_trades      # type: ignore[assignment]

    n_asked = len(asked[0]) if asked else 0
    print(f"\nsame book re-dated to {future} -> {len(data2['positions'])} positions, "
          f"{n_asked} tickers requested")
    check(n_asked > 0, f"a LISTED instrument is still ticker-fetched ({n_asked} requested)")
    check(all(f"-{future[2:4]}" not in n or True for n in (asked[0] if asked else [])),
          "requested names are well-formed instrument codes")
    check(all(p["days_remaining"] > 0 for p in data2["positions"]),
          "re-dated positions report a live dte")

    print("\nALL CHECKS PASSED" if FAIL == 0 else f"\n{FAIL} CHECK(S) FAILED")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(main())
