"""Counterparty reconciliation.

A counterparty sends us their record of the trades they executed with us (one row
per option leg) plus the collateral they hold for us (ETH/FIL/USD/USDC). We match
that against our own open trades and collateral records, surface every discrepancy,
and return a report.

`/run` is READ-ONLY — it computes a diff and never touches the DB. Applying the
add/remove decisions is done by the frontend through the existing audited
`/api/trades` endpoints, so every change keeps its audit-log trail.
"""
from __future__ import annotations

from datetime import datetime, date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades

router = APIRouter()

COLLATERAL_ASSETS = ["ETH", "FIL", "USD", "USDC"]
QTY_TOL = 1.0       # absolute quantity tolerance for a "match"
COLLAT_TOL = 1e-6   # collateral tolerance


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class TheirTrade(BaseModel):
    trade_id: str = ""
    trade_date: str = ""
    side: str = ""           # Buy / Sell (the side PLGO took)
    option_type: str = ""    # Call / Put (or C / P)
    strike: float = 0.0
    expiry: str = ""         # ISO date or DDMMMYY
    qty: float = 0.0
    premium_usd: float = 0.0
    instrument: str = ""


class TheirCollateral(BaseModel):
    asset: str
    qty: float = 0.0


class ReconRequest(BaseModel):
    asset: str = "ETH"
    counterparty: str
    their_trades: list[TheirTrade] = []
    their_collateral: list[TheirCollateral] = []


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def _norm_opt(v) -> str:
    return "C" if str(v or "").strip().lower().startswith("c") else "P"


def _side_sign(v) -> float:
    return 1.0 if str(v or "").strip().lower() in ("buy", "long", "b", "+", "1") else -1.0


