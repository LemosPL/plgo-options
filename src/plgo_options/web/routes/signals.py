"""Deal action signals API — the trigger map, and the vol-history log it needs.

``POST /api/signals/deals`` is the phase-1 surface: it returns, per deal, the
actions worth taking and the spot levels at which they switch on. The same
payload is what the 09:00 brief and the proximity alerts will render later, so
the numbers a Slack message quotes are provably the numbers on the page.

``POST /api/signals/snapshot-iv`` exists to start accruing history *now*. The DB
already keeps a daily spot + greeks series (``portfolio_mtm_history``) but has
never stored implied vol, so vol-regime awareness has no past to look at. One
row per (date, asset, tenor) costs nothing and makes IV percentiles possible in
a few weeks.
"""

from __future__ import annotations

from dataclasses import fields as dc_fields
from datetime import date, timedelta

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from plgo_options.config import BRIEF_TIMEZONE, QUIET_HOURS, SIGNALS_TOKEN
from plgo_options.data.database import get_db
from plgo_options.web import alerting, slack
from plgo_options.web.brief_composer import compose_brief
from plgo_options.web.market_trend import build_market_trend
from plgo_options.web.signal_engine import KINDS, SignalConfig, scan_deals
from plgo_options.web.routes.portfolio import _iv_from_surface, build_market_context

router = APIRouter()

DEFAULT_ASSETS = ["ETH", "FIL"]


def _require_token(token: str | None) -> None:
    """Guard the scheduler-driven endpoints.

    Cloud Run URLs are public by default, so the daily brief, the poller and the
    IV snapshot need a shared secret — otherwise anyone who finds the URL can
    make the desk's Slack channel say whatever a scan happens to return. An
    unset SIGNALS_TOKEN disables the check so local dev is frictionless.
    """
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")

# Tenors for the daily ATM IV term-structure snapshot.
SNAPSHOT_TENORS = (7, 14, 30, 60, 90, 180)

_CONFIG_FIELDS = {f.name: f.type for f in dc_fields(SignalConfig)}


def _config_from(raw: dict | None) -> SignalConfig:
    """Build a config from a partial dict, ignoring unknown keys."""
    cfg = SignalConfig()
    for key, val in (raw or {}).items():
        if key not in _CONFIG_FIELDS or val is None:
            continue
        try:
            setattr(cfg, key, int(val) if _CONFIG_FIELDS[key] is int else float(val))
        except (TypeError, ValueError):
            continue
    return cfg


class SignalsRequest(BaseModel):
    asset: str = "ETH"
    include_expired: bool = False
    # Same manual-grouping overrides the Deals page sends, so the signals are
    # computed against the deal boundaries the user actually sees.
    overrides: dict[str, dict[str, str]] | None = None
    config: dict | None = None
    kinds: list[str] | None = None
    # Experimentation aid: also return packages that FAILED the economic gate,
    # with the reason, so thresholds can be tuned against real rejections.
    include_rejected: bool = False


@router.get("/config")
async def get_config():
    """Default thresholds — the UI seeds its tuning panel from this."""
    cfg = SignalConfig()
    return {"config": {f.name: getattr(cfg, f.name) for f in dc_fields(cfg)},
            "kinds": list(KINDS)}


