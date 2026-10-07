"""The eight desk agents, one function each, on the B2 timetable (UK time).

    row_watcher      every 5 min   spot vs the B1 rows, stop, margin; wakes people only for B2b cases
    morning_open     09:00         overnight fills, spot vs rows, margin and funding
    optimizer        09:30, 16:00  Optimizer v4 sweep + two-curve check (B2a)
    handover         15:30         the six handover lines (B2b)
    night_desk       23:00         mark, dealing cost today, tomorrow's brief, night orders
    close_check      02:00         orders live, no margin issue; silent unless something is wrong
    monday_pack      Mon 07:30     last week, view scorecard, draft rows, pre-solved row trades
    monthly_review   1st           dealing cost, decay, funding, month on month (A3)

Every agent: gathers numbers with code, writes FACTS, optionally has Claude
narrate, stores the run, posts to Slack when configured. Nothing here places an
order or writes a trade.
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from plgo_options.agents import gate as gate_mod
from plgo_options.agents import optimizer as opt_mod
from plgo_options.agents import store
from plgo_options.agents.narrate import narrate
from plgo_options.agents.policy import AssetPolicy
from plgo_options.agents.rows import UK, build_rows, crossed_rows, month_key, next_rows, week_key
from plgo_options.data.database import get_db

ASSETS = ("ETH", "FIL")
MAX_PROPOSALS = 5           # per sweep and asset; older open sweep proposals are superseded


# ── data helpers (each one survives a failing upstream) ─────────────────────

async def _safe(coro: Awaitable, default: Any = None) -> tuple[Any, str | None]:
    try:
        return await coro, None
    except Exception as e:
        return default, f"{type(e).__name__}: {e}"[:200]


async def get_spot(asset: str) -> float | None:
    from plgo_options.web.routes.market import get_eth_spot
    d = await get_eth_spot(asset)
    v = d.get("fil_spot") if asset.upper() == "FIL" else d.get("eth_spot")
    return float(v) if v else None


async def get_book(asset: str) -> dict:
    from plgo_options.web.routes.portfolio import portfolio_pnl
    pnl = await portfolio_pnl(asset=asset, include_expired=False)
    t = pnl.get("totals") or {}
    positions = [p for p in (pnl.get("positions") or []) if str(p.get("opt") or "").upper() != "F"]
    # portfolio_pnl names the asset's spot eth_spot, FIL included.
    spot = float(pnl.get("spot") or pnl.get("eth_spot") or 0)
    return {"spot": spot, "mtm": float(t.get("current_total_mtm") or 0),
            "delta": float(t.get("portfolio_delta") or 0), "theta": float(t.get("portfolio_theta") or 0),
            "vega": float(t.get("portfolio_vega") or 0), "positions": positions, "n": len(positions)}


async def get_perp(asset: str) -> dict:
    from plgo_options.web.routes.perps import build_summary
    return await build_summary(asset)


async def get_collateral() -> dict:
    from plgo_options.web.routes.collateral import collateral_summary
    return await collateral_summary("all")


async def get_open_orders() -> dict:
    from plgo_options.web.routes.execution import get_open_orders as oo
    return await oo()


async def mtm_on(asset: str, day: str) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM portfolio_mtm_history WHERE asset=? AND snapshot_date<=? "
        "ORDER BY snapshot_date DESC LIMIT 1", (asset.upper(), day))
    r = await cur.fetchone()
    return dict(r) if r else None


def _saved_profiles(asset: str) -> set[str]:
    from plgo_options.optimization.misc_utils import list_target_profiles
    try:
        return {p["file"] for p in list_target_profiles(asset.upper())}
    except Exception:
        return set()


def _m(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1e6:
        return f"{s}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{s}${a / 1e3:,.1f}k"
    return f"{s}${a:,.0f}"


def _p(asset: str, v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.4f}" if asset.upper() == "FIL" else f"{v:,.0f}"


async def _gate_ctx(pol: AssetPolicy, spot: float, book: dict | None = None,
                    perp: dict | None = None) -> gate_mod.GateContext:
    monday = await mtm_on(pol.asset, week_key())
    return gate_mod.GateContext(
        policy=pol, spot=spot,
        kill_switch=await store.kill_switch_on(),
        curves_frozen=(await store.get_flag(f"curves_frozen_{pol.asset}", "off")) == "on",
        fired_this_week=await store.fired_rows(pol.asset, week_key()),
        month_cost_usd=await store.month_to_date_cost(pol.asset, month_key()),
        perp_notional_usd=float((perp or {}).get("notional_usd") or 0),
        perp_funding_month_usd=abs(float(((perp or {}).get("funding") or {}).get("last_30d_usd") or 0)),
        monday_mtm_usd=float(monday["mtm_usd"]) if monday else None,
        current_mtm_usd=(book or {}).get("mtm"),
        book_delta_usd=float(book["delta"]) * spot if book and book.get("delta") is not None and spot else None,
    )


def _header(pols: list[AssetPolicy]) -> str:
    ex = [p.asset for p in pols if p.is_example]
    lines = ["SHADOW MODE - agents propose, people trade."]
    if ex:
        lines.append(f"WARNING: {', '.join(ex)} still on the manual's EXAMPLE limits and rows. "
                     "Set real numbers in Agents > Policy.")
    return "\n".join(lines)


async def get_validation_book(asset: str) -> dict:
    """The full book payload the v4 engine prices against (same as a sweep)."""
    pnl, _ = await opt_mod.gather_book(asset)
    return pnl


async def _validated(prop: dict, pol: AssetPolicy, book_payload: dict | None = None) -> dict:
    """Price the proposal and run the policy tests (agents/validate.py) before
    the gate. A proposal the agent could not price is never shown as viable."""
    from plgo_options.agents import validate as val_mod
    if not prop.get("legs"):
        return prop
    try:
        pnl = book_payload if book_payload is not None else await get_validation_book(pol.asset)
        v = await asyncio.to_thread(val_mod.validate, pnl, pol, prop["legs"], prop.get("source"))
        val_mod.apply_to_proposal(prop, v)
    except Exception as e:
        prop["validation"] = {"findings": [("chris", f"Not validated - the agent could not price it "
                                                     f"({type(e).__name__}: {str(e)[:120]})", "B3")]}
    return prop


# ── 1. Row watcher ────────────────────────────────────────────────────────

def _row_trade(asset: str, row, spot: float, book: dict, pol: AssetPolicy) -> dict:
    """B1a: the trade a fired row calls for, sized by the manual's rules."""
    if row.instrument == "perp":
        # "Bring exposure back to target": target = delta at Monday's reference.
        ref_delta = getattr(pol, "reference_delta", None)
        drift_units = (book["delta"] - ref_delta) if ref_delta is not None else None
        if drift_units is None:
            single, _ = pol.perp_limits(None)
            notional = single * (0.83 if row.step == 1 else 1.0)
        else:
            notional = abs(drift_units) * spot
        side = "Sell" if row.direction > 0 else "Buy"
        qty = notional / spot if spot else 0
        return {"asset": asset, "source": "row", "purpose": "direction", "row_key": row.key,
                "legs": [{"kind": "perp", "side": side, "qty": round(qty, 2),
                          "counterparty": pol.perp_venue, "opt": "F"}],
                "notional_usd": notional, "net_cost_usd": notional * 0.0002, "spot": spot,
                "reason": f"Row {row.key} fired: {row.action}"}
    if row.instrument == "options" and asset.upper() == "ETH":
        calls = [p for p in book["positions"] if str(p.get("opt")).upper() == "C"
                 and float(p.get("qty") or 0) > 0 and float(p.get("strike") or 0) < spot]
        puts = [p for p in book["positions"] if str(p.get("opt")).upper() == "P"
                and float(p.get("qty") or 0) > 0 and float(p.get("strike") or 0) > spot]
        legs = []
        if row.direction > 0:
            for c in calls:
                legs.append({"kind": "option", "side": "Sell", "opt": "C", "strike": c.get("strike"),
                             "expiry": str(c.get("expiry")), "qty": round(float(c["qty"]) * 0.25),
                             "counterparty": c.get("counterparty"), "is_unwind": True})
            text = (f"Sell 25% of the ITM calls ({len(calls)} lines); buy a put spread "
                    f"long {spot * 0.9:,.0f} / short {spot * 0.75:,.0f}, same expiry, sized to the premium.")
        else:
            for p in puts:
                legs.append({"kind": "option", "side": "Sell", "opt": "P", "strike": p.get("strike"),
                             "expiry": str(p.get("expiry")), "qty": round(float(p["qty"]) * 0.4),
                             "counterparty": p.get("counterparty"), "is_unwind": True})
            text = (f"Sell 30-50% of the puts that gained ({len(puts)} lines); buy calls struck "
                    f"{spot * 1.1:,.0f}, 3-6 months out, sized to the premium.")
        return {"asset": asset, "source": "row", "purpose": "shape", "row_key": row.key,
                "legs": legs, "notional_usd": sum(abs(float(l['qty'])) for l in legs) * spot,
                "net_cost_usd": 0.0, "spot": spot, "reason": f"Row {row.key}: {text}", "text": text}
    return {"asset": asset, "source": "row", "purpose": "shape", "row_key": row.key, "legs": [],
            "notional_usd": 0, "net_cost_usd": 0, "spot": spot, "reason": row.action}


