"""Live top of book, sizes included, for a set of Deribit instruments: one WebSocket
subscribed to their public quote.{instrument} channels.

Deribit's book summary (one HTTP call, every option) has bid and ask prices but no
sizes; over HTTP, sizes come one instrument at a time. The quote channel pushes an
instrument's best bid and ask, price and amount, whenever they change, and its current
quote on subscribe: all 258 ETH quarterly options arrive within ~0.05 s of one subscribe
request, then ~100 messages (~27 KB) a second.

The execution bot (execution/bot.py) holds one feed for each 55-second scheduler call;
the quote recorder (market_data/quote_recorder.py) writes down every message of one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

import websockets

WS_URL = "wss://www.deribit.com/ws/api/v2"


class QuoteFeed:
    """``quotes``: {instrument: {best_bid_price, best_bid_amount, best_ask_price,
    best_ask_amount, timestamp}}, kept current while the feed is ``alive``.
    ``on_message(channel, data)``, if given, sees every message, extra channels included."""

    def __init__(self, url: str = WS_URL, connect: Callable[..., Any] = websockets.connect,
                 on_message: Callable[[str, dict], None] | None = None) -> None:
        self.url, self._connect, self._on_message = url, connect, on_message
        self.quotes: dict[str, dict] = {}
        self.error: str | None = None
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done() and self.error is None

    async def start(self, instruments: Iterable[str], timeout: float = 5.0, extra: Iterable[str] = ()) -> bool:
        """Connect, subscribe (to the instruments' quotes and any ``extra`` channels), and
        wait (up to ``timeout``) for every instrument's first quote. False, with ``error``
        set, when the feed cannot be used."""
        names = sorted(set(instruments))
        try:
            self._ws = await asyncio.wait_for(self._connect(self.url, max_size=2 ** 22), timeout)
            await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                                            "params": {"channels": [f"quote.{n}" for n in names] + list(extra)}}))
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return False
        self._task = asyncio.create_task(self._read())
        end = time.monotonic() + timeout
        while self.alive and time.monotonic() < end and any(n not in self.quotes for n in names):
            await asyncio.sleep(0.02)
        return self.alive

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("method") == "subscription":
                    ch, q = msg["params"]["channel"], msg["params"]["data"]
                    if ch.startswith("quote."):
                        self.quotes[q["instrument_name"]] = q
                    if self._on_message is not None:
                        self._on_message(ch, q)
                elif "error" in msg:
                    self.error = f"Deribit: {msg['error'].get('message', msg['error'])}"
                    return
            self.error = "connection closed"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
