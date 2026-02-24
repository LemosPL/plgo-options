"""Market data endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.market_data.schemas import OptionTicker, PerpetualTicker

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