async def row_watcher(ctx: dict) -> dict:
    lines, wake, pols = [], [], []
    for asset in ASSETS:
        pol = await store.get_policy(asset)
        pols.append(pol)
        spot, err = await _safe(get_spot(asset))
        if not spot:
            lines.append(f"{asset}: no spot ({err}).")
            continue
        if pol.stop_price and spot <= pol.stop_price:
            wake.append(f"{asset} at {_p(asset, spot)} is AT/BELOW THE STOP {_p(asset, pol.stop_price)}. "
                        "Close, do not roll. Chris decides (A3).")
        if not pol.reference_price:
            lines.append(f"{asset} {_p(asset, spot)}: no Monday reference set - rows inactive.")
            continue
        rows = build_rows(asset, pol.reference_price, pol.row_steps_pct)
        hit = crossed_rows(rows, pol.reference_price, spot)
        below, above = next_rows(rows, spot)
        lines.append(f"{asset} {_p(asset, spot)} vs ref {_p(asset, pol.reference_price)}; "
                     f"next rows {below.key if below else '-'} {_p(asset, below.price if below else None)} / "
                     f"{above.key if above else '-'} {_p(asset, above.price if above else None)}")
        if not hit:
            continue
        row = hit[0]
        first = await store.mark_fired(asset, week_key(), row.key, spot)
        if not first:
            continue                                # B1: once per week
        if row.step >= 3:
            wake.append(f"{asset} row {row.key} at {_p(asset, spot)}: {row.action}")
        book, _ = await _safe(get_book(asset), {"delta": 0, "positions": [], "mtm": None})
        perp, _ = await _safe(get_perp(asset), {})
        prop = await _validated(_row_trade(asset, row, spot, book, pol), pol)
        gctx = await _gate_ctx(pol, spot, book, perp)
        gctx.fired_this_week.discard(row.key)       # we just fired it ourselves
        res = gate_mod.evaluate(prop, gctx)
        route = res.route
        if row.who == "lucas_tell_chris" and route == gate_mod.CHRIS and row.step == 2 \
                and not any("REJECT" in r.upper() for r in res.reasons):
            route = "lucas_tell_chris"              # B1: Lucas acts, tells Chris same day
        pid = await store.add_proposal("row_watcher", asset, f"row {row.key}",
                                       prop.get("text") or row.action, route, res.reasons,
                                       {**prop, "gate": res.to_dict()})
        lines.append(f"  ROW {row.key} FIRED -> proposal #{pid}, route {route.upper()}: "
                     f"{'; '.join(res.reasons)}")
    # Margin (B2b: margin call wakes people).
    col, cerr = await _safe(get_collateral(), {})
    for r in (col or {}).get("rows", []) or []:
        sf = float(r.get("shortfall_haircut") or 0)
        if sf > 0:
            wake.append(f"Margin shortfall {r.get('counterparty')}/{r.get('portfolio_asset')}: {_m(sf)}.")
    facts = _header(pols) + "\n" + "\n".join(lines)
    return {"facts": facts, "wake": wake, "quiet_ok": not wake,
            "deliver": bool(wake) or any("FIRED" in l for l in lines)}


