"""Execution bot: threshold schedule and decision, paper fills across book levels,
whole ticks against a synthetic smile (wait, take, cap, fill, kill switch, deadline,
lock), and the routes."""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from plgo_options.agents import store as agent_store
from plgo_options.data import database
from plgo_options.execution import bot, store
from plgo_options.execution.models import BookDepth, BookTop, OrderIntent
from plgo_options.execution.paper import PaperConnector, deribit_option_fee
from plgo_options.execution.threshold import ThresholdOrder, decide, level_cost, threshold_vp
from plgo_options.pricing.smile_fit import black, parse_exp, svi_iv
from plgo_options.web.routes import execution_bot as routes

NOW = dt.datetime(2026, 11, 3, 15, 0, tzinfo=dt.timezone.utc).timestamp()
H = 3600.0
EXPIRY, F, S = "25DEC26", 2300.0, 2290.0
SVI = np.array([0.03, 0.05, -0.3, 0.02, 0.2])
STRIKES = [1400, 1600, 1800, 1900, 2000, 2100, 2200, 2300, 2400, 2500, 2600, 2800, 3000, 3200, 3500]
NAME = f"ETH-{EXPIRY}-2400-C"


def _order(**kw) -> ThresholdOrder:
    base = dict(id=1, instrument=NAME, side="buy", qty=50, filled=0, created_ts=NOW, deadline_ts=NOW + 12 * H,
                start_vp=0.5, end_vp=0.7, hard_vp=0.9, cap=40)
    return ThresholdOrder(**{**base, **kw})


def test_threshold_ramps_to_the_end_an_hour_early_then_holds_the_hard_limit():
    o = _order()
    assert threshold_vp(o, NOW) == pytest.approx(0.5)
    assert threshold_vp(o, NOW + 5.5 * H) == pytest.approx(0.6)
    assert threshold_vp(o, NOW + 11 * H - 1) == pytest.approx(0.7, abs=1e-4)
    assert threshold_vp(o, NOW + 11 * H) == threshold_vp(o, NOW + 13 * H) == 0.9   # never above hard_vp


def test_decide_takes_the_top_of_book_only_up_to_its_size_and_the_cap():
    fair = dict(fair=100.0, iv=0.5, vega=4.0, inside=True)        # threshold 0.5 vp = $2.00
    px = lambda usd: usd / S                                       # noqa: E731
    book = BookDepth(NAME, bids=[(px(97), 100)], asks=[(px(100.5), 25), (px(101), 30), (px(103), 99)])
    d = decide(_order(), NOW, fair, S, book)
    # levels cost 0.5 / 1.0 / 3.0 + fee 0.69: the second is cheap enough too, but only the top is taken
    assert d.action == "take" and d.qty == 25 and d.limit_price == pytest.approx(px(100.5))
    assert d.est_cost == d.best_cost
    big = BookDepth(NAME, bids=[], asks=[(px(100.5), 99)])
    assert decide(_order(), NOW, fair, S, big).qty == 40                       # the cap
    assert d.best_cost == pytest.approx(level_cost("buy", px(100.5), S, 100.0)) == pytest.approx(0.5 + 0.687)
    assert decide(_order(filled=45), NOW, fair, S, book).qty == 5             # the remainder, not the cap
    sell = decide(_order(side="sell"), NOW, fair, S, book)                      # bid 97: costs 3.69 > 2.00
    assert sell.action == "wait" and sell.qty == 0
    assert decide(_order(), NOW, {**fair, "inside": False}, S, book).action == "outside"
    assert decide(_order(), NOW, fair, S, BookDepth(NAME, [], [])).action == "no_book"


def test_paper_ioc_walks_the_levels_each_at_its_own_price():
    book = BookDepth(NAME, bids=[], asks=[(0.040, 25), (0.041, 30), (0.045, 99)])

    async def top(_):
        return BookTop(NAME, None, 0.040, None, 25)

    async def depth(_):
        return book

    async def go():
        pc = PaperConnector(top, fee_fn=deribit_option_fee, depth=depth)
        st = await pc.submit(OrderIntent("t1", "p", NAME, "buy", 40, limit_price=0.041, time_in_force="IOC"))
        assert [(f.price, f.qty) for f in st.fills] == [(0.040, 25), (0.041, 15)]
        assert st.fills[0].fee == pytest.approx(25 * 0.0003)
        assert st.done and st.filled_qty == 40
        assert (await pc.book_depth(NAME, 2)).asks == book.asks[:2]
    asyncio.run(go())


