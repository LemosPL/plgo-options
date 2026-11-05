"""DeribitConnector against a simulated Deribit, no network.

``FakeDeribit`` answers the JSON-RPC the connector sends through an httpx mock
transport and keeps the behaviours that make this connector hard: labels are
not unique, closed orders leave the label lookup after 30 minutes, unfilled
ones are hidden from history unless asked for, empty book sides read 0.0, and
errors arrive as HTTP 400 with a JSON body. The real thing is covered by
test_deribit_testnet.py.
"""

from __future__ import annotations

import json

import httpx
import pytest

from plgo_options.execution.algo import ExecutionRequest, work_order
from plgo_options.execution.deribit import ConnectorError, DeribitConnector
from plgo_options.execution.models import OrderIntent, OrderStatus

PERP = "ETH-PERPETUAL"
OPTION = "ETH-27DEC26-3000-C"
INSTRUMENTS = {
    PERP: {"settlement_currency": "ETH", "tick_size": 0.05, "tick_size_steps": []},
    OPTION: {"settlement_currency": "ETH", "tick_size": 0.0001,
             "tick_size_steps": [{"above_price": 0.005, "tick_size": 0.0005}]},
}
OK_SCOPE = "connection mainaccount account:read trade:read_write wallet:none"


class FakeDeribit:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.books = {PERP: (2500.0, 2501.0, 1e6, 1e6), OPTION: (0.05, 0.07, 10.0, 10.0)}
        self.mark = 2500.5
        self.orders: list[dict] = []
        self.trades: list[dict] = []
        self.positions: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.scope = OK_SCOPE
        self.locked: dict = {"locked": "false", "locked_indices": []}
        self.bad_tokens: set[str] = set()
        self.tokens = 0
        self.fail_next: dict[str, Exception | tuple[int, str]] = {}  # method -> failure
        self.lose_response_of: set[str] = set()  # the order lands, the answer doesn't arrive
        self.after_buy: dict[str, Exception] = {}  # failures armed once an order is sent

    # -- what the connector sees ----------------------------------------------

    async def clock_sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append((method, params))

        if method == "private/buy":
            self.fail_next |= self.after_buy
            self.after_buy = {}
        failure = self.fail_next.pop(method, None)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return self._error(*failure)
        if method.startswith("private/"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not token or token in self.bad_tokens:
                return self._error(13009, "unauthorized")

        result = getattr(self, "_" + method.split("/")[1])(params)
        if isinstance(result, httpx.Response):
            return result
        if method in self.lose_response_of:
            self.lose_response_of.remove(method)
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result,
                                         "usOut": int(self.now * 1e6)})

    @staticmethod
    def _error(code: int, message: str, data=None) -> httpx.Response:
        return httpx.Response(400, json={"jsonrpc": "2.0", "error": {
            "code": code, "message": message, "data": data}})

    # -- JSON-RPC methods -----------------------------------------------------

    def _auth(self, _p):
        self.tokens += 1
        return {"access_token": f"tok{self.tokens}", "expires_in": 900, "scope": self.scope}

    def _status(self, _p):
        return self.locked

    def _get_instrument(self, p):
        if p["instrument_name"] not in INSTRUMENTS:
            return self._error(13020, "not_found")
        return INSTRUMENTS[p["instrument_name"]]

    def _get_order_book(self, p):
        bid, ask, bid_size, ask_size = self.books[p["instrument_name"]]
        return {"best_bid_price": bid, "best_ask_price": ask, "best_bid_amount": bid_size,
                "best_ask_amount": ask_size, "mark_price": self.mark, "timestamp": 1_000_000}

    def _buy(self, p):
        return self._place("buy", p)

    def _sell(self, p):
        return self._place("sell", p)

    def _place(self, side: str, p: dict):
        if p["valid_until"] < self.now * 1000:
            return self._error(13888, "timed_out")
        if p["amount"] > 1e7:
            return self._error(10009, "not_enough_funds")
        bid, ask, bid_size, ask_size = self.books[p["instrument_name"]]
        touch, size = (ask, ask_size) if side == "buy" else (bid, bid_size)
        crosses = touch and (p["price"] >= touch if side == "buy" else p["price"] <= touch)
        if p.get("post_only") and crosses:
            if p.get("reject_post_only"):
                return self._error(11054, "post_only_reject")
            p = p | {"price": bid if side == "buy" else ask}  # silent reprice
            crosses = False
        order = {"order_id": f"ETH-{len(self.orders) + 1}", "label": p["label"],
                 "instrument_name": p["instrument_name"], "direction": side,
                 "amount": p["amount"], "price": p["price"], "filled_amount": 0.0,
                 "order_state": "open", "time_in_force": p["time_in_force"],
                 "post_only": bool(p.get("post_only")), "reduce_only": p["reduce_only"],
                 "creation_timestamp": len(self.orders) + 1, "closed_at": None}
        self.orders.append(order)
        trades = []
        if crosses:
            trades = [self.fill(order, min(size, p["amount"]), touch, "T")]
        if order["filled_amount"] >= order["amount"]:
            self.close(order, "filled")
        elif p["time_in_force"] == "immediate_or_cancel":
            self.close(order, "cancelled")
        return {"order": self._public(order), "trades": trades}

    def fill(self, order: dict, qty: float, price: float, liquidity: str) -> dict:
        trade = {"trade_id": f"t{len(self.trades) + 1}", "order_id": order["order_id"],
                 "amount": qty, "price": price, "fee": 0.25, "liquidity": liquidity,
                 "timestamp": 1_000_000}
        self.trades.append(trade)
        order["filled_amount"] += qty
        return trade

    def close(self, order: dict, state: str) -> None:
        order["order_state"] = state
        order["closed_at"] = self.now

    @staticmethod
    def _public(order: dict) -> dict:
        return {k: v for k, v in order.items() if k != "closed_at"}

    def _recent(self, order: dict) -> bool:
        return order["closed_at"] is None or self.now - order["closed_at"] < 1800

    def _get_order_state(self, p):
        for o in self.orders:
            if o["order_id"] == p["order_id"] and self._recent(o):
                return self._public(o)
        return self._error(10004, "order_not_found")

    def _get_order_state_by_label(self, p):
        return [self._public(o) for o in self.orders if o["label"] == p["label"]
                and self._recent(o)]

    def _get_order_history_by_instrument(self, p):
        return [self._public(o) for o in self.orders
                if o["instrument_name"] == p["instrument_name"] and o["closed_at"] is not None
                and (o["filled_amount"] > 0 or p.get("include_unfilled"))]

    def _get_user_trades_by_order(self, p):
        return [t for t in self.trades if t["order_id"] == p["order_id"]]

    def _cancel(self, p):
        for o in self.orders:
            if o["order_id"] == p["order_id"]:
                if o["order_state"] != "open":
                    return self._error(11044, "not_open_order")
                self.close(o, "cancelled")
                return self._public(o)
        return self._error(10004, "order_not_found")

    def _get_positions(self, p):
        return self.positions if p["currency"] == "ETH" else []

    def _get_account_summary(self, _p):
        return {"equity": 12.5, "initial_margin": 2.0, "maintenance_margin": 1.0,
                "available_funds": 10.5}


