"""Proximity alerts: the escalate-only latch, plus the signal journal.

A naive "is spot within 10% of a trigger?" check re-fires on every poll while
spot oscillates around the boundary. So each signal gets a small state machine
that only ever escalates:

    FAR --10%--> APPROACHING --5%--> IMMINENT --cross--> LIVE

A band fires at most once. To re-arm, spot has to retreat past that band's edge
plus a hysteresis buffer (a Schmitt trigger), and a per-band cooldown applies on
top. Retreats are silent — you'd learn about them in the next morning brief
rather than getting a "never mind" ping.

Two deliberate choices:

* **Stable identity.** The engine's signal id embeds the trigger direction, which
  changes when a signal goes from "approaching from below" to live. Latching on
  that would treat an escalation as a brand-new signal, so the latch key is
  ``deal|kind|subject`` instead — stable across direction and state.
* **One message per poll**, not one per signal. Three pings arriving together
  read as noise; one message listing three escalations reads as information.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from plgo_options.config import (
    ALERT_COOLDOWN_HOURS,
    BRIEF_TIMEZONE,
    QUIET_HOURS,
)
from plgo_options.data.database import get_db
from plgo_options.web import slack
from plgo_options.web.brief_composer import KIND_VERB, _price, effect_bits
from plgo_options.web.signal_engine import SignalConfig, scan_deals

# Band ordering — alerts only ever move up this ladder.
BAND_RANK = {"approaching": 1, "imminent": 2, "live": 3}

# Extra distance spot must retreat before a fired band re-arms (percentage
# points). Without this, a price sitting on a band edge re-fires all day.
HYSTERESIS_PP = 1.5


def _key(sig: dict) -> str:
    return f"{sig['deal_id']}|{sig['kind']}|{sig['subject']}"


def _band_edge(band: str, cfg: dict) -> float:
    if band == "live":
        return 0.0
    if band == "imminent":
        return float(cfg.get("imminent_pct") or 5.0)
    return float(cfg.get("approach_pct") or 10.0)


def _parse_quiet_hours(spec: str) -> tuple[time, time] | None:
    """"22:00-07:00" -> (22:00, 07:00). "off"/"" disables."""
    spec = (spec or "").strip().lower()
    if not spec or spec in ("off", "none", "0"):
        return None
    try:
        a, b = spec.split("-")
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
        return time(ah, am), time(bh, bm)
    except Exception:
        return None


def in_quiet_hours(now: datetime | None = None) -> bool:
    window = _parse_quiet_hours(QUIET_HOURS)
    if not window:
        return False
    tz = ZoneInfo(BRIEF_TIMEZONE)
    t = (now or datetime.now(tz)).astimezone(tz).time()
    start, end = window
    if start <= end:                       # e.g. 01:00-06:00
        return start <= t < end
    return t >= start or t < end           # wraps midnight, e.g. 22:00-07:00


# ---------------------------------------------------------------------------
# Latch state
# ---------------------------------------------------------------------------


async def _load_latches(asset: str) -> dict[str, dict[str, dict]]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT signal_key, band, fired_at, spot_at_fire, trigger_spot
           FROM signal_alert_state WHERE asset = ?""", (asset.upper(),))
    out: dict[str, dict[str, dict]] = {}
    for r in await cursor.fetchall():
        out.setdefault(r["signal_key"], {})[r["band"]] = dict(r)
    return out


