"""The perp hedge book: ledger, net position, and funding accrual.

Why this exists
---------------
The optimizer's delta-rehedge step (``optimization/delta_hedger.py``) proposes
a perp trade on Binance Futures whenever the book's net option delta leaves its
band — but until now there was nowhere to record the fill. The ``trades`` table
holds OTC option legs reconciled against counterparty spreadsheets and
normalizes every ``option_type`` to Call/Put, so a perp could not live there.
The consequences were all live at once: ``check_rehedge`` always read an
existing perp position of zero and re-proposed the whole hedge on every run,
Portfolio greeks showed the *unhedged* book, and the funding paid to carry the
hedge appeared nowhere in P&L.

Model
-----
``perp_trades`` is a ledger of fills, never a net position. The net is
``Σ signed qty``, and funding accrual needs to know what was held at each past
funding timestamp — a question only a ledger can answer.

Average entry and realized P&L follow a signed weighted-average-cost model:
adding to a position moves the average, reducing it realizes against the
average, and flipping through zero closes the old side out first and re-opens
the new one at the fill price.

Funding sign convention
-----------------------
Longs pay shorts when the funding rate is positive, so the payment *to the
holder* is ``-qty * mark * rate``: short (qty < 0) into positive funding is
money in. This is the opposite sign to the rate itself, which is where this
gets mis-booked, so it is computed in exactly one place (``_payment_usd``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiosqlite

from plgo_options.market_data import binance_client

logger = logging.getLogger(__name__)

# The optimizer hedges on one venue (see optimization/optimizer_v3.py's
# PERP_COUNTERPARTY); the schema is per-venue so a second one can be added
# without a migration.
DEFAULT_VENUE = "Binance Futures"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def to_epoch_ms(value: str | None) -> int:
    """Parse a stored ``traded_at`` into epoch ms, treating naive input as UTC.

    Accepts the ISO forms the UI and scripts produce (``2026-09-16``,
    ``2026-09-16T14:30``, ``...Z``). An unparseable value sorts to the epoch so
    it counts as held from the beginning rather than silently dropping out of
    every funding window.
    """
    text = (value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def signed_qty(side: str, qty: float) -> float:
    """Signed token quantity: Buy/Long positive, Sell/Short negative."""
    s = (side or "").strip().lower()
    sign = -1.0 if s in ("sell", "short", "s") else 1.0
    return sign * abs(float(qty or 0.0))


def _payment_usd(position_qty: float, mark_price: float, rate: float) -> float:
    """Funding paid to (positive) or by (negative) the holder of the position.

    Longs pay shorts when ``rate`` is positive, hence the leading minus.
    """
    return -float(position_qty) * float(mark_price) * float(rate)


# ── Ledger ────────────────────────────────────────────────────────────────

async def list_trades(
    db: aiosqlite.Connection,
    asset: str | None = None,
    venue: str | None = None,
    include_closed: bool = False,
) -> list[dict]:
    """Ledger rows oldest first — the order the average-cost walk needs."""
    where = []
    params: list[object] = []
    if asset:
        where.append("asset = ? COLLATE NOCASE")
        params.append(asset)
    if venue:
        where.append("venue = ? COLLATE NOCASE")
        params.append(venue)
    if not include_closed:
        where.append("status = 'active'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    cursor = await db.execute(
        f"SELECT * FROM perp_trades {clause} ORDER BY traded_at ASC, id ASC", params
    )
    return [dict(r) for r in await cursor.fetchall()]


async def add_trade(
    db: aiosqlite.Connection,
    asset: str,
    side: str,
    qty: float,
    price: float,
    traded_at: str | None = None,
    venue: str = DEFAULT_VENUE,
    fee_usd: float = 0.0,
    source: str = "manual",
    note: str = "",
) -> int:
    asset = (asset or "").strip().upper()
    stamp = (traded_at or "").strip() or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = await db.execute(
        """INSERT INTO perp_trades
             (asset, venue, symbol, side, qty, price, fee_usd, traded_at, source, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset,
            venue,
            binance_client.symbol_for(asset),
            "Sell" if signed_qty(side, 1) < 0 else "Buy",
            abs(float(qty or 0.0)),
            float(price or 0.0),
            float(fee_usd or 0.0),
            stamp,
            source,
            note or "",
        ),
    )
    await db.commit()
    await invalidate_funding_from(db, asset, venue, to_epoch_ms(stamp))
    return int(cursor.lastrowid or 0)


