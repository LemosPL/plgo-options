"""Market data endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
import numpy as np

from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.market_data.schemas import OptionTicker, PerpetualTicker
from plgo_options.pricing.vol_surface import VolSmile

router = APIRouter()
client = DeribitClient()


@router.get("/spot")
async def get_eth_spot(asset: str = "ETH") -> dict:
    if asset.upper() == "FIL":
        price = await client.get_fil_spot_price()
        return {"fil_spot": price}
    price = await client.get_eth_spot_price()
    return {"eth_spot": price}


@router.get("/perpetual", response_model=PerpetualTicker)
async def get_perpetual() -> PerpetualTicker:
    return await client.get_perpetual_ticker()


@router.get("/options", response_model=list[OptionTicker])
async def get_options(expiry: str | None = None) -> list[OptionTicker]:
    """
    Get option tickers. Optionally filter by expiry like '28MAR25'.
    Multiple expiries can be comma-separated.
    """
    expiry_dates = set(expiry.split(",")) if expiry else None
    return await client.get_all_option_tickers(expiry_dates)


@router.get("/expirations")
async def get_expirations() -> list[str]:
    """Return all active ETH option expiration date strings."""
    instruments = await client.get_option_instruments()
    expiries = {inst["instrument_name"].split("-")[1] for inst in instruments}
    return sorted(expiries)


@router.get("/surface-curve")
async def get_surface_curve(expiry: str) -> dict:
    """Return option prices for a single expiry to build the protection/income curve.

    For each strike, returns the mark price (in USD) for calls and puts.
    Used by the Vol Surface Curve page.
    """
    spot = await client.get_eth_spot_price()

    summaries = await client._get("get_book_summary_by_currency", {
        "currency": "ETH",
        "kind": "option",
    })

    calls: list[dict] = []  # {strike, mark_usd, mark_iv, bid_usd, ask_usd}
    puts: list[dict] = []

    for s in summaries:
        name = s.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) < 4:
            continue
        if parts[1] != expiry:
            continue

        strike = float(parts[2])
        opt_type = parts[3]  # "C" or "P"
        mark_price = s.get("mark_price")  # in ETH
        mark_iv = s.get("mark_iv")
        bid = s.get("bid_price")
        ask = s.get("ask_price")

        if mark_price is None or mark_price <= 0:
            continue

        entry = {
            "strike": strike,
            "mark_usd": mark_price * spot,
            "mark_iv": mark_iv,
            "bid_usd": (bid * spot) if bid and bid > 0 else None,
            "ask_usd": (ask * spot) if ask and ask > 0 else None,
        }

        if opt_type == "C":
            calls.append(entry)
        else:
            puts.append(entry)

    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])

    return {"spot": spot, "expiry": expiry, "calls": calls, "puts": puts}


@router.get("/vol-surface")
async def get_vol_surface(asset: str = "ETH") -> dict:
    """Fetch vol surface. ETH from Deribit directly; FIL scaled from ETH by HV ratio."""
    is_fil = asset.upper() == "FIL"

    if is_fil:
        spot = await client.get_fil_spot_price()
    else:
        spot = await client.get_eth_spot_price()

    summaries = await client._get("get_book_summary_by_currency", {
        "currency": "ETH",
        "kind": "option",
    })

    # Collect (expiry_code → strike → [ivs])
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

    # For FIL: scale ETH IVs by historical vol ratio and project strikes to FIL moneyness
    vol_ratio = 1.0
    eth_spot_for_moneyness = None
    if is_fil:
        try:
            vol_ratio = await client.get_historical_vol_ratio(days=30)
            eth_spot_for_moneyness = await client.get_eth_spot_price()
        except Exception:
            vol_ratio = 1.5

    today = datetime.utcnow().date()
    smiles = []
    for exp_code, strike_ivs in expiry_data.items():
        strikes = sorted(strike_ivs.keys())
        ivs = [float(np.mean(strike_ivs[k])) * vol_ratio for k in strikes]
        if len(strikes) < 2:
            continue
        try:
            exp_date = datetime.strptime(exp_code, "%d%b%y").date()
        except ValueError:
            continue
        dte = max((exp_date - today).days, 0)

        # Project ETH strikes to FIL via moneyness: FIL_strike = ETH_strike / ETH_spot * FIL_spot
        if is_fil and eth_spot_for_moneyness and eth_spot_for_moneyness > 0:
            fil_strikes = [round(k / eth_spot_for_moneyness * spot, 4) for k in strikes]
        else:
            fil_strikes = [float(k) for k in strikes]

        smiles.append({
            "expiry_code": exp_code,
            "expiry_date": exp_date.isoformat(),
            "dte": dte,
            "strikes": fil_strikes,
            "ivs": ivs,
        })

    smiles.sort(key=lambda x: x["dte"])

    return {
        "eth_spot": spot,
        "smiles": smiles,
    }