@pytest.fixture
def fake() -> FakeDeribit:
    return FakeDeribit()


def _connector(fake: FakeDeribit, **kw) -> DeribitConnector:
    return DeribitConnector("id", "secret", transport=httpx.MockTransport(fake.handler),
                            clock=fake.clock, sleep=fake.clock_sleep, **kw)


def _intent(intent_id: str = "a", **kw) -> OrderIntent:
    base = dict(proposal_id="p", instrument=PERP, side="buy", qty=10.0, limit_price=2400.0)
    return OrderIntent(intent_id=intent_id, **(base | kw))


def _sent(fake: FakeDeribit, method: str = "private/buy") -> list[dict]:
    return [p for m, p in fake.calls if m == method]


# -- order parameters ----------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tif,wire", [("GTC", "good_til_cancelled"),
                                      ("IOC", "immediate_or_cancel"),
                                      ("good_til_day", "good_til_day")])
async def test_time_in_force_on_the_wire(fake, tif, wire):
    await _connector(fake).submit(_intent(time_in_force=tif))
    assert _sent(fake)[0]["time_in_force"] == wire


@pytest.mark.asyncio
async def test_label_reduce_only_and_side_on_the_wire(fake):
    await _connector(fake).submit(_intent(side="sell", limit_price=2600.0, reduce_only=True))
    sent = _sent(fake, "private/sell")[0]
    assert sent["label"] == "a" and sent["reduce_only"] is True and sent["amount"] == 10.0