def _norm_expiry(v) -> str:
    """Return an ISO 'YYYY-MM-DD' string from many input formats, or '' if unparseable."""
    s = str(v or "").strip()
    if not s:
        return ""
    if "T" in s:
        s = s.split("T")[0]
    fmts = ["%Y-%m-%d", "%d%b%y", "%d-%b-%y", "%d%b%Y", "%d-%b-%Y",
            "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]
    for f in fmts:
        for cand in (s, s.upper()):
            try:
                return datetime.strptime(cand, f).date().isoformat()
            except ValueError:
                continue
    return ""


def _ddmmmyy(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return ""
    return f"{d.day}{d.strftime('%b').upper()}{d.strftime('%y')}"


def _key(opt: str, strike: float, expiry_iso: str) -> tuple:
    return (opt, round(float(strike), 6), expiry_iso)


def _aggregate(trades, getter) -> dict:
    """Group trades by (opt, strike, expiry) → signed net qty, ids, premium, rows."""
    book: dict[tuple, dict] = {}
    for t in trades:
        opt = _norm_opt(getter(t, "option_type"))
        try:
            strike = float(getter(t, "strike") or 0)
        except (TypeError, ValueError):
            strike = 0.0
        expiry = _norm_expiry(getter(t, "expiry"))
        if strike <= 0 or not expiry:
            continue
        sign = _side_sign(getter(t, "side"))
        try:
            qty = float(getter(t, "qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            prem = float(getter(t, "premium_usd") or 0)
        except (TypeError, ValueError):
            prem = 0.0
        k = _key(opt, strike, expiry)
        e = book.setdefault(k, {"opt": opt, "strike": strike, "expiry": expiry,
                                "net": 0.0, "prem": 0.0, "ids": [], "rows": []})
        e["net"] += sign * qty
        e["prem"] += prem
        if getter(t, "id") is not None:
            e["ids"].append(getter(t, "id"))
        e["rows"].append(t)
    return book


def _dict_get(d, k):
    return d.get(k)


def _model_get(m, k):
    return getattr(m, k, None)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post("/run")
async def run_reconciliation(req: ReconRequest):
    asset = (req.asset or "ETH").strip().upper()
    cp = req.counterparty.strip()
    if not cp:
        raise HTTPException(400, "counterparty is required")

    # 1. Our open trades for this counterparty + asset.
    try:
        db = await get_db()
        all_trades = await list_trades(db, include_expired=False, include_deleted=False, asset=asset)
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")
    our_trades = [t for t in all_trades if str(t.get("counterparty", "")).strip().lower() == cp.lower()]

    our_book = _aggregate(our_trades, _dict_get)
    their_book = _aggregate(req.their_trades, _model_get)

    # 2. Diff trades.
    trade_results = []
    counts = {"match": 0, "qty_mismatch": 0, "only_ours": 0, "only_theirs": 0}
    for k in sorted(set(our_book) | set(their_book), key=lambda x: (x[2], x[0], x[1])):
        ours = our_book.get(k)
        theirs = their_book.get(k)
        opt = (ours or theirs)["opt"]
        strike = (ours or theirs)["strike"]
        expiry = (ours or theirs)["expiry"]
        our_net = ours["net"] if ours else 0.0
        their_net = theirs["net"] if theirs else 0.0

        if ours and theirs:
            status = "match" if abs(our_net - their_net) <= QTY_TOL else "qty_mismatch"
        elif ours:
            status = "only_ours"
        else:
            status = "only_theirs"
        counts[status] += 1

        # Suggested trade to ADD when the leg is only on their side.
        suggested = None
        if status == "only_theirs":
            side = "Buy" if their_net > 0 else "Sell"
            suggested = {
                "asset": asset, "counterparty": cp,
                "side": side, "option_type": "Call" if opt == "C" else "Put",
                "strike": strike, "expiry": expiry, "qty": abs(their_net),
                "premium_usd": round(theirs["prem"], 2),
                "instrument": f"{asset}-{_ddmmmyy(expiry)}-{strike:g}-{opt}",
                "trade_id": (theirs["rows"][0].trade_id if theirs["rows"] else ""),
                "trade_date": (theirs["rows"][0].trade_date if theirs["rows"] else ""),
            }

        trade_results.append({
            "status": status,
            "opt": opt, "type": "Call" if opt == "C" else "Put",
            "strike": strike, "expiry": expiry,
            "our_net": round(our_net, 2), "their_net": round(their_net, 2),
            "qty_diff": round(our_net - their_net, 2),
            "our_premium": round(ours["prem"], 2) if ours else None,
            "their_premium": round(theirs["prem"], 2) if theirs else None,
            "our_ids": ours["ids"] if ours else [],
            "suggested_add": suggested,
        })

    # 3. Collateral: our records vs theirs.
    our_collat: dict[str, float] = {}
    try:
        # Sum across books (ETH/FIL) — reconciliation compares total posted.
        cur = await db.execute(
            "SELECT asset, SUM(qty) AS qty FROM counterparty_collateral WHERE counterparty = ? COLLATE NOCASE GROUP BY asset",
            (cp,),
        )
        for row in await cur.fetchall():
            our_collat[str(row["asset"]).upper()] = float(row["qty"] or 0)
    except Exception:
        our_collat = {}  # table may not exist yet — treat as no record

    their_collat = {c.asset.strip().upper(): float(c.qty or 0) for c in req.their_collateral if c.asset.strip()}

    collat_results = []
    collat_mismatch = 0
    for a in COLLATERAL_ASSETS + [x for x in (set(our_collat) | set(their_collat)) if x not in COLLATERAL_ASSETS]:
        oq = our_collat.get(a)
        tq = their_collat.get(a)
        if oq is None and tq is None:
            continue
        diff = (oq or 0.0) - (tq or 0.0)
        match = abs(diff) <= COLLAT_TOL
        if not match:
            collat_mismatch += 1
        collat_results.append({
            "asset": a,
            "our_qty": oq, "their_qty": tq,
            "diff": round(diff, 6), "match": match,
        })

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    report_md = _build_report(asset, cp, generated_at, counts, trade_results,
                              collat_results, collat_mismatch, len(our_trades), len(req.their_trades))

    return {
        "asset": asset, "counterparty": cp, "generated_at": generated_at,
        "summary": {**counts, "collateral_mismatch": collat_mismatch,
                    "our_trade_count": len(our_trades), "their_trade_count": len(req.their_trades)},
        "trades": trade_results,
        "collateral": collat_results,
        "report_md": report_md,
    }


def _build_report(asset, cp, generated_at, counts, trades, collat, collat_mismatch,
                  our_n, their_n) -> str:
    L = []
    L.append(f"# Reconciliation Report — {cp} ({asset})")
    L.append("")
    L.append(f"_Generated {generated_at}_")
    L.append("")
    total_disc = counts["qty_mismatch"] + counts["only_ours"] + counts["only_theirs"] + collat_mismatch
    verdict = "OK — fully reconciled" if total_disc == 0 else f"WARNING — {total_disc} discrepancy(ies) found"
    L.append(f"**{verdict}**")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(f"- Our open legs: **{our_n}** | Their reported legs: **{their_n}**")
    L.append(f"- Matched: **{counts['match']}**")
    L.append(f"- Quantity mismatches: **{counts['qty_mismatch']}**")
    L.append(f"- Only in our book (they did not report): **{counts['only_ours']}**")
    L.append(f"- Only in their book (missing from ours): **{counts['only_theirs']}**")
    L.append(f"- Collateral mismatches: **{collat_mismatch}**")
    L.append("")
    L.append("## Trade reconciliation")
    L.append("")
    L.append("| Status | Type | Strike | Expiry | Our net qty | Their net qty | Diff |")
    L.append("|---|---|---|---|---|---|---|")
    label = {"match": "OK", "qty_mismatch": "QTY MISMATCH",
             "only_ours": "ONLY OURS", "only_theirs": "MISSING (add?)"}
    for t in trades:
        L.append(f"| {label[t['status']]} | {t['type']} | {t['strike']:g} | {t['expiry']} "
                 f"| {t['our_net']:,.0f} | {t['their_net']:,.0f} | {t['qty_diff']:,.0f} |")
    L.append("")
    L.append("## Collateral reconciliation")
    L.append("")
    L.append("| Asset | Our record | Their record | Diff |")
    L.append("|---|---|---|---|")
    for c in collat:
        oq = "—" if c["our_qty"] is None else f"{c['our_qty']:,.4f}"
        tq = "—" if c["their_qty"] is None else f"{c['their_qty']:,.4f}"
        flag = "" if c["match"] else "  <-- mismatch"
        L.append(f"| {c['asset']} | {oq} | {tq} | {c['diff']:,.4f}{flag} |")
    L.append("")
    return "\n".join(L)
