"""Counterparty risk report — the Collateral Platform and Greeks screens.

Laid out like the position report counterparties send us (Flowdesk's
"Protocol Labs Positions", Oct 2026), per asset:

* Positions: the spot collateral posted with the counterparty as the first
  row, then every option leg (and any OTC perp), with Delta (underlying
  units), Delta ($) = Delta x spot, Gamma (per 1 unit of spot), Gamma ($) =
  Gamma x spot^2 / 100 (delta change in $ for a 1% move), Theta ($/day) and
  Vega ($ per vol point).
* Slides: P&L against today of options + collateral for spot shocks (rows)
  x vol shocks (columns), instantaneous (no decay), each leg repriced
  Black-Scholes from the same IV the book is marked at (portfolio /pnl, so
  counterparty mark vols apply). Vol shocks are vol points by default, or
  relative (x (1 + shock)) with vol_mode=relative. The default,
  vol_mode=flowdesk, matches Flowdesk's slides: the shift is quoted for a
  6-month tenor and each leg moves shift x sqrt(6M / its time to expiry)
  vol points, so shorter expiries move more. Fitted on their Oct 2026
  report (both books 66 days out): it reproduces their vol columns within
  ~$2k (FIL) / ~$10k (ETH, whose skew the test flattens) per cell, against
  $66-76k for a plain vol-point shift.
* Summary bullets in the same words.

counterparty=ALL aggregates the whole book (the Greeks screen's default).
"""
from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
from scipy.special import ndtr

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from plgo_options.data.collar_loans import drop_collar_loans
from plgo_options.data.database import get_db
from plgo_options.pricing.options import bs_price
from plgo_options.web.routes.collateral import (
    _effective_prices,
    _gather_portfolio,
    _otc_perps,
    effective_haircut_rate,
)
from plgo_options.web.routes.portfolio import _leg_iv, build_market_context

router = APIRouter()

ASSETS = ("ETH", "FIL")
SPOT_SHOCKS = [1.00, 0.50, 0.30, 0.20, 0.10, 0.05, 0.03, 0.02, 0.01, 0.0,
               -0.01, -0.02, -0.03, -0.05, -0.10, -0.20, -0.50, -0.80]