# ── 2. Morning open ───────────────────────────────────────────────────────

async def morning_open(ctx: dict) -> dict:
    out, pols, data = [], [], {}
    since = (datetime.now(UK) - timedelta(hours=10)).astimezone().isoformat()
    for asset in ASSETS:
        pol = await store.get_policy(asset)
        pols.append(pol)
        book, berr = await _safe(get_book(asset))
        perp, perr = await _safe(get_perp(asset), {})
        spot = (book or {}).get("spot")
        if not spot:
            spot, serr = await _safe(get_spot(asset))
        out.append(f"== {asset} ==")
        d = data.setdefault(asset, {})          # the same figures, for the Agents page panel
        if not spot:
            d["error"] = f"No spot ({berr or serr})"
            out.append(f"No spot available ({berr or serr}). Check market data before trading.")
            continue
        if book:
            d.update(spot=spot, mtm=book["mtm"], delta=book["delta"], delta_usd=book["delta"] * spot,
                     theta=book["theta"], vega=book["vega"])
            out.append(f"Spot {_p(asset, spot)} | MTM {_m(book['mtm'])} | delta {book['delta']:,.0f} "
                       f"({_m(book['delta'] * spot)}) | theta {_m(book['theta'])}/day | vega {_m(book['vega'])}")
        else:
            d.update(spot=spot, book_error=berr)
            out.append(f"Book unavailable: {berr}")
        if pol.reference_price:
            rows = build_rows(asset, pol.reference_price, pol.row_steps_pct)
            b, a = next_rows(rows, spot)
            fired = sorted(await store.fired_rows(asset, week_key()))
            d.update(reference=pol.reference_price, fired=fired,
                     row_down={"key": b.key, "price": b.price, "pct": (b.price / spot - 1) * 100} if b else None,
                     row_up={"key": a.key, "price": a.price, "pct": (a.price / spot - 1) * 100} if a else None)
            out.append(f"Ref {_p(asset, pol.reference_price)}. Next row down {b.key if b else '-'} at "
                       f"{_p(asset, b.price if b else None)} ({(b.price / spot - 1) * 100 if b else 0:+.1f}%), "
                       f"up {a.key if a else '-'} at {_p(asset, a.price if a else None)} "
                       f"({(a.price / spot - 1) * 100 if a else 0:+.1f}%). Fired this week: {', '.join(fired) or 'none'}.")
        else:
            out.append("No Monday reference set - rows inactive.")
        if pol.stop_price:
            d.update(stop=pol.stop_price, stop_pct=(spot / pol.stop_price - 1) * 100)
            out.append(f"Stop {_p(asset, pol.stop_price)}: spot is {(spot / pol.stop_price - 1) * 100:+.1f}% away.")
        if perp:
            f = perp.get("funding") or {}
            d.update(perp_venue=perp.get("venue"), perp_qty=perp.get("net_qty"),
                     perp_usd=perp.get("notional_usd"), funding_30d=f.get("last_30d_usd"),
                     perp_error=perp.get("market_error"))
            out.append(f"Perp {perp.get('venue')}: {perp.get('net_qty')} ({_m(perp.get('notional_usd'))}), "
                       f"funding 30d {_m(f.get('last_30d_usd'))}"
                       + (f" | ERROR {perp.get('market_error')}" if perp.get("market_error") else ""))
        # The sweep reads its targets at 09:30; set them in Agents > Policy before then.
        tg = pol.optimizer.targets(asset)
        missing = [t["name"] for t in tg if t["file"] and t["file"] not in _saved_profiles(asset)]
        d.update(targets=[t["name"] for t in tg], targets_set=bool(pol.optimizer.target_grid),
                 targets_missing=missing)
        out.append(("Sweep targets today (" + ("set" if pol.optimizer.target_grid else "auto") + "): "
                    + "; ".join(t["name"] for t in tg))
                   + (f" | MISSING: {', '.join(missing)}" if missing else ""))
        props = [p for p in await store.list_proposals("open") if p["asset"] == asset]
        d["open_proposals"] = {r: sum(1 for p in props if p["route"] == r) for r in ("lucas", "chris", "rejected")}
        if props:
            out.append(f"Open proposals: " + "; ".join(f"#{p['id']} {p['kind']} -> {p['route']}" for p in props[:6]))
    oo, oerr = await _safe(get_open_orders(), None)
    data["_all"] = {"resting_orders": len((oo or {}).get("orders") or []) if oo else None,
                    "orders_error": None if oo else oerr}
    out.append(f"Resting orders: {len((oo or {}).get('orders') or []) if oo else 'unavailable (' + str(oerr) + ')'}")
    col, _ = await _safe(get_collateral(), {})
    tot = (col or {}).get("totals") or {}
    if tot:
        data["_all"].update(shortfall=tot.get("shortfall_haircut"), liability=tot.get("liability_usd"))
        out.append(f"Collateral: shortfall (haircut) {_m(tot.get('shortfall_haircut'))}, "
                   f"liability {_m(tot.get('liability_usd'))}.")
    return {"facts": _header(pols) + "\n" + "\n".join(out), "deliver": True, "data": data}


