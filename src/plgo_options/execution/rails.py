"""Safety rails for the bot's orders that reach a venue (testnet and live; paper sends nothing).

    production off     "live" orders are refused, and never sent, unless EXEC_BOT_LIVE=1 (Lucas sets
                       it once testnet has run) and the connector is really on production
                       (DERIBIT_TESTNET=0, DERIBIT_CONNECTOR_PRODUCTION=1, a production key)
    confirmation       a testnet or live order is created only with confirmed=true (the page asks first)
    size per order     at most MAX_ORDER_CONTRACTS contracts per order
    daily caps         per mode, since 00:00 UTC: at most MAX_DAY_CONTRACTS contracts and
                       MAX_DAY_PREMIUM_USD of premium filled (every fill of the mode, Block RFQ legs
                       included); a take that would pass either is cut down to what is left, or not sent
    margin check       a testnet or live batch is created only if the account's available funds cover the
                       initial margin of its orders and of what the mode's open orders still have to fill
                       (margin_check), up to MARGIN_USE of them; all or nothing, so a hedged batch is never
                       half created
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
from .deribit import ConnectorError, DeribitError, deribit_keys, get_connector, testnet_connector
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


PERP = "ETH-PERPETUAL"
PACE = 0.2   # seconds between the margin check's reads


def margin_use() -> float:
    """The share of the available funds a batch, with the open orders, may need (EXEC_MARGIN_USE)."""
    return _num("EXEC_MARGIN_USE", 0.9)


def venue(mode: str) -> Any:
    return testnet_connector() if mode == "testnet" else live_connector()


async def margin_check(mode: str, orders: list[dict[str, Any]], open_orders: list[dict[str, Any]],
                       conn: Any = None) -> dict[str, Any]:
    """Whether the account can margin ``orders`` (instrument, side, qty; the perp's qty in ETH) on top of what
    ``open_orders`` still have to fill (instrument, side, qty, filled).

    Under standard margin, each order's initial margin from Deribit (get_margins, at the mark), the part
    that closes a position we hold counted as free; summed, so a conservative figure. Under portfolio
    margin, Deribit's simulation of the account with them all (simulate_portfolio), the sum if it fails.
    {"ok", "need", "available", "limit", "model", "method", "unlisted": [instruments the venue does not
    list, left out], "detail"}; amounts in ETH."""
    conn = conn or venue(mode)

    async def paced(call, *a):   # a batch is a dozen private reads: keep under Deribit's rate limit
        for i in range(3):
            try:
                return await call(*a)
            except DeribitError as ex:
                if ex.code != 10028 or i == 2:
                    raise
                await SLEEP(1.0 + i)
            finally:
                await SLEEP(PACE)

    acct = await conn.margin_state()
    avail = float(acct.get("available_funds") or 0.0)
    model = str(acct.get("margin_model") or "")
    held = {p.instrument: p.size for p in await conn.positions()}
    legs = [(o, o["qty"]) for o in orders] + [(o, o["qty"] - o.get("filled", 0.0)) for o in open_orders]
    need, unlisted, sim, marks = 0.0, [], {}, {}
    for o, qty in legs:
        name = o["instrument"]
        if qty <= 0 or name in unlisted:
            continue
        try:
            if name not in marks:
                marks[name] = (await paced(conn.book, name)).mark
        except (ConnectorError, DeribitError) as ex:
            if "not found" in str(ex).lower() or "not open" in str(ex).lower():
                unlisted.append(name)
                continue
            raise
        mark = marks[name]
        if not mark:
            raise ConnectorError(f"{name}: no mark price on {mode} to margin the order at")
        amount = round(qty * mark) if name == PERP else qty      # Deribit sizes the perp in USD
        signed = amount if o["side"] == "buy" else -amount
        pos = held.get(name, 0.0)
        closes = min(abs(signed), abs(pos)) if pos * signed < 0 else 0.0
        held[name] = pos + signed
        sim[name] = sim.get(name, 0.0) + signed
        opens = abs(signed) - closes
        if opens >= (1 if name == PERP else QTY_STEP):
            need += await paced(conn.order_margin, name, o["side"], opens, mark)
    method = "sum of each order's margin"
    if "pm" in model.split("_")[-1] and sim:
        try:
            r = await conn.simulate_margin({k: v for k, v in sim.items() if v})
            need = max(0.0, float(r["projected_initial_margin"]) - float(acct.get("initial_margin") or 0.0))
            method = "Deribit's portfolio margin simulation"
        except (ConnectorError, DeribitError, KeyError, TypeError, ValueError):
            pass   # the sum: more than portfolio margin would ask, never less
    limit = margin_use() * avail
    ok = need <= limit
    n_open = len([x for x in open_orders if x["qty"] > x.get("filled", 0.0)])
    detail = (f"needs ~{need:,.1f} ETH of initial margin ({method}"
              + (f", {n_open} open {mode} order(s) included" if n_open else "")
              + f"); the {mode} account has {avail:,.1f} ETH available, {margin_use():.0%} usable: {limit:,.1f}")
    return {"ok": ok, "need": need, "available": avail, "limit": limit, "model": model, "method": method,
            "unlisted": unlisted, "detail": detail}