@pytest.mark.asyncio
async def test_post_only_never_gets_repriced(fake):
    s = await _connector(fake).submit(_intent(order_type="post_only", limit_price=2501.0))
    assert s.status is OrderStatus.REJECTED and "post_only" in s.reject_reason
    assert _sent(fake)[0]["reject_post_only"] is True
    assert fake.orders == []


@pytest.mark.asyncio
async def test_market_is_a_limit_ioc_at_the_slippage_cap(fake):
    s = await _connector(fake).submit(_intent(order_type="market", limit_price=None,
                                              max_slippage_bps=20.0))
    sent = _sent(fake)[0]
    assert sent["type"] == "limit" and sent["time_in_force"] == "immediate_or_cancel"
    assert sent["price"] == pytest.approx(2505.5)  # 2500.5 * 1.002 = 2505.501, floored to tick
    assert s.status is OrderStatus.FILLED and s.avg_price == 2501.0


@pytest.mark.asyncio
async def test_market_cap_uses_the_option_tick_at_that_price(fake):
    await _connector(fake).submit(_intent(instrument=OPTION, side="sell", qty=1.0, order_type="market",
                                          limit_price=None, max_slippage_bps=1900.0))
    assert _sent(fake, "private/sell")[0]["price"] == 0.049  # 0.0486 rounded up to a 0.0005 tick


@pytest.mark.asyncio
async def test_market_outside_slippage_sends_nothing(fake):
    fake.books[PERP] = (2500.0, 2600.0, 1e6, 1e6)
    s = await _connector(fake).submit(_intent(order_type="market", limit_price=None,
                                              max_slippage_bps=5.0))
    assert s.status is OrderStatus.REJECTED and "max_slippage_bps" in s.reject_reason
    assert _sent(fake) == []


# -- idempotency ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_resend_in_the_same_process_places_nothing(fake):
    c = _connector(fake)
    first = await c.submit(_intent())
    again = await c.submit(_intent())
    assert again.order_id == first.order_id and len(fake.orders) == 1


@pytest.mark.asyncio
async def test_resend_after_restart_finds_the_open_order(fake):
    first = await _connector(fake).submit(_intent())
    again = await _connector(fake).submit(_intent())
    assert again.order_id == first.order_id and len(fake.orders) == 1


@pytest.mark.asyncio
async def test_resend_after_restart_finds_an_order_past_the_30_minute_window(fake):
    first = await _connector(fake).submit(_intent())
    await _connector(fake).cancel(first.order_id)
    fake.now += 3600  # gone from get_order_state_by_label, and it never filled
    again = await _connector(fake).submit(_intent())
    assert again.order_id == first.order_id and again.status is OrderStatus.CANCELLED
    assert len(fake.orders) == 1


@pytest.mark.asyncio
async def test_too_much_history_refuses_rather_than_risking_a_double(fake, monkeypatch):
    monkeypatch.setattr("plgo_options.execution.deribit._HISTORY_PAGE", 1)
    first = await _connector(fake).submit(_intent("other"))
    await _connector(fake).cancel(first.order_id)
    with pytest.raises(ConnectorError, match="too long"):
        await _connector(fake).submit(_intent("new"))
    assert len(fake.orders) == 1


