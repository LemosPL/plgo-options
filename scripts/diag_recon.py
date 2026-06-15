"""Smoke test for the reconciliation engine against real DB data (no network)."""
import asyncio
from collections import Counter

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.web.routes.reconciliation import run_reconciliation, ReconRequest, TheirTrade, TheirCollateral


async def main():
    db = await get_db()
    for asset in ("FIL", "ETH"):
        trades = await list_trades(db, include_expired=False, include_deleted=False, asset=asset)
        by_cp = Counter(t["counterparty"] for t in trades)
        if not by_cp:
            print(f"[{asset}] no trades"); continue
        cp = by_cp.most_common(1)[0][0]
        ours = [t for t in trades if t["counterparty"] == cp]
        print(f"\n[{asset}] counterparty='{cp}' has {len(ours)} legs")

        # Build "their" book: mirror all but the last leg (=> only_ours for the last),
        # tweak the first leg's qty (=> qty_mismatch), add a fake leg (=> only_theirs).
        their = []
        for t in ours[:-1]:
            q = float(t["qty"])
            their.append(TheirTrade(
                side=t["side"], option_type=t["option_type"], strike=float(t["strike"]),
                expiry=t["expiry"], qty=q, premium_usd=float(t["premium_usd"] or 0),
            ))
        if their:
            their[0].qty += 99999  # force a qty mismatch on the first leg
        their.append(TheirTrade(side="Buy", option_type="Call", strike=999.0,
                                expiry="2026-12-25", qty=12345, premium_usd=100))

        req = ReconRequest(
            asset=asset, counterparty=cp, their_trades=their,
            their_collateral=[TheirCollateral(asset="USD", qty=1_000_000),
                              TheirCollateral(asset=asset, qty=50_000)],
        )
        res = await run_reconciliation(req)
        s = res["summary"]
        print(f"  summary: {s}")
        # Show the non-OK rows
        for t in res["trades"]:
            if t["status"] != "match":
                print(f"    {t['status']:<12} {t['type']} {t['strike']:g} {t['expiry']} "
                      f"our={t['our_net']:,.0f} their={t['their_net']:,.0f} diff={t['qty_diff']:,.0f}"
                      + (f"  -> add {t['suggested_add']['side']} {t['suggested_add']['qty']:,.0f}" if t["suggested_add"] else ""))
        print("  collateral:")
        for c in res["collateral"]:
            print(f"    {c['asset']}: ours={c['our_qty']} theirs={c['their_qty']} diff={c['diff']} match={c['match']}")
        print("  --- report head ---")
        print("\n".join("  " + ln for ln in res["report_md"].splitlines()[:12]))


asyncio.run(main())
