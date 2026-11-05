"""DeribitConnector: the Connector protocol against the Deribit JSON-RPC API.

Talks plain HTTP POST, so the client secret stays out of URLs and logs. Keys
come from ``from_env``; production needs an explicit opt-in on top of
``DERIBIT_TESTNET=0`` (see ``__init__``), and a key that can move funds is
refused outright (see ``_check_scope``).

Three facts of the Deribit API shape this file:

    Labels are not unique. Deribit accepts a second order under a label it has
    already seen, so idempotency is ours: ``submit`` looks the label up first,
    and only places an order once it has proved none exists. The lookup has
    two stages because ``get_order_state_by_label`` forgets an order 30 minutes
    after it closes; older ones come from the order history, including the
    unfilled ones it hides by default.

    A timeout says nothing about the order. It may have landed. Every order is
    therefore sent with ``valid_until``, which stops the matching engine
    accepting it after a few seconds; ``submit`` waits that out and asks by
    label. The order is adopted if it landed, and the call raises if it did
    not. While the outcome is unknown ``healthy`` stays False.

    Market orders have no price protection, but the contract promises
    ``max_slippage_bps``. So ``market`` goes out as a limit IOC at the worst
    acceptable price, never as a Deribit market order.

One connector per account, in one process: the label check and the send are
only atomic against other calls on this object. Two processes submitting the
same intent at the same moment could both pass the check.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from plgo_options.config import (
    DERIBIT_BASE_URL,
    DERIBIT_CLIENT_ID,
    DERIBIT_CLIENT_SECRET,
    DERIBIT_CONNECTOR_PRODUCTION,
    DERIBIT_TESTNET,
    DERIBIT_TESTNET_CLIENT_ID,
    DERIBIT_TESTNET_CLIENT_SECRET,
    REQUEST_TIMEOUT,
)

from .models import (
    AccountState,
    BookTop,
    Fill,
    OrderIntent,
    OrderState,
    OrderStatus,
    Position,
    TimeInForce,
)

logger = logging.getLogger(__name__)

TESTNET_URL = "https://test.deribit.com/api/v2"
PRODUCTION_URL = DERIBIT_BASE_URL

TIME_IN_FORCE: dict[TimeInForce, str] = {
    "GTC": "good_til_cancelled",
    "IOC": "immediate_or_cancel",
    "good_til_day": "good_til_day",
}
_STATUS = {
    "open": OrderStatus.OPEN,
    "untriggered": OrderStatus.OPEN,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}

# Deribit error codes, from the detailed table at docs.deribit.com/articles/errors.
_UNAUTHORIZED = 13009  # expired or bad token: re-authenticate, nothing was placed
_RATE_LIMITED = {10028, 10047, 10066}  # too_many_requests, queue full, too many concurrent
# The connector or the venue is the problem and nothing was placed: raise, not REJECTED.
_NOT_PLACED = _RATE_LIMITED | {
    _UNAUTHORIZED,
    10000,  # authorization_required
    10040,  # retry
    11051,  # system_maintenance
    13004,  # invalid_credentials
    13021,  # forbidden
    13025,  # method_switched_off_by_admin
    13888,  # timed_out: valid_until passed before the engine took it
}
# The server failed mid-request, so the order may or may not exist.
_OUTCOME_UNKNOWN = {10001, 11094, 13028}  # error, internal_server_error, temporarily_unavailable
_ALREADY_CLOSED = {10010, 11008, 11044}  # already_closed, already_filled, not_open_order
_ORDER_NOT_FOUND = 10004

_FILL_EPSILON = 1e-9
_HISTORY_PAGE = 1000
_HISTORY_MAX_PAGES = 10


class ConnectorError(Exception):
    """The connector cannot do its job. Exchange rejections are never this."""


class DeribitError(ConnectorError):
    """A JSON-RPC error from Deribit, which the caller turns into a rejection or a fault."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.reason = f"{message}: {data}" if data else message
        super().__init__(f"Deribit error {code}: {self.reason}")