# ── 3. Optimizer (+ two-curve check) ──────────────────────────────────────

async def optimizer(ctx: dict) -> dict:
    label = ctx.get("label") or ("am" if datetime.now(UK).hour < 13 else "pm")
    assets = [a.upper() for a in (ctx.get("assets") or ["ETH"])]
    out, pols, data = [], [], {}
    for asset in assets:
        pol = await store.get_policy(asset)
        pols.append(pol)
        res = await opt_mod.sweep(pol)
        best = res["best"]
        out.append(f"== {asset} Optimizer v4 sweep ({label}) - {res['runs']} runs, "
                   f"{res['target']['expiry']} {','.join(res['target']['counterparties'] or ['all'])}, "
                   f"{len(res['targets'])} target profiles: {'; '.join(res['targets'])} ==")
        fine = {k: v for k, v in (res.get("lam_refined") or {}).items() if v}
        if fine:
            out.append("λ fine search: " + "; ".join(
                f"{k} {v[0]:g}-{v[-1]:g}" for k, v in fine.items()))
        if res.get("sized"):
            out.append("Size search (best λ, max trades, max qty): " + "; ".join(
                f"{k} λ{v['lam_factor']:g} {v['max_trades']} trades / {v['max_qty']:,.0f}"
                for k, v in res["sized"].items()))
        if res["errors"]:
            out.append(f"{len(res['errors'])} runs failed.")
        if not best:
            out.append("No run passed the filters (converged, follows the target, floor / max loss / view, "
                       "no giveback beyond limit).")
        # Say why a target produced nothing, so a bad target is visible, not silent.
        got = {s["target"]["name"] for s in best}
        for tname in res["targets"]:
            if tname in got:
                continue
            why: dict[str, int] = {}
            for s in res["ranked"]:
                if s["target"]["name"] == tname:
                    for d in s["disqualified"]:
                        k = re.sub(r"\$[-\d,.]+M?", "$..", d.split(":")[0])
                        why[k] = why.get(k, 0) + 1
            n_err = sum(1 for e in res["errors"] if e.get("target") == tname)
            mine = [s for s in res["ranked"] if s["target"]["name"] == tname and s["key_spot_changes"]]
            near = max(mine, key=lambda s: s["track_gain_pct"] - 1e3 * len(s["disqualified"]), default=None)
            detail = ""
            if near:
                w = min(near["key_spot_changes"], key=lambda d: d["change_usd"])
                nv = near["variant"]
                detail = (f" | closest: λ{nv['lam_factor']} max{nv['max_trades']} target {near['track_gain_pct']:+.1f}%, "
                          f"out for: {'; '.join(near['disqualified'])} | worst {_m(w['change_usd'])} at "
                          f"{w['moneyness']:+.0%} (T+{near.get('judged_on_days', 0)}d) - "
                          + ", ".join(f"{d['moneyness']:+.0%} {_m(d['change_usd'])}" for d in near["key_spot_changes"]))
            out.append(f"[{tname}] no run passed: " + (", ".join(f"{k} x{v}" for k, v in why.items()) or "-")
                       + (f", {n_err} errors" if n_err else "") + detail)

        # Each target's passing runs, best first, through the validation and the
        # gate: the first one they accept is the proposal. If every one tried is
        # rejected, the top one is still filed so the reason is on screen.
        perp, _ = await _safe(get_perp(asset), {})
        gctx = await _gate_ctx(pol, res["spot"], None, perp)
        picks = []
        for tname in res["targets"]:
            cands = (res.get("candidates") or {}).get(tname) or []
            tried, first = 0, None
            for s in cands[: max(1, pol.optimizer.gate_retries)]:
                tried += 1
                prop = opt_mod.to_proposal(asset, s)
                # What "Validate in v4" replays: the page's own run parameters.
                prop["variant"] = s["variant"]
                prop["v4_params"] = opt_mod.run_kwargs(asset, pol.optimizer, s["variant"], s["target"])
                prop["target"] = s["target"]
                await _validated(prop, pol, res.get("book"))
                gres = gate_mod.evaluate(prop, gctx)
                first = first or (s, prop, gres, tried)
                if gres.route != gate_mod.REJECTED:
                    picks.append((s, prop, gres, tried))
                    break
            else:
                if first:
                    picks.append(first)
                    out.append(f"[{tname}] the gate rejected all {tried} runs tried; filing the top one with its reasons.")
        new_ids = []
        for i, (s, prop, gres, tried) in enumerate(picks[:MAX_PROPOSALS], 1):
            v = s["variant"]
            ch = ", ".join(f"{d['moneyness']:+.0%} {_m(d['change_usd'])}" for d in s["key_spot_changes"])
            rank_note = f" (rank {tried}: the {tried - 1} above it were rejected)" if tried > 1 else ""
            pid = await store.add_proposal(
                "optimizer", asset, f"v4 {label} #{i}",
                f"λ{v['lam_factor']} κ{v['downside_factor']} T+90 {v['t90_weight']} max {v['max_trades']} "
                f"trades / {v['max_qty']:g} qty: target {s['track_gain_pct']:+.1f}% closer, fit +{s['fit_gain_pct']}%, "
                f"{s['option_lines']} lines, cost {_m(s['cost_usd'])} | target {s['target']['name']} | "
                f"judged at T+{s.get('judged_on_days', 0)}d{rank_note}",
                gres.route, gres.reasons, {**prop, "gate": gres.to_dict()})
            new_ids.append(pid)
            out.append(f"{i}. [{s['target']['name']}] λ{v['lam_factor']} κ{v['downside_factor']} T+90 {v['t90_weight']} "
                       f"max{v['max_trades']}/{v['max_qty']:g}: target {s['track_gain_pct']:+.1f}% closer "
                       f"(fit +{s['fit_gain_pct']}%), {s['option_lines']} option lines, cost {_m(s['cost_usd'])}, net prem "
                       f"{_m(s['net_premium_usd'])}{rank_note} | vs book at T+{s.get('judged_on_days', 0)}d: {ch} | "
                       f"proposal #{pid} -> {gres.route.upper()}"
                       + (": " + " / ".join(gres.rejected_by or gres.needs_chris) if gres.route != gate_mod.LUCAS else ""))
        # Older proposals were judged on an older book (and maybe older rules):
        # this sweep replaces them even when it files nothing itself.
        n_old = await store.supersede_open(asset, "optimizer", new_ids)
        if n_old:
            out.append(f"{n_old} older open sweep proposal(s) for {asset} superseded by this run.")
        # Two-curve check against the previous run today.
        top = best[0] if best else None
        if top:
            # Same day and same target only: curves fitted to different targets differ by design.
            tname = top["target"]["name"]
            prev = [c for c in await store.recent_curves(asset, 10)
                    if c["created_at"][:10] == store.now_iso()[:10]
                    and (c["params"] or {}).get("target") == tname]
            await store.save_curve(asset, label, top["spot"], {**top["variant"], "target": tname},
                                   {k: top[k] for k in ("fit_gain_pct", "cost_usd", "option_lines", "trades")})
            if prev:
                p0 = prev[0]
                move = (top["spot"] / p0["spot"] - 1) * 100 if p0.get("spot") else 0
                cmp = opt_mod.compare_curves(p0["summary"], top, move)
                out.append(f"Two-curve check vs {p0['run_label']} run: {cmp['verdict'].upper()} "
                           f"(overlap {cmp['overlap']:.0%}, spot moved {cmp['spot_move_pct']:+.2f}%).")
                if cmp["verdict"] == "disagree":
                    await store.set_flag(f"curves_frozen_{asset}", "on", "optimizer")
                    out.append("Curves DISAGREE: trading frozen until inputs are checked (B2a). "
                               "Clear in Agents > Flags once found.")
        data[asset] = {"best": [{k: s[k] for k in ("variant", "track_gain_pct", "fit_gain_pct", "cost_usd", "option_lines",
                                                  "net_premium_usd", "key_spot_changes", "score")}
                                for s in best],
                       "pareto": [{k: s[k] for k in ("variant", "track_gain_pct", "fit_gain_pct", "cost_usd", "option_lines")}
                                  for s in res["pareto"]]}
    return {"facts": _header(pols) + "\n" + "\n".join(out), "deliver": True, "data": data}


