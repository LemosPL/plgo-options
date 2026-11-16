"""Safety rails for the bot's orders that reach a venue (testnet and live; paper sends nothing).

    production off     "live" orders are refused, and never sent, unless EXEC_BOT_LIVE=1 (Lucas sets
                       it once testnet has run) and the connector is really on production
                       (DERIBIT_TESTNET=0, DERIBIT_CONNECTOR_PRODUCTION=1, a production key)
    confirmation       a testnet or live order is created only with confirmed=true (the page asks first)
    size per order     at most MAX_ORDER_CONTRACTS contracts per order
    daily caps         per mode, since 00:00 UTC: at most MAX_DAY_CONTRACTS contracts and
                       MAX_DAY_PREMIUM_USD of premium filled (every fill of the mode, Block RFQ legs
                       included); a take that would pass either is cut down to what is left, or not sent
    position check     before each IOC the bot reads the account's position in the option, after a fill
                       it reads it again: if it did not move by what was filled, the mode is halted
                       (nothing more sent in it) until someone clears the halt on the page

The caps come from the environment so Lucas can change them without a deploy of new code.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import Awaitable, Callable
from typing import Any

from plgo_options import config

from . import store
from .deribit import ConnectorError, deribit_keys, get_connector
from .threshold import QTY_STEP

VENUE_MODES = ("testnet", "live")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def live_enabled() -> bool:
    return os.environ.get("EXEC_BOT_LIVE", "0") == "1"


def caps() -> dict[str, float]:
    return {"order_contracts": _num("EXEC_MAX_ORDER_CONTRACTS", 250),
            "day_contracts": _num("EXEC_MAX_DAY_CONTRACTS", 1000),
            "day_premium_usd": _num("EXEC_MAX_DAY_PREMIUM_USD", 250_000)}


def live_blocker() -> str | None:
    """Why production orders cannot be created or sent now, or None."""
    if not live_enabled():
        return "production is off: Lucas sets EXEC_BOT_LIVE=1 once the bot's testnet run has passed"
    if config.DERIBIT_TESTNET or not config.DERIBIT_CONNECTOR_PRODUCTION:
        return "the connector is not on production: needs DERIBIT_TESTNET=0 and DERIBIT_CONNECTOR_PRODUCTION=1"
    if not all(deribit_keys(False)):
        return "no production Deribit key on this server"
    return None


def live_connector():
    """The production connector, or ConnectorError: never a testnet one under the 'live' name."""
    if reason := live_blocker():
        raise ConnectorError(reason)
    conn = get_connector()
    if conn.testnet:
        raise ConnectorError("the shared connector is on testnet: refusing to send a live order there")
    return conn


def check_create(mode: str, qty: float, confirmed: bool) -> str | None:
    """Why an order of this mode and size cannot be created, or None."""
    if mode not in VENUE_MODES:
        return None
    if mode == "live" and (reason := live_blocker()):
        return reason
    if not confirmed:
        return f"a {mode} order sends real orders to Deribit {'testnet' if mode == 'testnet' else 'production'}: confirm it first"
    cap = caps()["order_contracts"]
    if qty > cap:
        return f"{qty:g} contracts is over the {cap:g} per-order cap for {mode} (EXEC_MAX_ORDER_CONTRACTS)"
    return None


def day_start(now: float) -> float:
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()


async def room(mode: str, qty: float, price_usd: float, now: float) -> tuple[float, str]:
    """How much of a take (qty at price_usd a contract) the day's caps leave, and why if cut."""
    c = caps()
    used_q, used_p = await store.day_totals(mode, day_start(now))
    left_q = c["day_contracts"] - used_q
    left_p = c["day_premium_usd"] - used_p
    allowed = min(qty, left_q, left_p / price_usd if price_usd > 0 else qty)
    allowed = max(0.0, (allowed // QTY_STEP) * QTY_STEP)
    if allowed >= qty:
        return qty, ""
    return allowed, (f"{mode} daily cap: {used_q:g} of {c['day_contracts']:g} contracts and "
                     f"${used_p:,.0f} of ${c['day_premium_usd']:,.0f} premium used today (UTC)")


async def position(conn: Any, instrument: str) -> float:
    return sum(p.size for p in await conn.positions() if p.instrument == instrument)


SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep


async def reconcile(conn: Any, instrument: str, before: float, change: float, tries: int = 3) -> str | None:
    """None when the position moved by ``change`` since ``before``; otherwise what is wrong.
    Deribit can take a moment to show a trade in positions: read up to ``tries`` times."""
    after = None
    for i in range(tries):
        try:
            after = await position(conn, instrument)
        except ConnectorError as ex:
            after, err = None, str(ex)
        else:
            if abs(after - before - change) < QTY_STEP / 2:
                return None
        if i < tries - 1:
            await SLEEP(0.5)
    if after is None:
        return f"could not read the {instrument} position after the fill: {err}"
    return (f"{instrument} position {before:g} -> {after:g} after a fill of {change:+g}: "
            f"expected {before + change:g}")
