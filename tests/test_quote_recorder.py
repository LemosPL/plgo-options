"""Quote recorder: one file per call, every message a row, the route's end date."""

from __future__ import annotations

import asyncio

import pytest

from plgo_options.market_data import quote_recorder as rec
from plgo_options.web.routes import quote_recorder as routes

NAMES = ["ETH-25DEC26-2500-C", "ETH-25DEC26-2400-P"]


class _Feed:
    def __init__(self, on_message):
        self.on_message, self.error, self.alive, self.started = on_message, None, False, None

    async def start(self, names, timeout=5.0, extra=()):
        self.started, self.alive = (list(names), list(extra)), True
        for n in names:  # the current quote on subscribe
            self.on_message(f"quote.{n}", {"instrument_name": n, "timestamp": 1, "best_bid_price": 0.04,
                                           "best_bid_amount": 10.0, "best_ask_price": 0.041, "best_ask_amount": 0.0})
        self.on_message(rec.INDEX_CHANNEL, {"index_name": "eth_usd", "price": 2290.5, "timestamp": 2})
        return True

    async def close(self):
        self.alive = False


def test_one_call_writes_every_message_to_one_file(tmp_path):
    feeds = []

    def feed_fn(on_message):
        feeds.append(_Feed(on_message))
        return feeds[-1]

    async def names(n):
        assert n == 3
        return NAMES

    r = asyncio.run(rec.record(seconds=0.3, root=tmp_path, feed_fn=feed_fn, names_fn=names))
    assert feeds[0].started == (NAMES, [rec.INDEX_CHANNEL])
    df = rec.load(tmp_path / r["file"].split(str(tmp_path) + "/")[1])
    assert list(df.columns) == rec.COLUMNS and r["rows"] == len(df) == 3 and r["error"] is None
    assert set(df.instrument) == {*NAMES, "index"}
    assert df[df.instrument == "index"].bid.iloc[0] == 2290.5
    assert df[df.instrument == NAMES[0]].bid_amount.iloc[0] == 10.0
    day = next(iter(rec.days(tmp_path)))
    assert rec.days(tmp_path)[day]["files"] == 1


def test_route_stops_after_its_end_date_and_lists_the_days(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTE_RECORDER_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "SIGNALS_TOKEN", "")
    out = asyncio.run(routes.tick(until="2020-01-01", x_signals_token=None))
    assert out == {"skipped": "recording ended on 2020-01-01"}
    with pytest.raises(Exception):
        asyncio.run(routes.tick(until="soon", x_signals_token=None))
    (tmp_path / "2026-11-06").mkdir()
    (tmp_path / "2026-11-06" / "120000.csv.gz").write_bytes(b"x" * 2000)
    st = asyncio.run(routes.status())
    assert st["files"] == 1 and st["days"]["2026-11-06"]["files"] == 1
