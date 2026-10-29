"""Shadow run: what the phase-1 hedge bot would do right now, on live ETH data.

One tick:
    1. the ETH book's delta (options + forwards) and the perp hedge across
       venues, the same numbers the desk agents read (agents.desk);
    2. check_rehedge against the policy's delta_band_usd / spot;
    3. if breached, rehedge_request priced off live Deribit ETH-PERPETUAL, worked
       by work_order on a PaperConnector over the same live book, with the
       real kill switch;
    4. stored as an agent run ("shadow_hedge"), so the shadow log sits next
       to the desk agents' and can be compared with the hedges traded by hand.

Nothing is sent to Deribit and nothing is booked: the paper connector is
fresh every tick, so each tick starts from the real perp position.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..optimization.delta_hedger import check_rehedge
from .algo import work_order
from .hedge import rehedge_request
from .paper import PaperConnector, QuoteSource, deribit_quotes

AGENT = "shadow_hedge"
ASSET = "ETH"
INSTRUMENT = "ETH-PERPETUAL"


async def _live_book(asset: str) -> dict:
    from plgo_options.agents.desk import get_book
    return await get_book(asset)


async def _live_perp(asset: str) -> dict:
    from plgo_options.agents.desk import get_perp
    return await get_perp(asset)


async def _band_usd(asset: str) -> float:
    from plgo_options.agents.store import get_policy
    return float((await get_policy(asset)).optimizer.delta_band_usd)


async def _kill_switch() -> bool:
    from plgo_options.agents.store import kill_switch_on
    return await kill_switch_on()


def _live_quotes() -> QuoteSource:
    from plgo_options.market_data.deribit_client import DeribitClient
    return deribit_quotes(DeribitClient())


async def shadow_tick(
        *,
        book_fn: Callable[[str], Awaitable[dict]] = _live_book,
        perp_fn: Callable[[str], Awaitable[dict]] = _live_perp,
        band_fn: Callable[[str], Awaitable[float]] = _band_usd,
        halted: Callable[[], Awaitable[bool]] = _kill_switch,
        quotes: QuoteSource | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_slippage_bps: float = 10.0,
) -> dict[str, Any]:
    """One shadow decision + simulated execution. Returns the run's data payload."""
    quotes = quotes or _live_quotes()
    now = datetime.now(timezone.utc)
    book, perp, band_usd = await book_fn(ASSET), await perp_fn(ASSET), await band_fn(ASSET)
    spot = float(book.get("spot") or 0)
    if spot <= 0:
        return {"status": "error", "detail": "no ETH spot from the book"}

    book_delta = float(book.get("delta") or 0)
    perp_qty = float(perp.get("net_qty") or 0)
    # Positions are already folded into the book's delta, so none are passed.
    decision = check_rehedge([], spot, perp_qty, band_usd / spot, extra_option_delta=book_delta)
    data: dict[str, Any] = {
        "ts": now.isoformat(timespec="seconds"), "spot": spot,
        "book_delta_eth": round(book_delta, 3), "perp_eth": round(perp_qty, 3),
        "mismatch_eth": round(decision.mismatch, 3), "band_eth": round(decision.band, 3),
        "band_usd": band_usd, "breached": decision.breached,
        "trade_eth": round(decision.trade_qty, 3),
    }
    if not decision.breached:
        return {**data, "status": "inside_band"}

    top = await quotes(INSTRUMENT)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    req = rehedge_request(decision, top, request_id=f"shadow-{stamp}",
                          proposal_id=f"shadow-{stamp}", max_slippage_bps=max_slippage_bps)
    if req is None:
        return {**data, "status": "below_min_size"}

    started = time.monotonic()
    report = await work_order(req, PaperConnector(quotes), halted=halted, sleep=sleep)
    mid = top.mid
    avg = report.avg_price
    sign = 1 if req.side == "buy" else -1
    data.update({
        "status": report.outcome, "detail": report.detail,
        "side": req.side, "qty_usd": req.qty, "limit": round(req.limit_price, 2),
        "mid_at_decision": mid, "filled_usd": report.filled_qty,
        "avg_price": round(avg, 2) if avg else None,
        # Positive = paid away against the mid when the decision was taken.
        "slippage_bps": round(sign * (avg - mid) / mid * 1e4, 2) if avg and mid else None,
        "fees": round(report.fees, 4), "children": len(report.children),
        "seconds": round(time.monotonic() - started, 1),
    })
    return data


def summarize(d: dict[str, Any]) -> str:
    head = (f"ETH {d.get('spot', 0):,.2f} · book Δ {d.get('book_delta_eth', 0):+,.1f} · "
            f"perp {d.get('perp_eth', 0):+,.1f} · mismatch {d.get('mismatch_eth', 0):+,.1f} "
            f"vs band ±{d.get('band_eth', 0):,.1f} ETH")
    status = d.get("status")
    if status == "inside_band":
        return f"{head} → inside band, no hedge"
    if status in ("error", "below_min_size"):
        return f"{head} → {status} {d.get('detail', '')}".strip()
    would = f"{head} → would {d['side']} {d['qty_usd']:,.0f} USD {INSTRUMENT}: {status}"
    if not d.get("filled_usd"):
        return f"{would}, nothing filled (limit {d['limit']}) {d.get('detail', '')}".strip()
    return (f"{would}, filled {d['filled_usd']:,.0f} @ {d['avg_price']} "
            f"({d['slippage_bps']} bps vs mid, limit {d['limit']}) in {d['seconds']}s")


async def run_and_store(**kw: Any) -> dict[str, Any]:
    """shadow_tick, stored as an agent run. A failure is stored too, never raised."""
    from plgo_options.agents import store
    run_id = await store.start_run(AGENT)
    try:
        data = await shadow_tick(**kw)
        status = "error" if data.get("status") == "error" else "ok"
    except Exception as exc:  # a shadow run must never take the loop down
        data = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        status = "error"
    text = summarize(data) if status == "ok" else f"shadow hedge failed: {data.get('detail')}"
    await store.finish_run(run_id, status, text, data, delivered=False)
    return {**data, "text": text, "run_id": run_id}