# ── 4. Handover (the six lines) ───────────────────────────────────────────

async def handover(ctx: dict) -> dict:
    pols = [await store.get_policy(a) for a in ASSETS]
    l1, l5 = [], []
    for pol in pols:
        spot, _ = await _safe(get_spot(pol.asset))
        if spot and pol.reference_price:
            rows = build_rows(pol.asset, pol.reference_price, pol.row_steps_pct)
            b, a = next_rows(rows, spot)
            dist = min([abs(r.price / spot - 1) * 100 for r in (b, a) if r], default=None)
            l1.append(f"{pol.asset} {_p(pol.asset, spot)} ({dist:.1f}% to next row)" if dist is not None
                      else f"{pol.asset} {_p(pol.asset, spot)}")
        else:
            l1.append(f"{pol.asset} {_p(pol.asset, spot)} (no reference)")
        perp, _ = await _safe(get_perp(pol.asset), {})
        l5.append(f"{pol.asset} perp funding 30d {_m(((perp or {}).get('funding') or {}).get('last_30d_usd'))}")
    today = store.now_iso()[:10]
    runs = [r for r in await store.list_runs("optimizer", 5) if (r["started_at"] or "")[:10] == today]
    l2 = (runs[0]["text"].split("\n")[2:5] if runs and runs[0]["text"] else ["no optimizer run today"])
    props = await store.list_proposals(limit=200)
    traded = [p for p in props if p["status"] == "executed" and (p["decided_at"] or "")[:10] == today]
    waiting = [p for p in props if p["status"] == "open" and p["route"] in ("chris", "lucas_tell_chris")]
    col, _ = await _safe(get_collateral(), {})
    tot = (col or {}).get("totals") or {}
    l5.insert(0, f"margin shortfall (haircut) {_m(tot.get('shortfall_haircut'))}")
    six = [
        "1. Spot: " + "; ".join(l1),
        "2. Optimizer this morning: " + " | ".join(x.strip() for x in l2),
        "3. Traded since last handover: " + ("; ".join(f"#{p['id']} {p['summary']}" for p in traded) or "nothing"),
        "4. Waiting for Chris: " + ("; ".join(f"#{p['id']} {p['asset']} {p['kind']} ({p['reasons'][0] if p['reasons'] else ''})"
                                              for p in waiting[:8]) or "nothing"),
        "5. Margin and funding: " + "; ".join(l5),
        "6. Anything that would change Monday's view: " + (ctx.get("note") or "none flagged by the agents"),
    ]
    return {"facts": _header(pols) + "\nHANDOVER\n" + "\n".join(six), "deliver": True}


