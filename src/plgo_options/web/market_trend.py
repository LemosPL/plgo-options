"""Market/trend context for the daily brief, from the two history tables.

``portfolio_mtm_history`` has carried a daily spot + MTM + greeks series for a
while, so realised vol and price trend are available immediately.
``iv_surface_history`` only starts accruing the day the snapshot endpoint first
runs, so implied-vol level is available at once but *percentiles* need a few
weeks of history — every field here reports how many observations backed it so
the brief can say "IV 62% (no history yet)" instead of inventing a percentile.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from plgo_options.data.database import get_db

# Tenor used as "the" IV level in the brief headline.
HEADLINE_TENOR = 30


async def spot_series(asset: str, days: int = 90) -> list[tuple[str, float]]:
    """(date, spot) ascending, from the daily MTM snapshots."""
    db = await get_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    cursor = await db.execute(
        """SELECT snapshot_date, spot FROM portfolio_mtm_history
           WHERE asset = ? AND spot > 0 AND snapshot_date >= ?
           ORDER BY snapshot_date ASC""",
        (asset.upper(), cutoff),
    )
    return [(r["snapshot_date"], float(r["spot"])) for r in await cursor.fetchall()]


def realised_vol(series: list[tuple[str, float]], window: int = 20) -> float | None:
    """Annualised close-to-close realised vol (%) over the last ``window`` days.

    Uses whatever it has if the series is short, but needs at least 5 returns to
    say anything at all — below that the number is noise dressed as a signal.
    """
    spots = [s for _, s in series][-(window + 1):]
    if len(spots) < 6:
        return None
    rets = [math.log(spots[i] / spots[i - 1])
            for i in range(1, len(spots)) if spots[i - 1] > 0 and spots[i] > 0]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(365.25) * 100.0


def pct_change(series: list[tuple[str, float]], days_back: int,
               latest: float | None = None,
               tolerance_days: int | None = None) -> float | None:
    """% change of the LIVE spot vs the snapshot ~``days_back`` days before today.

    Anchored on today, not on the newest snapshot. Anchoring on the snapshot is
    silently wrong whenever the daily job has gaps: with a series ending seven
    weeks ago it would compare the live spot against "one day before that last
    row" and label a seven-week move as `1d`. So the target date is derived from
    today, the closest snapshot within tolerance wins, and a series too stale to
    answer the question returns None rather than a confident wrong number.
    """
    if not series:
        return None
    now = latest if latest is not None else series[-1][1]
    if not now or now <= 0:
        return None
    target = date.today() - timedelta(days=days_back)
    tol = tolerance_days if tolerance_days is not None else max(2, days_back // 2)

    best: tuple[int, float] | None = None
    for d, s in series:
        try:
            gap = abs((date.fromisoformat(d) - target).days)
        except ValueError:
            continue
        if gap <= tol and s > 0 and (best is None or gap < best[0]):
            best = (gap, s)
    if best is None:
        return None
    return (now / best[1] - 1.0) * 100.0


def staleness_days(series: list[tuple[str, float]]) -> int | None:
    """How many days old the newest snapshot is (None = no history at all)."""
    if not series:
        return None
    try:
        return (date.today() - date.fromisoformat(series[-1][0])).days
    except ValueError:
        return None


async def iv_context(asset: str, tenor: int = HEADLINE_TENOR,
                     lookback: int = 365) -> dict:
    """ATM IV level, 1-day change, and percentile rank within available history."""
    db = await get_db()
    cutoff = (date.today() - timedelta(days=lookback)).isoformat()
    cursor = await db.execute(
        """SELECT snapshot_date, atm_iv_pct FROM iv_surface_history
           WHERE asset = ? AND tenor_days = ? AND atm_iv_pct IS NOT NULL
             AND snapshot_date >= ?
           ORDER BY snapshot_date ASC""",
        (asset.upper(), int(tenor), cutoff),
    )
    rows = [(r["snapshot_date"], float(r["atm_iv_pct"])) for r in await cursor.fetchall()]
    if not rows:
        return {"tenor_days": tenor, "level_pct": None, "change_1d": None,
                "percentile": None, "observations": 0}

    level = rows[-1][1]
    change = (level - rows[-2][1]) if len(rows) >= 2 else None
    # A percentile off a handful of points is meaningless; 20 is the floor at
    # which it starts carrying information.
    pct = None
    if len(rows) >= 20:
        below = sum(1 for _, v in rows if v <= level)
        pct = below / len(rows) * 100.0
    return {"tenor_days": tenor, "level_pct": level, "change_1d": change,
            "percentile": pct, "observations": len(rows),
            "as_of": rows[-1][0]}


async def term_structure(asset: str) -> list[dict]:
    """Today's (or latest) ATM IV by tenor — the shape, not just the level."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT snapshot_date, tenor_days, atm_iv_pct FROM iv_surface_history
           WHERE asset = ? AND snapshot_date = (
               SELECT MAX(snapshot_date) FROM iv_surface_history WHERE asset = ?)
           ORDER BY tenor_days ASC""",
        (asset.upper(), asset.upper()),
    )
    return [{"tenor_days": r["tenor_days"], "atm_iv_pct": r["atm_iv_pct"],
             "as_of": r["snapshot_date"]}
            for r in await cursor.fetchall() if r["atm_iv_pct"] is not None]


async def portfolio_snapshot(asset: str) -> dict | None:
    """Latest daily greeks/MTM row, for the one-line portfolio summary."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT snapshot_date, spot, mtm_usd, position_count, delta, gamma, theta, vega
           FROM portfolio_mtm_history WHERE asset = ?
           ORDER BY snapshot_date DESC LIMIT 1""",
        (asset.upper(),),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def last_action_spot(asset: str, deal_id: str) -> float | None:
    """Spot when this deal was last acted on — the anchor for "has it moved?".

    Without this, a signal that has already been executed keeps re-firing on
    every scan. With it, "movement since the last action" is answerable.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT outcome_spot, spot FROM signal_journal
           WHERE asset = ? AND deal_id = ? AND outcome = 'executed'
           ORDER BY COALESCE(outcome_at, fired_at) DESC LIMIT 1""",
        (asset.upper(), deal_id),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return float(row["outcome_spot"] or row["spot"] or 0.0) or None


async def build_market_trend(asset: str, live_spot: float | None = None) -> dict:
    """Everything the brief's MARKET section needs, in one call."""
    series = await spot_series(asset)
    spot = live_spot if (live_spot or 0) > 0 else (series[-1][1] if series else None)
    stale = staleness_days(series)
    # Realised vol off a gappy series is not 20-day realised vol. Suppress it
    # rather than let a stale number sit next to a live implied vol.
    rv = realised_vol(series, 20) if (stale is not None and stale <= 5) else None
    return {
        "asset": asset.upper(),
        "spot": spot,
        "change_1d_pct": pct_change(series, 1, spot),
        "change_7d_pct": pct_change(series, 7, spot),
        "change_30d_pct": pct_change(series, 30, spot),
        "realised_vol_20d_pct": rv,
        "spot_observations": len(series),
        "history_stale_days": stale,
        "latest_snapshot": series[-1][0] if series else None,
        "iv": await iv_context(asset),
        "term_structure": await term_structure(asset),
        "portfolio": await portfolio_snapshot(asset),
    }