# -- unknown outcomes ----------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_after_the_order_landed_adopts_it(fake):
    c = _connector(fake)
    fake.lose_response_of.add("private/buy")
    s = await c.submit(_intent())
    assert s.status is OrderStatus.OPEN and s.order_id == "ETH-1"
    assert len(fake.orders) == 1 and await c.healthy()


@pytest.mark.asyncio
async def test_timeout_before_the_order_landed_raises_and_does_not_resend(fake):
    c = _connector(fake)
    fake.fail_next["private/buy"] = httpx.ReadTimeout("no answer")
    with pytest.raises(ConnectorError, match="no order landed"):
        await c.submit(_intent())
    assert fake.orders == [] and len(_sent(fake)) == 1


@pytest.mark.asyncio
async def test_lookup_outage_keeps_the_doubt_and_blocks_a_resend(fake):
    c = _connector(fake)
    fake.lose_response_of.add("private/buy")
    fake.after_buy["private/get_order_state_by_label"] = httpx.ConnectError("down")
    with pytest.raises(ConnectorError):
        await c.submit(_intent())
    assert c._doubts and len(fake.orders) == 1
    # The connection comes back: healthy settles the doubt by finding the order.
    assert await c.healthy()
    again = await c.submit(_intent())
    assert again.order_id == "ETH-1" and len(fake.orders) == 1


@pytest.mark.asyncio
async def test_doubt_blocks_the_resend_until_the_deadline(fake):
    c = _connector(fake, order_window_seconds=5.0)
    fake.fail_next["private/buy"] = httpx.ReadTimeout("no answer")
    fake.after_buy["private/get_order_state_by_label"] = httpx.ConnectError("down")
    with pytest.raises(ConnectorError):
        await c.submit(_intent())
    fake.now -= 3  # inside the window: the first send could still land
    assert not await c.healthy()
    with pytest.raises(ConnectorError, match="still land"):
        await c.submit(_intent())
    assert len(_sent(fake)) == 1


@pytest.mark.asyncio
async def test_never_connected_raises_without_waiting(fake):
    c = _connector(fake)
    fake.fail_next["private/buy"] = httpx.ConnectError("refused")
    before = fake.now
    with pytest.raises(ConnectorError, match="cannot reach"):
        await c.submit(_intent())
    assert fake.now == before and not c._doubts


@pytest.mark.asyncio
async def test_server_error_with_unknown_outcome_is_checked_by_label(fake):
    c = _connector(fake)
    fake.fail_next["private/buy"] = (11094, "internal_server_error")
    with pytest.raises(ConnectorError, match="no order landed"):
        await c.submit(_intent())
    assert ("private/get_order_state_by_label",
            {"currency": "ETH", "label": "a"}) in fake.calls[-3:]


@pytest.mark.asyncio
async def test_expired_order_window_is_a_fault_not_a_rejection(fake):
    fake.fail_next["private/buy"] = (13888, "timed_out")
    with pytest.raises(ConnectorError):
        await _connector(fake).submit(_intent())


# -- rejections ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_rejection_is_a_state_not_an_exception(fake):
    c = _connector(fake)
    s = await c.submit(_intent(qty=2e7))
    assert s.status is OrderStatus.REJECTED and s.reject_reason == "not_enough_funds"
    assert (await c.submit(_intent(qty=2e7))).order_id == s.order_id  # resend: same rejection
    assert len(_sent(fake)) == 1
    assert (await c.order(s.order_id)).status is OrderStatus.REJECTED
    assert (await c.cancel(s.order_id)).status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_unknown_instrument_is_rejected(fake):
    s = await _connector(fake).submit(_intent(instrument="ETH-NOPE"))
    assert s.status is OrderStatus.REJECTED and "not_found" in s.reject_reason


@pytest.mark.asyncio
async def test_rejection_detail_reaches_the_reason(fake):
    fake.fail_next["private/buy"] = (-32602, "Invalid params", {"param": "price"})
    s = await _connector(fake).submit(_intent())
    assert s.status is OrderStatus.REJECTED and "price" in s.reject_reason


