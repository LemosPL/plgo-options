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
async def get_eth_spot() -> dict:
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


@router.get("/vol-surface")
async def get_vol_surface() -> dict:
    """Fetch the full ETH vol surface from Deribit (all expiries).

    Returns spot price, expiry list with DTE, and per-expiry smile data
    (strikes + IVs) for frontend caching.
    """
    eth_spot = await client.get_eth_spot_price()
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

    today = datetime.utcnow().date()
    smiles = []
    for exp_code, strike_ivs in expiry_data.items():
        strikes = sorted(strike_ivs.keys())
        ivs = [float(np.mean(strike_ivs[k])) for k in strikes]
        if len(strikes) < 2:
            continue
        try:
            exp_date = datetime.strptime(exp_code, "%d%b%y").date()
        except ValueError:
            continue
        dte = max((exp_date - today).days, 0)
        smiles.append({
            "expiry_code": exp_code,
            "expiry_date": exp_date.isoformat(),
            "dte": dte,
            "strikes": [float(k) for k in strikes],
            "ivs": ivs,
        })

    smiles.sort(key=lambda x: x["dte"])

    return {
        "eth_spot": eth_spot,
        "smiles": smiles,
    }