@router.post("/deals")
async def signals_for_deals(req: SignalsRequest):
    """Scan all deals for actions + the spot levels that trigger them."""
    kinds = [k for k in (req.kinds or KINDS) if k in KINDS] or list(KINDS)
    try:
        return await scan_deals(
            asset=req.asset,
            include_expired=req.include_expired,
            overrides=req.overrides,
            cfg=_config_from(req.config),
            kinds=kinds,
            include_rejected=req.include_rejected,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signal scan failed: {e}")


# ---------------------------------------------------------------------------
# Vol history log
# ---------------------------------------------------------------------------


class SnapshotRequest(BaseModel):
    assets: list[str] | None = None      # default: both books


@router.post("/snapshot-iv")
async def snapshot_iv(req: SnapshotRequest | None = None,
                      x_signals_token: str | None = Header(default=None)):
    """Record today's ATM IV term structure for each asset (idempotent).

    Safe to call repeatedly — the primary key is (snapshot_date, asset, tenor),
    so re-running on the same day overwrites that day's row rather than
    accumulating duplicates. Intended to be driven by Cloud Scheduler.
    """
    _require_token(x_signals_token)
    assets = [a.upper() for a in ((req.assets if req else None) or DEFAULT_ASSETS)]
    today = date.today()
    db = await get_db()
    written = []

    for asset in assets:
        try:
            ctx = await build_market_context(asset)
        except Exception as e:
            written.append({"asset": asset, "rows": 0, "error": str(e)})
            continue
        spot = float(ctx.get("spot") or 0.0)
        smiles = ctx.get("smiles") or {}
        dates = ctx.get("deribit_dates") or {}
        if spot <= 0 or not smiles:
            written.append({"asset": asset, "rows": 0,
                            "error": "no spot or empty vol surface"})
            continue

        rows = 0
        for tenor in SNAPSHOT_TENORS:
            iv = _iv_from_surface((today + timedelta(days=tenor)).isoformat(),
                                  spot, smiles, dates, today)
            if iv is None:
                continue
            await db.execute(
                """INSERT OR REPLACE INTO iv_surface_history
                   (snapshot_date, asset, tenor_days, spot, atm_iv_pct)
                   VALUES (?, ?, ?, ?, ?)""",
                (today.isoformat(), asset, tenor, spot, float(iv)),
            )
            rows += 1
        await db.commit()
        written.append({"asset": asset, "rows": rows, "spot": spot, "error": None})

    return {"snapshot_date": today.isoformat(), "results": written}


@router.get("/iv-history")
async def iv_history(asset: str = "ETH", tenor_days: int | None = None,
                     limit: int = 400):
    """Read back the ATM IV log (newest first)."""
    db = await get_db()
    sql = ("SELECT snapshot_date, asset, tenor_days, spot, atm_iv_pct "
           "FROM iv_surface_history WHERE asset = ?")
    params: list = [asset.upper()]
    if tenor_days:
        sql += " AND tenor_days = ?"
        params.append(int(tenor_days))
    sql += " ORDER BY snapshot_date DESC, tenor_days ASC LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))

    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return {"asset": asset.upper(), "rows": [dict(r) for r in rows]}


@router.get("/market-trend")
async def market_trend(asset: str = "ETH"):
    """Spot/vol trend context — what the brief's MARKET section is built from."""
    return await build_market_trend(asset)


# ---------------------------------------------------------------------------
# Daily brief
# ---------------------------------------------------------------------------


class BriefRequest(BaseModel):
    assets: list[str] | None = None
    use_ai: bool = True
    # False = build and return the text without posting to Slack (UI preview).
    deliver: bool = False
    config: dict | None = None
    overrides: dict[str, dict[str, str]] | None = None


@router.post("/brief")
async def brief(req: BriefRequest | None = None,
                x_signals_token: str | None = Header(default=None)):
    """Build the 09:00 brief; optionally post it to Slack.

    Preview from the UI with ``deliver: false``. Cloud Scheduler calls it with
    ``deliver: true`` at 09:00 Europe/London.
    """
    req = req or BriefRequest()
    if req.deliver:
        _require_token(x_signals_token)          # only delivery needs the secret

    assets = [a.upper() for a in (req.assets or DEFAULT_ASSETS)]
    try:
        result = await compose_brief(
            assets, use_ai=req.use_ai,
            cfg=_config_from(req.config), overrides=req.overrides)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Brief generation failed: {e}")

    delivery = {"delivered": False, "detail": "deliver=false (preview)"}
    journaled = 0
    if req.deliver:
        delivery = await slack.post_message(result["text"])
        # Journal what was sent even if Slack rejected it — the record of what
        # the engine recommended is the point, not whether the pipe worked.
        for book in result["data"]["books"]:
            for sig in book["live"]:
                await alerting.journal_signal(book["asset"], sig, "live", "brief",
                                              book["spot"] or 0.0)
                journaled += 1

    return {
        "text": result["text"],
        "narrative": result["narrative"],
        "delivery": delivery,
        "journaled": journaled,
        "slack_configured": slack.is_configured(),
        "generated_at": result["data"]["generated_at"],
        "books": [{
            "asset": b["asset"], "spot": b["spot"], "counts": b["counts"],
            "deals_scanned": b["deals_scanned"], "warning": b["warning"],
            "live": len(b["live"]), "pending": len(b["pending"]),
        } for b in result["data"]["books"]],
    }


# ---------------------------------------------------------------------------
# Proximity poller
# ---------------------------------------------------------------------------


class ProximityRequest(BaseModel):
    assets: list[str] | None = None
    deliver: bool = True
    config: dict | None = None
    overrides: dict[str, dict[str, str]] | None = None


@router.post("/proximity-check")
async def proximity_check(req: ProximityRequest | None = None,
                          x_signals_token: str | None = Header(default=None)):
    """One poll of the proximity latches. Driven by Cloud Scheduler (~5 min)."""
    req = req or ProximityRequest()
    if req.deliver:
        _require_token(x_signals_token)
    assets = [a.upper() for a in (req.assets or DEFAULT_ASSETS)]
    try:
        return await alerting.proximity_check(
            assets, cfg=_config_from(req.config),
            overrides=req.overrides, deliver=req.deliver)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proximity check failed: {e}")


@router.get("/alert-state")
async def alert_state(asset: str = "ETH"):
    """Current latch rows — what has already alerted and won't re-fire."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT signal_key, band, fired_at, spot_at_fire, trigger_spot
           FROM signal_alert_state WHERE asset = ?
           ORDER BY fired_at DESC""", (asset.upper(),))
    return {"asset": asset.upper(),
            "quiet_hours": QUIET_HOURS,
            "in_quiet_hours": alerting.in_quiet_hours(),
            "timezone": BRIEF_TIMEZONE,
            "rows": [dict(r) for r in await cursor.fetchall()]}


