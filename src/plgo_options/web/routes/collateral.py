"""Counterparty collateral & margin endpoints.

Data model — one row per (counterparty, portfolio_asset):

    counterparty_margin (
        counterparty,          -- "FlowDesk", "KeyRock", ...
        portfolio_asset,       -- ETH or FIL — which options book this backs
        eth_qty,               -- ETH posted as collateral against this book
        fil_qty,               -- FIL posted as collateral against this book
        requested_usd,         -- USD amount the counterparty themselves ask for
        notes,
        updated_at
    )

Liability per row = Σ max(0, −MtM_usd) across the counterparty's open
positions in `portfolio_asset` options (BS MtM, matches Portfolio P&L page).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from plgo_options.data.database import get_db
from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.web.routes.portfolio import portfolio_pnl

_client = DeribitClient()

router = APIRouter()

# Haircuts: ETH counted at 90% of spot, FIL at 50%.
HAIRCUTS = {"ETH": 0.10, "FIL": 0.50}
TARGET_MARGIN = 1.00


async def _gather_portfolio(asset: str) -> dict | None:
    try:
        return await portfolio_pnl(asset=asset, include_expired=False)
    except (HTTPException, StarletteHTTPException) as e:
        if getattr(e, "status_code", 500) in (404, 502):
            return None
        raise


def _haircut_value(eth_qty: float, fil_qty: float, eth_spot: float, fil_spot: float) -> tuple[float, float]:
    """Return (no_haircut_usd, haircut_usd)."""
    nh = eth_qty * eth_spot + fil_qty * fil_spot
    hc = (
        eth_qty * eth_spot * (1 - HAIRCUTS["ETH"])
        + fil_qty * fil_spot * (1 - HAIRCUTS["FIL"])
    )
    return nh, hc


async def _load_margin_rows() -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT counterparty, portfolio_asset, eth_qty, fil_qty, requested_usd, notes, updated_at
           FROM counterparty_margin"""
    )
    rows = await cursor.fetchall()
    return [
        {
            "counterparty": r["counterparty"],
            "portfolio_asset": (r["portfolio_asset"] or "").upper(),
            "eth_qty": float(r["eth_qty"] or 0),
            "fil_qty": float(r["fil_qty"] or 0),
            "requested_usd": (None if r["requested_usd"] is None else float(r["requested_usd"])),
            "notes": r["notes"] or "",
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def _compute_liabilities(data: dict | None) -> tuple[dict[str, float], dict[str, str], dict[str, int]]:
    """For a single-asset portfolio response, return:
       liabilities[cp_lower], display_names[cp_lower], position_count[cp_lower].
    """
    liabilities: dict[str, float] = {}
    display: dict[str, str] = {}
    counts: dict[str, int] = {}
    if not data:
        return liabilities, display, counts
    for p in data.get("positions", []):
        cp = (p.get("counterparty") or "").strip()
        if not cp:
            continue
        k = cp.lower()
        display.setdefault(k, cp)
        mtm = float(p.get("current_mtm") or 0)
        if mtm < 0:
            liabilities[k] = liabilities.get(k, 0.0) + (-mtm)
        counts[k] = counts.get(k, 0) + 1
    return liabilities, display, counts


@router.get("/summary")
async def collateral_summary(asset: str = "all"):
    """Per-(counterparty, portfolio_asset) margin breakdown.

    Query params:
        asset: "all" | "ETH" | "FIL" — filter to a single options book or both.

    Each returned row is one counterparty × portfolio_asset bucket with:
        liability_usd          — Σ max(0,−MtM) for that counterparty in that book
        eth_qty, fil_qty       — collateral posted against that book
        collateral_usd_*       — USD value with/without haircuts
        margin_ratio_*         — collateral / liability (None if liability == 0)
        requested_usd          — what the counterparty says we owe
        diff_usd               — ours_liability − requested  (positive = we owe more than they're asking; negative = they're calling too much)
        shortfall_*            — max(0, liability − collateral)
    """
    asset_filter = (asset or "all").strip().lower()
    want_eth = asset_filter in ("all", "eth")
    want_fil = asset_filter in ("all", "fil")
    if not (want_eth or want_fil):
        raise HTTPException(status_code=400, detail="asset must be one of: all, ETH, FIL")

    # Always fetch both books for display-name stability (case differences across
    # ETH "FlowDesk" vs FIL "Flowdesk"). The asset filter only controls which
    # rows we *return*, not which we look up names from.
    eth_data = await _gather_portfolio("ETH")
    fil_data = await _gather_portfolio("FIL")

    if want_eth and want_fil and not eth_data and not fil_data:
        raise HTTPException(status_code=404, detail="No trades found for either asset")

    spots: dict[str, float] = {}
    if eth_data:
        spots["ETH"] = float(eth_data.get("eth_spot") or 0)
    if fil_data:
        spots["FIL"] = float(fil_data.get("eth_spot") or 0)  # field name is misleading; it's the asset spot

    eth_liab, eth_disp, eth_count = _compute_liabilities(eth_data)
    fil_liab, fil_disp, fil_count = _compute_liabilities(fil_data)

    # Canonical display name: ETH casing wins over FIL casing (alphabetical
    # tiebreak when neither has been seen).
    all_display: dict[str, str] = {}
    for k, n in eth_disp.items(): all_display.setdefault(k, n)
    for k, n in fil_disp.items(): all_display.setdefault(k, n)

    # Pull stored margin rows and merge in any counterparties we know about from the DB.
    margin_rows = await _load_margin_rows()
    by_key: dict[tuple[str, str], dict] = {
        (m["counterparty"].lower(), m["portfolio_asset"]): m
        for m in margin_rows
    }
    for m in margin_rows:
        all_display.setdefault(m["counterparty"].lower(), m["counterparty"])

    eth_spot = spots.get("ETH", 0.0)
    fil_spot = spots.get("FIL", 0.0)

    # Build per-(counterparty, portfolio_asset) rows.
    out_rows: list[dict] = []
    for k in sorted(all_display.keys()):
        cp = all_display[k]
        portfolios: list[str] = []
        if want_eth:
            portfolios.append("ETH")
        if want_fil:
            portfolios.append("FIL")

        for pa in portfolios:
            if pa == "ETH":
                liability = eth_liab.get(k, 0.0)
                pos_count = eth_count.get(k, 0)
            else:
                liability = fil_liab.get(k, 0.0)
                pos_count = fil_count.get(k, 0)

            stored = by_key.get((k, pa))
            eth_qty = float(stored["eth_qty"]) if stored else 0.0
            fil_qty = float(stored["fil_qty"]) if stored else 0.0
            requested = (stored["requested_usd"] if stored else None)
            notes = (stored["notes"] if stored else "")
            updated_at = (stored["updated_at"] if stored else None)

            # Skip pure-empty rows when filter is "all" and there's nothing here.
            if pos_count == 0 and eth_qty == 0 and fil_qty == 0 and not requested:
                continue

            nh, hc = _haircut_value(eth_qty, fil_qty, eth_spot, fil_spot)
            ratio_nh = (nh / liability) if liability > 0 else None
            ratio_hc = (hc / liability) if liability > 0 else None

            diff_usd = None if requested is None else round(liability - float(requested), 2)

            out_rows.append({
                "counterparty": cp,
                "portfolio_asset": pa,
                "position_count": pos_count,
                "liability_usd": round(liability, 2),
                "eth_qty": round(eth_qty, 4),
                "fil_qty": round(fil_qty, 4),
                "collateral_usd_no_haircut": round(nh, 2),
                "collateral_usd_haircut": round(hc, 2),
                "margin_ratio_no_haircut": ratio_nh,
                "margin_ratio_haircut": ratio_hc,
                "requested_usd": (None if requested is None else round(float(requested), 2)),
                "diff_usd": diff_usd,
                "notes": notes,
                "shortfall_no_haircut": round(max(0.0, liability - nh), 2),
                "shortfall_haircut": round(max(0.0, liability - hc), 2),
                "updated_at": updated_at,
            })

    # Portfolio totals across filtered rows.
    def _sum(field: str) -> float:
        return sum((r[field] or 0) for r in out_rows)

    total_liab = _sum("liability_usd")
    total_nh = _sum("collateral_usd_no_haircut")
    total_hc = _sum("collateral_usd_haircut")
    total_req = sum((r["requested_usd"] or 0) for r in out_rows if r["requested_usd"] is not None)
    any_request = any(r["requested_usd"] is not None for r in out_rows)

    portfolio = {
        "liability_usd": round(total_liab, 2),
        "eth_qty": round(_sum("eth_qty"), 4),
        "fil_qty": round(_sum("fil_qty"), 4),
        "collateral_usd_no_haircut": round(total_nh, 2),
        "collateral_usd_haircut": round(total_hc, 2),
        "margin_ratio_no_haircut": (total_nh / total_liab) if total_liab > 0 else None,
        "margin_ratio_haircut": (total_hc / total_liab) if total_liab > 0 else None,
        "shortfall_no_haircut": round(max(0.0, total_liab - total_nh), 2),
        "shortfall_haircut": round(max(0.0, total_liab - total_hc), 2),
        "requested_usd": round(total_req, 2) if any_request else None,
        "diff_usd": round(total_liab - total_req, 2) if any_request else None,
    }

    return {
        "asset_filter": "ETH" if asset_filter == "eth" else "FIL" if asset_filter == "fil" else "all",
        "spots": spots,
        "haircuts": HAIRCUTS,
        "target_margin": TARGET_MARGIN,
        "rows": out_rows,
        "portfolio": portfolio,
    }


class MarginUpdate(BaseModel):
    counterparty: str
    portfolio_asset: str
    eth_qty: float = 0.0
    fil_qty: float = 0.0
    requested_usd: float | None = None
    notes: str | None = None


@router.put("/margin")
async def upsert_margin(payload: MarginUpdate):
    cp = payload.counterparty.strip()
    pa = payload.portfolio_asset.strip().upper()
    if not cp:
        raise HTTPException(status_code=400, detail="counterparty required")
    if pa not in ("ETH", "FIL"):
        raise HTTPException(status_code=400, detail="portfolio_asset must be ETH or FIL")
    if payload.eth_qty < 0 or payload.fil_qty < 0:
        raise HTTPException(status_code=400, detail="qty must be >= 0")
    if payload.requested_usd is not None and payload.requested_usd < 0:
        raise HTTPException(status_code=400, detail="requested_usd must be >= 0")

    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO counterparty_margin
              (counterparty, portfolio_asset, eth_qty, fil_qty, requested_usd, notes, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(counterparty, portfolio_asset) DO UPDATE SET
              eth_qty = excluded.eth_qty,
              fil_qty = excluded.fil_qty,
              requested_usd = excluded.requested_usd,
              notes = excluded.notes,
              updated_at = excluded.updated_at""",
        (
            cp, pa,
            float(payload.eth_qty),
            float(payload.fil_qty),
            (None if payload.requested_usd is None else float(payload.requested_usd)),
            payload.notes,
            now,
        ),
    )
    await db.commit()
    return {
        "counterparty": cp,
        "portfolio_asset": pa,
        "eth_qty": float(payload.eth_qty),
        "fil_qty": float(payload.fil_qty),
        "requested_usd": payload.requested_usd,
        "notes": payload.notes,
        "updated_at": now,
    }


@router.get("/scenario")
async def collateral_scenario(asset: str = "ETH"):
    """Spot-ladder stress view for a single options book.

    For each spot price in the asset's ladder, repricing positions via BS at
    that spot (horizon=0, IV frozen at today's), returns:
        liability_usd            — Σ max(0, −MtM) across positions in this book
        collateral_usd_haircut   — collateral value at this scenario spot, w/ haircuts
        collateral_usd_no_haircut
        residual_haircut         — collateral − liability  (positive = surplus)
        margin_ratio_haircut

    The *other* asset's collateral (e.g., FIL holdings against the ETH book) is
    valued at its current spot — only the laddered asset's price moves.
    """
    a = (asset or "").strip().upper()
    if a not in ("ETH", "FIL"):
        raise HTTPException(status_code=400, detail="asset must be ETH or FIL")

    data = await _gather_portfolio(a)
    if not data:
        raise HTTPException(status_code=404, detail=f"No {a} trades")

    current_spot = float(data.get("eth_spot") or 0)  # field misleadingly named in portfolio_pnl
    ladder: list[float] = list(data.get("spot_ladder") or [])
    positions: list[dict] = data.get("positions") or []

    if not ladder:
        raise HTTPException(status_code=502, detail="No spot ladder from portfolio engine")

    # Fetch the *other* asset's current spot for fixed-side collateral valuation.
    other_spot = 0.0
    try:
        if a == "ETH":
            other_spot = float(await _client.get_fil_spot_price())
        else:
            other_spot = float(await _client.get_eth_spot_price())
    except Exception:
        other_spot = 0.0

    # Load collateral rows for this book only.
    margin_rows = await _load_margin_rows()
    rows_for_book = [m for m in margin_rows if m["portfolio_asset"] == a]
    total_eth_qty = sum(m["eth_qty"] for m in rows_for_book)
    total_fil_qty = sum(m["fil_qty"] for m in rows_for_book)

    # For each spot in the ladder, sum liability and value the collateral.
    scenarios = []
    for i, s in enumerate(ladder):
        # Liability at this spot = Σ max(0, −signed_qty × value) across positions.
        # portfolio_pnl already returns payoff_by_horizon["0"] = signed_qty × value
        # at each spot in the ladder.
        liability = 0.0
        for p in positions:
            mtm_arr = (p.get("payoff_by_horizon") or {}).get("0") or []
            if i < len(mtm_arr):
                mtm = float(mtm_arr[i])
                if mtm < 0:
                    liability += -mtm

        eth_px = s if a == "ETH" else other_spot
        fil_px = s if a == "FIL" else other_spot
        nh, hc = _haircut_value(total_eth_qty, total_fil_qty, eth_px, fil_px)

        residual_nh = nh - liability
        residual_hc = hc - liability
        ratio_nh = (nh / liability) if liability > 0 else None
        ratio_hc = (hc / liability) if liability > 0 else None

        scenarios.append({
            "spot": round(float(s), 4),
            "spot_pct": (round((s / current_spot - 1) * 100, 2) if current_spot > 0 else 0.0),
            "liability_usd": round(liability, 2),
            "collateral_usd_no_haircut": round(nh, 2),
            "collateral_usd_haircut": round(hc, 2),
            "residual_no_haircut": round(residual_nh, 2),
            "residual_haircut": round(residual_hc, 2),
            "margin_ratio_no_haircut": ratio_nh,
            "margin_ratio_haircut": ratio_hc,
        })

    return {
        "asset": a,
        "current_spot": current_spot,
        "other_asset": "FIL" if a == "ETH" else "ETH",
        "other_spot": other_spot,
        "haircuts": HAIRCUTS,
        "total_eth_qty": round(total_eth_qty, 4),
        "total_fil_qty": round(total_fil_qty, 4),
        "ladder": scenarios,
    }


@router.delete("/margin")
async def delete_margin(counterparty: str, portfolio_asset: str):
    cp = counterparty.strip()
    pa = portfolio_asset.strip().upper()
    if not cp or pa not in ("ETH", "FIL"):
        raise HTTPException(status_code=400, detail="counterparty and portfolio_asset (ETH|FIL) required")
    db = await get_db()
    await db.execute(
        "DELETE FROM counterparty_margin WHERE counterparty = ? COLLATE NOCASE AND portfolio_asset = ? COLLATE NOCASE",
        (cp, pa),
    )
    await db.commit()
    return {"ok": True}
