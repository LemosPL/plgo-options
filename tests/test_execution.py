"""Execution bot: interface contract, paper fills, the passive-to-aggressive algo, phase-1 hedge.

No network: every book is stubbed. ``sleep`` is a no-op, so a whole parent
order runs instantly; tests that need the market to move between steps do it
inside the stubbed sleep.
"""

from __future__ import annotations

import pytest

from plgo_options.execution.algo import ExecutionRequest, step_price, work_order
from plgo_options.execution.hedge import perp_amount, rehedge_request
from plgo_options.execution.models import BookTop, OrderIntent, OrderStatus
from plgo_options.execution.paper import PaperConnector, flat_fee
from plgo_options.optimization.delta_hedger import RehedgeDecision

PERP = "ETH-PERPETUAL"


class Books:
    """A mutable stub QuoteSource."""

    def __init__(self, **tops: BookTop) -> None:
        self.tops = {t.instrument: t for t in tops.values()}

    def set(self, instrument: str, bid: float | None, ask: float | None, **kw) -> None:
        self.tops[instrument] = BookTop(instrument=instrument, best_bid=bid, best_ask=ask, **kw)

    async def __call__(self, instrument: str) -> BookTop:
        return self.tops[instrument]


def _books(bid=2500.0, ask=2501.0, **kw) -> Books:
    b = Books()
    b.set(PERP, bid, ask, **kw)
    return b


async def _no_sleep(_s: float) -> None:
    return None


def _req(**kw) -> ExecutionRequest:
    base = dict(request_id="r1", proposal_id="p1", instrument=PERP, side="buy", qty=1000.0,
                limit_price=2502.0, steps=4, tick_size=0.05)
    base.update(kw)
    return ExecutionRequest(**base)


# -- contract ------------------------------------------------------------------

def test_intent_rejects_malformed_orders():
    with pytest.raises(ValueError):
        OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy", qty=1,
                    order_type="limit")
    with pytest.raises(ValueError):
        OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy", qty=1,
                    order_type="market")
    with pytest.raises(ValueError):
        OrderIntent(intent_id="x" * 65, proposal_id="p", instrument=PERP, side="buy", qty=1,
                    limit_price=1.0)


# -- paper connector -----------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_is_idempotent_on_intent_id():
    pc = PaperConnector(_books())
    intent = OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy", qty=10,
                         limit_price=2400.0)
    first = await pc.submit(intent)
    again = await pc.submit(intent)
    assert first.order_id == again.order_id
    assert len(pc.submitted) == 1


@pytest.mark.asyncio
async def test_post_only_that_would_cross_is_rejected():
    pc = PaperConnector(_books())
    s = await pc.submit(OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy",
                                    qty=10, order_type="post_only", limit_price=2501.0))
    assert s.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_marketable_limit_takes_the_touch_up_to_displayed_size():
    pc = PaperConnector(_books(ask_size=400.0), fee_fn=flat_fee(taker_rate=0.001))
    s = await pc.submit(OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy",
                                    qty=1000, limit_price=2505.0, time_in_force="IOC"))
    assert s.status is OrderStatus.CANCELLED  # IOC remainder dropped
    assert s.filled_qty == 400 and s.avg_price == 2501.0
    assert s.fees == pytest.approx(0.001 * 400 * 2501.0)
    assert not s.fills[0].maker


@pytest.mark.asyncio
async def test_resting_order_fills_only_once_the_book_trades_through():
    books = _books()
    pc = PaperConnector(books)
    s = await pc.submit(OrderIntent(intent_id="a", proposal_id="p", instrument=PERP, side="buy",
                                    qty=10, limit_price=2500.0))
    assert (await pc.order(s.order_id)).status is OrderStatus.OPEN  # at the bid: no fill
    books.set(PERP, 2499.0, 2500.0)
    s = await pc.order(s.order_id)
    assert s.status is OrderStatus.FILLED and s.fills[0].maker and s.avg_price == 2500.0
    pos = await pc.positions()
    assert pos[0].size == 10


# -- algo ----------------------------------------------------------------------

