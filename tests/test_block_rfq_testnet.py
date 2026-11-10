"""BlockRfqClient against the real Deribit testnet: what the fake assumes, checked.

Needs testnet keys with the Block RFQ scopes (block_rfq:read_write, block_trade:read),
and Block RFQ enabled on the testnet account:

    DERIBIT_TESTNET_CLIENT_ID=... DERIBIT_TESTNET_CLIENT_SECRET=... \\
        python -m pytest tests/test_block_rfq_testnet.py -v -s

Skipped without them. Opens RFQs and cancels them; trades nothing unless
``BLOCK_RFQ_TESTNET_ACCEPT=1``, and then only a 1-unit box if a maker quotes it
within 30 s inside 5% of its mark. ``-s`` prints what each check found, which is
what to send back:

    * the sizes we send come back as the ratios and amount we expect (50:25:50:34 x 1);
    * the sign of a structure's price: Deribit's combo mark against the legs' marks,
      buys added and sells subtracted, in Deribit's direction of the structure;
    * whether Deribit writes our structure as we sent it or flipped, and the RFQ's
      lifetime and minimum trade amount.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio

from plgo_options.execution.block_rfq import GRACE_SECONDS, BlockRfqClient, Rfq
from plgo_options.execution.deribit import DeribitConnector

CLIENT_ID = os.environ.get("DERIBIT_TESTNET_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DERIBIT_TESTNET_CLIENT_SECRET", "")
ACCEPT = os.environ.get("BLOCK_RFQ_TESTNET_ACCEPT") == "1"

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not (CLIENT_ID and CLIENT_SECRET),
    reason="set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET")]


@pytest_asyncio.fixture
async def client():
    conn = DeribitConnector(CLIENT_ID, CLIENT_SECRET, testnet=True)
    c = BlockRfqClient(conn)
    print("\nscope:", await c.check_scope())
    c.opened = []
    yield c
    for rfq in c.opened:
        if rfq.open:
            await c.cancel(rfq)
    await conn.close()


def _label() -> str:
    return f"plgo-test-{uuid.uuid4().hex[:12]}"


async def _chain(c: BlockRfqClient) -> tuple[str, list[float], dict[str, float]]:
    """An ETH expiry 2 weeks to 4 months out with the most strikes, its strikes, and marks."""
    rows = await c.conn._rpc("public/get_instruments", {"currency": "ETH", "kind": "option", "expired": False})
    now = time.time() * 1000
    by: dict[str, list[dict]] = {}
    for r in rows:
        if 14 * 86400e3 < r["expiration_timestamp"] - now < 120 * 86400e3:
            by.setdefault(r["instrument_name"].split("-")[1], []).append(r)
    expiry = max(by, key=lambda e: len(by[e]))
    summary = await c.conn._rpc("public/get_book_summary_by_currency", {"currency": "ETH", "kind": "option"})
    marks = {s["instrument_name"]: s["mark_price"] for s in summary if s.get("mark_price") is not None}
    strikes = sorted({float(r["strike"]) for r in by[expiry]})
    return expiry, strikes, marks


def _name(expiry: str, k: float, cp: str) -> str:
    return f"ETH-{expiry}-{k:g}-{cp}"


def _legs_mark(rfq: Rfq, marks: dict[str, float]) -> float:
    """Deribit's structure priced from its legs' marks: buys added, sells subtracted."""
    return sum((1 if x["direction"] == "buy" else -1) * x["ratio"] * marks[x["instrument_name"]] for x in rfq.legs)


def _report(what: str, rfq: Rfq, marks: dict[str, float]) -> float:
    implied = _legs_mark(rfq, marks)
    print(f"{what}: RFQ {rfq.block_rfq_id} {rfq.combo_id} amount {rfq.amount} min {rfq.min_trade_amount}, "
          f"lives {rfq.expires - rfq.created:.0f} s, written {'as sent' if rfq.side == 'buy' else 'flipped'}; "
          f"legs {[(x['instrument_name'], x['direction'], x['ratio']) for x in rfq.legs]}; "
          f"combo mark {rfq.mark_price} vs legs {implied:+.5f}")
    return implied


async def test_four_legs_come_back_as_our_ratios(client):
    expiry, strikes, marks = await _chain(client)
    mid = len(strikes) // 2
    kp1, kp2, kc1, kc2 = strikes[mid - 3], strikes[mid - 2], strikes[mid + 1], strikes[mid + 2]
    legs = [dict(instrument=_name(expiry, kp1, "P"), side="buy", qty=50),
            dict(instrument=_name(expiry, kp2, "P"), side="sell", qty=25),
            dict(instrument=_name(expiry, kc1, "C"), side="buy", qty=50),
            dict(instrument=_name(expiry, kc2, "C"), side="buy", qty=34)]
    rfq = await client.create(legs, _label())
    client.opened.append(rfq)
    assert rfq.amount == 1 and sorted(x["ratio"] for x in rfq.legs) == [25, 34, 50, 50]
    implied = _report("four legs", rfq, marks)
    if rfq.mark_price is not None:
        assert rfq.mark_price == pytest.approx(implied, rel=0.05, abs=2e-4), \
            "the combo mark is not buys minus sells in Deribit's direction: check the sign convention"
    await client.cancel(rfq)
    assert rfq.state == "cancelled"


async def test_a_sold_box_and_its_price_sign(client):
    expiry, strikes, marks = await _chain(client)
    lo, hi = strikes[len(strikes) // 4], strikes[3 * len(strikes) // 4]
    legs = [dict(instrument=_name(expiry, lo, "C"), side="sell", qty=1),
            dict(instrument=_name(expiry, lo, "P"), side="buy", qty=1),
            dict(instrument=_name(expiry, hi, "C"), side="buy", qty=1),
            dict(instrument=_name(expiry, hi, "P"), side="sell", qty=1)]
    rfq = await client.create(legs, _label())
    client.opened.append(rfq)
    implied = _report(f"box {lo:g}/{hi:g} sold", rfq, marks)
    # In our direction a sold box pays about -(width)/spot per unit: negative, whatever Deribit's.
    print(f"  ours per unit: {rfq.ours(implied):+.5f} ETH")
    assert rfq.ours(implied) < 0
    if ACCEPT:
        await _accept_if_quoted(client, rfq, implied)
    await client.cancel(rfq)


async def _accept_if_quoted(client: BlockRfqClient, rfq: Rfq, mark: float) -> None:
    await asyncio.sleep(GRACE_SECONDS + 1)
    deadline = time.time() + 30
    while time.time() < deadline:
        await client.read(rfq)
        best = rfq.best()
        if best is not None:
            limit = rfq.ours(mark) + 0.05 * abs(rfq.ours(mark))
            print(f"  best quote {best.price} (ours {rfq.ours(best.price):+.5f}), limit {limit:+.5f}")
            res = await client.accept(rfq, limit)
            print(f"  accepted: filled={res.filled} reason={res.reason} trades={res.trades}")
            return
        await asyncio.sleep(2)
    print("  no quote within 30 s: testnet makers may be absent")
