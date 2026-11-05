"""The Deribit ETH connector over HTTP, for the platform's own screens and tools.

Same calls as the Connector protocol, on the connector from ``get_connector``:
testnet unless production has been switched on. Behind the normal sign-in.

    GET  /api/connector/catalog                every call, for the Connectors screen
    GET  /api/connector/healthy                healthy()
    GET  /api/connector/book/{instrument}      book()
    GET  /api/connector/positions              positions()
    GET  /api/connector/account                account()
    GET  /api/connector/orders/{order_id}      order()
    POST /api/connector/orders                 submit()
    POST /api/connector/orders/{order_id}/cancel   cancel()

``submit`` stops on the kill switch, on an unhealthy connector, and on production
until TESTS records it as passed. ``cancel`` never stops: a halted desk must
still be able to clear what is resting. An exchange rejection is a normal
response with status "rejected", and 503 means the connector itself has a problem.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from plgo_options.agents.store import kill_switch_on
from plgo_options.execution.deribit import (
    _NOT_PLACED,
    _OUTCOME_UNKNOWN,
    ConnectorError,
    DeribitConnector,
    DeribitError,
    get_connector,
)
from plgo_options.execution.models import OrderIntent, OrderState
from plgo_options.execution.status import TESTS
from plgo_options.web.auth import current_user

logger = logging.getLogger(__name__)
router = APIRouter()

PYTHON_EXAMPLE = '''\
from plgo_options.agents.store import kill_switch_on
from plgo_options.execution.algo import ExecutionRequest, work_order
from plgo_options.execution.deribit import get_connector

conn = get_connector()  # the one shared connector: testnet unless production is on

book = await conn.book("ETH-PERPETUAL")
positions = await conn.positions()

# Work one order from passive to aggressive, never past limit_price.
# request_id must be unique: re-running the same id places nothing new.
report = await work_order(
    ExecutionRequest(request_id="my-tool-001", proposal_id="p1",
                     instrument="ETH-PERPETUAL", side="buy", qty=10,
                     limit_price=book.best_ask, tick_size=0.05),
    conn,
    halted=kill_switch_on,  # always pass this: it is what stops the desk
)
print(report.outcome, report.filled_qty, report.avg_price)
'''

_INTENT_PARAMS: list[dict[str, Any]] = [
    {"name": "intent_id", "in": "body", "type": "id", "required": True,
     "help": "Unique per order. Sending the same id again returns the first order."},
    {"name": "instrument", "in": "body", "type": "text", "required": True,
     "default": "ETH-PERPETUAL"},
    {"name": "side", "in": "body", "type": "select", "options": ["buy", "sell"],
     "required": True, "default": "buy"},
    {"name": "qty", "in": "body", "type": "number", "required": True,
     "help": "USD on ETH-PERPETUAL, ETH on options."},
    {"name": "order_type", "in": "body", "type": "select",
     "options": ["limit", "post_only", "market"], "default": "limit"},
    {"name": "limit_price", "in": "body", "type": "number",
     "help": "Required for limit and post_only. USD on the perp, ETH on options."},
    {"name": "max_slippage_bps", "in": "body", "type": "number",
     "help": "Required for market: sent as a limit IOC at this cap."},
    {"name": "time_in_force", "in": "body", "type": "select",
     "options": ["GTC", "IOC", "good_til_day"], "default": "GTC"},
    {"name": "reduce_only", "in": "body", "type": "bool", "default": False},
]
_ORDER_ID = {"name": "order_id", "in": "path", "type": "text", "required": True}

CATALOG: list[dict[str, Any]] = [
    {"name": "healthy", "method": "GET", "path": "/api/connector/healthy",
     "python": "await conn.healthy()", "sends_orders": False, "params": [],
     "summary": "False whenever the connection, login or rate limit is in doubt."},
    {"name": "book", "method": "GET", "path": "/api/connector/book/{instrument}",
     "python": "await conn.book(instrument)", "sends_orders": False,
     "summary": "Top of the order book. Needs no key.",
     "params": [{"name": "instrument", "in": "path", "type": "text", "required": True,
                 "default": "ETH-PERPETUAL"}]},
    {"name": "positions", "method": "GET", "path": "/api/connector/positions",
     "python": "await conn.positions()", "sends_orders": False, "params": [],
     "summary": "Open positions on the account."},
    {"name": "account", "method": "GET", "path": "/api/connector/account",
     "python": "await conn.account()", "sends_orders": False, "params": [],
     "summary": "Equity, margin and available funds, in ETH."},
    {"name": "order", "method": "GET", "path": "/api/connector/orders/{order_id}",
     "python": "await conn.order(order_id)", "sends_orders": False, "params": [_ORDER_ID],
     "summary": "One order's status and fills."},
    {"name": "submit", "method": "POST", "path": "/api/connector/orders",
     "python": "await conn.submit(OrderIntent(...))", "sends_orders": True,
     "params": _INTENT_PARAMS,
     "summary": "Place an order. Stops on the kill switch or an unhealthy connector."},
    {"name": "cancel", "method": "POST", "path": "/api/connector/orders/{order_id}/cancel",
     "python": "await conn.cancel(order_id)", "sends_orders": True, "params": [_ORDER_ID],
     "summary": "Cancel an order. Works even with the kill switch on."},
]


class SubmitBody(BaseModel):
    intent_id: str = Field(min_length=1, max_length=64)
    instrument: str
    side: Literal["buy", "sell"]
    qty: float
    order_type: Literal["limit", "market", "post_only"] = "limit"
    limit_price: float | None = None
    max_slippage_bps: float | None = None
    time_in_force: Literal["GTC", "IOC", "good_til_day"] = "GTC"
    reduce_only: bool = False


def _connector() -> DeribitConnector:
    try:
        return get_connector()
    except ConnectorError as exc:
        raise HTTPException(503, str(exc)) from exc


def _http_error(exc: ConnectorError) -> HTTPException:
    """A bad instrument or order id is the caller's mistake; the rest is the connector's."""
    if isinstance(exc, DeribitError) and exc.code not in _NOT_PLACED | _OUTCOME_UNKNOWN:
        return HTTPException(400, exc.reason)
    return HTTPException(503, str(exc))


def _order_json(state: OrderState) -> dict[str, Any]:
    return {"order_id": state.order_id, "intent": asdict(state.intent),
            "status": state.status.value, "reject_reason": state.reject_reason,
            "filled_qty": state.filled_qty, "remaining": state.remaining,
            "avg_price": state.avg_price, "fees": state.fees, "done": state.done,
            "fills": [asdict(f) for f in state.fills]}


def _user(request: Request) -> str:
    return (current_user(request) or {}).get("email") or "unknown"


@router.get("/catalog")
async def catalog():
    try:
        network = "Testnet" if get_connector().testnet else "Production"
    except ConnectorError:
        network = "Production, not switched on"
    return {"network": network, "kill_switch": await kill_switch_on(),
            "calls": CATALOG, "python_example": PYTHON_EXAMPLE}


@router.get("/healthy")
async def healthy():
    return {"healthy": await _connector().healthy()}


@router.get("/book/{instrument:path}")
async def book(instrument: str):
    try:
        return asdict(await _connector().book(instrument))
    except ConnectorError as exc:
        raise _http_error(exc) from exc


@router.get("/positions")
async def positions():
    try:
        return {"positions": [asdict(p) for p in await _connector().positions()]}
    except ConnectorError as exc:
        raise _http_error(exc) from exc


@router.get("/account")
async def account():
    try:
        return asdict(await _connector().account())
    except ConnectorError as exc:
        raise _http_error(exc) from exc


@router.get("/orders/{order_id:path}")
async def order(order_id: str):
    try:
        return _order_json(await _connector().order(order_id))
    except ConnectorError as exc:
        raise _http_error(exc) from exc


@router.post("/orders")
async def submit(body: SubmitBody, request: Request):
    connector = _connector()
    if await kill_switch_on():
        raise HTTPException(423, "Kill switch is on: nothing is being sent")
    if not connector.testnet and TESTS["deribit-production"]["status"] != "passed":
        raise HTTPException(403, "Production orders stay off until its tests are recorded as passed")
    if not await connector.healthy():
        raise HTTPException(503, "Connector is not healthy: nothing was sent")
    user = _user(request)
    try:
        intent = OrderIntent(proposal_id=f"manual:{user}", **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    logger.warning("connector order by %s on %s: %s", user,
                   "testnet" if connector.testnet else "PRODUCTION", intent)
    try:
        return _order_json(await connector.submit(intent))
    except ConnectorError as exc:
        raise _http_error(exc) from exc


@router.post("/orders/{order_id:path}/cancel")
async def cancel(order_id: str, request: Request):
    connector = _connector()
    logger.warning("connector cancel by %s: %s", _user(request), order_id)
    try:
        return _order_json(await connector.cancel(order_id))
    except ConnectorError as exc:
        raise _http_error(exc) from exc
