"""The remainder at a deadline as a Block RFQ the bot sends and accepts (execution/leftovers.py)."""

from __future__ import annotations

import asyncio

import pytest

from plgo_options.execution import bot, leftovers, rails, rfq_desk, store

from test_block_rfq import FakeRfq, _client
from test_execution_bot import EXPIRY, NAME, NOW, S, _books, _db

PUT = f"ETH-{EXPIRY}-2000-P"


async def _books_fn(expiries, instruments):
    return {EXPIRY: _books()}


async def _not_halted():
    return False


@pytest.fixture
def venue(monkeypatch):
    fake = FakeRfq()
    fake.now = NOW
    monkeypatch.setattr(rfq_desk, "_client", _client(fake))
    recorded = []

    async def record(rid, trades, by=""):
        recorded.append((rid, [(t["instrument_name"], t["direction"], t["amount"]) for t in trades], by))
        return [901]
    monkeypatch.setattr(rfq_desk, "record", record)

    async def room(mode, qty, price_usd, now):
        return qty, ""
    monkeypatch.setattr(rails, "room", room)
    fake.recorded = recorded
    return fake


async def _expired(instrument, side, qty, filled, mode="testnet", leftover_rfq=True, hard_vp=0.9):
    oid = await store.create_order(instrument, side, qty, NOW - 1, 0.5, 0.7, hard_vp, 500, mode,
                                   now=NOW - 7200, leftover_rfq=leftover_rfq)
    db_filled = filled
    if db_filled:
        from plgo_options.data.database import get_db
        db = await get_db()
        await db.execute("UPDATE exec_orders SET filled = ? WHERE id = ?", (db_filled, oid))
        await db.commit()
    return oid


async def _state(oid):
    import json
    return json.loads((await store.get_order(oid))["leftover_json"] or "null")


def test_the_remainder_goes_out_as_one_rfq_and_the_bot_accepts_a_quote_inside_the_hard_limit(tmp_path, venue):
    run = _db(tmp_path, "left.db")

    async def body():
        a = await _expired(NAME, "buy", 1000, 500)        # 500 left
        b = await _expired(PUT, "sell", 400, 150)         # 250 left: one structure, 2:1 x 250
        out = await bot._pass(NOW, _books_fn, _not_halted, record=True)
        assert [o["action"] for o in out["orders"]] == ["expired", "expired"]
        assert out["leftovers"][0]["action"] == "sent"
        (sent,) = [p for m, p in venue.calls if m == "private/create_block_rfq"]
        assert {(x["instrument_name"], x["direction"], x["amount"]) for x in sent["legs"]} == \
               {(NAME, "buy", 500), (PUT, "sell", 250)}
        sa, sb = await _state(a), await _state(b)
        assert sa["status"] == sb["status"] == "sent" and sa["rfq"]["block_rfq_id"] == sb["rfq"]["block_rfq_id"]
        # our limit: fair + the hard limit x the structure's vega, less the RFQ's fee (a buy-heavy structure: we pay)
        assert sa["fair_usd"] < sa["limit_usd"] < sa["fair_usd"] + 0.9 * 50
        assert await bot._busy(NOW + 1)                   # an RFQ out keeps the bot ticking

        limit = sa["limit_eth"]
        venue.quote("asks", round(limit - 0.0003, 4), 250)
        venue.now = NOW + 10                               # quotes showing for 5 s: wait for others to quote
        out = await leftovers.step(NOW + 10, _books_fn, _not_halted)
        assert out[0]["action"] == "watching" and (await _state(a))["status"] == "sent"
        venue.now = NOW + 25
        out = await leftovers.step(NOW + 25, _books_fn, _not_halted)
        assert out[0]["action"] == "filled"
        sa = await _state(a)
        assert sa["status"] == "filled" and sa["recorded"] == [901] and "#901" in sa["detail"]
        (rid, trades, by) = venue.recorded[0]
        assert rid == sa["rfq"]["block_rfq_id"] and by == leftovers.BY and len(trades) == 2
        assert await leftovers.step(NOW + 30, _books_fn, _not_halted) == []   # done: nothing left to do
    run(body)


def test_a_quote_over_the_limit_is_never_taken_and_the_rfq_lapses_before_it_ends(tmp_path, venue):
    run = _db(tmp_path, "lapse.db")

    async def body():
        a = await _expired(NAME, "buy", 300, 0)
        await bot._pass(NOW, _books_fn, _not_halted, record=True)
        st = await _state(a)
        venue.quote("asks", round(st["limit_eth"] + 0.001, 4), 300)
        venue.now = NOW + 60
        assert (await leftovers.step(NOW + 60, _books_fn, _not_halted))[0]["action"] == "watching"
        venue.now = NOW + 295                              # the fake's RFQ lives 300 s
        out = await leftovers.step(NOW + 295, _books_fn, _not_halted)
        assert out[0]["action"] == "lapsed"
        st = await _state(a)
        assert st["status"] == "lapsed" and "vs our limit" in st["detail"]
        assert not [p for m, p in venue.calls if m == "private/accept_block_rfq"]
        assert venue.rfqs[-1]["state"] == "cancelled"
    run(body)


def test_paper_live_unticked_and_small_remainders_send_nothing(tmp_path, venue):
    run = _db(tmp_path, "skip.db")

    async def body():
        p = await _expired(NAME, "buy", 1000, 0, mode="paper")
        off = await _expired(NAME, "buy", 1000, 0, leftover_rfq=False)
        small = await _expired(PUT, "buy", 300, 100)       # 200 left: under Deribit's 250 minimum
        await bot._pass(NOW, _books_fn, _not_halted, record=True)
        assert (await _state(p))["status"] == "skipped" and "paper" in (await _state(p))["detail"]
        assert await _state(off) is None
        assert (await _state(small))["status"] == "skipped" and "minimum" in (await _state(small))["detail"]
        assert not [m for m, _ in venue.calls if m == "private/create_block_rfq"]
        live = await _expired(NAME, "buy", 1000, 0, mode="live")
        await bot._pass(NOW + 1, _books_fn, _not_halted, record=True)
        assert "testnet-only" in (await _state(live))["detail"]
    run(body)


def test_the_kill_switch_stops_sending(tmp_path, venue):
    run = _db(tmp_path, "halt.db")

    async def halted():
        return True

    async def body():
        a = await _expired(NAME, "buy", 1000, 0)
        out = await bot._pass(NOW, _books_fn, halted, record=True)
        assert out["leftovers"][0]["action"] == "halted" and (await _state(a))["status"] == "pending"
        assert not venue.calls
    run(body)
