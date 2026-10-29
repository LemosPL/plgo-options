"""Live exchange quotes for the "exchange_book" pricing model (Deribit).

A calibrated OTC counterparty is priced off a model fitted to past trades
(pricing.cpty_pricing). An exchange needs no fitting: its bid and ask are on
screen. ``load`` pulls the whole option book in one call
(public/get_book_summary_by_currency) and turns each bid and ask into an
implied vol at the exchange's own underlying; ``lookup`` hands those vols to
cpty_pricing.resolve_price, which reprices them at the optimizer's spot, so a
hypothetical-spot run moves the exchange's prices with it (sticky strike).

Quotes are keyed by (expiry date, strike, C/P) and dated by the (local) day
they were loaded: a run whose ``today`` is another day (a replay) finds
nothing and falls back to the mid vol surface. Only instruments quoted on BOTH sides are
kept, since the optimizer treats a leg with a buy price as fully priced and
would otherwise sell into a missing bid at mid for free.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from scipy.optimize import brentq

from ..optimization.math_utils import bs_price

logger = logging.getLogger(__name__)

# (venue, asset) -> {(expiry_date, strike, opt): (bid_iv_pct, ask_iv_pct)}
_QUOTES: dict[tuple[str, str], dict[tuple[date, float, str], tuple[float, float]]] = {}
_LOADED_ON: dict[tuple[str, str], date] = {}

_EXPIRY_HOUR_UTC = 8  # Deribit options expire at 08:00 UTC


def _implied_vol(price: float, S: float, K: float, T: float, opt: str) -> float | None:
    intrinsic = max(S - K, 0.0) if opt == "C" else max(K - S, 0.0)
    if T <= 0 or price <= intrinsic + 1e-12 or price >= (S if opt == "C" else K):
        return None
    try:
        return brentq(lambda v: bs_price(S, K, T, 0.0, v, opt) - price, 1e-4, 10.0, xtol=1e-7)
    except ValueError:
        return None


def parse(summaries: list[dict[str, Any]], now: datetime) -> dict:
    """Book-summary rows -> {(expiry, strike, opt): (bid_iv_pct, ask_iv_pct)}."""
    out: dict[tuple[date, float, str], tuple[float, float]] = {}
    for s in summaries:
        parts = str(s.get("instrument_name", "")).split("-")
        bid, ask, und = s.get("bid_price"), s.get("ask_price"), s.get("underlying_price")
        if len(parts) != 4 or not bid or not ask or not und or bid <= 0 or ask < bid:
            continue
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y").replace(
                hour=_EXPIRY_HOUR_UTC, tzinfo=timezone.utc)
            strike, opt = float(parts[2]), parts[3]
        except ValueError:
            continue
        T = (expiry - now).total_seconds() / (365.25 * 86400)
        # Deribit quotes options in the underlying (ETH); the model prices in USD.
        bid_iv = _implied_vol(bid * und, und, strike, T, opt)
        ask_iv = _implied_vol(ask * und, und, strike, T, opt)
        if bid_iv is None or ask_iv is None:
            continue
        out[(expiry.date(), strike, opt)] = (bid_iv * 100.0, ask_iv * 100.0)
    return out


async def load(venue: str = "Deribit", asset: str = "ETH", client: Any = None) -> int:
    """Refresh one venue's quotes. Returns how many instruments are two-sided."""
    if client is None:
        from ..market_data.deribit_client import DeribitClient
        client = DeribitClient()
    summaries = await client._get("get_book_summary_by_currency",
                                  {"currency": asset.upper(), "kind": "option"})
    now = datetime.now(timezone.utc)
    key = (venue.lower(), asset.upper())
    _QUOTES[key] = parse(summaries, now)
    _LOADED_ON[key] = date.today()  # local, like the optimizer's datetime.today()
    logger.info("exchange quotes: %s %s, %d two-sided", venue, asset, len(_QUOTES[key]))
    return len(_QUOTES[key])


def lookup(venue: str, asset: str, strike: float, T: float, opt: str) -> tuple[float, float] | None:
    """(bid_iv_pct, ask_iv_pct) for the instrument expiring ``T`` years from the load day.

    ``T`` is dte / 365.25 with the optimizer's dte = (expiry midnight -
    datetime.today()).days, which floors: during the day it is one short of
    the calendar-day gap, hence the + 1.
    """
    key = (str(venue).strip().lower(), asset.upper())
    quotes, loaded_on = _QUOTES.get(key), _LOADED_ON.get(key)
    if not quotes or loaded_on is None:
        return None
    expiry = date.fromordinal(loaded_on.toordinal() + round(T * 365.25) + 1)
    return quotes.get((expiry, float(strike), opt))


def deribit_option_fee_usd(S: float, price_usd: float) -> float:
    """Deribit's option taker fee per 1-ETH contract: 0.03% of the underlying,
    capped at 12.5% of the option price. Check against the current fee schedule."""
    return min(0.0003 * S, 0.125 * price_usd) if math.isfinite(price_usd) else 0.0
