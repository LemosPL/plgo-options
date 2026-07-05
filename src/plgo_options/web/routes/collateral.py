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

# Haircuts applied to posted collateral. ETH counted at 90% of spot, FIL at
# 50%, BTC at 85%, WAVE at 50% (illiquid), USD/USDC at face (no haircut).
HAIRCUTS = {"ETH": 0.10, "FIL": 0.50, "BTC": 0.15, "WAVE": 0.50, "USDC": 0.0}
TARGET_MARGIN = 1.00

# Collateral asset legs (other than USDC, which is always face value).
_TOKEN_ASSETS = ("ETH", "FIL", "BTC", "WAVE")


async def _gather_portfolio(asset: str) -> dict | None:
    try:
        return await portfolio_pnl(asset=asset, include_expired=False)
    except (HTTPException, StarletteHTTPException) as e:
        if getattr(e, "status_code", 500) in (404, 502):
            return None
        raise


def _haircut_value(qtys: dict[str, float], prices: dict[str, float]) -> tuple[float, float]:
    """Value a bundle of collateral in USD.

    qtys keyed by asset (ETH/FIL/BTC/WAVE/USDC). prices is USD per unit for
    each token (USDC is always 1.0). Returns (no_haircut_usd, haircut_usd).
    """
    nh = hc = 0.0
    for asset, qty in qtys.items():
        if not qty:
            continue
        px = 1.0 if asset == "USDC" else prices.get(asset, 0.0)
        usd = qty * px
        nh += usd
        hc += usd * (1 - HAIRCUTS.get(asset, 0.0))
    return nh, hc


def _row_qtys(row: dict) -> dict[str, float]:
    """Collateral quantities posted on a single margin row."""
    return {
        "ETH": float(row.get("eth_qty") or 0),
        "FIL": float(row.get("fil_qty") or 0),
        "BTC": float(row.get("btc_qty") or 0),
        "WAVE": float(row.get("wave_qty") or 0),
        "USDC": float(row.get("usdc_usd") or 0),
    }


