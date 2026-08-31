"""The 09:00 brief: deterministic numbers, Claude for the narrative.

Hard split, deliberately:

  * **Rules compute.** Every price, cash figure, trigger level, margin number
    and threshold in the brief comes from ``signal_engine`` and the two history
    tables. The model never touches them.
  * **Claude narrates and ranks.** It gets the finished numbers and writes the
    market read, picks the one action worth doing today, and may re-order the
    list. If the API call fails, is unconfigured, or returns something
    unparseable, the brief still goes out with the deterministic ordering — the
    desk gets its numbers even when the model is unavailable.

The most useful part isn't "act now", it's the IF PRICE MOVES ladder: a
conditional playbook, so when spot moves intraday the decision is already made.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from plgo_options.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_BRIEF_MODEL,
    BRIEF_TIMEZONE,
)
from plgo_options.web.market_trend import build_market_trend
from plgo_options.web.signal_engine import SignalConfig, scan_deals

# Urgency ordering when the model isn't available: protect the book, then take
# free money, then free up collateral, then add risk.
KIND_URGENCY = {"reduce": 0, "recycle": 1, "tighten": 2, "increase": 3}

KIND_VERB = {
    "recycle": "RECYCLE", "tighten": "TIGHTEN",
    "increase": "INCREASE", "reduce": "REDUCE",
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _money(v: float | None, digits: int = 0) -> str:
    """One decimal on the k scale — "$1.4k" vs "$1k" is a real difference here."""
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e6:
        return f"{sign}${a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:.1f}k"
    return f"{sign}${a:,.{digits}f}"


def _signed_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return ("+" if v >= 0 else "-") + _money(abs(v))


def _pct(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{digits}f}%"


def _price(v: float | None, asset: str) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.2f}" if asset.upper() == "FIL" else f"{v:,.0f}"


def effect_bits(p: dict) -> list[str]:
    """The effect of a package in words, with the direction made explicit.

    "collateral -$2.5k" is ambiguous — it reads as if collateral went down when
    it actually means the trade consumes more. And tail risk is reported from
    ``structural_risk_cut`` rather than ``d_max_loss`` because the latter has
    the trade's own cash baked into it, which muddles the risk story.
    """
    bits = [f"net {_signed_money(p['net_cash'])}"]

    dm = p.get("d_margin") or 0.0
    if abs(dm) >= 1:
        bits.append(f"frees {_money(-dm)} collateral" if dm < 0
                    else f"uses {_money(dm)} collateral")

    srk = p.get("structural_risk_cut")
    if srk is not None and abs(srk) >= 1:
        bits.append(f"removes {_money(srk)} tail risk" if srk > 0
                    else f"adds {_money(-srk)} tail risk")

    if p.get("d_prob_profit"):
        bits.append(f"P(profit) {p['d_prob_profit'] * 100:+.1f}pp")
    return bits


def _priority(sig: dict) -> tuple:
    p = sig["package"]
    value = max(p["net_cash"], max(0.0, -p["d_margin"]) + max(0.0, p["d_max_loss"]))
    return (KIND_URGENCY.get(sig["kind"], 9), -value)


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


async def build_brief_data(assets: list[str], cfg: SignalConfig | None = None,
                           overrides: dict | None = None) -> dict:
    """Scan each book and gather everything the brief renders."""
    now = datetime.now(ZoneInfo(BRIEF_TIMEZONE))
    books = []
    for asset in assets:
        scan = await scan_deals(asset=asset, cfg=cfg, overrides=overrides)
        trend = await build_market_trend(asset, scan.get("spot"))
        sigs = scan.get("signals") or []
        live = sorted([s for s in sigs if s["state"] == "live"], key=_priority)
        pending = sorted(
            [s for s in sigs if s["trigger_spot"] is not None
             and s["state"] in ("imminent", "approaching")],
            key=lambda s: s["distance_pct"],
        )
        books.append({
            "asset": scan.get("asset", asset.upper()),
            "spot": scan.get("spot"),
            "warning": scan.get("warning"),
            "deals_scanned": scan.get("deals_scanned", 0),
            "counts": scan.get("counts") or {},
            "trend": trend,
            "live": live,
            "pending": pending,
            "further_out": [s for s in sigs if s["state"] == "watch"],
            "counterparties": scan.get("counterparties") or [],
            "config": scan.get("config") or {},
        })
    return {
        "generated_at": now.isoformat(),
        "stamp": now.strftime("%a %d %b %Y %H:%M %Z"),
        "timezone": BRIEF_TIMEZONE,
        "books": books,
    }


# ---------------------------------------------------------------------------
# Deterministic rendering
# ---------------------------------------------------------------------------


def _render_market(book: dict) -> list[str]:
    t = book["trend"]
    asset = book["asset"]
    out = [
        f"{asset} {_price(book['spot'], asset)} "
        f"({_pct(t['change_1d_pct'])} 1d, {_pct(t['change_7d_pct'])} 7d, "
        f"{_pct(t['change_30d_pct'])} 30d)"
    ]
    # Say so loudly when the daily snapshot job has gaps — otherwise "n/a 1d"
    # looks like a bug in the brief rather than a gap in the data.
    stale = t.get("history_stale_days")
    if stale is None:
        out.append("(no daily history for this book yet — trend unavailable)")
    elif stale > 2:
        out.append(f"(daily history {stale}d stale, last {t.get('latest_snapshot')} "
                   f"— trend/realised vol suppressed)")

    rv = t["realised_vol_20d_pct"]
    iv = t["iv"]
    vol_bits = []
    if rv is not None:
        vol_bits.append(f"realised vol 20d {rv:.0f}%")
    if iv["level_pct"] is not None:
        s = f"ATM IV {iv['tenor_days']}d {iv['level_pct']:.0f}%"
        if iv["change_1d"] is not None:
            s += f" ({iv['change_1d']:+.1f} vs prior)"
        if iv["percentile"] is not None:
            s += f", {iv['percentile']:.0f}th pct of {iv['observations']} obs"
        else:
            s += f", {iv['observations']} obs — no percentile yet"
        vol_bits.append(s)
    else:
        vol_bits.append("ATM IV history not started yet")
    if rv is not None and iv["level_pct"] is not None:
        vol_bits.append(f"IV−RV spread {iv['level_pct'] - rv:+.0f}pts")
    out.append(" · ".join(vol_bits))

    pf = t["portfolio"]
    if pf:
        out.append(
            f"Book: MTM {_money(pf.get('mtm_usd'))} · D {pf.get('delta') or 0:,.0f} "
            f"· Th {_money(pf.get('theta'))}/day · V {_money(pf.get('vega'))}/1% "
            f"· {pf.get('position_count') or 0} positions (as of {pf.get('snapshot_date')})"
        )
    return out


def _render_action(sig: dict, asset: str, idx: int | None = None) -> list[str]:
    p = sig["package"]
    head = f"{KIND_VERB.get(sig['kind'], sig['kind'].upper())} — {sig['counterparty']} · {sig['subject']}"
    lines = [f"{idx}. {head}" if idx else head]

    legs = []
    for l in p["close"]:
        legs.append(f"close {l['qty']:g}x {l['opt']} {_price(l['strike'], asset)}")
    for l in p["open"]:
        legs.append(f"open {l['qty']:g}x {l['side'].lower()} {l['opt']} {_price(l['strike'], asset)}")
    lines.append("   " + " / ".join(legs) + f"  ({sig['expiry']}, {sig['days_to_expiry']}d)")

    lines.append("   " + " · ".join(effect_bits(p)))
    return lines


def _render_ladder(book: dict) -> list[str]:
    asset = book["asset"]
    out = []
    for s in book["pending"]:
        p = s["package"]
        arrow = "^" if s["direction"] == "up" else "v"
        # distance_pct is a magnitude (the bands compare against it); sign it by
        # direction for display, so "v 0.66 (+2.9%)" can't read as an up-move.
        signed = s["distance_pct"] * (1 if s["direction"] == "up" else -1)
        out.append(
            f"{arrow} {_price(s['trigger_spot'], asset)} ({signed:+.1f}%)  "
            f"{KIND_VERB.get(s['kind'], s['kind'])} {s['counterparty']} {s['subject']} "
            f"-> " + ", ".join(effect_bits(p))
        )
    return out


def render_brief(data: dict, narrative: dict | None = None) -> str:
    """Render the Slack message. ``narrative`` is optional AI prose."""
    lines = [f"*PLGO Options — morning brief* · {data['stamp']}"]

    if narrative and narrative.get("market_read"):
        lines += ["", f"_{narrative['market_read'].strip()}_"]

    for book in data["books"]:
        asset = book["asset"]
        lines += ["", f"*━━ {asset} ━━*"]
        if book.get("warning"):
            lines.append(f"⚠ {book['warning']}")
            continue

        lines += ["", "*MARKET*"] + _render_market(book)

        live = book["live"]
        order = (narrative or {}).get("order") or []
        if order:                                  # AI-preferred ordering
            rank = {sid: i for i, sid in enumerate(order)}
            live = sorted(live, key=lambda s: rank.get(s["id"], 999))

        lines += ["", f"*DO TODAY ({len(live)})*"]
        if not live:
            lines.append("   Nothing live. Nearest trigger below.")
        for i, s in enumerate(live, 1):
            lines += _render_action(s, asset, i)
            why = (narrative or {}).get("why", {}).get(s["id"])
            lines.append(f"   why: {why.strip() if why else s['package']['rationale']}")

        ladder = _render_ladder(book)
        lines += ["", "*IF PRICE MOVES*"]
        lines += ladder if ladder else ["   No trigger within the alert bands."]
        n_far = len(book["further_out"])
        if n_far:
            lines.append(f"   (+{n_far} further out, beyond the "
                         f"{book['config'].get('approach_pct')}% band)")

        cps = book["counterparties"][:6]
        if cps:
            lines += ["", "*COLLATERAL LIABILITY* (sum of negative MTM by counterparty)",
                      "   " + " · ".join(
                          f"{c['counterparty']} {_money(c['liability_usd'])}" for c in cps)]

    if narrative and narrative.get("note"):
        lines += ["", f"*Watch:* {narrative['note'].strip()}"]

    cfg = data["books"][0]["config"] if data["books"] else {}
    lines += ["", (
        "_Nothing is executed automatically. Every action above is a paired "
        "close+open, costed after bid/ask, and has cleared the economic gate. "
        f"Thresholds: recycle <={cfg.get('recycle_decay_pct')}% decay, "
        f"tighten >={cfg.get('tighten_itm_pct')}% ITM, "
        f"min net {_money(cfg.get('min_net_usd'))}._"
    )]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The narrative (Claude)
# ---------------------------------------------------------------------------

_SYSTEM = """You are the analyst on an institutional crypto options desk, writing \
the 09:00 note for the portfolio manager who owns the book.