@router.delete("/alert-state")
async def clear_alert_state(asset: str = "ETH", signal_key: str | None = None):
    """Re-arm latches by hand (only touches signal_alert_state, never trades)."""
    db = await get_db()
    if signal_key:
        cursor = await db.execute(
            "DELETE FROM signal_alert_state WHERE asset = ? AND signal_key = ?",
            (asset.upper(), signal_key))
    else:
        cursor = await db.execute(
            "DELETE FROM signal_alert_state WHERE asset = ?", (asset.upper(),))
    await db.commit()
    return {"asset": asset.upper(), "cleared": cursor.rowcount}


# ---------------------------------------------------------------------------
# Signal journal
# ---------------------------------------------------------------------------


class OutcomeRequest(BaseModel):
    outcome: str                     # executed | dismissed | expired | open
    spot: float | None = None
    note: str | None = None


@router.get("/journal")
async def journal(asset: str | None = None, outcome: str | None = None,
                  deal_id: str | None = None, limit: int = 200):
    """Every delivered signal, newest first — the record thresholds get tuned on."""
    db = await get_db()
    sql = ("SELECT id, fired_at, asset, signal_key, deal_id, counterparty, strategy, "
           "expiry, kind, subject, state, band, channel, spot, trigger_spot, "
           "distance_pct, net_cash, d_margin, d_max_loss, d_prob_profit, "
           "outcome, outcome_at, outcome_spot, outcome_note "
           "FROM signal_journal WHERE 1=1")
    params: list = []
    if asset:
        sql += " AND asset = ?"
        params.append(asset.upper())
    if outcome:
        sql += " AND outcome = ?"
        params.append(outcome)
    if deal_id:
        sql += " AND deal_id = ?"
        params.append(deal_id)
    sql += " ORDER BY fired_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 2000)))

    cursor = await db.execute(sql, params)
    rows = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT outcome, COUNT(*) c FROM signal_journal GROUP BY outcome")
    return {"rows": rows,
            "totals": {r["outcome"]: r["c"] for r in await cursor.fetchall()}}


@router.post("/journal/{journal_id}/outcome")
async def journal_outcome(journal_id: int, req: OutcomeRequest):
    """Record what was actually done. 'executed' anchors the deal's last action."""
    try:
        ok = await alerting.set_outcome(journal_id, req.outcome, req.spot, req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"No journal entry {journal_id}")
    return {"id": journal_id, "outcome": req.outcome}
