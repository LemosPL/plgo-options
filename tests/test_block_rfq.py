"""BlockRfqClient against a simulated Deribit Block RFQ, no network.

``FakeRfq`` keeps what makes the API awkward for a taker: Deribit turns the leg
sizes we send into ratios times an amount and may write the structure the other
way round, quotes come in Deribit's direction, labels are not unique, and an
answer can be lost after the request was carried out. The real thing is
test_block_rfq_testnet.py.
"""

from __future__ import annotations

import json
import math
from functools import reduce

import httpx
import pytest

from plgo_options.execution.block_rfq import BlockRfqClient
from plgo_options.execution.deribit import ConnectorError, DeribitConnector

SCOPE = "connection mainaccount account:read trade:read_write block_rfq:read_write block_trade:read"
P2000, P2200, C2400, C2500 = (f"ETH-25DEC26-{k}" for k in ("2000-P", "2200-P", "2400-C", "2500-C"))
pytestmark = pytest.mark.asyncio

LEGS = [dict(instrument=P2000, side="buy", qty=5000), dict(instrument=P2200, side="sell", qty=2500),
        dict(instrument=C2400, side="buy", qty=5000), dict(instrument=C2500, side="buy", qty=3400)]


class FakeRfq:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.scope = SCOPE
        self.rfqs: list[dict] = []
        self.block_trades: dict[int, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.flip = False  # Deribit writes the structure with every leg reversed
        self.ratio_off = False  # Deribit's ratios do not give back our sizes
        self.lose_response_of: set[str] = set()

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.now += s

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append((method, params))
        result = getattr(self, "_" + method.split("/")[1])(params)
        if isinstance(result, httpx.Response):
            return result
        if method in self.lose_response_of:
            self.lose_response_of.remove(method)
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "usOut": int(self.now * 1e6)})

    @staticmethod
    def _error(code: int, message: str) -> httpx.Response:
        return httpx.Response(400, json={"jsonrpc": "2.0", "error": {"code": code, "message": message}})

    def _auth(self, _p):
        return {"access_token": "tok", "expires_in": 900, "scope": self.scope}

    def _create_block_rfq(self, p):
        amounts = [leg["amount"] for leg in p["legs"]]
        amount = reduce(math.gcd, [int(a) for a in amounts])
        rev = {"buy": "sell", "sell": "buy"}
        legs = [{"instrument_name": x["instrument_name"], "ratio": int(x["amount"]) // amount + (1 if self.ratio_off and i == 0 else 0),
                 "direction": rev[x["direction"]] if self.flip else x["direction"]}
                for i, x in enumerate(p["legs"])]
        rfq = {"block_rfq_id": len(self.rfqs) + 1, "label": p.get("label"), "state": "open", "role": "taker",
               "amount": amount, "legs": legs, "bids": [], "asks": [], "min_trade_amount": 1,
               "creation_timestamp": int(self.now * 1000), "expiration_timestamp": int((self.now + 300) * 1000),
               "combo_id": "ETH-COMBO", "mark_price": 0.01}
        if p.get("hedge"):
            rfq["hedge"] = json.loads(p["hedge"])
        self.rfqs.append(rfq)
        return rfq

    def _get_block_rfqs(self, p):
        rows = [r for r in self.rfqs if "block_rfq_id" not in p or r["block_rfq_id"] == p["block_rfq_id"]]
        return {"block_rfqs": list(reversed(rows)), "continuation": None}

    def _find(self, rid):
        return next(r for r in self.rfqs if r["block_rfq_id"] == rid)

    def _cancel_block_rfq(self, p):
        r = self._find(p["block_rfq_id"])
        if r["state"] != "open":
            return self._error(10010, "already_closed")
        r["state"] = "cancelled"
        return r

    def _accept_block_rfq(self, p):
        r = self._find(p["block_rfq_id"])
        if r["state"] != "open":
            return self._error(10010, "already_closed")
        assert [(x["instrument_name"], x["direction"], x["ratio"]) for x in p["legs"]] == \
               [(x["instrument_name"], x["direction"], x["ratio"]) for x in r["legs"]]
        assert p["time_in_force"] == "fill_or_kill"
        book = r["asks"] if p["direction"] == "buy" else r["bids"]
        ok = [q for q in book if (q["price"] <= p["price"] if p["direction"] == "buy" else q["price"] >= p["price"])
              and q["amount"] >= p["amount"]]
        if not ok:
            return {"block_trades": []}
        q = min(ok, key=lambda q: q["price"]) if p["direction"] == "buy" else max(ok, key=lambda q: q["price"])
        r["state"] = "filled"
        trades = [{"block_trade_id": f"BLOCK-{r['block_rfq_id']}", "instrument_name": x["instrument_name"],
                   "direction": x["direction"] if p["direction"] == "buy" else {"buy": "sell", "sell": "buy"}[x["direction"]],
                   "amount": x["ratio"] * p["amount"], "price": 0.01, "fee": 0.0001, "trade_id": f"t{i}"}
                  for i, x in enumerate(r["legs"])]
        self.block_trades[r["block_rfq_id"]] = [{"id": f"BLOCK-{r['block_rfq_id']}", "trades": trades, "quote": q["price"]}]
        return {"block_trades": self.block_trades[r["block_rfq_id"]]}

    def _get_block_trades(self, p):
        return self.block_trades.get(p["block_rfq_id"], [])

    def quote(self, side: str, price: float, amount: float, aon: bool = True) -> None:
        self.rfqs[-1][side].append({"price": price, "amount": amount, "makers": ["MM1"],
                                    "execution_instruction": "all_or_none" if aon else "any_part_of",
                                    "expires_at": int((self.now + 60) * 1000)})