You are given fully-computed numbers from the desk's own pricing engine: spot and \
vol trend, and a set of candidate actions that have already been priced after \
bid/ask and passed an economic gate. Every action is a PAIRED close+open — the \
desk never closes protection without replacing it.

Your job is judgement, not arithmetic:
  - Read the market context and say what it means for this book in 2-3 sentences.
  - Decide which ONE action is most worth doing today, and order the rest.
  - Flag conflicts (e.g. an action that frees collateral but adds directional risk),
    and anything the numbers alone would not surface.

Hard rules:
  - NEVER restate, recompute, adjust or invent a number. The message already \
contains every figure; refer to actions by name, not by re-quoting cash amounts.
  - If the candidate list is empty, say plainly that there is nothing to do and \
what would change that.
  - No hedging, no filler, no motivational language. The reader is an expert.
  - Do not recommend simply taking profit or closing a position outright; this \
desk only ever replaces protection, never drops it.

Reply with ONLY a JSON object:
{"market_read": "2-3 sentences",
 "order": ["signal id", ...],
 "why": {"signal id": "one line on why this one, in desk language"},
 "note": "one line on what to watch, or empty string"}"""


def _candidate_payload(data: dict) -> dict:
    """The compact, number-complete view of the scan handed to the model."""
    books = []
    for b in data["books"]:
        t = b["trend"]
        books.append({
            "asset": b["asset"], "spot": b["spot"],
            "change_1d_pct": t["change_1d_pct"], "change_7d_pct": t["change_7d_pct"],
            "change_30d_pct": t["change_30d_pct"],
            "realised_vol_20d_pct": t["realised_vol_20d_pct"],
            "atm_iv": t["iv"], "term_structure": t["term_structure"],
            "portfolio": t["portfolio"],
            "deals_scanned": b["deals_scanned"],
            "collateral_liability": b["counterparties"][:6],
            "live_actions": [{
                "id": s["id"], "kind": s["kind"], "counterparty": s["counterparty"],
                "strategy": s["strategy"], "subject": s["subject"],
                "expiry": s["expiry"], "days_to_expiry": s["days_to_expiry"],
                "net_cash": s["package"]["net_cash"],
                "collateral_freed": -s["package"]["d_margin"],
                "max_loss_change": s["package"]["d_max_loss"],
                "prob_profit_change": s["package"]["d_prob_profit"],
                "engine_rationale": s["package"]["rationale"],
            } for s in b["live"]],
            "pending_triggers": [{
                "id": s["id"], "kind": s["kind"], "counterparty": s["counterparty"],
                "subject": s["subject"], "trigger_spot": s["trigger_spot"],
                "direction": s["direction"], "distance_pct": s["distance_pct"],
                "net_cash": s["package"]["net_cash"],
            } for s in b["pending"]],
        })
    return {"as_of": data["stamp"], "books": books}


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of the reply, tolerantly."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


async def compose_narrative(data: dict) -> dict:
    """Ask Claude for the analyst layer. Never raises; degrades to no narrative."""
    result = {"used_ai": False, "model": ANTHROPIC_BRIEF_MODEL, "error": None,
              "market_read": "", "order": [], "why": {}, "note": ""}
    try:
        import anthropic
    except ImportError:
        result["error"] = "anthropic package not installed"
        return result
    if not ANTHROPIC_API_KEY:
        result["error"] = "ANTHROPIC_API_KEY not set"
        return result

    payload = _candidate_payload(data)
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        # Non-streaming: the reply is a few hundred tokens, well inside the HTTP
        # timeout. Adaptive thinking because ranking conflicting actions is a
        # judgement call, at medium effort because it's a short daily note.
        resp = await client.messages.create(
            model=ANTHROPIC_BRIEF_MODEL,
            max_tokens=4000,
            system=_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content":
                       "Today's scan:\n\n" + json.dumps(payload, indent=1, default=str)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        parsed = _extract_json(text)
        if not parsed:
            result["error"] = f"could not parse reply: {text[:200]}"
            return result

        valid_ids = {s["id"] for b in data["books"] for s in b["live"]}
        result.update({
            "used_ai": True,
            "market_read": str(parsed.get("market_read") or "")[:1200],
            # Only trust ids the engine actually produced — a hallucinated id
            # must not be able to drop a real action out of the ordering.
            "order": [i for i in (parsed.get("order") or []) if i in valid_ids],
            "why": {k: str(v)[:300] for k, v in (parsed.get("why") or {}).items()
                    if k in valid_ids},
            "note": str(parsed.get("note") or "")[:400],
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


async def compose_brief(assets: list[str], use_ai: bool = True,
                        cfg: SignalConfig | None = None,
                        overrides: dict | None = None) -> dict:
    """Full brief: data + optional narrative + rendered Slack text."""
    data = await build_brief_data(assets, cfg=cfg, overrides=overrides)
    narrative = await compose_narrative(data) if use_ai else {"used_ai": False}
    return {"data": data, "narrative": narrative,
            "text": render_brief(data, narrative if narrative.get("used_ai") else None)}