# ── 5. Night desk (23:00) and 6. close check (02:00) ──────────────────────

async def night_desk(ctx: dict) -> dict:
    pols, out = [], []
    today = store.now_iso()[:10]
    for asset in ASSETS:
        pol = await store.get_policy(asset)
        pols.append(pol)
        book, _ = await _safe(get_book(asset))
        spot = (book or {}).get("spot")
        if book:
            out.append(f"{asset}: mark {_m(book['mtm'])} at {_p(asset, spot)}, theta {_m(book['theta'])}/day.")
        props = await store.list_proposals(limit=200)
        done = [p for p in props if p["asset"] == asset and p["status"] == "executed"
                and (p["decided_at"] or "")[:10] == today]
        cost = sum(float(p["proposal"].get("net_cost_usd") or 0) for p in done)
        out.append(f"  Dealing today: {len(done)} trades, net {_m(cost)}.")
        if pol.reference_price and spot:
            fired = await store.fired_rows(asset, week_key())
            rows = [r for r in build_rows(asset, pol.reference_price, pol.row_steps_pct)
                    if r.step == 1 and r.key not in fired]
            for r in rows:
                side = "SELL" if r.direction > 0 else "BUY"
                out.append(f"  Night order (for Chris to place): {side} perp "
                           f"{_m(pol.max_single_trade_usd * 0.83)} if {asset} trades {_p(asset, r.price)} "
                           f"(row {r.key}). Cancel and rewrite at the next handover.")
    return {"facts": _header(pols) + "\nNIGHT DESK - tomorrow's brief for Lucas\n" + "\n".join(out),
            "deliver": True}