def test_step_price_walks_from_touch_to_far_side_on_the_tick_grid():
    book = BookTop(PERP, best_bid=2500.0, best_ask=2501.0)
    buys = [step_price(book, _req(), s) for s in range(5)]
    assert buys == [2500.0, 2500.25, 2500.5, 2500.75, 2501.0]
    sells = [step_price(book, _req(side="sell", limit_price=2499.0), s) for s in range(5)]
    assert sells == [2501.0, 2500.75, 2500.5, 2500.25, 2500.0]


def test_step_price_never_goes_past_the_limit():
    book = BookTop(PERP, best_bid=2500.0, best_ask=2510.0)
    assert step_price(book, _req(limit_price=2503.03), 4) == 2503.0  # capped, rounded down
    assert step_price(book, _req(side="sell", limit_price=2509.97), 4) == 2510.0  # rounded up


def test_step_price_mid_start_and_one_sided_books():
    book = BookTop("ETH-27DEC26-3000-C", best_bid=0.05, best_ask=0.07, mark=0.06)
    req = _req(instrument=book.instrument, start="mid", limit_price=0.08, tick_size=0.0005)
    assert step_price(book, req, 0) == 0.06
    no_ask = BookTop(book.instrument, best_bid=0.05, best_ask=None, mark=0.06)
    assert step_price(no_ask, req, 4) == 0.06  # nothing to cross to: stays at mid
    empty = BookTop(book.instrument, best_bid=None, best_ask=None)
    assert step_price(empty, req, 0) is None


@pytest.mark.asyncio
async def test_static_book_fills_on_the_final_ioc():
    pc = PaperConnector(_books())
    rep = await work_order(_req(), pc, sleep=_no_sleep)
    assert rep.outcome == "filled" and rep.filled_qty == 1000
    assert rep.avg_price == 2501.0
    assert [i.time_in_force for i in pc.submitted] == ["GTC"] * 4 + ["IOC"]
    assert all(c.done for c in rep.children)  # nothing left resting


@pytest.mark.asyncio
async def test_never_pays_past_the_limit():
    pc = PaperConnector(_books(ask=2510.0))
    rep = await work_order(_req(limit_price=2502.0), pc, sleep=_no_sleep)
    assert rep.outcome == "unfilled" and rep.filled_qty == 0
    assert max(i.limit_price for i in pc.submitted) <= 2502.0


@pytest.mark.asyncio
async def test_passive_fill_then_remainder_on_later_steps():
    books = _books(ask_size=None)
    pc = PaperConnector(books)

    async def market_dips(_s: float) -> None:
        # While step 0 rests at 2500, the offer drops through it with 300 shown;
        # by step 1 the book is back where it was, with depth.
        if len(pc.submitted) == 1:
            books.set(PERP, 2499.0, 2500.0, ask_size=300.0)
        else:
            books.set(PERP, 2500.0, 2501.0)

    rep = await work_order(_req(), pc, sleep=market_dips)
    assert rep.outcome == "filled" and rep.filled_qty == 1000
    assert rep.children[0].filled_qty == 300 and rep.children[0].fills[0].maker
    assert pc.submitted[1].qty == 700


@pytest.mark.asyncio
async def test_kill_switch_cancels_the_resting_child_and_stops():
    pc = PaperConnector(_books())
    calls = 0

    async def halted() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1  # trips while step 0 is resting

    rep = await work_order(_req(), pc, halted=halted, sleep=_no_sleep)
    assert rep.outcome == "halted"
    assert len(pc.submitted) == 1
    assert rep.children[0].status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_unhealthy_connector_sends_nothing():
    pc = PaperConnector(_books())
    pc.is_healthy = False
    rep = await work_order(_req(), pc, sleep=_no_sleep)
    assert rep.outcome == "connector_down" and pc.submitted == []