def _books(ask_shift: float = 0.0) -> list[dict]:
    """A clean SVI smile quoted +/- $4 around fair; the 2400C's ask moved by ask_shift
    USD, with a second level $1 above it."""
    T = (parse_exp(EXPIRY).timestamp() - NOW) / (365 * 86400)
    out = []
    for K in STRIKES:
        v = float(svi_iv(SVI, [K], F, T)[0])
        for cp in ("C", "P"):
            fair = black(F, K, T, v, cp)
            bid, ask = fair - 4, fair + 4
            asks = [[ask / S, 30.0]]
            if (K, cp) == (2400, "C"):
                ask = fair + ask_shift
                asks = [[ask / S, 30.0], [(ask + 1) / S, 50.0]]
            out.append({"instrument_name": f"ETH-{EXPIRY}-{K}-{cp}", "bids": [[bid / S, 20.0]], "asks": asks,
                        "best_bid_price": bid / S, "best_ask_price": ask / S, "best_bid_amount": 20.0,
                        "best_ask_amount": 30.0, "mark_price": fair / S, "mark_iv": v * 100,
                        "underlying_price": F, "index_price": S, "estimated_delivery_price": S,
                        "open_interest": 0.0, "stats": {"volume": 0.0}})
    return out


def _db(tmp_path, name: str):
    database._db = None
    database.DB_PATH = tmp_path / name

    def run(body):
        async def go():
            try:
                await agent_store.init_agent_tables()
                await store.init_exec_tables()
                await body()
            finally:
                await database.close_db()
        asyncio.run(go())
    return run


def test_ticks_wait_take_up_to_the_cap_then_fill(tmp_path):
    run = _db(tmp_path, "bot.db")
    shift = {"v": 4.0}

    async def books(expiries, instruments):
        assert expiries == [EXPIRY] and instruments == [NAME]
        return {EXPIRY: _books(shift["v"])}

    async def off():
        return False

    async def body():
        oid = await store.create_order(NAME, "buy", 50, NOW + 12 * H, 0.5, 0.7, 0.9, 25, now=NOW)
        out = await bot.run_tick(NOW + 60, books, off)                 # ask $4 over the curve: wait
        assert out["orders"][0]["action"] == "wait"
        shift["v"] = -3.0          # ask $3 under: its own quote pulls the spline down, but not all the way
        out = await bot.run_tick(NOW + 120, books, off)
        assert out["orders"][0]["action"] == "take" and out["orders"][0]["qty"] == 25   # the cap
        o = await store.get_order(oid)
        assert o["filled"] == 25 and o["status"] == "working"
        out = await bot.run_tick(NOW + 180, books, off)                # the option rests 5 minutes
        assert out["orders"][0]["action"] == "cooldown"
        await bot.run_tick(NOW + 421, books, off)                      # then the last 25
        d = await store.order_detail(oid)
        assert d["order"]["filled"] == 50 and d["order"]["status"] == "filled"
        assert [f["qty"] for f in d["fills"]] == [25, 25]
        first = d["fills"][0]
        assert first["cost_usd"] == pytest.approx(first["price_usd"] - first["fair_usd"] + 0.687, abs=0.01)
        # the books' mark is the true smile; the spline leans toward the strike's $3-cheap ask
        assert 0 < first["mark_usd"] - first["fair_usd"] < 4.0
        assert [t["action"] for t in d["ticks"]] == ["wait", "take", "cooldown", "take"]
        assert d["ticks"][0]["best_cost"] > d["ticks"][0]["theta_usd"] > d["ticks"][1]["best_cost"]
        assert (await bot.run_tick(NOW + 480, books, off))["orders"] == []   # nothing left to work
    run(body)


def test_kill_switch_deadline_and_lock(tmp_path):
    run = _db(tmp_path, "bot2.db")

    async def books(expiries, instruments):
        raise AssertionError("no books are read while halted")

    async def on():
        return True

    async def body():
        a = await store.create_order(NAME, "buy", 50, NOW + 12 * H, 0.5, 0.7, 0.9, 40, now=NOW)
        b = await store.create_order(NAME, "sell", 50, NOW + 2 * H, 0.5, 0.7, 0.9, 40, now=NOW)
        out = await bot.run_tick(NOW + 60, books, on)
        assert out["halted"] and {x["action"] for x in out["orders"]} == {"halted"}
        out = await bot.run_tick(NOW + 3 * H, books, on)               # b's deadline passed
        assert {"id": b, "action": "expired", "detail": "deadline passed with 50 of 50 unfilled"} in out["orders"]
        assert (await store.get_order(b))["status"] == "expired"
        assert (await store.get_order(a))["status"] == "working"
        assert await store.acquire_lock(bot.LOCK, 120, NOW)
        assert not await store.acquire_lock(bot.LOCK, 120, NOW + 1)    # a second tick is turned away
        assert (await bot.run_tick(NOW + 2, books, on))["skipped"]
        assert await store.acquire_lock(bot.LOCK, 120, NOW + 200)      # an expired lease is taken over
    run(body)