# -- cancel and fills ----------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_open_order(fake):
    c = _connector(fake)
    s = await c.submit(_intent())
    assert (await c.cancel(s.order_id)).status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_after_a_fill_raced_it_returns_the_final_state(fake):
    c = _connector(fake)
    s = await c.submit(_intent())
    real_state = fake._get_order_state
    # order() sees it open, then it fills before the cancel reaches the engine.
    def fill_after_read(p):
        out = real_state(p)
        fake._get_order_state = real_state
        fake.fill(fake.orders[0], 10.0, 2400.0, "M")
        fake.close(fake.orders[0], "filled")
        return out
    fake._get_order_state = fill_after_read
    final = await c.cancel(s.order_id)
    assert final.status is OrderStatus.FILLED and final.fills[0].maker


@pytest.mark.asyncio
async def test_cancel_finished_order_does_not_call_cancel(fake):
    c = _connector(fake)
    s = await c.submit(_intent(limit_price=2501.0))
    assert s.status is OrderStatus.FILLED
    assert (await c.cancel(s.order_id)).status is OrderStatus.FILLED
    assert _sent(fake, "private/cancel") == []


@pytest.mark.asyncio
async def test_order_past_the_30_minute_window_comes_from_history(fake):
    c = _connector(fake)
    s = await c.submit(_intent())
    fake.fill(fake.orders[0], 4.0, 2400.0, "M")
    fake.close(fake.orders[0], "cancelled")
    fake.now += 3600
    done = await c.order(s.order_id)
    assert done.status is OrderStatus.CANCELLED and done.filled_qty == 4.0  # partial survives


@pytest.mark.asyncio
async def test_order_unknown_to_this_process_is_rebuilt(fake):
    s = await _connector(fake).submit(_intent(order_type="post_only"))
    other = await _connector(fake).order(s.order_id)
    assert other.intent.intent_id == "a" and other.intent.order_type == "post_only"
    assert other.intent.limit_price == 2400.0


@pytest.mark.asyncio
async def test_partial_ioc_reports_fills_and_fees(fake):
    fake.books[PERP] = (2500.0, 2501.0, 1e6, 4.0)
    s = await _connector(fake).submit(_intent(limit_price=2505.0, time_in_force="IOC"))
    assert s.status is OrderStatus.CANCELLED and s.filled_qty == 4.0 and s.remaining == 6.0
    assert s.fees == 0.25 and not s.fills[0].maker


@pytest.mark.asyncio
async def test_lagging_trades_are_retried_then_raise_rather_than_underreport(fake):
    c = _connector(fake)
    s = await c.submit(_intent())
    fake.orders[0]["filled_amount"] = 10.0  # order says filled, trades don't show it yet
    fake.orders[0]["order_state"] = "filled"
    with pytest.raises(ConnectorError, match="add up"):
        await c.order(s.order_id)
    assert s.status is OrderStatus.OPEN  # never "done" with fills missing
    fake.trades.append({"trade_id": "t1", "order_id": s.order_id, "amount": 10.0,
                        "price": 2400.0, "fee": 0.0, "liquidity": "M", "timestamp": 1})
    assert (await c.order(s.order_id)).filled_qty == 10.0


# -- book, positions, account --------------------------------------------------

@pytest.mark.asyncio
async def test_book_reads_an_empty_side_as_none(fake):
    fake.books[OPTION] = (0.0, 0.07, 0.0, 5.0)
    fake.mark = 0.0
    top = await _connector(fake).book(OPTION)
    assert top.best_bid is None and top.bid_size is None and top.mark is None
    assert top.best_ask == 0.07 and top.ask_size == 5.0


@pytest.mark.asyncio
async def test_book_is_never_cached(fake):
    c = _connector(fake)
    await c.book(PERP)
    fake.books[PERP] = (2600.0, 2601.0, 1.0, 1.0)
    assert (await c.book(PERP)).best_bid == 2600.0


