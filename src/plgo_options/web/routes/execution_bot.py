"""Execution bot endpoints (execution/bot.py, execution/threshold.py).

Scheduler-facing (guarded by X-Signals-Token):
    POST /api/execution-bot/tick                 work the open parent orders every 5 s for 55 s

People-facing:
    GET  /api/execution-bot/status               kill switch, last tick, order counts, modes available
    GET  /api/execution-bot/orders               parent orders, open ones first
    POST /api/execution-bot/orders               create one (paper only for now)
    POST /api/execution-bot/preview              an optimizer solution's Deribit legs, priced now
    POST /api/execution-bot/rfq-preview          the same solution as Block RFQ structures (nothing sent)
    POST /api/execution-bot/rfq/send             open a previewed structure as a Block RFQ (testnet only)
    GET  /api/execution-bot/rfq/{id}             its state and the quotes on our side, as what we pay
    POST /api/execution-bot/rfq/{id}/{action}    accept (whole amount at our limit, fill-or-kill) | cancel
    POST /api/execution-bot/orders/batch         create several (the reviewed preview)
    GET  /api/execution-bot/orders/{id}          the order, its ticks and fills
    GET  /api/execution-bot/costs                each leg's fill cost vs the spline, the mark and the mid
    POST /api/execution-bot/orders/{id}/{action} pause | resume | cancel
"""

from __future__ import annotations

import re
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from plgo_options.agents.store import kill_switch_on
from plgo_options.config import SIGNALS_TOKEN
from plgo_options.execution import bot, from_optimizer, rfq, rfq_desk
from plgo_options.execution.deribit import ConnectorError, testnet_configured
from plgo_options.execution import store
from plgo_options.pricing.smile_fit import parse_exp
from plgo_options.web.auth import current_user

router = APIRouter()
INSTRUMENT = re.compile(r"ETH-\d{1,2}[A-Z]{3}\d{2}-\d+-[CP]")
API = "https://www.deribit.com/api/v2/public"


def _require_token(token: str | None) -> None:
    if not SIGNALS_TOKEN:
        return
    if (token or "") != SIGNALS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Signals-Token")


class OrderCreate(BaseModel):
    instrument: str = Field(description="Deribit option, e.g. ETH-25DEC26-2500-C")
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0, description="contracts (ETH)")
    hours: float = Field(gt=1, le=7 * 24, description="deadline, hours from now (more than the final hour)")
    start_vp: float = Field(0.5, ge=0, description="threshold at the start, vol points")
    end_vp: float = Field(0.7, ge=0, description="threshold one hour before the deadline, vol points")
    hard_vp: float = Field(0.9, ge=0, description="threshold in the final hour; never exceeded")
    cap: float = Field(500, ge=1, description="max contracts per take (the option then rests 2 minutes)")
    mode: Literal["paper", "testnet", "live"] = "paper"
    note: str = ""


async def _listed(instrument: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/get_instrument", params={"instrument_name": instrument})
    return r.status_code == 200 and bool((r.json().get("result") or {}).get("is_active"))


@router.post("/tick")
async def tick(x_signals_token: str | None = Header(default=None)):
    _require_token(x_signals_token)
    try:
        return await bot.run_minute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"execution bot: {type(e).__name__}: {e}")


@router.get("/status")
async def status():
    orders = await store.list_orders(limit=500)
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    return {"kill_switch": await kill_switch_on(), "last_tick": await store.last_run(bot.LOCK),
            "counts": counts, "modes": {"paper": True, "testnet": testnet_configured(), "live": False},
            "live_note": "production execution waits for the bot's testnet run"}


@router.get("/orders")
async def orders(limit: int = 100):
    return {"orders": await store.list_orders(limit)}


async def _check(req: OrderCreate, now: float) -> str | None:
    """Why the order cannot be created, or None."""
    if req.mode == "live":
        return "production execution waits for the bot's testnet run"
    if req.mode == "testnet" and not testnet_configured():
        return "no testnet key on this server: set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET"
    if not INSTRUMENT.fullmatch(req.instrument):
        return "instrument must look like ETH-25DEC26-2500-C"
    if not req.start_vp <= req.end_vp <= req.hard_vp:
        return "thresholds must satisfy start_vp <= end_vp <= hard_vp"
    if req.qty != int(req.qty):
        return "qty must be a whole number of contracts"
    if parse_exp(req.instrument.split("-")[1]).timestamp() <= now + req.hours * 3600:
        return "the option expires before the deadline"
    if not await _listed(req.instrument):
        return f"{req.instrument} is not an active Deribit instrument"
    return None


async def _create(req: OrderCreate, by: str, now: float) -> int:
    return await store.create_order(req.instrument, req.side, req.qty, now + req.hours * 3600, req.start_vp,
                                    req.end_vp, req.hard_vp, req.cap, req.mode, by, req.note, now)