def test_routes_create_act_and_refuse_live(tmp_path, monkeypatch):
    run = _db(tmp_path, "bot3.db")

    async def listed(_):
        return True
    monkeypatch.setattr(routes, "_listed", listed)
    req = Request({"type": "http", "session": {}, "headers": []})

    async def body():
        with pytest.raises(HTTPException) as e:
            await routes.create(routes.OrderCreate(instrument=NAME, side="buy", qty=10, hours=12, mode="live"), req)
        assert e.value.status_code == 400 and "testnet" in e.value.detail
        with pytest.raises(HTTPException):
            await routes.create(routes.OrderCreate(instrument=NAME, side="buy", qty=10, hours=12, start_vp=0.8), req)
        oid = (await routes.create(routes.OrderCreate(instrument=NAME, side="buy", qty=10, hours=12), req))["id"]
        assert (await routes.act(oid, "pause"))["order"]["status"] == "paused"
        await agent_store.set_flag("kill_switch", "on")
        with pytest.raises(HTTPException) as e:
            await routes.act(oid, "resume")
        assert e.value.status_code == 409
        await agent_store.set_flag("kill_switch", "off")
        assert (await routes.act(oid, "resume"))["order"]["status"] == "working"
        assert (await routes.act(oid, "cancel"))["order"]["status"] == "cancelled"
        with pytest.raises(HTTPException) as e:
            await routes.act(oid, "resume")
        assert e.value.status_code == 409
        st = await routes.status()
        assert st["counts"] == {"cancelled": 1} and st["modes"] == {"paper": True, "live": False}
        assert (await routes.detail(oid))["order"]["id"] == oid
    run(body)


def test_optimizer_legs_kept_named_netted_and_skipped_with_reasons():
    from plgo_options.execution.from_optimizer import deribit_legs
    trades = [
        {"counterparty": "Deribit", "opt": "C", "strike": 2500, "expiry": "2026-12-25", "qty": 300, "side": "Buy"},
        {"counterparty": "Deribit", "opt": "C", "strike": 2500, "instrument": "ETH-25DEC26-2500-C", "qty": -100,
         "side": "Sell", "is_unwind": True},                                       # nets with the first: buy 200
        {"counterparty": "deribit", "opt": "P", "strike": 2000.0, "expiry": "2027-03-26", "qty": -50.4, "side": "Sell"},
        {"counterparty": "Deribit", "opt": "P", "strike": 1800, "expiry": "2026-12-25", "qty": 10, "side": "Buy"},
        {"counterparty": "Deribit", "opt": "P", "strike": 1800, "expiry": "2026-12-25", "qty": 10, "side": "Sell"},
        {"counterparty": "Flowdesk", "opt": "C", "strike": 3000, "expiry": "2026-12-25", "qty": 5, "side": "Buy"},
        {"counterparty": "Deribit", "opt": "", "strike": 0, "expiry": "2026-12-25", "qty": 5, "side": "Buy"},
        {"counterparty": "Deribit", "opt": "C", "strike": 2512.5, "expiry": "2026-12-25", "qty": 5, "side": "Buy"},
    ]
    legs, skipped = deribit_legs(trades)
    assert [(x["instrument"], x["side"], x["qty"]) for x in legs] == [
        ("ETH-25DEC26-2500-C", "buy", 200), ("ETH-26MAR27-2000-P", "sell", 50)]
    reasons = sorted(s["reason"] for s in skipped)
    assert reasons == ["booked with Flowdesk, not Deribit", "nets to zero",
                       "no Deribit expiry code or whole strike", "not an option leg"]