class TransportError(ConnectorError):
    """No usable response. ``maybe_sent`` is False only when the request never left."""

    def __init__(self, message: str, maybe_sent: bool) -> None:
        self.maybe_sent = maybe_sent
        super().__init__(message)


class DeribitConnector:
    def __init__(
            self,
            client_id: str,
            client_secret: str,
            *,
            testnet: bool = True,
            allow_production: bool = False,
            currency: str = "ETH",
            position_currencies: tuple[str, ...] = ("ETH", "USDC"),
            order_window_seconds: float = 5.0,
            rate_limit_cooldown_seconds: float = 5.0,
            timeout: float = REQUEST_TIMEOUT,
            transport: httpx.AsyncBaseTransport | None = None,
            clock: Callable[[], float] = time.time,
            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not testnet and not allow_production:
            raise ConnectorError(
                "refusing production without allow_production=True: run testnet first")
        self.url = TESTNET_URL if testnet else PRODUCTION_URL
        self.testnet = testnet
        self.currency = currency
        self.position_currencies = position_currencies
        self._client_id = client_id
        self._client_secret = client_secret
        self._window = order_window_seconds
        self._rate_cooldown = rate_limit_cooldown_seconds
        self._clock = clock
        self._sleep = sleep
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

        self._token: str | None = None
        self._token_expiry = 0.0
        self._scope: str | None = None
        self._auth_lock = asyncio.Lock()
        self._server_offset = 0.0  # server clock minus ours, for valid_until
        self._down_until = 0.0
        self._instruments: dict[str, dict[str, Any]] = {}

        self._orders: dict[str, OrderState] = {}
        self._by_intent: dict[str, str] = {}
        self._intent_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._doubts: dict[str, tuple[OrderIntent, float]] = {}  # intent_id -> (intent, deadline)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "DeribitConnector":
        """Keys and network from config.py. Testnet unless ``DERIBIT_TESTNET=0`` and
        ``DERIBIT_CONNECTOR_PRODUCTION=1`` are both set."""
        return cls(*deribit_keys(DERIBIT_TESTNET), testnet=DERIBIT_TESTNET,
                   allow_production=DERIBIT_CONNECTOR_PRODUCTION, **kwargs)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DeribitConnector":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- Connector protocol ---------------------------------------------------

    async def submit(self, intent: OrderIntent) -> OrderState:
        async with self._intent_locks[intent.intent_id]:
            known = self._by_intent.get(intent.intent_id)
            if known is not None:
                return await self.order(known)
            try:
                meta = await self._instrument(intent.instrument)
            except DeribitError as exc:
                if exc.code in _NOT_PLACED or exc.code in _OUTCOME_UNKNOWN:
                    raise
                return self._reject(intent, exc.reason)

            existing = await self._find(intent, meta)
            if existing is not None:
                self._doubts.pop(intent.intent_id, None)
                return await self._adopt(intent, existing)
            if intent.intent_id in self._doubts:
                _, deadline = self._doubts[intent.intent_id]
                if self._clock() < deadline:
                    raise ConnectorError(
                        f"{intent.intent_id}: an earlier send may still land, not resending")
                self._doubts.pop(intent.intent_id, None)
            return await self._place(intent, meta)

    async def cancel(self, order_id: str) -> OrderState:
        state = await self.order(order_id)
        if state.done:
            return state
        try:
            raw = await self._rpc("private/cancel", {"order_id": order_id})
        except DeribitError as exc:
            if exc.code in _ALREADY_CLOSED or exc.code == _ORDER_NOT_FOUND:
                return await self.order(order_id)  # it finished first: report how
            raise
        return await self._apply(state, raw)

    async def order(self, order_id: str) -> OrderState:
        state = self._orders.get(order_id)
        if state is not None and state.done:
            return state
        try:
            raw = await self._rpc("private/get_order_state", {"order_id": order_id})
        except DeribitError as exc:
            if exc.code != _ORDER_NOT_FOUND or state is None:
                raise
            # Closed over 30 minutes ago, which get_order_state no longer covers.
            rows = await self._search_history(state.intent.instrument,
                                              lambda o: o.get("order_id") == order_id)
            if not rows:
                raise
            raw = rows[0]
        if state is None:
            return await self._adopt(_intent_from_order(raw), raw)
        return await self._apply(state, raw)

    async def book(self, instrument: str) -> BookTop:
        raw = await self._rpc("public/get_order_book",
                              {"instrument_name": instrument, "depth": 1})
        # Deribit reports an empty side as 0.0, and the contract says None.
        return BookTop(instrument=instrument, best_bid=_positive(raw.get("best_bid_price")),
                       best_ask=_positive(raw.get("best_ask_price")),
                       bid_size=_positive(raw.get("best_bid_amount")),
                       ask_size=_positive(raw.get("best_ask_amount")),
                       mark=_positive(raw.get("mark_price")),
                       ts=(raw.get("timestamp") or 0) / 1000)

    async def positions(self) -> list[Position]:
        out: list[Position] = []
        for currency in self.position_currencies:
            rows = await self._rpc("private/get_positions", {"currency": currency})
            out.extend(Position(instrument=r["instrument_name"], size=float(r["size"]),
                                avg_price=float(r.get("average_price") or 0.0),
                                delta=r.get("delta"))
                       for r in rows if r.get("size"))
        return out

    async def account(self) -> AccountState:
        raw = await self._rpc("private/get_account_summary", {"currency": self.currency})
        return AccountState(currency=self.currency, equity=float(raw["equity"]),
                            initial_margin=float(raw["initial_margin"]),
                            maintenance_margin=float(raw["maintenance_margin"]),
                            available_funds=float(raw["available_funds"]))

    async def healthy(self) -> bool:
        """False unless a private round trip works right now and nothing is in doubt."""
        if self._clock() < self._down_until:
            return False
        try:
            await self._access_token()
            await self._settle_doubts()
            if _locked(await self._rpc("public/status"), self.currency):
                return False
            await self._rpc("private/get_account_summary", {"currency": self.currency})
        except ConnectorError as exc:
            logger.warning("Deribit connector unhealthy: %s", exc)
            return False
        return not self._doubts

    async def diagnostics(self) -> dict[str, Any]:
        """What the status screen shows. Read-only, and never includes the secret."""
        out: dict[str, Any] = {
            "network": "testnet" if self.testnet else "production",
            "configured": bool(self._client_id and self._client_secret),
            "authenticated": False, "scope": None, "locked": None,
            "healthy": False, "unreachable": False, "error": None,
        }
        try:
            out["locked"] = _locked(await self._rpc("public/status"), self.currency)
            await self._access_token()
            out |= {"authenticated": True, "scope": self._scope}
            out["healthy"] = await self.healthy()
        except ConnectorError as exc:
            out["error"] = str(exc)
            out["unreachable"] = isinstance(exc, TransportError)
        return out

    # -- placing --------------------------------------------------------------

    async def _place(self, intent: OrderIntent, meta: dict[str, Any]) -> OrderState:
        price = intent.limit_price
        tif = TIME_IN_FORCE[intent.time_in_force]
        if intent.order_type == "market":
            book = await self.book(intent.instrument)
            price = _market_limit(intent, book, meta)
            if price is None:
                return self._reject(intent, "no liquidity inside max_slippage_bps")
            tif = TIME_IN_FORCE["IOC"]

        lands_by = self._clock() + self._window
        params: dict[str, Any] = {
            "instrument_name": intent.instrument,
            "amount": intent.qty,
            "type": "limit",
            "price": price,
            "time_in_force": tif,
            "label": intent.intent_id,
            "reduce_only": intent.reduce_only,
            "valid_until": int((lands_by + self._server_offset) * 1000),
        }
        if intent.order_type == "post_only":
            # Without reject_post_only Deribit quietly reprices instead of refusing.
            params |= {"post_only": True, "reject_post_only": True}

        try:
            result = await self._rpc(f"private/{intent.side}", params)
        except TransportError as exc:
            if exc.maybe_sent:
                return await self._resolve_unknown(intent, meta, lands_by, str(exc))
            raise
        except DeribitError as exc:
            if exc.code in _OUTCOME_UNKNOWN:
                return await self._resolve_unknown(intent, meta, lands_by, str(exc))
            if exc.code in _NOT_PLACED:
                raise
            return self._reject(intent, exc.reason)
        return await self._adopt(intent, result["order"], result.get("trades"))

    async def _resolve_unknown(self, intent: OrderIntent, meta: dict[str, Any],
                               lands_by: float, why: str) -> OrderState:
        """Wait out ``valid_until``, then the label says whether the order exists."""
        # The margin covers an engine that took the order right at the deadline.
        deadline = lands_by + 2.0
        self._doubts[intent.intent_id] = (intent, deadline)
        logger.error("%s: outcome unknown (%s); checking by label", intent.intent_id, why)
        await self._sleep(max(deadline - self._clock(), 0.0))
        found = await self._find(intent, meta)  # on failure the doubt stays: healthy is False
        self._doubts.pop(intent.intent_id, None)  # healthy may have settled it meanwhile
        if found is not None:
            return await self._adopt(intent, found)
        self._mark_down()
        raise ConnectorError(f"{intent.intent_id}: send failed and no order landed ({why})")

    async def _settle_doubts(self) -> None:
        for intent_id, (intent, deadline) in list(self._doubts.items()):
            found = await self._find(intent, await self._instrument(intent.instrument))
            if found is not None:
                self._doubts.pop(intent_id, None)
                await self._adopt(intent, found)
            elif self._clock() >= deadline:
                self._doubts.pop(intent_id, None)

    async def _find(self, intent: OrderIntent, meta: dict[str, Any]) -> dict[str, Any] | None:
        """The order already placed under this intent's label, or None if there is none."""
        label = intent.intent_id
        try:
            rows = await self._rpc("private/get_order_state_by_label",
                                   {"currency": meta["settlement_currency"], "label": label})
        except DeribitError as exc:
            if exc.code != _ORDER_NOT_FOUND:
                raise
            rows = []
        rows = [r for r in rows if r.get("label") == label]
        if not rows:
            rows = await self._search_history(intent.instrument,
                                              lambda o: o.get("label") == label)
        if not rows:
            return None
        if len(rows) > 1:
            logger.error("%s: %d orders share this label, tracking the oldest", label, len(rows))
        return min(rows, key=lambda r: r.get("creation_timestamp") or 0)

    async def _search_history(self, instrument: str,
                              match: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        for page in range(_HISTORY_MAX_PAGES):
            rows = await self._rpc("private/get_order_history_by_instrument", {
                "instrument_name": instrument, "count": _HISTORY_PAGE,
                "offset": page * _HISTORY_PAGE, "include_old": True,
                "include_unfilled": True, "historical": True,
            })
            hits = [r for r in rows if match(r)]
            if hits or len(rows) < _HISTORY_PAGE:
                return hits
        # Saying "not found" here could double an order, so refuse to conclude.
        raise ConnectorError(f"{instrument}: order history too long to prove an order is absent")

    # -- order state ----------------------------------------------------------

    async def _adopt(self, intent: OrderIntent, raw: dict[str, Any],
                     trades: list[dict[str, Any]] | None = None) -> OrderState:
        order_id = str(raw["order_id"])
        state = self._orders.get(order_id)
        if state is None:
            state = OrderState(order_id=order_id, intent=intent, status=OrderStatus.OPEN)
            self._orders[order_id] = state
            self._by_intent[intent.intent_id] = order_id
        return await self._apply(state, raw, trades)

    async def _apply(self, state: OrderState, raw: dict[str, Any],
                     trades: list[dict[str, Any]] | None = None) -> OrderState:
        status = _STATUS.get(raw.get("order_state", ""))
        if status is None:
            raise ConnectorError(f"{state.order_id}: unknown state {raw.get('order_state')!r}")
        # Before the status changes, so a failure here can't leave a done order
        # with fills missing: the algo sizes the next child from filled_qty.
        state.fills = await self._fills(state, raw, trades)
        state.status = status
        if status is OrderStatus.REJECTED:
            state.reject_reason = raw.get("cancel_reason") or "rejected by Deribit"
        return state

    async def _fills(self, state: OrderState, raw: dict[str, Any],
                     trades: list[dict[str, Any]] | None) -> list[Fill]:
        filled = float(raw.get("filled_amount") or 0.0)
        if filled <= 0:
            return []
        rows = {str(t["trade_id"]): t for t in trades or []}
        # A fresh trade can lag the order. Past 24 hours only history has them.
        for historical in (False, False, True):
            if _covers(rows, filled):
                break
            found = await self._rpc("private/get_user_trades_by_order", {
                "order_id": state.order_id, "sorting": "asc", "historical": historical})
            rows |= {str(t["trade_id"]): t for t in found}
            if not _covers(rows, filled):
                await self._sleep(0.2)
        if not _covers(rows, filled):
            raise ConnectorError(f"{state.order_id}: order shows {filled} filled, "
                                 "trades do not add up to it")
        intent = state.intent
        return [Fill(intent_id=intent.intent_id, order_id=state.order_id,
                     instrument=intent.instrument, side=intent.side, qty=float(t["amount"]),
                     price=float(t["price"]), fee=float(t.get("fee") or 0.0),
                     maker=t.get("liquidity") == "M", trade_id=trade_id,
                     ts=(t.get("timestamp") or 0) / 1000)
                for trade_id, t in rows.items()]

    def _reject(self, intent: OrderIntent, reason: str) -> OrderState:
        """Nothing exists on Deribit, so the id is ours. A resend returns this rejection."""
        state = OrderState(order_id=f"rejected-{intent.intent_id}", intent=intent,
                           status=OrderStatus.REJECTED, reject_reason=reason)
        self._orders[state.order_id] = state
        self._by_intent[intent.intent_id] = state.order_id
        return state

    # -- transport ------------------------------------------------------------

    async def _instrument(self, name: str) -> dict[str, Any]:
        if name not in self._instruments:
            self._instruments[name] = await self._rpc("public/get_instrument",
                                                      {"instrument_name": name})
        return self._instruments[name]

    async def _access_token(self) -> str:
        async with self._auth_lock:
            if self._token and self._clock() < self._token_expiry - 30:
                return self._token
            if not self._client_id or not self._client_secret:
                raise ConnectorError("Deribit API credentials are not configured")
            result = await self._post("public/auth", {
                "grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._client_secret}, token=None)
            _check_scope(result.get("scope", ""))
            self._scope = result.get("scope", "")
            self._token = result["access_token"]
            self._token_expiry = self._clock() + float(result.get("expires_in", 900))
            return self._token

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        private = method.startswith("private/")
        for retry_auth in (True, False):
            token = await self._access_token() if private else None
            try:
                return await self._post(method, params or {}, token)
            except DeribitError as exc:
                if exc.code in _RATE_LIMITED:
                    self._mark_down()
                if exc.code == _UNAUTHORIZED and private and retry_auth:
                    self._token = None  # expired early or revoked; nothing was done
                    continue
                raise
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def _post(self, method: str, params: dict[str, Any], token: str | None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._http.post(f"{self.url}/{method}", json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TransportError(f"{method}: cannot reach Deribit ({exc!r})", False) from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"{method}: no response ({exc!r})", True) from exc

        if resp.status_code == 429:
            self._mark_down()
            raise DeribitError(10028, "too_many_requests")
        try:
            data = resp.json()  # Deribit errors arrive as HTTP 400 with a JSON body
        except ValueError as exc:
            raise TransportError(f"{method}: HTTP {resp.status_code}, not JSON", True) from exc
        if "usOut" in data:
            self._server_offset = data["usOut"] / 1e6 - self._clock()
        if "error" in data:
            err = data["error"]
            raise DeribitError(err.get("code", 0), err.get("message", "error"), err.get("data"))
        if "result" not in data:
            raise TransportError(f"{method}: HTTP {resp.status_code}, no result", True)
        return data["result"]

    def _mark_down(self) -> None:
        self._down_until = self._clock() + self._rate_cooldown


def deribit_keys(testnet: bool) -> tuple[str, str]:
    """The key for a network. The shared DERIBIT_CLIENT_* pair belongs to whichever
    network DERIBIT_TESTNET names, so it can never be sent to the other one."""
    if testnet and DERIBIT_TESTNET_CLIENT_ID:
        return DERIBIT_TESTNET_CLIENT_ID, DERIBIT_TESTNET_CLIENT_SECRET
    if testnet == DERIBIT_TESTNET:
        return DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
    return "", ""


_connector: DeribitConnector | None = None


def get_connector() -> DeribitConnector:
    """The one connector for this process. Trading code gets it here and nowhere else:
    a second instance would have its own memory of what was submitted."""
    global _connector
    if _connector is None:
        _connector = DeribitConnector.from_env()
    return _connector


def _check_scope(scope: str) -> None:
    """The key must be able to trade and must not be able to move funds."""
    granted = set(scope.split())
    if "trade:read_write" not in granted:
        raise ConnectorError(f"API key lacks trade:read_write (scope: {scope!r})")
    forbidden = granted & {"wallet:read_write", "custody:read_write"}
    if forbidden:
        raise ConnectorError(f"refusing an API key that can move funds: {sorted(forbidden)}")


def _locked(status: dict[str, Any], currency: str) -> bool:
    """Is trading in ``currency`` locked? ``locked`` alone also covers unrelated indices."""
    if str(status.get("locked", False)).lower() == "false":
        return False
    indices = status.get("locked_indices") or []
    prefix = f"{currency.lower()}_"
    return not indices or any(i.startswith(prefix) for i in indices)


def _positive(value: float | None) -> float | None:
    return value if value else None


def _covers(trades: dict[str, dict[str, Any]], filled: float) -> bool:
    return sum(float(t["amount"]) for t in trades.values()) >= filled * (1 - _FILL_EPSILON)


def _tick_at(price: float, meta: dict[str, Any]) -> float:
    """Option ticks widen with price: ``tick_size_steps`` overrides ``tick_size`` above each level."""
    tick = float(meta["tick_size"])
    for step in sorted(meta.get("tick_size_steps") or [], key=lambda s: s["above_price"]):
        if price >= step["above_price"]:
            tick = float(step["tick_size"])
    return tick


def _market_limit(intent: OrderIntent, book: BookTop, meta: dict[str, Any]) -> float | None:
    """Worst price within ``max_slippage_bps`` of mid, or None when the touch is outside it.

    Same test as PaperConnector. Rounded toward our own side, like the algo, so
    the tick grid can't take the order past the limit.
    """
    buy = intent.side == "buy"
    touch = book.best_ask if buy else book.best_bid
    if touch is None or book.mid is None:
        return None
    slip = (intent.max_slippage_bps or 0) / 1e4
    worst = book.mid * (1 + slip if buy else 1 - slip)
    if (touch > worst) if buy else (touch < worst):
        return None
    tick = _tick_at(worst, meta)
    ticks = math.floor(worst / tick + 1e-9) if buy else math.ceil(worst / tick - 1e-9)
    return round(ticks * tick, 10)


def _intent_from_order(raw: dict[str, Any]) -> OrderIntent:
    """Rebuild an intent for an order this process did not place, e.g. after a restart.

    The proposal id never reached Deribit, so it comes back empty.
    """
    price = raw.get("price")
    priced = isinstance(price, (int, float))
    tif = {v: k for k, v in TIME_IN_FORCE.items()}.get(raw.get("time_in_force", ""), "IOC")
    post_only = bool(raw.get("post_only")) and tif != "IOC"
    return OrderIntent(
        intent_id=raw.get("label") or str(raw["order_id"]), proposal_id="",
        instrument=raw["instrument_name"], side=raw["direction"], qty=float(raw["amount"]),
        order_type="post_only" if post_only else ("limit" if priced else "market"),
        limit_price=float(price) if priced else None,
        max_slippage_bps=None if priced else 0.0,
        time_in_force=tif, reduce_only=bool(raw.get("reduce_only")))
