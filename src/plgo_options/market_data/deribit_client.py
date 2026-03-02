"""Async Deribit public API client for ETH market data."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plgo_options.config import DERIBIT_BASE_URL, DEFAULT_CURRENCY, REQUEST_TIMEOUT
from plgo_options.market_data.schemas import OptionTicker, PerpetualTicker


class DeribitClient:
    """Thin async wrapper around Deribit public JSON-RPC endpoints."""

    def __init__(self, base_url: str = DERIBIT_BASE_URL) -> None:
        self.base_url = base_url

    # -- low-level --------------------------------------------------------

    async def _get(self, method: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/public/{method}"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"Deribit API error: {data['error']}")
            return data["result"]

    # -- instruments ------------------------------------------------------

    async def get_eth_spot_price(self) -> float:
        """Return ETH spot price from the perpetual last price."""
        ticker = await self._get("ticker", {"instrument_name": "ETH-PERPETUAL"})
        return float(ticker["last_price"])

    async def get_perpetual_ticker(self) -> PerpetualTicker:
        raw = await self._get("ticker", {"instrument_name": "ETH-PERPETUAL"})
        return PerpetualTicker(
            instrument_name="ETH-PERPETUAL",
            last_price=raw["last_price"],
            mark_price=raw["mark_price"],
            best_bid=raw.get("best_bid_price"),
            best_ask=raw.get("best_ask_price"),
        )

    async def get_option_instruments(
        self,
        expiry_dates: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        instruments = await self._get(
            "get_instruments",
            {"currency": DEFAULT_CURRENCY, "kind": "option"},
        )
        if expiry_dates is None:
            return instruments
        return [
            inst for inst in instruments
            if any(d in inst["instrument_name"] for d in expiry_dates)
        ]

    async def get_option_ticker(self, instrument_name: str) -> OptionTicker:
        raw = await self._get("ticker", {"instrument_name": instrument_name})
        greeks = raw.get("greeks", {})
        return OptionTicker(
            instrument_name=instrument_name,
            mark_price=raw.get("mark_price"),
            mark_iv=raw.get("mark_iv"),
            underlying_price=raw.get("underlying_price"),
            best_bid=raw.get("best_bid_price"),
            best_ask=raw.get("best_ask_price"),
            delta=greeks.get("delta"),
            gamma=greeks.get("gamma"),
            theta=greeks.get("theta"),
            vega=greeks.get("vega"),
            rho=greeks.get("rho"),
        )

    async def get_option_tickers_batch(
        self,
        instrument_names: list[str],
        concurrency: int = 10,
    ) -> dict[str, OptionTicker | None]:
        """Fetch tickers for multiple instruments concurrently.

        Returns a dict mapping instrument_name → OptionTicker (or None if
        the instrument is expired / errored).
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, OptionTicker | None] = {}

        async def _fetch_one(name: str) -> None:
            async with sem:
                try:
                    results[name] = await self.get_option_ticker(name)
                except Exception:
                    results[name] = None

        await asyncio.gather(*[_fetch_one(n) for n in instrument_names])
        return results

    async def get_all_option_tickers(
        self,
        expiry_dates: set[str] | None = None,
    ) -> list[OptionTicker]:
        instruments = await self.get_option_instruments(expiry_dates)
        tickers: list[OptionTicker] = []
        for inst in instruments:
            try:
                tk = await self.get_option_ticker(inst["instrument_name"])
                tickers.append(tk)
            except Exception:
                continue  # skip illiquid / errored instruments
        return tickers