@pytest.fixture
def fake() -> FakeRfq:
    return FakeRfq()


def _client(fake: FakeRfq) -> BlockRfqClient:
    conn = DeribitConnector("id", "secret", transport=httpx.MockTransport(fake.handler),
                            clock=fake.clock, sleep=fake.sleep)
    return BlockRfqClient(conn)


def _sent(fake: FakeRfq, method: str) -> list[dict]:
    return [p for m, p in fake.calls if m == f"private/{method}"]


async def test_create_sends_sizes_and_reads_the_ratios_back(fake):
    rfq = await _client(fake).create(LEGS, "plgo-1")
    [sent] = _sent(fake, "create_block_rfq")
    assert [(x["instrument_name"], x["amount"], x["direction"]) for x in sent["legs"]] == \
           [(P2000, 5000, "buy"), (P2200, 2500, "sell"), (C2400, 5000, "buy"), (C2500, 3400, "buy")]
    assert sent["label"] == "plgo-1" and sent["disclosed"] is True
    assert rfq.amount == 100 and [x["ratio"] for x in rfq.legs] == [50, 25, 50, 34]
    assert rfq.side == "buy" and rfq.ours(0.02) == 0.02


async def test_a_structure_written_the_other_way_round_is_ours_to_sell(fake):
    fake.flip = True
    rfq = await _client(fake).create(LEGS, "plgo-1")
    assert rfq.side == "sell"
    assert rfq.ours(-0.02) == 0.02  # Deribit's bid of -0.02 for its structure: we pay 0.02 for ours


async def test_a_structure_that_is_not_ours_is_cancelled_and_raised(fake):
    fake.ratio_off = True
    with pytest.raises(ConnectorError, match="not the 5000"):
        await _client(fake).create(LEGS, "plgo-1")
    assert fake.rfqs[0]["state"] == "cancelled"


async def test_no_block_rfq_scope_sends_nothing(fake):
    fake.scope = "connection trade:read_write"
    with pytest.raises(ConnectorError, match="block_rfq:read_write"):
        await _client(fake).create(LEGS, "plgo-1")
    assert not _sent(fake, "create_block_rfq")