@pytest.mark.asyncio
async def test_positions_drops_flat_lines(fake):
    fake.positions = [
        {"instrument_name": PERP, "size": -5000.0, "average_price": 2500.0, "delta": -2.0},
        {"instrument_name": OPTION, "size": 0.0, "average_price": 0.0, "delta": 0.0}]
    pos = await _connector(fake).positions()
    assert len(pos) == 1 and pos[0].size == -5000.0 and pos[0].delta == -2.0


@pytest.mark.asyncio
async def test_account(fake):
    acct = await _connector(fake).account()
    assert acct.currency == "ETH" and acct.equity == 12.5
    assert acct.initial_margin == 2.0 and acct.available_funds == 10.5


# -- auth and health -----------------------------------------------------------

def test_production_needs_an_explicit_opt_in():
    with pytest.raises(ConnectorError, match="testnet first"):
        DeribitConnector("id", "secret", testnet=False)
    c = DeribitConnector("id", "secret", testnet=False, allow_production=True)
    assert c.url == "https://www.deribit.com/api/v2"


@pytest.mark.asyncio
async def test_healthy_on_a_good_connection(fake):
    assert await _connector(fake).healthy()


@pytest.mark.asyncio
async def test_key_that_can_withdraw_is_refused(fake):
    fake.scope = "connection trade:read_write wallet:read_write"
    c = _connector(fake)
    assert not await c.healthy()
    with pytest.raises(ConnectorError, match="move funds"):
        await c.submit(_intent())
    assert _sent(fake) == []


@pytest.mark.asyncio
async def test_key_that_cannot_trade_is_refused(fake):
    fake.scope = "connection trade:read wallet:none"
    assert not await _connector(fake).healthy()


@pytest.mark.asyncio
async def test_no_credentials_is_unhealthy(fake):
    c = DeribitConnector("", "", transport=httpx.MockTransport(fake.handler))
    assert not await c.healthy()


@pytest.mark.asyncio
async def test_bad_credentials_is_unhealthy(fake):
    fake.fail_next["public/auth"] = (13004, "invalid_credentials")
    assert not await _connector(fake).healthy()


@pytest.mark.asyncio
async def test_revoked_token_reauthenticates_once_and_the_order_goes_through(fake):
    c = _connector(fake)
    await c.account()
    fake.bad_tokens.add("tok1")
    s = await c.submit(_intent())
    assert s.status is OrderStatus.OPEN and fake.tokens == 2 and len(fake.orders) == 1


@pytest.mark.asyncio
async def test_token_refreshes_before_it_expires(fake):
    c = _connector(fake)
    await c.account()
    fake.now += 880
    await c.account()
    assert fake.tokens == 2


@pytest.mark.asyncio
async def test_rate_limit_turns_health_off_then_recovers(fake):
    c = _connector(fake, rate_limit_cooldown_seconds=5.0)
    fake.fail_next["private/get_account_summary"] = (10028, "too_many_requests")
    with pytest.raises(ConnectorError):
        await c.account()
    assert not await c.healthy()
    fake.now += 6
    assert await c.healthy()


@pytest.mark.asyncio
async def test_http_429_counts_as_a_rate_limit(fake):
    c = _connector(fake)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert not await c.healthy()
    with pytest.raises(ConnectorError):
        await c.book(PERP)
    assert not await c.healthy()


@pytest.mark.asyncio
async def test_rate_limited_order_raises_rather_than_rejects(fake):
    fake.fail_next["private/buy"] = (10028, "too_many_requests")
    with pytest.raises(ConnectorError):
        await _connector(fake).submit(_intent())


@pytest.mark.asyncio
async def test_locked_eth_is_unhealthy_but_other_locks_are_not(fake):
    c = _connector(fake)
    fake.locked = {"locked": True, "locked_indices": ["eth_usd", "btc_usd"]}
    assert not await c.healthy()
    fake.locked = {"locked": "partial", "locked_indices": ["btc_usd"]}
    assert await c.healthy()
    fake.locked = {"locked": "true", "locked_indices": []}
    assert not await c.healthy()