@pytest.mark.asyncio
async def test_rejected_child_stops_the_parent():
    # The paper connector's only reject is a crossing post_only. Plant one under the id
    # the algo's step 0 will use: the idempotent resend hands the algo that rejection.
    pc = PaperConnector(_books(bid=2390.0, ask=2399.0))
    pre = OrderIntent(intent_id="r1-0", proposal_id="p1", instrument=PERP, side="buy", qty=1,
                      order_type="post_only", limit_price=2400.0)
    assert (await pc.submit(pre)).status is OrderStatus.REJECTED
    rep = await work_order(_req(), pc, sleep=_no_sleep)
    assert rep.outcome == "rejected" and "post_only" in rep.detail


# -- phase 1 hedge -------------------------------------------------------------

def _decision(trade_qty: float, perp_position: float = 0.0, breached: bool = True):
    return RehedgeDecision(net_option_delta=-trade_qty - perp_position,
                           perp_position=perp_position, mismatch=-trade_qty, band=5.0,
                           breached=breached, trade_qty=trade_qty)


def test_perp_amount_is_usd_rounded_down_on_the_inverse_perp():
    assert perp_amount(PERP, 2.0, 2500.4) == 5000.0


def test_rehedge_request_sizes_sides_and_limits():
    book = BookTop(PERP, best_bid=2500.0, best_ask=2501.0)
    req = rehedge_request(_decision(-4.0), book, request_id="h1", proposal_id="p1")
    assert req.side == "sell" and req.qty == 10002.0
    assert req.limit_price == pytest.approx(2500.5 * (1 - 0.001))
    assert not req.reduce_only
    buy_back = rehedge_request(_decision(3.0, perp_position=-10.0), book,
                               request_id="h2", proposal_id="p1")
    assert buy_back.side == "buy" and buy_back.reduce_only


def test_rehedge_request_none_inside_band():
    book = BookTop(PERP, best_bid=2500.0, best_ask=2501.0)
    assert rehedge_request(_decision(0.0, breached=False), book,
                           request_id="h", proposal_id="p") is None


@pytest.mark.asyncio
async def test_rehedge_end_to_end_on_paper():
    books = _books()
    pc = PaperConnector(books)
    req = rehedge_request(_decision(2.0), await pc.book(PERP), request_id="h1", proposal_id="p1")
    rep = await work_order(req, pc, sleep=_no_sleep)
    assert rep.outcome == "filled"
    pos = await pc.positions()
    assert pos[0].size == req.qty


# -- shadow run ----------------------------------------------------------------

def _shadow(book_delta: float, perp: float, books: Books, band_usd: float = 150_000.0,
            halted=None):
    from plgo_options.execution.shadow import shadow_tick

    async def book_fn(_a):
        return {"spot": 2500.0, "delta": book_delta}

    async def perp_fn(_a):
        return {"net_qty": perp}

    async def band_fn(_a):
        return band_usd

    async def never():
        return False

    return shadow_tick(book_fn=book_fn, perp_fn=perp_fn, band_fn=band_fn, quotes=books,
                       halted=halted or never, sleep=_no_sleep)


@pytest.mark.asyncio
async def test_shadow_inside_band_trades_nothing():
    d = await _shadow(book_delta=50.0, perp=-20.0, books=_books())  # 30 ETH < 60 ETH band
    assert d["status"] == "inside_band" and d["band_eth"] == 60.0


@pytest.mark.asyncio
async def test_shadow_breach_works_the_hedge_on_paper():
    from plgo_options.execution.shadow import summarize
    d = await _shadow(book_delta=100.0, perp=-10.0, books=_books())  # 90 ETH long: sell
    assert d["status"] == "filled" and d["side"] == "sell"
    assert d["qty_usd"] == pytest.approx(90 * 2500.5, abs=1)
    assert d["avg_price"] == 2500.0 and d["slippage_bps"] == pytest.approx(2.0, abs=0.01)
    assert "would sell" in summarize(d)


@pytest.mark.asyncio
async def test_shadow_respects_the_kill_switch():
    from plgo_options.execution.shadow import summarize

    async def on():
        return True

    d = await _shadow(book_delta=100.0, perp=0.0, books=_books(), halted=on)
    assert d["status"] == "halted" and d["filled_usd"] == 0
    assert "nothing filled" in summarize(d)