async def close_check(ctx: dict) -> dict:
    problems = []
    oo, oerr = await _safe(get_open_orders(), None)
    if oo is None:
        problems.append(f"Cannot read resting orders: {oerr}")
    col, _ = await _safe(get_collateral(), {})
    for r in (col or {}).get("rows", []) or []:
        if float(r.get("shortfall_haircut") or 0) > 0:
            problems.append(f"Margin shortfall {r.get('counterparty')}/{r.get('portfolio_asset')}: "
                            f"{_m(r.get('shortfall_haircut'))}")
    n = len((oo or {}).get("orders") or []) if oo else 0
    facts = f"02:00 close check: {n} resting orders live. " + ("; ".join(problems) or "No margin issue.")
    return {"facts": facts, "deliver": bool(problems), "wake": problems if any("Margin" in p for p in problems) else []}


# ── 7. Monday pack and 8. monthly review ──────────────────────────────────

async def set_monday_reference(asset: str, spot: float | None, pol: AssetPolicy) -> str | None:
    """B1: Monday's reference populates every row, so set it from spot.

    Observation, not judgement — the reference is where the market was when the
    week's levels were struck, which is why the manual files it under "Chris
    sets, Lucas fills in" rather than under the view.

    Only ever writes once per week, and never overwrites a reference a person
    already set this Monday: if someone has struck the week deliberately, the
    agent must not move it underneath them. Everything else in the policy is
    left alone — in particular is_example, because whether the limits are real
    is a decision about A3, not about B1.
    """
    this_monday = week_key()
    if not spot or spot <= 0:
        return None
    if pol.reference_set_on == this_monday:
        return None                                   # already struck this week
    before = pol.reference_price
    pol.reference_price = float(spot)
    pol.reference_set_on = this_monday
    await store.save_policy(pol, by="monday-pack")
    return (f"Reference set to {_p(asset, spot)} for the week of {this_monday}"
            + (f" (was {_p(asset, before)})." if before else " (none before)."))


async def monday_pack(ctx: dict) -> dict:
    from plgo_options.web.market_trend import build_market_trend
    pols, out, set_notes = [], [], []
    last_week = (datetime.now(UK) - timedelta(days=7))
    lw_key = week_key(last_week)
    for asset in ASSETS:
        pol = await store.get_policy(asset)
        pols.append(pol)
        book, _ = await _safe(get_book(asset))
        spot = (book or {}).get("spot")
        # A preview must not move the mandate, so only the real 07:30 run writes.
        if ctx.get("deliver"):
            note, err = await _safe(set_monday_reference(asset, spot, pol))
            if note:
                set_notes.append(f"{asset}: {note}")
            elif err:
                set_notes.append(f"{asset}: reference NOT set - {err}")
        trend, _ = await _safe(build_market_trend(asset, spot), {})
        fired = sorted(await store.fired_rows(asset, lw_key))
        props = [p for p in await store.list_proposals(limit=300)
                 if p["asset"] == asset and (p["created_at"] or "") >= last_week.isoformat()[:10]]
        by_route = {}
        for p in props:
            by_route[p["route"]] = by_route.get(p["route"], 0) + 1
        out.append(f"== {asset} ==")
        if book:
            out.append(f"Spot {_p(asset, spot)} | MTM {_m(book['mtm'])} | theta {_m(book['theta'])}/day "
                       f"(~{_m(book['theta'] * 7)}/week of decay).")
        out.append(f"Last week: rows fired {', '.join(fired) or 'none'}; proposals {sum(by_route.values())} "
                   f"({', '.join(f'{k} {v}' for k, v in by_route.items()) or 'none'}).")
        if trend:
            iv = (trend.get("iv") or {}).get("level_pct")
            f = lambda v: "n/a" if v is None else f"{v}%"
            out.append(f"Moves: 7d {f(trend.get('change_7d_pct'))}, 30d {f(trend.get('change_30d_pct'))}. "
                       f"Realised vol 20d {f(trend.get('realised_vol_20d_pct'))}; implied 30d "
                       f"{f(iv) if iv is not None else 'no history yet'}.")
        if pol.view_range_low and pol.view_range_high and spot:
            inside = pol.view_range_low <= spot <= pol.view_range_high
            out.append(f"View '{pol.view}' range {_p(asset, pol.view_range_low)}-{_p(asset, pol.view_range_high)}: "
                       f"spot {'inside' if inside else 'OUTSIDE'}; check date {pol.view_check_date or 'not set'}.")
        else:
            out.append(f"View: '{pol.view}' - no 90-day range written yet (A1).")
        # The rows the watcher is actually measuring against this week. Drafts
        # off spot only when there is no reference struck this week to show.
        if pol.reference_price and pol.reference_set_on == week_key():
            fired_now = await store.fired_rows(asset, week_key())
            live = build_rows(asset, pol.reference_price, pol.row_steps_pct)
            out.append(f"Live rows around the reference {_p(asset, pol.reference_price)} "
                       f"(set {pol.reference_set_on}): " + ", ".join(
                           f"{r.key} {_p(asset, r.price)}"
                           + (f" ({(r.price / spot - 1) * 100:+.1f}% from spot)" if spot else "")
                           + (" FIRED" if r.key in fired_now else "")
                           for r in live))
            if pol.stop_price:
                out.append(f"Stop {_p(asset, pol.stop_price)}"
                           + (f" ({(pol.stop_price / spot - 1) * 100:+.1f}% from spot)." if spot else "."))
        elif spot:
            draft = build_rows(asset, spot, pol.row_steps_pct)
            out.append("No reference struck this week. Draft rows if the reference is today's spot: " +
                       ", ".join(f"{r.key} {_p(asset, r.price)}" for r in draft))
    if set_notes:
        out.append("REFERENCE (set automatically this morning): " + " ".join(set_notes))
    out.append("DECIDE (Lucas + Chris): the view, the stop, and whether the limits are still right. "
               "Save in Agents > Policy.")
    return {"facts": _header(pols) + "\nMONDAY PACK\n" + "\n".join(out), "deliver": True}