@pytest.mark.asyncio
async def test_lost_connection_is_unhealthy(fake):
    c = _connector(fake)
    assert await c.healthy()
    fake.fail_next["public/status"] = httpx.ConnectError("down")
    assert not await c.healthy()


# -- the algo on top -----------------------------------------------------------

def _req(**kw) -> ExecutionRequest:
    base = dict(request_id="r1", proposal_id="p1", instrument=PERP, side="buy", qty=1000.0,
                limit_price=2502.0, steps=4, tick_size=0.05)
    return ExecutionRequest(**(base | kw))


@pytest.mark.asyncio
async def test_work_order_walks_up_and_fills_on_the_last_ioc(fake):
    rep = await work_order(_req(), _connector(fake), sleep=fake.clock_sleep)
    assert rep.outcome == "filled" and rep.filled_qty == 1000 and rep.avg_price == 2501.0
    assert [o["order_state"] for o in fake.orders] == ["cancelled"] * 4 + ["filled"]


@pytest.mark.asyncio
async def test_work_order_rerun_after_a_crash_resubmits_nothing(fake):
    await work_order(_req(), _connector(fake), sleep=fake.clock_sleep)
    placed = len(fake.orders)
    rep = await work_order(_req(), _connector(fake), sleep=fake.clock_sleep)
    assert len(fake.orders) == placed and rep.filled_qty == 1000


@pytest.mark.asyncio
async def test_work_order_kill_switch_leaves_nothing_resting(fake):
    calls = 0

    async def halted() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    rep = await work_order(_req(), _connector(fake), halted=halted, sleep=fake.clock_sleep)
    assert rep.outcome == "halted" and fake.orders[0]["order_state"] == "cancelled"


@pytest.mark.asyncio
async def test_work_order_stops_on_an_exchange_rejection(fake):
    fake.fail_next["private/buy"] = (10009, "not_enough_funds")
    rep = await work_order(_req(), _connector(fake), sleep=fake.clock_sleep)
    assert rep.outcome == "rejected" and rep.detail == "not_enough_funds"


# -- status and wiring ---------------------------------------------------------

@pytest.mark.asyncio
async def test_diagnostics_on_a_good_key_shows_scope_not_secrets(fake):
    d = await _connector(fake).diagnostics()
    assert d["network"] == "testnet" and d["authenticated"] and d["healthy"]
    assert d["scope"] == OK_SCOPE and "secret" not in str(d).lower()


@pytest.mark.asyncio
async def test_diagnostics_without_a_key_says_so(fake):
    c = DeribitConnector("", "", transport=httpx.MockTransport(fake.handler))
    d = await c.diagnostics()
    assert not d["configured"] and not d["authenticated"] and not d["healthy"]
    assert "not configured" in d["error"] and d["locked"] is False


@pytest.mark.asyncio
async def test_diagnostics_reports_a_rejected_key(fake):
    fake.fail_next["public/auth"] = (13004, "invalid_credentials")
    d = await _connector(fake).diagnostics()
    assert d["configured"] and not d["authenticated"] and "invalid_credentials" in d["error"]


def test_get_connector_is_one_instance_and_never_production_by_default(monkeypatch):
    from plgo_options.execution import deribit
    monkeypatch.setattr(deribit, "_connector", None)
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET", False)
    monkeypatch.setattr(deribit, "DERIBIT_CONNECTOR_PRODUCTION", False)
    with pytest.raises(ConnectorError, match="testnet first"):
        deribit.get_connector()
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET", True)
    assert deribit.get_connector() is deribit.get_connector()


def _probe(**kw) -> dict:
    return {"configured": True, "authenticated": True, "healthy": True, "locked": False,
            "unreachable": False, "error": None} | kw


def _status(monkeypatch, tests: dict | None = None, enabled: bool = True):
    from plgo_options.execution import status
    passed = {"status": "passed", "detail": ""}
    monkeypatch.setattr(status, "TESTS", {"paper": passed, "deribit-testnet": passed,
                                          "deribit-production": passed} | (tests or {}))
    monkeypatch.setattr(status, "DERIBIT_CONNECTOR_PRODUCTION", enabled)
    return status