async def _latch(asset: str, key: str, band: str, spot: float,
                 trigger_spot: float | None) -> None:
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO signal_alert_state
           (asset, signal_key, band, fired_at, spot_at_fire, trigger_spot)
           VALUES (?, ?, ?, datetime('now'), ?, ?)""",
        (asset.upper(), key, band, spot, trigger_spot))
    await db.commit()


async def _unlatch(asset: str, key: str, bands: list[str]) -> None:
    if not bands:
        return
    db = await get_db()
    await db.execute(
        f"""DELETE FROM signal_alert_state WHERE asset = ? AND signal_key = ?
            AND band IN ({','.join('?' * len(bands))})""",
        (asset.upper(), key, *bands))
    await db.commit()


def _cooled_down(row: dict | None) -> bool:
    if not row or not row.get("fired_at"):
        return True
    try:
        fired = datetime.fromisoformat(str(row["fired_at"]).replace("Z", ""))
    except ValueError:
        return True
    return datetime.utcnow() - fired >= timedelta(hours=ALERT_COOLDOWN_HOURS)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def journal_signal(asset: str, sig: dict, band: str, channel: str,
                         spot: float, atm_iv_pct: float | None = None) -> int:
    """Record a delivered signal with the market state at delivery time."""
    import json

    db = await get_db()
    p = sig["package"]
    cursor = await db.execute(
        """INSERT INTO signal_journal
           (asset, signal_key, deal_id, counterparty, strategy, expiry, kind,
            subject, state, band, channel, spot, trigger_spot, distance_pct,
            net_cash, d_margin, d_max_loss, d_prob_profit, atm_iv_pct, package_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (asset.upper(), _key(sig), sig["deal_id"], sig["counterparty"],
         sig.get("strategy"), sig.get("expiry"), sig["kind"], sig["subject"],
         sig["state"], band, channel, spot, sig.get("trigger_spot"),
         sig.get("distance_pct"), p["net_cash"], p["d_margin"], p["d_max_loss"],
         p["d_prob_profit"], atm_iv_pct,
         json.dumps({k: v for k, v in p.items() if k != "after_payoff"})))
    await db.commit()
    return cursor.lastrowid


async def set_outcome(journal_id: int, outcome: str, spot: float | None = None,
                      note: str | None = None) -> bool:
    """Mark what was actually done — 'executed' also anchors the deal's last action."""
    if outcome not in ("open", "executed", "dismissed", "expired"):
        raise ValueError(f"unknown outcome: {outcome}")
    db = await get_db()
    cursor = await db.execute(
        """UPDATE signal_journal
           SET outcome = ?, outcome_at = datetime('now'), outcome_spot = ?,
               outcome_note = ?
           WHERE id = ?""", (outcome, spot, note, int(journal_id)))
    await db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------


def _best_per_key(signals: list[dict]) -> dict[str, dict]:
    """Collapse a signal's up/down variants to the most escalated / nearest one."""
    best: dict[str, dict] = {}
    for s in signals:
        if not s["package"]["passes_gate"]:
            continue
        k = _key(s)
        rank = BAND_RANK.get(s["state"], 0)
        cur = best.get(k)
        if cur is None:
            best[k] = s
            continue
        cur_rank = BAND_RANK.get(cur["state"], 0)
        if rank > cur_rank or (rank == cur_rank and
                               (s["distance_pct"] or 0) < (cur["distance_pct"] or 0)):
            best[k] = s
    return best


def _render_ping(fires: list[dict], stamp: str) -> str:
    lines = [f"*PLGO Options — threshold alert* · {stamp}"]
    for f in fires:
        s, asset = f["signal"], f["asset"]
        p = s["package"]
        if f["band"] == "live":
            where = f"LIVE at {_price(f['spot'], asset)}"
        else:
            signed = s["distance_pct"] * (1 if s["direction"] == "up" else -1)
            where = (f"{f['band']} — {signed:+.1f}% from "
                     f"{_price(s['trigger_spot'], asset)} "
                     f"(spot {_price(f['spot'], asset)})")
        lines += [
            "",
            f"*{KIND_VERB.get(s['kind'], s['kind'].upper())}* {asset} · "
            f"{s['counterparty']} · {s['subject']} — {where}",
            "   " + " / ".join(
                [f"close {l['qty']:g}x {l['opt']} {_price(l['strike'], asset)}"
                 for l in p["close"]]
                + [f"open {l['qty']:g}x {l['side'].lower()} {l['opt']} "
                   f"{_price(l['strike'], asset)}" for l in p["open"]]),
            "   " + " · ".join(effect_bits(p)),
            f"   {p['rationale']}",
        ]
    return "\n".join(lines)