async def delete_trade(db: aiosqlite.Connection, trade_id: int) -> bool:
    cursor = await db.execute(
        "SELECT asset, venue, traded_at FROM perp_trades WHERE id = ?", (int(trade_id),)
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    cursor = await db.execute("DELETE FROM perp_trades WHERE id = ?", (int(trade_id),))
    await db.commit()
    await invalidate_funding_from(db, row["asset"], row["venue"], to_epoch_ms(row["traded_at"]))
    return bool(cursor.rowcount)


async def invalidate_funding_from(
    db: aiosqlite.Connection, asset: str, venue: str, from_ms: int
) -> int:
    """Drop accrued funding at or after ``from_ms`` so it can be rebuilt.

    Every ``perp_funding`` row stores the position held when that payment
    settled. Adding a back-dated fill, or deleting one, changes what was held
    from that moment on, which makes every row at or after it wrong — and
    because the accrual walks forward from the newest stored row, none of them
    would ever be revisited. Clearing them moves the watermark back so the next
    accrual re-books the affected window against the corrected ledger.

    Rows strictly before ``from_ms`` are untouched: the position over that
    period genuinely did not change.
    """
    cursor = await db.execute(
        """DELETE FROM perp_funding
           WHERE asset = ? COLLATE NOCASE AND venue = ? COLLATE NOCASE
             AND funding_time_ms >= ?""",
        (asset, venue, int(from_ms)),
    )
    await db.commit()
    if cursor.rowcount:
        logger.info("Invalidated %d funding rows for %s/%s from %d onwards",
                    cursor.rowcount, asset, venue, from_ms)
    return int(cursor.rowcount or 0)


# ── Net position ──────────────────────────────────────────────────────────

@dataclass
class PerpPosition:
    asset: str
    venue: str
    net_qty: float = 0.0
    avg_entry: float = 0.0
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    trade_count: int = 0
    first_traded_ms: int = 0
    last_traded_at: str = ""
    legs: list[dict] = field(default_factory=list)

    @property
    def is_flat(self) -> bool:
        return abs(self.net_qty) < 1e-12

    def unrealized_pnl_usd(self, mark_price: float) -> float:
        return (float(mark_price) - self.avg_entry) * self.net_qty

    def notional_usd(self, mark_price: float) -> float:
        return abs(self.net_qty) * float(mark_price)


def summarize(trades: list[dict], asset: str = "", venue: str = DEFAULT_VENUE) -> PerpPosition:
    """Walk the ledger into a net position under signed weighted-average cost."""
    pos = PerpPosition(asset=asset, venue=venue, legs=trades)
    qty = 0.0
    avg = 0.0
    for t in trades:
        q = signed_qty(t.get("side", ""), t.get("qty", 0.0))
        px = float(t.get("price") or 0.0)
        pos.fees_usd += float(t.get("fee_usd") or 0.0)
        if qty == 0.0 or (qty > 0) == (q > 0):
            # Opening or adding — the average moves toward the new fill.
            total = qty + q
            avg = ((avg * qty) + (px * q)) / total if total != 0 else 0.0
            qty = total
        elif abs(q) <= abs(qty):
            # Reducing: realize the closed portion against the running average;
            # the average of what remains is unchanged.
            pos.realized_pnl_usd += (px - avg) * (-q)
            qty += q
            if abs(qty) < 1e-12:
                qty, avg = 0.0, 0.0
        else:
            # Flipping through zero: close the whole old side, then open the
            # remainder on the other side at this fill's price.
            pos.realized_pnl_usd += (px - avg) * qty
            qty += q
            avg = px
    pos.net_qty = qty
    pos.avg_entry = avg
    pos.trade_count = len(trades)
    if trades:
        pos.first_traded_ms = min(to_epoch_ms(t.get("traded_at")) for t in trades)
        pos.last_traded_at = max(str(t.get("traded_at") or "") for t in trades)
    return pos


def qty_at(trades: list[dict], at_ms: int) -> float:
    """Signed position held at ``at_ms`` — every fill stamped at or before it."""
    return sum(
        signed_qty(t.get("side", ""), t.get("qty", 0.0))
        for t in trades
        if to_epoch_ms(t.get("traded_at")) <= at_ms
    )


async def get_position(
    db: aiosqlite.Connection, asset: str, venue: str = DEFAULT_VENUE
) -> PerpPosition:
    trades = await list_trades(db, asset=asset, venue=venue)
    return summarize(trades, asset=(asset or "").upper(), venue=venue)


# ── Funding ───────────────────────────────────────────────────────────────

async def last_accrued_ms(
    db: aiosqlite.Connection, asset: str, venue: str = DEFAULT_VENUE
) -> int:
    cursor = await db.execute(
        """SELECT MAX(funding_time_ms) FROM perp_funding
           WHERE asset = ? COLLATE NOCASE AND venue = ? COLLATE NOCASE""",
        (asset, venue),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def accrue_funding(
    db: aiosqlite.Connection, asset: str, venue: str = DEFAULT_VENUE
) -> dict:
    """Book every funding event that has settled since the last accrual.

    Walks forward from ``max(last accrual, first fill)`` — there is no funding
    to book before the position existed — and values each event against the
    position the ledger says was held *at that moment*, not today's. Idempotent:
    the ``perp_funding`` primary key absorbs a re-run over an overlapping
    window, so this is safe to call from a scheduler on any cadence.
    """
    asset = (asset or "").strip().upper()
    trades = await list_trades(db, asset=asset, venue=venue)
    if not trades:
        return {"asset": asset, "venue": venue, "events": 0, "payment_usd": 0.0,
                "reason": "no perp trades"}

    first_ms = min(to_epoch_ms(t.get("traded_at")) for t in trades)
    watermark = await last_accrued_ms(db, asset, venue)
    start_ms = max(watermark + 1, first_ms)

    booked = 0
    total = 0.0
    cursor_ms = start_ms
    # Binance caps a page at 1000 events (~11 months at 8h). Page forward so a
    # first accrual over a long-standing hedge doesn't silently stop at 1000.
    while True:
        events = await binance_client.get_funding_history(asset, start_ms=cursor_ms, limit=1000)
        if not events:
            break
        for ev in events:
            qty = qty_at(trades, ev.funding_time_ms)
            payment = _payment_usd(qty, ev.mark_price, ev.rate)
            stamp = datetime.fromtimestamp(
                ev.funding_time_ms / 1000, tz=timezone.utc
            ).isoformat(timespec="seconds")
            await db.execute(
                """INSERT OR IGNORE INTO perp_funding
                     (asset, venue, funding_time, funding_time_ms,
                      funding_rate, mark_price, position_qty, payment_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset, venue, stamp, ev.funding_time_ms,
                 ev.rate, ev.mark_price, qty, payment),
            )
            booked += 1
            total += payment
        if len(events) < 1000:
            break
        cursor_ms = events[-1].funding_time_ms + 1
    await db.commit()
    logger.info("Accrued %d funding events for %s/%s (%.2f USD)", booked, asset, venue, total)
    return {"asset": asset, "venue": venue, "events": booked,
            "payment_usd": round(total, 2), "from_ms": start_ms}


async def funding_summary(
    db: aiosqlite.Connection, asset: str, venue: str = DEFAULT_VENUE
) -> dict:
    """Settled funding to date, plus the last 30 days, for the perp panel."""
    cursor = await db.execute(
        """SELECT COALESCE(SUM(payment_usd), 0), COUNT(*), MAX(funding_time)
           FROM perp_funding
           WHERE asset = ? COLLATE NOCASE AND venue = ? COLLATE NOCASE
             AND position_qty <> 0""",
        (asset, venue),
    )
    row = await cursor.fetchone()
    total, count, last_at = (row[0] or 0.0), int(row[1] or 0), (row[2] or "")

    cutoff = datetime.fromtimestamp((_now_ms() - 30 * binance_client.DAY_MS) / 1000,
                                    tz=timezone.utc).isoformat(timespec="seconds")
    cursor = await db.execute(
        """SELECT COALESCE(SUM(payment_usd), 0) FROM perp_funding
           WHERE asset = ? COLLATE NOCASE AND venue = ? COLLATE NOCASE
             AND funding_time >= ?""",
        (asset, venue, cutoff),
    )
    row = await cursor.fetchone()
    return {
        "total_usd": round(float(total), 2),
        "last_30d_usd": round(float(row[0] or 0.0), 2),
        "events": count,
        "last_funding_at": last_at,
    }


async def funding_rows(
    db: aiosqlite.Connection, asset: str, venue: str = DEFAULT_VENUE, limit: int = 60
) -> list[dict]:
    """Most recent settled funding payments, newest first (paid ones only)."""
    cursor = await db.execute(
        """SELECT funding_time, funding_rate, mark_price, position_qty, payment_usd
           FROM perp_funding
           WHERE asset = ? COLLATE NOCASE AND venue = ? COLLATE NOCASE
             AND position_qty <> 0
           ORDER BY funding_time_ms DESC LIMIT ?""",
        (asset, venue, int(limit)),
    )
    return [dict(r) for r in await cursor.fetchall()]