VOL_SHOCKS = [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
MIN_VOL = 0.01
VOL_MODES = ("flowdesk", "points", "relative")
# Flowdesk's reference tenor for a vol shift (see module docstring).
FLOWDESK_REF_DAYS = 182.5


def _symbol(asset: str, expiry: str, strike: float, opt: str) -> str:
    """FILUSDC-25DEC26-1600-0.5-C-USDC, as on the counterparty's report."""
    try:
        code = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").strftime("%d%b%y").upper()
    except ValueError:
        code = str(expiry)
    k = int(strike) if float(strike).is_integer() else strike
    return f"{asset}USDC-{code}-1600-{k}-{opt}-USDC"


def _expiry_label(expiry: str) -> str:
    try:
        return datetime.strptime(str(expiry)[:10], "%Y-%m-%d").strftime("%b%y")
    except ValueError:
        return str(expiry)


def _shocked_vol(sigma: float, dv: float, mode: str, T: float) -> float:
    if mode == "relative":
        return max(sigma * (1.0 + dv), MIN_VOL)
    if mode == "flowdesk":
        dv *= math.sqrt(FLOWDESK_REF_DAYS / max(T * 365.25, 1.0))
    return max(sigma + dv, MIN_VOL)


def _bs_grid(S: np.ndarray, K: float, T: float, sigma: np.ndarray, opt: str) -> np.ndarray:
    """bs_price (r = 0) broadcast over a spot column x vol row."""
    if T <= 0:
        return np.maximum(S - K, 0.0) if opt == "C" else np.maximum(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sigma ** 2 * T) / sq
    d2 = d1 - sq
    if opt == "C":
        return S * ndtr(d1) - K * ndtr(d2)
    return K * ndtr(-d2) - S * ndtr(-d1)


def slides_grid(legs: list[dict], spot: float, collateral_qty: float,
                mode: str = "flowdesk") -> list[list[float]]:
    """P&L vs today for every (spot shock, vol shock): Σ legs repriced minus
    their value today, plus the collateral's own move. legs: net_qty, strike,
    opt, T (years), sigma (decimal). Perps are legs with opt "PERP"."""
    S = spot * (1.0 + np.asarray(SPOT_SHOCKS))[:, None]
    move = np.broadcast_to(S - spot, (len(SPOT_SHOCKS), len(VOL_SHOCKS)))
    pnl = collateral_qty * move
    for lg in legs:
        if lg["opt"] == "PERP":
            pnl = pnl + lg["net_qty"] * move
            continue
        base = bs_price(spot, lg["strike"], lg["T"], 0.0, lg["sigma"], lg["opt"])
        vols = np.array([_shocked_vol(lg["sigma"], dv, mode, lg["T"]) for dv in VOL_SHOCKS])[None, :]
        pnl = pnl + lg["net_qty"] * (_bs_grid(S, lg["strike"], lg["T"], vols, lg["opt"]) - base)
    return [[round(float(v), 0) for v in row] for row in pnl]


def optimizer_ladder(positions: list[dict], trades: list[dict], spot: float, asset: str,
                     collateral_before: dict[str, dict[str, float]],
                     collateral_after: dict[str, dict[str, float]] | None = None,
                     counterparties: list[str] | None = None) -> dict:
    """The Collateral Platform's ladder, before and after an optimizer run,
    per counterparty and for every vol mode.

    positions: the run's input book (/pnl positions plus the injected perps,
    opt "F"); trades: the run's proposed trades (signed qty, dte, iv_pct).
    A trade on a leg already held nets into it at the held leg's IV, so an
    unwind cancels exactly instead of leaving a residual from two marks.
    Collateral: this book's own-asset collateral per counterparty, after the
    proposed transfers when the run planned any; other assets don't move
    with this book's spot, so they don't change the P&L."""
    # cp (lower) -> leg key -> leg; display spelling from the first seen
    books: dict[str, dict[tuple, dict]] = {}
    names: dict[str, str] = {}

    def add(row: dict, qty: float, days: float, sides: tuple[str, ...]) -> None:
        cp, opt = str(row.get("counterparty") or "").strip(), str(row.get("opt") or "C")
        strike, iv_pct = float(row.get("strike") or 0.0), float(row.get("iv_pct") or 0.0)
        if not cp or not qty or (opt != "F" and days <= 0):
            return
        names.setdefault(cp.lower(), cp)
        opt = "PERP" if opt == "F" else opt
        key = ("PERP",) if opt == "PERP" else (str(row.get("expiry"))[:10], round(strike, 6), opt)
        lg = books.setdefault(cp.lower(), {}).setdefault(key, {
            "strike": float(strike), "opt": opt, "T": days / 365.25,
            "sigma": max(iv_pct / 100.0, MIN_VOL), "before": 0.0, "after": 0.0})
        for side in sides:
            lg[side] += qty

    for p in positions or []:
        add(p, float(p.get("net_qty") or 0.0), float(p.get("days_remaining") or 0.0), ("before", "after"))
    for t in trades or []:
        add(t, float(t.get("qty") or 0.0), float(t.get("dte") or 0.0), ("after",))

    def coll(src: dict | None, cp: str) -> float | None:
        for name, by_asset in (src or {}).items():
            if str(name).strip().lower() == cp:
                return float((by_asset or {}).get(asset, 0.0) or 0.0)
        return None

    for name in collateral_before or {}:
        names.setdefault(str(name).strip().lower(), str(name).strip())
    wanted = [c.strip().lower() for c in counterparties or [] if c and c.strip()]
    cps = [c for c in names if (c in wanted if wanted else (c in books or coll(collateral_before, c)))]
    if wanted:
        cps += [c for c in wanted if c not in cps]

    out = []
    for cp in sorted(cps, key=lambda c: names.get(c, c).lower()):
        legs = books.get(cp, {}).values()
        before_q = coll(collateral_before, cp) or 0.0
        after_q = coll(collateral_after, cp)
        after_q = before_q if after_q is None else after_q
        before = [{**lg, "net_qty": lg["before"]} for lg in legs if abs(lg["before"]) > 1e-9]
        after = [{**lg, "net_qty": lg["after"]} for lg in legs if abs(lg["after"]) > 1e-9]
        if not before and not after and not before_q and not after_q:
            continue
        out.append({
            "counterparty": names.get(cp, cp),
            "collateral_before": before_q,
            "collateral_after": after_q,
            "grids": {m: {"before": slides_grid(before, spot, before_q, m),
                          "after": slides_grid(after, spot, after_q, m)} for m in VOL_MODES},
        })
    return {
        "asset": asset,
        "spot": spot,
        "spot_shocks": SPOT_SHOCKS,
        "vol_shocks": VOL_SHOCKS,
        "spots": [spot * (1 + s) for s in SPOT_SHOCKS],
        "counterparties": out,
    }


def _bullets(asset: str, tot: dict, coll_qty: float, opt_delta: float,
             grid: list[list[float]], atm: tuple[str, float] | None) -> list[str]:
    def big(x: float) -> str:
        a = abs(x)
        if a >= 1e6:
            return f"{a / 1e6:.1f}m"
        if a >= 1e5:
            return f"{a / 1e3:.0f}k"
        return f"{a:,.0f}"

    def usd_k(x: float) -> str:
        a = abs(x)
        return f"${a / 1e6:.2f}m" if a >= 1e6 else f"${a / 1e3:.1f}k" if a >= 1e3 else f"${a:,.0f}"

    out = []
    d = tot["delta"]
    line = f"{'Long' if d >= 0 else 'Short'} {big(d)} {asset} delta"
    if coll_qty:
        line += f" (including the {big(coll_qty)} {asset} collateral)"
    out.append(line)
    if coll_qty and (opt_delta < 0) != (d < 0):
        out.append(f"Option positions on their own are {'short' if opt_delta < 0 else 'long'} "
                   f"{big(opt_delta)} {asset} delta")
    th = tot["theta"]
    if abs(th) >= 1:
        out.append(f"{'Paying' if th < 0 else 'Earning'} {usd_k(th)} decay a day")
    vg = tot["vega"]
    if abs(vg) >= 1:
        line = f"{'Long' if vg >= 0 else 'Short'} {usd_k(vg)} vega"
        if atm:
            line += f", with {atm[0]} ATM implied volatility at around {atm[1]:.0f}%"
        out.append(line)
    # Direction from the slides: spot +/-10% at unchanged vol, vol +/-5 at unchanged spot.
    i0, iu, idn = SPOT_SHOCKS.index(0.0), SPOT_SHOCKS.index(0.10), SPOT_SHOCKS.index(-0.10)
    j0, ju, jd = VOL_SHOCKS.index(0.0), VOL_SHOCKS.index(0.05), VOL_SHOCKS.index(-0.05)
    up_spot = grid[iu][j0] - grid[idn][j0]
    up_vol = grid[i0][ju] - grid[i0][jd]
    line = f"Make money on market {'up' if up_spot >= 0 else 'down'}"
    if abs(up_vol) >= 1:
        line += f" or vol {'up' if up_vol >= 0 else 'down'}"
    out.append(line)
    return out


async def _posted_collateral(counterparty: str | None) -> dict[str, float]:
    """Collateral posted per asset (all books), for one counterparty or all."""
    db = await get_db()
    cur = await db.execute(
        "SELECT counterparty, asset, SUM(qty) AS qty FROM counterparty_collateral GROUP BY counterparty, asset")
    rows = drop_collar_loans([dict(r) for r in await cur.fetchall()])
    out: dict[str, float] = {}
    for r in rows:
        if counterparty and str(r["counterparty"]).strip().lower() != counterparty.lower():
            continue
        a = str(r["asset"]).upper()
        out[a] = out.get(a, 0.0) + float(r["qty"] or 0.0)
    return {a: q for a, q in out.items() if q}


async def _asset_section(asset: str, counterparty: str | None, coll_qty: float,
                         mode: str = "flowdesk") -> dict | None:
    port = await _gather_portfolio(asset) or {}
    spot = float(port.get("eth_spot") or port.get("spot") or 0.0)
    if spot <= 0:
        # No open options in this book: still show the collateral at market.
        spot = float((await _effective_prices([asset]))[0].get(asset) or 0.0)
    if spot <= 0:
        return None
    today = date.today()
    rows, legs, vega_by_exp = [], [], {}
    cps = set()
    for p in port.get("positions") or []:
        cp = str(p.get("counterparty") or "").strip()
        if counterparty and cp.lower() != counterparty.lower():
            continue
        q = float(p.get("net_qty") or 0.0)
        dte = float(p.get("days_remaining") or 0.0)
        if not q or dte <= 0:
            continue
        cps.add(cp)
        opt = str(p.get("opt") or "C")
        k = float(p["strike"])
        d, g = float(p.get("delta") or 0.0) * q, float(p.get("gamma") or 0.0) * q
        th, vg = float(p.get("theta") or 0.0) * q, float(p.get("vega") or 0.0) * q
        rows.append({
            "kind": "option", "counterparty": cp, "size": q, "expiry": p.get("expiry"),
            "strike": k, "opt": opt, "symbol": _symbol(asset, p.get("expiry"), k, opt),
            "iv_pct": float(p.get("iv_pct") or 0.0), "mtm": float(p.get("current_mtm") or 0.0),
            "delta": d, "delta_usd": d * spot, "gamma": g, "gamma_usd": g * spot * spot / 100.0,
            "theta": th, "vega": vg,
        })
        legs.append({"net_qty": q, "strike": k, "opt": opt, "T": dte / 365.25,
                     "sigma": max(float(p.get("iv_pct") or 0.0) / 100.0, MIN_VOL)})
        vega_by_exp[str(p.get("expiry"))[:10]] = vega_by_exp.get(str(p.get("expiry"))[:10], 0.0) + abs(vg)
    for k, perp in _otc_perps(port).items():
        if counterparty and k != counterparty.lower():
            continue
        q = perp["net_qty"]
        rows.append({
            "kind": "perp", "counterparty": perp["venue"], "size": q, "symbol": f"{asset}-PERP ({perp['venue']})",
            "mtm": perp["mtm"], "delta": q, "delta_usd": q * spot, "gamma": 0.0, "gamma_usd": 0.0,
            "theta": 0.0, "vega": 0.0,
        })
        legs.append({"net_qty": q, "strike": 0.0, "opt": "PERP", "T": 0.0, "sigma": 0.0})
    rows.sort(key=lambda r: (r["kind"] != "option", str(r.get("expiry") or ""), r.get("strike") or 0, r.get("opt") or ""))
    if not rows and not coll_qty:
        return None

    opt_tot = {f: sum(r[f] for r in rows) for f in ("delta", "delta_usd", "gamma", "gamma_usd", "theta", "vega", "mtm")}
    if coll_qty:
        rows.insert(0, {"kind": "collateral", "size": coll_qty, "symbol": f"{asset} Spot Collateral",
                        "mtm": coll_qty * spot, "delta": coll_qty, "delta_usd": coll_qty * spot,
                        "gamma": 0.0, "gamma_usd": 0.0, "theta": 0.0, "vega": 0.0})
    tot = {f: sum(r[f] for r in rows if r["kind"] != "collateral") for f in opt_tot}
    tot["delta"] += coll_qty
    tot["delta_usd"] += coll_qty * spot

    atm = None
    if vega_by_exp:
        main = max(vega_by_exp, key=vega_by_exp.get)
        try:
            ctx = await build_market_context(asset)
            cp_for_iv = counterparty or (next(iter(cps)) if len(cps) == 1 else "")
            iv = _leg_iv(cp_for_iv, asset, main, spot, spot, ctx["smiles"], ctx["deribit_dates"], today)
            if iv:
                atm = (_expiry_label(main), float(iv))
        except Exception:
            atm = None

    grid = slides_grid(legs, spot, coll_qty, mode)
    return {
        "asset": asset,
        "spot": spot,
        "rows": rows,
        "totals": tot,
        "options_totals": opt_tot,
        "collateral_qty": coll_qty,
        "slides": {
            "spot_shocks": SPOT_SHOCKS,
            "vol_shocks": VOL_SHOCKS,
            "vol_mode": mode,
            "spots": [spot * (1 + s) for s in SPOT_SHOCKS],
            "pnl": grid,
        },
        "bullets": _bullets(asset, tot, coll_qty, opt_tot["delta"], grid, atm),
    }


@router.get("")
async def risk_report(counterparty: str = "ALL", asset: str = "ALL", vol_mode: str = "flowdesk"):
    """Positions + greeks, spot/vol slides and summary per asset, for one
    counterparty (or ALL), plus that counterparty's posted collateral."""
    cp = None if counterparty.strip().upper() in ("", "ALL") else counterparty.strip()
    assets = ASSETS if asset.upper() == "ALL" else (asset.upper(),)
    if any(a not in ASSETS for a in assets):
        raise HTTPException(status_code=400, detail=f"asset must be one of {ASSETS} or ALL")
    mode = vol_mode.lower()
    if mode not in VOL_MODES:
        raise HTTPException(status_code=400, detail=f"vol_mode must be one of {VOL_MODES}")

    posted = await _posted_collateral(cp)
    sections = []
    for a in assets:
        sec = await _asset_section(a, cp, posted.get(a, 0.0), mode)
        if sec:
            sections.append(sec)

    # Collateral summary: posted value (haircut) vs the options MtM with them.
    prices, _, _ = await _effective_prices(sorted(set(posted) | {"ETH", "FIL"}))
    coll_rows, coll_usd, coll_usd_hc = [], 0.0, 0.0
    for a, q in sorted(posted.items()):
        px = 1.0 if a in ("USD", "USDC") else float(prices.get(a, 0.0))
        rate = effective_haircut_rate(cp, a, q) if cp else 0.0
        usd = q * px
        coll_rows.append({"asset": a, "qty": q, "price": px, "usd": usd, "haircut": rate, "usd_haircut": usd * (1 - rate)})
        coll_usd += usd
        coll_usd_hc += usd * (1 - rate)
    mtm = sum(s["options_totals"]["mtm"] for s in sections)
    return {
        "counterparty": cp or "ALL",
        "as_of": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sections": sections,
        "collateral": {
            "rows": coll_rows,
            "posted_usd": coll_usd,
            "posted_usd_haircut": coll_usd_hc,
            "options_mtm": mtm,
            # Netted balance: collateral after haircut plus our MtM with them
            # (negative MtM is what they hold the collateral against).
            "balance": coll_usd_hc + mtm,
        },
    }


@router.get("/counterparties")
async def risk_report_counterparties():
    """Counterparties with open option legs or posted collateral."""
    names: dict[str, str] = {}
    for a in ASSETS:
        port = await _gather_portfolio(a)
        for p in (port or {}).get("positions") or []:
            cp = str(p.get("counterparty") or "").strip()
            if cp and float(p.get("days_remaining") or 0) > 0:
                names.setdefault(cp.lower(), cp)
    posted = await get_db()
    cur = await posted.execute("SELECT DISTINCT counterparty FROM counterparty_collateral WHERE qty != 0")
    for r in drop_collar_loans([dict(r) for r in await cur.fetchall()]):
        cp = str(r["counterparty"]).strip()
        names.setdefault(cp.lower(), cp)
    return sorted(names.values(), key=str.lower)


class NotesUpdate(BaseModel):
    counterparty: str
    notes: str


@router.get("/notes")
async def get_notes(counterparty: str):
    """The saved notes for a counterparty ('' when none yet)."""
    db = await get_db()
    cur = await db.execute(
        "SELECT notes, updated_by, updated_at FROM counterparty_notes WHERE counterparty = ?",
        (counterparty.strip(),))
    row = await cur.fetchone()
    if not row:
        return {"counterparty": counterparty.strip(), "notes": "", "updated_by": "", "updated_at": None}
    return {"counterparty": counterparty.strip(), **dict(row)}


@router.put("/notes")
async def put_notes(payload: NotesUpdate, request: Request):
    """Save a counterparty's notes, stamped with who saved them and when."""
    cp = payload.counterparty.strip()
    if not cp or cp.upper() == "ALL":
        raise HTTPException(status_code=400, detail="Pick a counterparty")
    if len(payload.notes) > 20000:
        raise HTTPException(status_code=400, detail="Notes are limited to 20,000 characters")
    user = (request.session.get("user") or {}) if "session" in request.scope else {}
    who = str(user.get("email") or "")
    db = await get_db()
    await db.execute(
        """INSERT INTO counterparty_notes (counterparty, notes, updated_by, updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(counterparty) DO UPDATE SET
             notes = excluded.notes, updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
        (cp, payload.notes, who))
    await db.commit()
    return await get_notes(cp)