async def proximity_check(assets: list[str], cfg: SignalConfig | None = None,
                          overrides: dict | None = None,
                          deliver: bool = True) -> dict:
    """One poll: scan, escalate/re-arm the latches, deliver anything new.

    Re-scans rather than reading a cached ladder: a scan is sub-second, so a
    cache would only add staleness. The trade-off is that levels drift slightly
    intraday as theta bleeds, which is arguably more correct than a fixed
    morning snapshot.
    """
    now = datetime.now(ZoneInfo(BRIEF_TIMEZONE))
    quiet = in_quiet_hours(now)
    fires: list[dict] = []
    deferred: list[str] = []
    rearmed = 0
    scanned = []

    for asset in assets:
        scan = await scan_deals(asset=asset, cfg=cfg, overrides=overrides)
        spot = float(scan.get("spot") or 0.0)
        conf = scan.get("config") or {}
        scanned.append({"asset": scan.get("asset", asset.upper()), "spot": spot,
                        "signals": len(scan.get("signals") or []),
                        "warning": scan.get("warning")})
        if spot <= 0:
            continue

        current = _best_per_key(scan.get("signals") or [])
        latches = await _load_latches(asset)

        # --- re-arm: drop latched bands the signal has retreated out of -----
        for key, bands in latches.items():
            sig = current.get(key)
            if sig is None:                      # signal gone entirely
                await _unlatch(asset, key, list(bands))
                rearmed += len(bands)
                continue
            dist = sig["distance_pct"]
            if dist is None:                     # still live, nothing to re-arm
                continue
            stale = [b for b in bands
                     if dist > _band_edge(b, conf) + HYSTERESIS_PP]
            if stale:
                await _unlatch(asset, key, stale)
                rearmed += len(stale)

        latches = await _load_latches(asset)     # reload post-re-arm

        # --- escalate: fire bands that are newly reached ---------------------
        for key, sig in current.items():
            band = sig["state"]
            if band not in BAND_RANK:            # "watch" — nothing to say
                continue
            fired = latches.get(key, {})
            highest = max((BAND_RANK[b] for b in fired if b in BAND_RANK), default=0)
            if BAND_RANK[band] <= highest:
                continue                         # already alerted at this level
            if not _cooled_down(fired.get(band)):
                continue
            if quiet and band == "approaching":
                # Held back rather than latched, so it can still fire once quiet
                # hours end (and it appears in the morning brief regardless).
                deferred.append(f"{asset} {sig['kind']} {sig['counterparty']}")
                continue
            await _latch(asset, key, band, spot, sig.get("trigger_spot"))
            fires.append({"asset": scan.get("asset", asset.upper()),
                          "signal": sig, "band": band, "spot": spot})

    delivery = {"delivered": False, "detail": "nothing to send"}
    if fires:
        stamp = now.strftime("%a %d %b %H:%M %Z")
        text = _render_ping(fires, stamp)
        if deliver:
            delivery = await slack.post_message(text)
        else:
            delivery = {"delivered": False, "detail": "deliver=false (preview)"}
        for f in fires:
            await journal_signal(f["asset"], f["signal"], f["band"],
                                 "proximity", f["spot"])
        return {"scanned": scanned, "fired": len(fires), "rearmed": rearmed,
                "deferred": deferred, "quiet_hours": quiet,
                "delivery": delivery, "text": text,
                "fires": [{"asset": f["asset"], "band": f["band"],
                           "kind": f["signal"]["kind"],
                           "counterparty": f["signal"]["counterparty"],
                           "subject": f["signal"]["subject"],
                           "trigger_spot": f["signal"].get("trigger_spot")}
                          for f in fires]}

    return {"scanned": scanned, "fired": 0, "rearmed": rearmed,
            "deferred": deferred, "quiet_hours": quiet,
            "delivery": delivery, "text": "", "fires": []}