async def _load_margin_rows() -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        """SELECT counterparty, portfolio_asset, eth_qty, fil_qty, usdc_usd,
                  btc_qty, wave_qty, wave_price, margin_req_tokens,
                  requested_usd, notes, updated_at
           FROM counterparty_margin"""
    )
    rows = await cursor.fetchall()
    return [
        {
            "counterparty": r["counterparty"],
            "portfolio_asset": (r["portfolio_asset"] or "").upper(),
            "eth_qty": float(r["eth_qty"] or 0),
            "fil_qty": float(r["fil_qty"] or 0),
            "usdc_usd": float(r["usdc_usd"] or 0),
            "btc_qty": float(r["btc_qty"] or 0),
            "wave_qty": float(r["wave_qty"] or 0),
            "wave_price": float(r["wave_price"] or 0),
            "margin_req_tokens": float(r["margin_req_tokens"] or 0),
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

    # Collateral can be posted in an asset that has no live option book (e.g. a
    # FIL-only portfolio still holding ETH as collateral). Trade data won't carry
    # that asset's spot, so fall back to a direct live fetch — otherwise that
    # collateral would silently value at $0. Mirrors the BTC block below.
    if not spots.get("ETH") and any(m["eth_qty"] for m in margin_rows):
        try:
            spots["ETH"] = float(await _client.get_eth_spot_price())
        except Exception:
            pass
    if not spots.get("FIL") and any(m["fil_qty"] for m in margin_rows):
        try:
            spots["FIL"] = float(await _client.get_fil_spot_price())
        except Exception:
            pass

    eth_spot = spots.get("ETH", 0.0)
    fil_spot = spots.get("FIL", 0.0)

    # BTC spot for any BTC collateral (WAVE is valued at its stored manual price).
    btc_spot = 0.0
    if any(m["btc_qty"] for m in margin_rows):
        try:
            btc_spot = float(await _client.get_btc_spot_price())
        except Exception:
            btc_spot = 0.0
    if btc_spot:
        spots["BTC"] = btc_spot

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
            qtys = _row_qtys(stored) if stored else _row_qtys({})
            wave_price = float(stored["wave_price"]) if stored else 0.0
            margin_req_tokens = float(stored["margin_req_tokens"]) if stored else 0.0
            requested = (stored["requested_usd"] if stored else None)
            notes = (stored["notes"] if stored else "")
            updated_at = (stored["updated_at"] if stored else None)

            # Skip pure-empty rows when filter is "all" and there's nothing here.
            if (pos_count == 0 and not any(qtys.values())
                    and not requested and margin_req_tokens == 0):
                continue

            prices = {"ETH": eth_spot, "FIL": fil_spot, "BTC": btc_spot, "WAVE": wave_price}
            nh, hc = _haircut_value(qtys, prices)
            ratio_nh = (nh / liability) if liability > 0 else None
            ratio_hc = (hc / liability) if liability > 0 else None

            # Counterparty's margin requirement, in the book's native token,
            # valued at that token's spot.
            book_spot = eth_spot if pa == "ETH" else fil_spot
            margin_req_usd = margin_req_tokens * book_spot

            # Balance (surplus) / debit (to post): post-haircut collateral minus
            # our MtM liability. Positive = balance; negative = we owe more.
            balance_usd = hc - liability

            diff_usd = None if requested is None else round(liability - float(requested), 2)

            out_rows.append({
                "counterparty": cp,
                "portfolio_asset": pa,
                "position_count": pos_count,
                "liability_usd": round(liability, 2),
                "eth_qty": round(qtys["ETH"], 4),
                "fil_qty": round(qtys["FIL"], 4),
                "btc_qty": round(qtys["BTC"], 6),
                "wave_qty": round(qtys["WAVE"], 4),
                "wave_price": round(wave_price, 6),
                "usdc_usd": round(qtys["USDC"], 2),
                "collateral_usd_no_haircut": round(nh, 2),
                "collateral_usd_haircut": round(hc, 2),
                "margin_ratio_no_haircut": ratio_nh,
                "margin_ratio_haircut": ratio_hc,
                "margin_req_tokens": round(margin_req_tokens, 4),
                "margin_req_usd": round(margin_req_usd, 2),
                "requested_usd": (None if requested is None else round(float(requested), 2)),
                "diff_usd": diff_usd,
                "notes": notes,
                "balance_usd": round(balance_usd, 2),
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
        "btc_qty": round(_sum("btc_qty"), 6),
        "wave_qty": round(_sum("wave_qty"), 4),
        "usdc_usd": round(_sum("usdc_usd"), 2),
        "collateral_usd_no_haircut": round(total_nh, 2),
        "collateral_usd_haircut": round(total_hc, 2),
        "margin_ratio_no_haircut": (total_nh / total_liab) if total_liab > 0 else None,
        "margin_ratio_haircut": (total_hc / total_liab) if total_liab > 0 else None,
        "margin_req_usd": round(_sum("margin_req_usd"), 2),
        "balance_usd": round(_sum("balance_usd"), 2),
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
    usdc_usd: float = 0.0
    btc_qty: float = 0.0
    wave_qty: float = 0.0
    wave_price: float = 0.0
    margin_req_tokens: float = 0.0
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
    if min(payload.eth_qty, payload.fil_qty, payload.usdc_usd,
           payload.btc_qty, payload.wave_qty, payload.wave_price,
           payload.margin_req_tokens) < 0:
        raise HTTPException(status_code=400, detail="quantities must be >= 0")
    if payload.requested_usd is not None and payload.requested_usd < 0:
        raise HTTPException(status_code=400, detail="requested_usd must be >= 0")

    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO counterparty_margin
              (counterparty, portfolio_asset, eth_qty, fil_qty, usdc_usd, btc_qty,
               wave_qty, wave_price, margin_req_tokens, requested_usd, notes, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(counterparty, portfolio_asset) DO UPDATE SET
              eth_qty = excluded.eth_qty,
              fil_qty = excluded.fil_qty,
              usdc_usd = excluded.usdc_usd,
              btc_qty = excluded.btc_qty,
              wave_qty = excluded.wave_qty,
              wave_price = excluded.wave_price,
              margin_req_tokens = excluded.margin_req_tokens,
              requested_usd = excluded.requested_usd,
              notes = excluded.notes,
              updated_at = excluded.updated_at""",
        (
            cp, pa,
            float(payload.eth_qty),
            float(payload.fil_qty),
            float(payload.usdc_usd),
            float(payload.btc_qty),
            float(payload.wave_qty),
            float(payload.wave_price),
            float(payload.margin_req_tokens),
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
        "usdc_usd": float(payload.usdc_usd),
        "btc_qty": float(payload.btc_qty),
        "wave_qty": float(payload.wave_qty),
        "wave_price": float(payload.wave_price),
        "margin_req_tokens": float(payload.margin_req_tokens),
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
    total_btc_qty = sum(m["btc_qty"] for m in rows_for_book)
    total_usdc = sum(m["usdc_usd"] for m in rows_for_book)
    # WAVE has no price feed — value each row at its stored manual price, in USD.
    total_wave_usd = sum(m["wave_qty"] * m["wave_price"] for m in rows_for_book)

    # BTC spot is fixed across the ladder (no BTC options book to move it).
    btc_spot = 0.0
    if total_btc_qty:
        try:
            btc_spot = float(await _client.get_btc_spot_price())
        except Exception:
            btc_spot = 0.0

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
        nh, hc = _haircut_value(
            {"ETH": total_eth_qty, "FIL": total_fil_qty, "BTC": total_btc_qty, "USDC": total_usdc},
            {"ETH": eth_px, "FIL": fil_px, "BTC": btc_spot},
        )
        # WAVE already in USD; apply its haircut.
        nh += total_wave_usd
        hc += total_wave_usd * (1 - HAIRCUTS["WAVE"])

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
        "total_btc_qty": round(total_btc_qty, 6),
        "total_usdc_usd": round(total_usdc, 2),
        "total_wave_usd": round(total_wave_usd, 2),
        "btc_spot": round(btc_spot, 2),
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


# ───────────────────────── Collateral Map ─────────────────────────
# Matrix of token holdings per (counterparty, asset). USD value = qty × price.
# Stored in counterparty_collateral. Prices default to live market, overridable
# per asset via collateral_price.

MAP_ASSETS = ["USDC", "ETH", "FIL", "BTC"]
MAP_ASSET_LABELS = {"USDC": "USD / USDC", "ETH": "ETH", "FIL": "FIL", "BTC": "BTC"}


async def _live_prices() -> dict[str, float]:
    """Live USD price per collateral asset (USDC is always 1.0)."""
    out = {"USDC": 1.0, "ETH": 0.0, "FIL": 0.0, "BTC": 0.0}
    try:
        out["ETH"] = float(await _client.get_eth_spot_price())
    except Exception:
        pass
    try:
        out["FIL"] = float(await _client.get_fil_spot_price())
    except Exception:
        pass
    try:
        out["BTC"] = float(await _client.get_btc_spot_price())
    except Exception:
        pass
    return out


async def _effective_prices(assets: list[str]) -> tuple[dict, dict, dict]:
    """Return (effective_prices, manual_overrides, live_prices) for `assets`.

    Effective = manual override if set, else live market (USDC always 1.0).
    """
    live = await _live_prices()
    db = await get_db()
    cur = await db.execute("SELECT asset, price FROM collateral_price")
    overrides = {r["asset"].upper(): float(r["price"]) for r in await cur.fetchall()}
    prices: dict[str, float] = {}
    for a in assets:
        if a in overrides:
            prices[a] = overrides[a]
        elif a == "USDC":
            prices[a] = 1.0
        else:
            prices[a] = live.get(a, 0.0)
    return prices, overrides, live


async def _liability_by_cp() -> tuple[dict[str, float], dict[str, str]]:
    """Per-counterparty MtM liability summed across both option books."""
    eth = await _gather_portfolio("ETH")
    fil = await _gather_portfolio("FIL")
    el, ed, _ = _compute_liabilities(eth)
    fl, fd, _ = _compute_liabilities(fil)
    liab: dict[str, float] = {}
    for src in (el, fl):
        for k, v in src.items():
            liab[k] = liab.get(k, 0.0) + v
    disp: dict[str, str] = {}
    for src in (ed, fd):
        for k, n in src.items():
            disp.setdefault(k, n)
    return liab, disp


@router.get("/map")
async def collateral_map():
    """Collateral map: USD value of each asset sitting at each counterparty.

    Cells are token quantities (counterparty_collateral); USD = qty × price.
    Also returns, per counterparty, our MtM liability and the post-haircut
    balance (surplus) / debit (to post) = haircut collateral − liability.
    """
    db = await get_db()
    cur = await db.execute("SELECT counterparty, asset, qty FROM counterparty_collateral")
    rows = await cur.fetchall()

    assets = list(MAP_ASSETS)
    for r in rows:
        a = (r["asset"] or "").upper()
        if a and a not in assets:
            assets.append(a)

    prices, overrides, live = await _effective_prices(assets)

    cps: dict[str, dict] = {}
    for r in rows:
        k = r["counterparty"].lower()
        entry = cps.setdefault(k, {"display": r["counterparty"], "qtys": {}})
        entry["qtys"][(r["asset"] or "").upper()] = float(r["qty"] or 0)

    liab, liab_disp = await _liability_by_cp()
    for k, n in liab_disp.items():
        cps.setdefault(k, {"display": n, "qtys": {}})

    out_cps = []
    for k, info in cps.items():
        qtys = {a: float(info["qtys"].get(a, 0) or 0) for a in assets}
        usd = {a: round(qtys[a] * prices[a], 2) for a in assets}
        total = round(sum(usd.values()), 2)
        _nh, hc = _haircut_value(qtys, prices)
        l = liab.get(k, 0.0)
        out_cps.append({
            "counterparty": info["display"],
            "qtys": qtys,
            "usd": usd,
            "total_usd": total,
            "liability_usd": round(l, 2),
            "collateral_haircut_usd": round(hc, 2),
            "balance_usd": round(hc - l, 2),
        })
    out_cps.sort(key=lambda c: c["total_usd"], reverse=True)

    asset_totals = {a: round(sum(c["usd"][a] for c in out_cps), 2) for a in assets}
    grand_total = round(sum(asset_totals.values()), 2)

    return {
        "assets": assets,
        "asset_labels": {a: MAP_ASSET_LABELS.get(a, a) for a in assets},
        "prices": prices,
        "price_overrides": overrides,
        "live_prices": live,
        "haircuts": HAIRCUTS,
        "counterparties": out_cps,
        "asset_totals": asset_totals,
        "grand_total_usd": grand_total,
        "total_liability_usd": round(sum(liab.values()), 2),
        "total_balance_usd": round(sum(c["balance_usd"] for c in out_cps), 2),
    }


class MapCell(BaseModel):
    counterparty: str
    asset: str
    qty: float = 0.0


@router.put("/map/cell")
async def upsert_map_cell(payload: MapCell):
    cp = payload.counterparty.strip()
    a = payload.asset.strip().upper()
    if not cp:
        raise HTTPException(status_code=400, detail="counterparty required")
    if not a:
        raise HTTPException(status_code=400, detail="asset required")
    if payload.qty < 0:
        raise HTTPException(status_code=400, detail="qty must be >= 0")
    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO counterparty_collateral (counterparty, asset, qty, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(counterparty, asset) DO UPDATE SET
              qty = excluded.qty, updated_at = excluded.updated_at""",
        (cp, a, float(payload.qty), now),
    )
    await db.commit()
    return {"ok": True}


class PriceUpdate(BaseModel):
    asset: str
    price: float | None = None  # null clears the override (revert to live)


@router.put("/price")
async def upsert_price(payload: PriceUpdate):
    a = payload.asset.strip().upper()
    if not a:
        raise HTTPException(status_code=400, detail="asset required")
    db = await get_db()
    if payload.price is None:
        await db.execute("DELETE FROM collateral_price WHERE asset = ? COLLATE NOCASE", (a,))
    else:
        if payload.price < 0:
            raise HTTPException(status_code=400, detail="price must be >= 0")
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO collateral_price (asset, price, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(asset) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at""",
            (a, float(payload.price), now),
        )
    await db.commit()
    return {"ok": True}


class CounterpartyAdd(BaseModel):
    counterparty: str


@router.post("/counterparty")
async def add_map_counterparty(payload: CounterpartyAdd):
    cp = payload.counterparty.strip()
    if not cp:
        raise HTTPException(status_code=400, detail="counterparty required")
    db = await get_db()
    now = datetime.utcnow().isoformat()
    # A zero USDC row makes the counterparty appear as a column in the map.
    await db.execute(
        """INSERT OR IGNORE INTO counterparty_collateral (counterparty, asset, qty, updated_at)
           VALUES (?, 'USDC', 0, ?)""",
        (cp, now),
    )
    await db.commit()
    return {"ok": True}


@router.delete("/counterparty")
async def delete_map_counterparty(counterparty: str):
    cp = counterparty.strip()
    if not cp:
        raise HTTPException(status_code=400, detail="counterparty required")
    db = await get_db()
    await db.execute(
        "DELETE FROM counterparty_collateral WHERE counterparty = ? COLLATE NOCASE", (cp,)
    )
    await db.commit()
    return {"ok": True}
