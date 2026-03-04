"""Portfolio P&L & MTM endpoints."""

from __future__ import annotations

import math
import re
from datetime import datetime, date

from fastapi import APIRouter, HTTPException
import numpy as np
from scipy.stats import norm

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.pricing.options import bs_price
from plgo_options.pricing.vol_surface import VolSmile
from plgo_options.market_data.deribit_client import DeribitClient

router = APIRouter()
client = DeribitClient()

# Spot ladder: $500 to $7000 at $100 intervals
SPOT_LADDER = list(range(500, 7100, 100))

# MTM matrix horizons (days forward from today)
MATRIX_HORIZONS = [30, 45, 60, 90, 120, 150, 180, 270, 360]

# Payoff chart horizons (days forward)
CHART_HORIZONS = [0, 16, 30, 60, 90]

DEFAULT_IV = 0.80  # 80% fallback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _bs_vec(
    spots: np.ndarray, K: float, T: float, r: float, sigma: float, opt: str,
) -> np.ndarray:
    """Vectorised Black-Scholes across an array of spot prices."""
    if T <= 0:
        return np.maximum(spots - K, 0.0) if opt == "C" else np.maximum(K - spots, 0.0)
    d1 = (np.log(spots / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "C":
        return spots * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - spots * norm.cdf(-d1)


def _iso_to_date(s: str) -> date | None:
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s).date()
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _deribit_expiry_to_date(expiry_str: str) -> date:
    return datetime.strptime(expiry_str, "%d%b%y").date()


_DERIBIT_INSTRUMENT_RE = re.compile(
    r"^ETH-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])$", re.IGNORECASE
)


def _parse_instrument(instrument: str) -> tuple[date, float, str] | None:
    """Parse Deribit instrument name → (expiry_date, strike, opt_type)."""
    m = _DERIBIT_INSTRUMENT_RE.match(instrument.strip().upper())
    if not m:
        return None
    try:
        expiry = datetime.strptime(m.group(1), "%d%b%y").date()
    except ValueError:
        return None
    return expiry, float(m.group(2)), m.group(3)


def _normalize_instrument(instrument: str) -> str:
    """Normalize instrument name to Deribit format.

    Spreadsheet may have 'ETH-02Mar26-3800-C' but Deribit uses 'ETH-2MAR26-3800-C'
    (no leading zero on day, all uppercase).
    """
    parsed = _parse_instrument(instrument)
    if not parsed:
        return instrument.strip().upper()
    expiry_date, strike, opt = parsed
    day = expiry_date.day
    mon = expiry_date.strftime("%b").upper()
    yr = expiry_date.strftime("%y")
    strike_str = str(int(strike)) if strike == int(strike) else str(strike)
    return f"ETH-{day}{mon}{yr}-{strike_str}-{opt}"


def _build_instrument(expiry_date: date, strike: float, opt: str) -> str:
    """Build Deribit-format instrument name from trade fields."""
    day = expiry_date.day
    mon = expiry_date.strftime("%b").upper()
    yr = expiry_date.strftime("%y")
    strike_str = str(int(strike)) if strike == int(strike) else str(strike)
    return f"ETH-{day}{mon}{yr}-{strike_str}-{opt}"


async def _fetch_smiles() -> dict[str, VolSmile]:
    """Fetch all ETH option IVs from Deribit in one call and build vol smiles."""
    summaries = await client._get("get_book_summary_by_currency", {
        "currency": "ETH",
        "kind": "option",
    })

    expiry_data: dict[str, dict[float, list[float]]] = {}
    for s in summaries:
        name = s.get("instrument_name", "")
        mark_iv = s.get("mark_iv")
        if not name or mark_iv is None or mark_iv <= 0:
            continue
        parts = name.split("-")
        if len(parts) < 4:
            continue
        expiry_data.setdefault(parts[1], {}).setdefault(float(parts[2]), []).append(mark_iv)

    smiles: dict[str, VolSmile] = {}
    for exp, strike_ivs in expiry_data.items():
        strikes = sorted(strike_ivs.keys())
        ivs = [float(np.mean(strike_ivs[k])) for k in strikes]
        if len(strikes) >= 2:
            smiles[exp] = VolSmile(strikes, ivs)
    return smiles


def _match_expiry(pos_expiry: str, deribit_map: dict[str, date]) -> str | None:
    pos_date = _iso_to_date(pos_expiry)
    if pos_date is None:
        return None
    best, best_diff = None, 999
    for dexp, ddate in deribit_map.items():
        diff = abs((ddate - pos_date).days)
        if diff < best_diff:
            best_diff, best = diff, dexp
    return best if best_diff <= 7 else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/pnl")
async def portfolio_pnl():
    """Return per-trade MTM across spot ladder and time horizons."""
    # 1. Read trades from database
    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=True, include_deleted=False)
        # Map DB fields to legacy column names the enrichment loop expects
        trades = []
        for t in db_trades:
            trades.append({
                "Counterparty": t["counterparty"],
                "ID": t["id"],
                "Initial Trade Date": t["trade_date"],
                "Buy / Sell / Unwind": t["side"],
                "Option Type": t["option_type"],
                "Trade_ID": t.get("trade_id", ""),
                "Option Expiry Date": t["expiry"],
                "Days Remaining to Expiry": 0,  # computed live
                "Strike": t["strike"],
                "Ref. Spot Price": t["ref_spot"],
                "% OTM": t["pct_otm"],
                "ETH Options": t["qty"],
                "$ Notional (mm)": t["notional_mm"],
                "Premium per Contract": t["premium_per"],
                "Premium USD": t["premium_usd"],
                "_db_status": t["status"],
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read trades: {e}")

    if not trades:
        raise HTTPException(status_code=404, detail="No trades found")

    # 2. Fetch market data — spot, smiles (for scenario curves), and batch tickers
    try:
        eth_spot = await client.get_eth_spot_price()
        smiles = await _fetch_smiles()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Market data error: {e}")

    # Collect unique Deribit instrument names for batch ticker fetch.
    # Build the real instrument from each trade's own expiry/strike/type,
    # because Trade_ID is a deal-level ID (not per-option).
    unique_instruments: set[str] = set()
    for t in trades:
        opt_code = "C" if "call" in str(t.get("Option Type") or "").lower() else "P"
        strike_val = _safe_float(t.get("Strike"))
        expiry_dt = _iso_to_date(str(t.get("Option Expiry Date") or ""))
        if expiry_dt and strike_val > 0:
            unique_instruments.add(_build_instrument(expiry_dt, strike_val, opt_code))

    # Batch-fetch tickers for live greeks / mark price
    tickers = {}
    if unique_instruments:
        try:
            tickers = await client.get_option_tickers_batch(list(unique_instruments))
        except Exception:
            pass  # graceful degradation — fall back to BS

    # Deribit expiry → date mapping (for smile matching)
    deribit_dates: dict[str, date] = {}
    for exp in smiles:
        try:
            deribit_dates[exp] = _deribit_expiry_to_date(exp)
        except ValueError:
            continue

    today = date.today()

    # 3. Enrich each trade
    enriched = []
    spot_arr = np.array(SPOT_LADDER, dtype=float)
    all_horizons = sorted(set(CHART_HORIZONS + MATRIX_HORIZONS + [0]))

    for idx, t in enumerate(trades):
        # Extract spreadsheet fields
        counterparty = str(t.get("Counterparty") or "").strip()
        trade_id = t.get("ID")
        trade_date = str(t.get("Initial Trade Date") or "").strip()
        side_raw = str(t.get("Buy / Sell / Unwind") or "").strip()
        option_type_raw = str(t.get("Option Type") or "").strip()
        trade_id_raw = str(t.get("Trade_ID") or "").strip()
        expiry_raw = str(t.get("Option Expiry Date") or "").strip()
        strike = _safe_float(t.get("Strike"))
        ref_spot = _safe_float(t.get("Ref. Spot Price"))
        pct_otm = _safe_float(t.get("% OTM"))
        qty = _safe_float(t.get("ETH Options"))
        notional_mm = _safe_float(t.get("$ Notional (mm)"))
        premium_per = _safe_float(t.get("Premium per Contract"))
        premium_usd = _safe_float(t.get("Premium USD"))

        if strike <= 0 or qty <= 0:
            continue

        # Derive side sign and opt code
        opt = "C" if "call" in option_type_raw.lower() else "P"
        side_lower = side_raw.lower()
        if side_lower in ("buy", "long"):
            sign = 1.0
            side_label = "Long"
        elif side_lower in ("sell", "short"):
            sign = -1.0
            side_label = "Short"
        elif side_lower == "unwind":
            sign = -1.0
            side_label = "Unwind"
        else:
            sign = 1.0
            side_label = side_raw

        signed_qty = sign * qty

        # ------------------------------------------------------------------
        # Build correct instrument name from trade's own fields
        # (Trade_ID is a deal-level ID, not per-option)
        # ------------------------------------------------------------------
        expiry_date = _iso_to_date(expiry_raw)
        if expiry_date:
            instrument = _build_instrument(expiry_date, strike, opt)
            days_rem = max((expiry_date - today).days, 0)
        else:
            instrument = trade_id_raw  # fallback
            days_rem = max(_safe_float(t.get("Days Remaining to Expiry")), 0)

        # ------------------------------------------------------------------
        # Deribit ticker → live IV, greeks, mark price
        # ------------------------------------------------------------------
        ticker = tickers.get(instrument)

        if ticker and ticker.mark_price is not None:
            # Deribit mark_price is in ETH — convert to USD
            deribit_mark_usd = ticker.mark_price * eth_spot
            iv_pct = ticker.mark_iv if ticker.mark_iv else DEFAULT_IV * 100
            delta = ticker.delta
            gamma = ticker.gamma
            theta = ticker.theta
            vega = ticker.vega
        else:
            # Expired or no ticker — compute from BS / smile
            deribit_mark_usd = None
            delta = None
            gamma = None
            theta = None
            vega = None
            iv_pct = DEFAULT_IV * 100
            # Try smile interpolation
            matched = _match_expiry(expiry_raw, deribit_dates)
            if matched and matched in smiles:
                iv_pct = smiles[matched].iv_at(strike)

        sigma = iv_pct / 100.0

        # ------------------------------------------------------------------
        # Current value per contract (USD)
        # ------------------------------------------------------------------
        if deribit_mark_usd is not None:
            cur_value_per = deribit_mark_usd
        else:
            # Fall back to BS for expired / unavailable instruments
            T_now = max(days_rem, 0) / 365.25
            cur_value_per = bs_price(eth_spot, strike, T_now, 0.0, sigma, opt)

        # For expired options with no ticker, use intrinsic
        if days_rem == 0 and deribit_mark_usd is None:
            cur_value_per = max(eth_spot - strike, 0) if opt == "C" else max(strike - eth_spot, 0)
            delta = (1.0 if eth_spot > strike else 0.0) if opt == "C" else (-1.0 if eth_spot < strike else 0.0)
            gamma = 0.0
            theta = 0.0
            vega = 0.0

        # ------------------------------------------------------------------
        # MTM = signed_qty * current_value (mark-to-market value)
        # ------------------------------------------------------------------
        cur_mtm = signed_qty * cur_value_per

        # Live % OTM based on current spot
        pct_otm_live = round((strike / eth_spot - 1) * 100, 1) if eth_spot > 0 else 0.0

        # Live notional based on current spot
        notional_live = round(qty * eth_spot, 2)

        # ------------------------------------------------------------------
        # IV for scenario curves: prefer Deribit ticker, fall back to smile
        # ------------------------------------------------------------------
        scenario_sigma = sigma
        if not (ticker and ticker.mark_iv):
            matched = _match_expiry(expiry_raw, deribit_dates)
            if matched and matched in smiles:
                scenario_sigma = smiles[matched].iv_at(strike) / 100.0

        # ------------------------------------------------------------------
        # MTM at each matrix horizon (at current spot) — FIXED formula
        # ------------------------------------------------------------------
        mtm_horizon = []
        for h in MATRIX_HORIZONS:
            T_h = max(days_rem - h, 0) / 365.25
            if T_h > 0:
                val = bs_price(eth_spot, strike, T_h, 0.0, scenario_sigma, opt)
            else:
                val = max(eth_spot - strike, 0) if opt == "C" else max(strike - eth_spot, 0)
            mtm_horizon.append(round(signed_qty * val, 2))

        # ------------------------------------------------------------------
        # Payoff curves across spot ladder — FIXED formula
        # ------------------------------------------------------------------
        trade_payoff: dict[str, list[float]] = {}
        for h in all_horizons:
            T_h = max(days_rem - h, 0) / 365.25
            vals = _bs_vec(spot_arr, strike, T_h, 0.0, scenario_sigma, opt)
            mtm_vals = signed_qty * vals
            trade_payoff[str(h)] = np.round(mtm_vals, 2).tolist()

        # Truncate date strings for display
        if "T" in trade_date:
            trade_date = trade_date.split("T")[0]
        expiry_display = expiry_raw.split("T")[0] if "T" in expiry_raw else expiry_raw

        enriched.append({
            "id": t.get("ID", idx),
            "counterparty": counterparty,
            "trade_id": trade_id,
            "trade_date": trade_date,
            "side_raw": side_raw,
            "option_type": option_type_raw,
            "instrument": instrument,
            "expiry": expiry_display,
            "days_remaining": days_rem,
            "strike": strike,
            "ref_spot": ref_spot,
            "pct_otm_entry": round(pct_otm * 100, 1) if abs(pct_otm) < 10 else round(pct_otm, 1),
            "qty": qty,
            "notional_mm": round(notional_mm, 2),
            "premium_per": round(premium_per, 2),
            "premium_usd": round(premium_usd, 2),
            # Live-computed fields
            "opt": opt,
            "side": side_label,
            "net_qty": signed_qty,
            "pct_otm_live": pct_otm_live,
            "iv_pct": round(iv_pct, 1),
            "delta": round(delta, 4) if delta is not None else None,
            "gamma": round(gamma, 6) if gamma is not None else None,
            "theta": round(theta, 4) if theta is not None else None,
            "vega": round(vega, 4) if vega is not None else None,
            "mark_price_usd": round(cur_value_per, 2),
            "current_mtm": round(cur_mtm, 2),
            "notional_live": notional_live,
            "mtm_by_horizon": mtm_horizon,
            "payoff_by_horizon": trade_payoff,
            "db_status": t.get("_db_status", "active"),
        })

    if not enriched:
        raise HTTPException(status_code=404, detail="No valid trades found")

    # 4. Totals
    total_entry = sum(abs(ep["premium_usd"]) for ep in enriched)
    total_mtm = sum(ep["current_mtm"] for ep in enriched)
    horizon_totals = [
        round(sum(ep["mtm_by_horizon"][i] for ep in enriched), 2)
        for i in range(len(MATRIX_HORIZONS))
    ]

    # Portfolio greek totals (only from positions with Deribit data)
    total_delta = sum((ep["delta"] or 0) * ep["net_qty"] for ep in enriched)
    total_gamma = sum((ep["gamma"] or 0) * ep["net_qty"] for ep in enriched)
    total_theta = sum((ep["theta"] or 0) * ep["net_qty"] for ep in enriched)
    total_vega = sum((ep["vega"] or 0) * ep["net_qty"] for ep in enriched)

    # 5. Serialize vol smiles for frontend roll pricing
    vol_surface = []
    for exp_code, smile in smiles.items():
        exp_date = deribit_dates.get(exp_code)
        if exp_date is None:
            continue
        dte = max((exp_date - today).days, 0)
        vol_surface.append({
            "expiry_code": exp_code,
            "expiry_date": exp_date.isoformat(),
            "dte": dte,
            "strikes": smile.strikes.tolist(),
            "ivs": smile.ivs.tolist(),  # in % (e.g. 80.0)
        })
    vol_surface.sort(key=lambda x: x["dte"])

    return {
        "eth_spot": eth_spot,
        "spot_ladder": SPOT_LADDER,
        "matrix_horizons": MATRIX_HORIZONS,
        "chart_horizons": sorted(set(CHART_HORIZONS + [0])),
        "all_horizons": sorted(set(CHART_HORIZONS + MATRIX_HORIZONS + [0])),
        "vol_surface": vol_surface,
        "positions": enriched,
        "totals": {
            "total_entry_premium": round(total_entry, 2),
            "current_total_mtm": round(total_mtm, 2),
            "mtm_by_horizon": horizon_totals,
            "portfolio_delta": round(total_delta, 2),
            "portfolio_gamma": round(total_gamma, 4),
            "portfolio_theta": round(total_theta, 2),
            "portfolio_vega": round(total_vega, 2),
        },
    }


@router.get("/ticker/{instrument_name:path}")
async def get_instrument_ticker(instrument_name: str):
    """Return live Deribit ticker data for a single instrument (for add-trade)."""
    name = instrument_name.strip().upper()
    if not _DERIBIT_INSTRUMENT_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid instrument name: {name}")

    try:
        eth_spot = await client.get_eth_spot_price()
        ticker = await client.get_option_ticker(name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Deribit error: {e}")

    parsed = _parse_instrument(name)
    expiry_date = parsed[0] if parsed else None
    days_rem = max((expiry_date - date.today()).days, 0) if expiry_date else 0

    mark_usd = (ticker.mark_price or 0) * eth_spot
    greeks = ticker.model_dump() if hasattr(ticker, "model_dump") else ticker.dict()

    return {
        "instrument_name": name,
        "eth_spot": eth_spot,
        "mark_price_eth": ticker.mark_price,
        "mark_price_usd": round(mark_usd, 2),
        "mark_iv": ticker.mark_iv,
        "delta": ticker.delta,
        "gamma": ticker.gamma,
        "theta": ticker.theta,
        "vega": ticker.vega,
        "days_remaining": days_rem,
        "strike": parsed[1] if parsed else None,
        "opt": parsed[2] if parsed else None,
        "expiry": expiry_date.isoformat() if expiry_date else None,
    }