@router.post("/orders")
async def create(req: OrderCreate, request: Request):
    now = time.time()
    if err := await _check(req, now):
        raise HTTPException(400, err)
    oid = await _create(req, (current_user(request) or {}).get("email", ""), now)
    return {"id": oid, "order": await store.get_order(oid)}


class Batch(BaseModel):
    orders: list[OrderCreate]


@router.post("/orders/batch")
async def create_batch(req: Batch, request: Request):
    """Every valid order is created; the others come back with their reason."""
    now, by = time.time(), (current_user(request) or {}).get("email", "")
    created, refused = [], []
    for o in req.orders:
        if err := await _check(o, now):
            refused.append({"instrument": o.instrument, "reason": err})
        else:
            created.append(await _create(o, by, now))
    return {"created": created, "refused": refused}


class PreviewReq(BaseModel):
    trades: list[dict]
    asset: str = "ETH"
    start_vp: float = 0.5
    hours: float = 12.0


@router.post("/preview")
async def preview(req: PreviewReq):
    if req.asset != "ETH":
        raise HTTPException(400, f"the execution bot trades Deribit ETH options only, not {req.asset}")
    try:
        return await from_optimizer.preview(req.trades, req.asset, req.start_vp, req.hours)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Deribit: {type(e).__name__}: {e}")


@router.get("/costs")
async def costs(limit: int = 50):
    return {"legs": await store.leg_costs(limit)}


class RfqPreviewReq(BaseModel):
    trades: list[dict]
    asset: str = "ETH"
    unit: int = Field(100, ge=1, description="contracts per unit of the directional structure")
    allowance_vp: float = Field(0.2, ge=0, description="worst price over fair, vol points x the structure's vega")
    box_bps: float = Field(40.0, ge=0, description="a box's worst price over fair, bps of its strike width")
    hedge: bool = Field(False, description="add a future hedge leg for the directional structure's delta")


@router.post("/rfq-preview")
async def rfq_preview(req: RfqPreviewReq):
    if req.asset != "ETH":
        raise HTTPException(400, f"Block RFQ is for Deribit ETH options only, not {req.asset}")
    try:
        return await rfq.preview_rfq(req.trades, req.asset, req.unit, req.allowance_vp, req.box_bps, req.hedge)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Deribit: {type(e).__name__}: {e}")


class RfqLeg(BaseModel):
    instrument: str
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0)


class RfqSend(BaseModel):
    kind: Literal["box", "directional"]
    legs: list[RfqLeg] = Field(min_length=1, max_length=20)
    amount: float = Field(gt=0, description="the preview's amount: limit_eth is per unit of it")
    limit_eth: float = Field(description="the preview's limit per unit, ETH (negative: receive at least)")
    spot: float = Field(gt=0, description="the preview's USD per ETH, to show quotes in dollars")


async def _desk(call):
    try:
        return await call
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ConnectorError as e:
        raise HTTPException(409, str(e))


@router.post("/rfq/send")
async def rfq_send(req: RfqSend, request: Request):
    for x in req.legs:
        if not INSTRUMENT.fullmatch(x.instrument):
            raise HTTPException(400, f"{x.instrument} is not a Deribit ETH option")
    by = (current_user(request) or {}).get("email", "")
    return await _desk(rfq_desk.send([x.model_dump() for x in req.legs], req.kind, req.limit_eth,
                                     req.amount, req.spot, by))


@router.get("/rfq/{rid}")
async def rfq_read(rid: int):
    return await _desk(rfq_desk.read(rid))


@router.post("/rfq/{rid}/{action}")
async def rfq_act(rid: int, action: Literal["accept", "cancel"]):
    if action == "accept" and await kill_switch_on():
        raise HTTPException(409, "the kill switch is on")
    return await _desk(rfq_desk.accept(rid) if action == "accept" else rfq_desk.cancel(rid))


@router.get("/orders/{oid}")
async def detail(oid: int, ticks: int = 2000):
    d = await store.order_detail(oid, ticks)
    if d is None:
        raise HTTPException(404, f"no order {oid}")
    return d


@router.post("/orders/{oid}/{action}")
async def act(oid: int, action: Literal["pause", "resume", "cancel"]):
    o = await store.get_order(oid)
    if o is None:
        raise HTTPException(404, f"no order {oid}")
    if o["status"] in store.FINISHED:
        raise HTTPException(409, f"order {oid} is already {o['status']}")
    if action == "pause":
        await store.set_status(oid, store.PAUSED)
    elif action == "resume":
        if await kill_switch_on():
            raise HTTPException(409, "the kill switch is on: turn it off before resuming")
        if time.time() >= o["deadline_ts"]:
            raise HTTPException(409, "past the deadline: create a new order instead")
        await store.set_status(oid, store.WORKING)
    else:
        await store.set_status(oid, "cancelled", f"cancelled with {o['qty'] - o['filled']:g} unfilled")
    return {"order": await store.get_order(oid)}