def test_preview_prices_listed_legs_and_drops_the_rest(monkeypatch):
    from plgo_options.execution import from_optimizer
    monkeypatch.setattr(from_optimizer.time, "time", lambda: NOW)
    trades = [{"counterparty": "Deribit", "opt": "C", "strike": 2400, "expiry": "2026-12-25", "qty": 40, "side": "Buy"},
              {"counterparty": "Deribit", "opt": "C", "strike": 2450, "expiry": "2026-12-25", "qty": 5, "side": "Buy"}]

    async def listed(_):
        return {NAME}

    async def books(expiries):
        assert expiries == [EXPIRY]
        return {EXPIRY: _books(-3.0)}, NOW, NOW + 1

    out = asyncio.run(from_optimizer.preview(trades, listed_fn=listed, books_fn=books))
    (leg,) = out["legs"]
    assert leg["instrument"] == NAME and leg["qty"] == 40 and leg["now_qty"] == 30   # the first level
    assert 0.687 < leg["best_cost_usd"] < 0.5 * leg["vega"] and leg["best_cost_vp"] < 0.5
    assert "not listed" in out["skipped"][0]["reason"]


def test_batch_creates_the_valid_orders_and_lists_them_with_their_last_tick(tmp_path, monkeypatch):
    run = _db(tmp_path, "bot4.db")

    async def listed(name):
        return name == NAME
    monkeypatch.setattr(routes, "_listed", listed)
    req = Request({"type": "http", "session": {}, "headers": []})

    async def body():
        mk = lambda inst, **kw: routes.OrderCreate(instrument=inst, side="buy", qty=10, hours=12, **kw)  # noqa: E731
        r = await routes.create_batch(routes.Batch(orders=[mk(NAME), mk("ETH-25DEC26-9999-C"), mk(NAME, mode="live")]), req)
        assert len(r["created"]) == 1 and len(r["refused"]) == 2
        await store.add_tick(r["created"][0], NOW, "wait", best_cost=2.5, theta_usd=1.75, detail="best $2.50 > $1.75")
        (o,) = (await routes.orders())["orders"]
        assert (o["last_action"], o["last_best_cost"], o["last_theta_usd"]) == ("wait", 2.5, 1.75)
    run(body)