async def test_the_same_label_adopts_the_open_rfq(fake):
    c = _client(fake)
    first = await c.create(LEGS, "plgo-1")
    again = await c.create(LEGS, "plgo-1")
    assert again.block_rfq_id == first.block_rfq_id and len(_sent(fake, "create_block_rfq")) == 1


async def test_a_lost_create_answer_is_found_by_label(fake):
    fake.lose_response_of.add("private/create_block_rfq")
    rfq = await _client(fake).create(LEGS, "plgo-1")
    assert rfq.block_rfq_id == 1 and len(_sent(fake, "create_block_rfq")) == 1


async def test_best_quote_must_fill_the_whole_amount(fake):
    c = _client(fake)
    rfq = await c.create(LEGS, "plgo-1")
    fake.quote("asks", 0.010, 40, aon=False)  # best price, but only 40 of 100
    fake.quote("asks", 0.012, 100)
    fake.quote("asks", 0.015, 100)
    await c.read(rfq)
    assert rfq.best().price == 0.012


async def test_accept_is_fill_or_kill_at_our_limit_and_reports_the_trades(fake):
    c = _client(fake)
    rfq = await c.create(LEGS, "plgo-1")
    fake.quote("asks", 0.012, 100)
    await c.read(rfq)
    res = await c.accept(rfq, 0.0125)
    [sent] = _sent(fake, "accept_block_rfq")
    assert sent["direction"] == "buy" and sent["price"] == 0.0125 and sent["amount"] == 100
    assert res.filled and rfq.state == "filled" and len(res.trades) == 4
    assert {t["instrument_name"]: t["amount"] for t in res.trades}[C2500] == 3400


async def test_accept_on_a_flipped_structure_sells_it_no_worse_than_our_limit(fake):
    fake.flip = True
    c = _client(fake)
    rfq = await c.create(LEGS, "plgo-1")
    fake.quote("bids", -0.013, 100)  # we would pay 0.013: worse than our 0.0125
    await c.read(rfq)
    res = await c.accept(rfq, 0.0125)
    [sent] = _sent(fake, "accept_block_rfq")
    assert sent["direction"] == "sell" and sent["price"] == -0.0125
    assert not res.filled and rfq.state == "open" and res.reason
    fake.quote("bids", -0.012, 100)
    assert (await c.accept(rfq, 0.0125)).filled


async def test_a_lost_accept_answer_is_read_back_not_resent(fake):
    c = _client(fake)
    rfq = await c.create(LEGS, "plgo-1")
    fake.quote("asks", 0.012, 100)
    fake.lose_response_of.add("private/accept_block_rfq")
    res = await c.accept(rfq, 0.0125)
    assert res.filled and len(res.trades) == 4 and len(_sent(fake, "accept_block_rfq")) == 1


async def test_cancel_after_a_fill_reads_the_final_state(fake):
    c = _client(fake)
    rfq = await c.create(LEGS, "plgo-1")
    fake.quote("asks", 0.012, 100)
    await c.accept(rfq, 0.0125)
    assert (await c.cancel(rfq)).state == "filled"
    rfq2 = await c.create(LEGS, "plgo-2")
    assert (await c.cancel(rfq2)).state == "cancelled"


async def test_hedge_leg_goes_as_a_json_string(fake):
    hedge = {"instrument_name": "ETH-25DEC26", "direction": "sell", "price": 2100.0, "amount": 3_000_000}
    rfq = await _client(fake).create(LEGS, "plgo-1", hedge=hedge)
    assert json.loads(_sent(fake, "create_block_rfq")[0]["hedge"]) == hedge and rfq.hedge == hedge


async def test_anonymous_needs_five_makers(fake):
    with pytest.raises(ConnectorError, match="5 makers"):
        await _client(fake).create(LEGS, "plgo-1", disclosed=False, makers=["A", "B"])
