"""Async Deribit public API client for ETH market data."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx
import numpy as np

from plgo_options.config import DERIBIT_BASE_URL, DEFAULT_CURRENCY, REQUEST_TIMEOUT
from plgo_options.market_data.schemas import OptionTicker, PerpetualTicker

# ---------------------------------------------------------------------------
# Simple TTL cache for expensive Deribit calls
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}  # key → (expiry_ts, data)
_cache_lock = asyncio.Lock() if hasattr(asyncio, "Lock") else None

CACHE_TTL_SECONDS = 10  # cache responses for 10 seconds


def _cache_key(method: str, params: dict | None) -> str:
    """Build a hashable cache key from method + sorted params."""
    p = tuple(sorted((params or {}).items()))
    return f"{method}|{p}"


class DeribitClient:
    """Thin async wrapper around Deribit public JSON-RPC endpoints."""

    def __init__(self, base_url: str = DERIBIT_BASE_URL) -> None:
        self.base_url = base_url

    # -- low-level --------------------------------------------------------

    async def _get(self, method: str, params: dict[str, Any] | None = None) -> Any:
        # Check cache first
        key = _cache_key(method, params)
        now = time.monotonic()
        if key in _cache:
            expiry_ts, cached_data = _cache[key]
            if now < expiry_ts:
                return cached_data

        url = f"{self.base_url}/public/{method}"

        # Retry with exponential backoff on 429 (rate limit)
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 429:
                        if attempt < max_retries:
                            wait = 1.0 * (2 ** attempt)  # 1s, 2s, 4s
                            await asyncio.sleep(wait)
                            continue
                    resp.raise_for_status()
                    data = resp.json()
                    if "error" in data:
                        raise RuntimeError(f"Deribit API error: {data['error']}")
                    result = data["result"]

                    # Cache the result
                    _cache[key] = (now + CACHE_TTL_SECONDS, result)
                    return result
            except httpx.HTTPStatusError:
                raise
            except httpx.TimeoutException:
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise

        # Should not reach here, but just in case
        raise RuntimeError(f"Deribit API request failed after {max_retries} retries")

    # -- instruments ------------------------------------------------------

    async def get_eth_spot_price(self) -> float:
        """Return ETH spot price from the perpetual last price."""
        ticker = await self._get("ticker", {"instrument_name": "ETH-PERPETUAL"})
        return float(ticker["last_price"])

    async def get_fil_spot_price(self) -> float:
        """Return FIL spot price from cryptoprices.cc."""
        url = "https://cryptoprices.cc/FIL/"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http_client:
            resp = await http_client.get(url)
            resp.raise_for_status()
            return float(resp.text.strip())

    async def get_historical_vol_ratio(self, days: int = 30) -> float:
        """Return annualised HV(FIL) / HV(ETH) using CoinGecko daily closes.

        Falls back to 1.5 if the fetch fails (FIL is typically ~1.5x ETH vol).
        """
        cg_url = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
        params = {"vs_currency": "usd", "days": days, "interval": "daily"}

        async def _fetch_prices(coin: str) -> list[float]:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
                resp = await c.get(cg_url.format(coin=coin), params=params)
                resp.raise_for_status()
                return [p[1] for p in resp.json()["prices"]]

        try:
            fil_prices, eth_prices = await asyncio.gather(
                _fetch_prices("filecoin"), _fetch_prices("ethereum"),
            )
        except Exception:
            return 1.5  # sensible default

        def _ann_vol(prices: list[float]) -> float:
            if len(prices) < 3:
                return 0.0
            log_ret = [math.log(prices[i] / prices[i - 1])
                       for i in range(1, len(prices)) if prices[i - 1] > 0]
            return float(np.std(log_ret) * math.sqrt(365))

        hv_fil = _ann_vol(fil_prices)
        hv_eth = _ann_vol(eth_prices)
        if hv_eth <= 0:
            return 1.5
        return max(hv_fil / hv_eth, 0.5)  # floor at 0.5x to avoid absurd ratios

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