def test_fair_value_includes_the_strikes_own_quote_unless_left_out():
    from plgo_options.pricing.smile_fit import otm_quotes, spline_fair, spline_smile
    T = (parse_exp(EXPIRY).timestamp() - NOW) / (365 * 86400)
    true = black(F, 2400, T, float(svi_iv(SVI, [2400], F, T)[0]), "C")
    q = otm_quotes(EXPIRY, _books(-3.0), dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
    fair = spline_fair(q, 2400, "C")
    assert fair == spline_fair(q, 2400, "C", spline_smile(q))       # one spline shared across strikes
    assert fair["fair"] < true - 2                                  # pulled towards its own (lower) mid
    assert spline_fair(q, 2400, "C", loo=True)["fair"] == pytest.approx(true, abs=0.3)


def test_a_minute_runs_a_pass_every_5_seconds_and_records_routine_passes_once(tmp_path):
    run = _db(tmp_path, "bot5.db")
    clock = {"t": NOW}
    shift = {"v": 4.0}
    seen = []

    async def books(expiries, instruments):
        seen.append(clock["t"])
        if clock["t"] >= NOW + 20:                                     # cheap from the 5th pass on
            shift["v"] = -3.0
        return {EXPIRY: _books(shift["v"])}

    async def sleep(s):
        clock["t"] += s

    async def off():
        return False

    async def body():
        oid = await store.create_order(NAME, "buy", 500, NOW + 12 * H, 0.5, 0.7, 0.9, 25, now=NOW)
        out = await bot.run_minute(books, off, clock=lambda: clock["t"], sleep=sleep)
        assert out["passes"] == 12 and [t - NOW for t in seen] == [0, 5, 10, 15, 20]  # resting after the take
        assert [r["qty"] for r in out["takes"]] == [25]
        d = await store.order_detail(oid)
        # the first pass's wait, the take; the waits and rests in between are not written
        assert [(t["ts"] - NOW, t["action"]) for t in d["ticks"]] == [(0, "wait"), (20, "take")]
        assert (await store.last_run(bot.LOCK))["passes"] == 12
        clock["t"] = NOW + 60                                          # the next minute: rests, recorded once
        out = await bot.run_minute(books, off, clock=lambda: clock["t"], sleep=sleep)
        assert {r["action"] for r in out["orders"]} == {"cooldown"} and out["passes"] == 12
        assert [t["action"] for t in (await store.order_detail(oid))["ticks"]][-1] == "cooldown"
        assert len((await store.order_detail(oid))["ticks"]) == 3
    run(body)


def test_a_failed_pass_costs_that_pass_only(tmp_path):
    run = _db(tmp_path, "bot5b.db")
    clock = {"t": NOW}

    async def books(expiries, instruments):
        if clock["t"] in (NOW, NOW + 5):                               # Deribit 525 on the first two passes
            raise RuntimeError("Server error '525 <none>'")
        return {EXPIRY: _books(-3.0)}

    async def sleep(s):
        clock["t"] += s

    async def off():
        return False

    async def body():
        oid = await store.create_order(NAME, "buy", 500, NOW + 12 * H, 0.5, 0.7, 0.9, 25, now=NOW)
        out = await bot.run_minute(books, off, clock=lambda: clock["t"], sleep=sleep)
        assert out["passes"] == 12 and out["failed"] == 2 and "525" in out["error"]
        assert [r["qty"] for r in out["takes"]] == [25]                # the third pass trades
        d = await store.order_detail(oid)
        assert [(t["ts"] - NOW, t["action"]) for t in d["ticks"]] == [(0, "no_book"), (10, "take")]
        assert "525" in d["ticks"][0]["detail"]
        assert (await store.last_run(bot.LOCK))["failed"] == 2
    run(body)


def test_the_bot_reads_the_smile_from_the_book_summary_and_its_options_books(monkeypatch):
    import httpx
    summ = [{"instrument_name": b["instrument_name"], "bid_price": b["best_bid_price"],
             "ask_price": b["best_ask_price"], "mark_price": b["mark_price"], "mark_iv": b["mark_iv"],
             "underlying_price": F, "estimated_delivery_price": S, "open_interest": 0.0, "volume": 0.0}
            for b in _books()] + [{"instrument_name": "ETH-26MAR27-2400-C", "bid_price": 0.1}]
    full = {b["instrument_name"]: b for b in _books(-3.0)}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("get_book_summary_by_currency"):
            return httpx.Response(200, json={"result": summ})
        return httpx.Response(200, json={"result": full[req.url.params["instrument_name"]]})

    real = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    books = asyncio.run(bot._books([EXPIRY], [NAME]))[EXPIRY]
    assert len(books) == 2 * len(STRIKES)
    own = next(b for b in books if b["instrument_name"] == NAME)
    assert own["asks"] == full[NAME]["asks"]                         # its own book, levels and all
    other = next(b for b in books if b["instrument_name"] != NAME)
    assert other["bids"] is None and other["best_bid_price"] > 0     # the summary's top of book


def test_quote_feed_keeps_every_instruments_top_of_book_and_notices_a_drop():
    import json

    import websockets

    from plgo_options.market_data.deribit_quotes import QuoteFeed
    names = [f"ETH-{EXPIRY}-{K}-C" for K in (2400, 2500)]
    go_on = asyncio.Event()

    async def server(ws):
        sub = json.loads(await ws.recv())
        chans = sub["params"]["channels"]
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": sub["id"], "result": chans}))
        for c in chans:                                  # the current quote on subscribe
            n = c.split(".", 1)[1]
            await ws.send(json.dumps({"method": "subscription", "params": {"channel": c, "data": {
                "instrument_name": n, "best_bid_price": 0.04, "best_bid_amount": 10.0,
                "best_ask_price": 0.045, "best_ask_amount": 20.0, "timestamp": 1}}}))
        await go_on.wait()
        await ws.send(json.dumps({"method": "subscription", "params": {"channel": chans[0], "data": {
            "instrument_name": names[0], "best_bid_price": 0.041, "best_bid_amount": 3.0,
            "best_ask_price": 0.0, "best_ask_amount": 0.0, "timestamp": 2}}}))
        await asyncio.sleep(0.05)

    async def go():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            feed = QuoteFeed(f"ws://127.0.0.1:{port}")
            assert await feed.start(names, timeout=2)
            assert set(feed.quotes) == set(names) and feed.alive
            go_on.set()
            for _ in range(100):
                if feed.quotes[names[0]]["timestamp"] == 2:
                    break
                await asyncio.sleep(0.01)
            assert feed.quotes[names[0]]["best_bid_amount"] == 3.0
            for _ in range(100):                         # the server hangs up
                if not feed.alive:
                    break
                await asyncio.sleep(0.01)
            assert not feed.alive and feed.error
            await feed.close()
        dead = QuoteFeed("ws://127.0.0.1:9")             # nothing listening
        assert not await dead.start(names, timeout=1) and dead.error
    asyncio.run(go())


class _Feed:
    def __init__(self, quotes, alive=True):
        self.quotes, self.alive, self.error, self.started, self.closed = quotes, alive, None, None, False

    async def start(self, names, timeout=5.0):
        self.started = names
        return self.alive

    async def close(self):
        self.closed = True


def _summary_rows():
    return [{"instrument_name": b["instrument_name"], "bid_price": b["best_bid_price"],
             "ask_price": b["best_ask_price"], "mark_price": b["mark_price"], "mark_iv": b["mark_iv"],
             "underlying_price": F, "estimated_delivery_price": S, "open_interest": 0.0, "volume": 0.0}
            for b in _books()]


def _mock_http(monkeypatch, calls):
    import httpx
    full = {b["instrument_name"]: b for b in _books(-3.0)}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path.rsplit("/", 1)[1])
        if req.url.path.endswith("get_book_summary_by_currency"):
            return httpx.Response(200, json={"result": _summary_rows()})
        return httpx.Response(200, json={"result": full[req.url.params["instrument_name"]]})
    real = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_books_take_prices_and_sizes_from_a_live_feed_and_fall_back_without_it(monkeypatch):
    calls = []
    _mock_http(monkeypatch, calls)
    quotes = {b["instrument_name"]: {"best_bid_price": b["best_bid_price"], "best_bid_amount": 7.0,
                                     "best_ask_price": b["best_ask_price"], "best_ask_amount": 9.0}
              for b in _books(-3.0)}
    books = asyncio.run(bot._books([EXPIRY], [NAME], feed=_Feed(quotes)))[EXPIRY]
    assert calls == ["get_book_summary_by_currency"]                  # no per-instrument reads
    own = next(b for b in books if b["instrument_name"] == NAME)
    assert own["asks"] == [[quotes[NAME]["best_ask_price"], 9.0]] and own["mark_iv"] > 0
    q = bot.otm_quotes(EXPIRY, books, dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
    assert (q["size"] == 7.0).all()                                   # sizes reach the fit
    calls.clear()
    books = asyncio.run(bot._books([EXPIRY], [NAME], feed=_Feed(quotes, alive=False)))[EXPIRY]
    assert sorted(calls) == ["get_book_summary_by_currency", "get_order_book"]
    assert bot.otm_quotes(EXPIRY, books, dt.datetime.fromtimestamp(NOW, dt.timezone.utc))["size"] is None


def test_a_minute_holds_one_feed_over_the_working_expiries_and_reports_it(tmp_path, monkeypatch):
    run = _db(tmp_path, "bot6.db")
    calls = []
    _mock_http(monkeypatch, calls)
    clock = {"t": NOW}
    feeds = []

    def feed_fn():
        quotes = {b["instrument_name"]: {"best_bid_price": b["best_bid_price"], "best_bid_amount": 50.0,
                                         "best_ask_price": b["best_ask_price"], "best_ask_amount": 60.0}
                  for b in _books(4.0)}
        feeds.append(_Feed(quotes))
        return feeds[-1]

    async def names(expiries):
        assert expiries == [EXPIRY]
        return [f"ETH-{EXPIRY}-{K}-{cp}" for K in STRIKES for cp in "CP"]

    async def sleep(s):
        clock["t"] += s

    async def off():
        return False

    async def body():
        await store.create_order(NAME, "buy", 50, NOW + 12 * H, 0.5, 0.7, 0.9, 25, now=NOW)
        out = await bot.run_minute(None, off, clock=lambda: clock["t"], sleep=sleep, feed_fn=feed_fn, names_fn=names)
        (feed,) = feeds
        assert len(feed.started) == 2 * len(STRIKES) and feed.closed
        assert out["quotes"] == "live" and (await store.last_run(bot.LOCK))["quotes"] == "live"
        assert "get_order_book" not in calls and out["passes"] == 12
        feeds.clear()

        def broken():
            f = _Feed({}, alive=False)
            f.error = "ConnectionRefusedError: refused"
            feeds.append(f)
            return f
        clock["t"] = NOW + 60
        out = await bot.run_minute(None, off, clock=lambda: clock["t"], sleep=sleep, feed_fn=broken, names_fn=names)
        assert out["quotes"] == "HTTP (ConnectionRefusedError: refused)" and "get_order_book" in calls
    run(body)