async def monthly_review(ctx: dict) -> dict:
    now = datetime.now(UK)
    this_m = month_key(now - timedelta(days=1))           # the month just ended
    prev_m = month_key((now - timedelta(days=1)).replace(day=1) - timedelta(days=1))
    db = await get_db()
    out = []
    for asset in ASSETS:
        props = await store.list_proposals(limit=1000)
        def month_stats(m):
            done = [p for p in props if p["asset"] == asset and p["status"] == "executed"
                    and (p["decided_at"] or "")[:7] == m]
            return len(done), sum(max(0.0, float(p["proposal"].get("net_cost_usd") or 0)) for p in done)
        n1, c1 = month_stats(this_m)
        n0, c0 = month_stats(prev_m)
        cur = await db.execute(
            "SELECT SUM(theta) AS th, SUM(perp_funding_usd) AS f FROM portfolio_mtm_history "
            "WHERE asset=? AND substr(snapshot_date,1,7)=?", (asset, this_m))
        r = await cur.fetchone()
        cur0 = await db.execute(
            "SELECT SUM(theta) AS th FROM portfolio_mtm_history WHERE asset=? AND substr(snapshot_date,1,7)=?",
            (asset, prev_m))
        r0 = await cur0.fetchone()
        th1, th0 = float((r["th"] if r else 0) or 0), float((r0["th"] if r0 else 0) or 0)
        if not th0 or (n0 == 0 and n1 == 0):
            verdict = "Not enough history yet to compare month on month."
        elif (c1 <= c0) and (abs(th1) <= abs(th0)):
            verdict = "Both falling - policy working."
        else:
            verdict = "NOT both falling - A3 says change the policy rather than defend it."
        out.append(f"{asset} {this_m}: {n1} trades, dealing cost {_m(c1)} (prev {_m(c0)}); "
                   f"time decay {_m(th1)} (prev {_m(th0)}). " + verdict)
    return {"facts": "MONTHLY REVIEW\n" + "\n".join(out), "deliver": True}


# ── registry and runner ───────────────────────────────────────────────────

AGENTS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "row-watcher": row_watcher,
    "morning-open": morning_open,
    "optimizer": optimizer,
    "handover": handover,
    "night-desk": night_desk,
    "close-check": close_check,
    "monday-pack": monday_pack,
    "monthly-review": monthly_review,
}

# Agents whose output is worth a model rewrite (the frequent ones stay plain).
NARRATED = {"morning-open", "handover", "night-desk", "monday-pack", "monthly-review", "optimizer"}


async def run_agent(name: str, ctx: dict | None = None, deliver: bool = True,
                    use_ai: bool = True) -> dict:
    from plgo_options.web import slack
    from plgo_options.web.alerting import in_quiet_hours

    if name not in AGENTS:
        raise KeyError(name)
    ctx = ctx or {}
    run_id = await store.start_run(name)
    try:
        if await store.kill_switch_on() and name not in ("close-check",):
            result = {"facts": "Kill switch is ON - agent skipped.", "deliver": False}
        else:
            # Agents that change stored state (monday-pack writes B1's reference)
            # need to know a preview from the real run, or a "what would this
            # say?" click would move the mandate.
            ctx.setdefault("deliver", deliver)
            result = await AGENTS[name](ctx)
        facts = result.get("facts", "")
        text, ai_err = (await narrate(name, facts, use_ai)) if name in NARRATED else (facts, None)
        wake = result.get("wake") or []
        if wake:
            text = "WAKE: " + " | ".join(wake) + "\n\n" + text
        should_send = deliver and result.get("deliver", True)
        if should_send and name == "row-watcher" and in_quiet_hours() and not wake:
            should_send = False                    # quiet hours: only B2b wake cases go out
        delivery = await slack.post_message(f"*{name}*\n{text}") if should_send else \
            {"delivered": False, "detail": "not sent"}
        await store.finish_run(run_id, "ok", text,
                               {"data": result.get("data"), "wake": wake, "ai_error": ai_err,
                                "delivery": delivery}, delivery.get("delivered", False))
        return {"run_id": run_id, "agent": name, "text": text, "wake": wake,
                "delivery": delivery, "ai_error": ai_err, "data": result.get("data")}
    except Exception as e:
        tb = traceback.format_exc()
        await store.finish_run(run_id, "error", f"{type(e).__name__}: {e}", {"traceback": tb[-4000:]}, False)
        raise