def test_testnet_row_stage_needs_a_connection_and_a_recorded_run(monkeypatch):
    not_run = {"status": "not_run", "detail": ""}
    status = _status(monkeypatch, {"deribit-testnet": not_run})
    row = status._testnet_row(_probe())
    assert row["connection"] == "Connected" and row["stage"] == "Not tested"
    assert "test_deribit_testnet" in row["next_step"]
    row = _status(monkeypatch)._testnet_row(_probe())
    assert row["stage"] == "Tested on testnet" and row["next_step"] is None
    row = _status(monkeypatch)._testnet_row(_probe(configured=False, authenticated=False))
    assert row["connection"] == "No key" and row["stage"] == "Not tested"


@pytest.mark.parametrize("probe,connection,step", [
    (dict(authenticated=False, error="invalid_credentials"), "Key refused", "invalid_credentials"),
    (dict(authenticated=False, unreachable=True, error="down"), "Unreachable", "down"),
    (dict(healthy=False, locked=True), "Connected, ETH locked", "locked"),
    (dict(healthy=False), "Connected, unhealthy", "unhealthy"),
])
def test_connection_problems_are_named(monkeypatch, probe, connection, step):
    row = _status(monkeypatch)._testnet_row(_probe(**probe))
    assert row["connection"] == connection and not row["connected"] and step in row["next_step"]


def test_production_is_ready_only_when_everything_agrees(monkeypatch):
    not_run = {"status": "not_run", "detail": ""}
    ready = _status(monkeypatch)._production_row(_probe())
    assert ready["ready"] and ready["stage"] == "Ready in prod"

    row = _status(monkeypatch, {"deribit-testnet": not_run})._production_row(_probe())
    assert not row["ready"] and "testnet" in row["next_step"]
    row = _status(monkeypatch, {"deribit-production": not_run})._production_row(_probe())
    assert not row["ready"] and "production key" in row["next_step"]
    row = _status(monkeypatch, enabled=False)._production_row(_probe())
    assert not row["ready"] and "DERIBIT_CONNECTOR_PRODUCTION" in row["next_step"]
    row = _status(monkeypatch)._production_row(_probe(healthy=False, locked=True))
    assert not row["ready"] and row["stage"] == "Not ready"


def test_shared_key_never_crosses_to_the_other_network(monkeypatch):
    from plgo_options.execution import deribit
    monkeypatch.setattr(deribit, "DERIBIT_CLIENT_ID", "shared")
    monkeypatch.setattr(deribit, "DERIBIT_CLIENT_SECRET", "s")
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET_CLIENT_ID", "")
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET", True)
    assert deribit.deribit_keys(True) == ("shared", "s") and deribit.deribit_keys(False) == ("", "")
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET", False)
    assert deribit.deribit_keys(True) == ("", "") and deribit.deribit_keys(False) == ("shared", "s")
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET_CLIENT_ID", "own")
    monkeypatch.setattr(deribit, "DERIBIT_TESTNET_CLIENT_SECRET", "t")
    assert deribit.deribit_keys(True) == ("own", "t")


def test_connectors_route_is_read_only_and_lists_all_three(fake, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plgo_options.execution import deribit, status
    from plgo_options.web.routes import execution

    real = deribit.DeribitConnector

    def on_the_fake(*a, **kw):
        return real(*a, **kw, transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(status, "DeribitConnector", on_the_fake)
    monkeypatch.setattr(status, "deribit_keys", lambda _testnet: ("id", "secret"))
    app = FastAPI()
    app.include_router(execution.router, prefix="/api/execution")
    rows = TestClient(app).get("/api/execution/connectors").json()["connectors"]
    assert [r["id"] for r in rows] == ["paper", "deribit-testnet", "deribit-production"]
    assert rows[1]["connection"] == "Connected" and rows[1]["stage"] == "Not tested"
    assert rows[2]["stage"] == "Not ready"
    assert not [m for m, _ in fake.calls if m in ("private/buy", "private/sell", "private/cancel")]
