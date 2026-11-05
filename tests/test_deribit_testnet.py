"""test_execution.py's scenarios against the real Deribit testnet.

Needs its own variables, so production keys can never end up here:

    DERIBIT_TESTNET_CLIENT_ID=... DERIBIT_TESTNET_CLIENT_SECRET=... \\
        python -m pytest tests/test_deribit_testnet.py -v

Skipped without them. Trades 10 USD of ETH-PERPETUAL and flattens afterwards.
Testnet locks ETH from time to time (public/status), and then these fail on
the health check at the start: that is the connector being right, try later.

The paper scenarios that need the market to move on cue (a resting order
filling, a reject planted under a later id) have no testnet equivalent here.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from plgo_options.execution.algo import ExecutionRequest, work_order
from plgo_options.execution.deribit import ConnectorError, DeribitConnector
from plgo_options.execution.models import BookTop, OrderIntent, OrderStatus

PERP = "ETH-PERPETUAL"
TICK = 0.05
QTY = 10.0

CLIENT_ID = os.environ.get("DERIBIT_TESTNET_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DERIBIT_TESTNET_CLIENT_SECRET", "")

pytestmark = pytest.mark.skipif(
    not (CLIENT_ID and CLIENT_SECRET),
    reason="set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET")


def _connector(**kw) -> DeribitConnector:
    return DeribitConnector(CLIENT_ID, CLIENT_SECRET, testnet=True, **kw)


@pytest_asyncio.fixture
async def conn():
    c = _connector()
    assert await c.healthy(), "connector unhealthy: bad key, or testnet has ETH locked"
    yield c
    await _flatten(c)
    await c.close()


async def _flatten(c: DeribitConnector) -> None:
    for pos in await c.positions():
        if pos.instrument != PERP:
            continue
        top = await c.book(PERP)
        side = "sell" if pos.size > 0 else "buy"
        price = _on_tick(top.best_bid * 0.995 if pos.size > 0 else top.best_ask * 1.005)
        await c.submit(OrderIntent(intent_id=_id(), proposal_id="cleanup", instrument=PERP,
                                   side=side, qty=abs(pos.size), limit_price=price,
                                   time_in_force="IOC", reduce_only=True))


def _id() -> str:
    return f"plgo-test-{uuid.uuid4().hex[:16]}"


def _on_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def _far_below(top: BookTop) -> float:
    """A buy price that rests: 5% under the bid."""
    return _on_tick(top.best_bid * 0.95)


def _intent(**kw) -> OrderIntent:
    base = dict(intent_id=_id(), proposal_id="testnet", instrument=PERP, side="buy", qty=QTY)
    return OrderIntent(**(base | kw))


# -- reads ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_book_positions_account(conn):
    top = await conn.book(PERP)
    assert 0 < top.best_bid < top.best_ask and top.bid_size and top.mark
    assert isinstance(await conn.positions(), list)
    acct = await conn.account()
    assert acct.currency == "ETH" and acct.equity > 0


# -- the contract --------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_is_idempotent_on_intent_id(conn):
    intent = _intent(limit_price=_far_below(await conn.book(PERP)))
    first = await conn.submit(intent)
    try:
        assert first.status is OrderStatus.OPEN
        assert (await conn.submit(intent)).order_id == first.order_id
        async with _connector() as restarted:
            assert (await restarted.submit(intent)).order_id == first.order_id
    finally:
        await conn.cancel(first.order_id)


@pytest.mark.asyncio
async def test_resend_of_a_cancelled_order_places_nothing_new(conn):
    intent = _intent(limit_price=_far_below(await conn.book(PERP)))
    first = await conn.submit(intent)
    await conn.cancel(first.order_id)
    async with _connector() as restarted:
        again = await restarted.submit(intent)
    assert again.order_id == first.order_id and again.status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_on_a_finished_order_returns_its_final_state(conn):
    s = await conn.submit(_intent(limit_price=_far_below(await conn.book(PERP))))
    done = await conn.cancel(s.order_id)
    assert done.status is OrderStatus.CANCELLED
    async with _connector() as fresh:  # no local copy: has to come from Deribit
        assert (await fresh.cancel(s.order_id)).status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_post_only_that_would_cross_is_rejected(conn):
    top = await conn.book(PERP)
    s = await conn.submit(_intent(order_type="post_only", limit_price=top.best_ask))
    assert s.status is OrderStatus.REJECTED and s.reject_reason
    assert s.fills == []


@pytest.mark.asyncio
async def test_post_only_that_rests(conn):
    s = await conn.submit(_intent(order_type="post_only",
                                  limit_price=_far_below(await conn.book(PERP))))
    assert s.status is OrderStatus.OPEN
    await conn.cancel(s.order_id)


@pytest.mark.asyncio
async def test_good_til_day_rests(conn):
    s = await conn.submit(_intent(time_in_force="good_til_day",
                                  limit_price=_far_below(await conn.book(PERP))))
    assert s.status is OrderStatus.OPEN
    await conn.cancel(s.order_id)


@pytest.mark.asyncio
async def test_exchange_rejection_is_a_state(conn):
    off_tick = _far_below(await conn.book(PERP)) + TICK / 3
    s = await conn.submit(_intent(limit_price=off_tick))
    assert s.status is OrderStatus.REJECTED and s.reject_reason


@pytest.mark.asyncio
async def test_reduce_only_on_a_flat_account_never_adds_a_position(conn):
    top = await conn.book(PERP)
    s = await conn.submit(_intent(reduce_only=True, time_in_force="IOC",
                                  limit_price=_on_tick(top.best_ask * 1.001)))
    assert s.filled_qty == 0 and s.done


@pytest.mark.asyncio
async def test_valid_until_is_in_the_unit_we_send():
    """The docs don't give the unit. Stale orders refused here and live ones accepted
    in every other test prove it: the wrong unit fails one or the other."""
    async with _connector(order_window_seconds=-60.0) as stale:
        assert await stale.healthy()
        top = await stale.book(PERP)
        with pytest.raises(ConnectorError):
            await stale.submit(_intent(limit_price=_far_below(top)))


# -- fills ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_marketable_limit_fills_with_fees_and_shows_in_positions(conn):
    top = await conn.book(PERP)
    s = await conn.submit(_intent(limit_price=_on_tick(top.best_ask * 1.001), time_in_force="IOC"))
    assert s.status is OrderStatus.FILLED and s.filled_qty == QTY
    assert s.avg_price and not s.fills[0].maker
    assert any(p.instrument == PERP for p in await conn.positions())


@pytest.mark.asyncio
async def test_market_order_fills_inside_its_slippage_cap(conn):
    s = await conn.submit(_intent(order_type="market", max_slippage_bps=50.0))
    assert s.status is OrderStatus.FILLED and s.filled_qty == QTY


# -- the algo ------------------------------------------------------------------

def _req(top: BookTop, **kw) -> ExecutionRequest:
    base = dict(request_id=_id()[:40], proposal_id="testnet", instrument=PERP, side="buy",
                qty=QTY, limit_price=_on_tick(top.best_ask * 1.002), steps=2,
                step_seconds=1.0, tick_size=TICK, qty_step=1.0)
    return ExecutionRequest(**(base | kw))


@pytest.mark.asyncio
async def test_work_order_fills_and_leaves_nothing_resting(conn):
    rep = await work_order(_req(await conn.book(PERP)), conn)
    assert rep.outcome == "filled" and rep.filled_qty == QTY
    assert all(c.done for c in rep.children)


@pytest.mark.asyncio
async def test_work_order_never_pays_past_the_limit(conn):
    top = await conn.book(PERP)
    rep = await work_order(_req(top, limit_price=_far_below(top)), conn)
    assert rep.outcome == "unfilled" and rep.filled_qty == 0
    assert all(c.intent.limit_price <= rep.request.limit_price for c in rep.children)


@pytest.mark.asyncio
async def test_kill_switch_cancels_the_resting_child(conn):
    calls = 0

    async def halted() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    top = await conn.book(PERP)
    rep = await work_order(_req(top, limit_price=_far_below(top)), conn, halted=halted)
    assert rep.outcome == "halted" and rep.children[0].status is OrderStatus.CANCELLED
