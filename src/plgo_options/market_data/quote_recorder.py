"""Quote recorder: every top-of-book change of the next ETH quarterlies' options, from
Deribit's WebSocket quote channel (market_data/deribit_quotes.QuoteFeed), plus the ETH
index. Research data for the spline's weights: how long a quote stands, how often it
is refreshed, and the 5-second noise of each strike's mid (scripts/quote_recorder.py).

One call (Cloud Scheduler: POST /api/quote-recorder/tick, every minute) holds the feed
for 55 seconds and writes one file, <root>/YYYY-MM-DD/HHMMSS.csv.gz, one row a message:

    recv_ms, ts_ms, instrument, bid, bid_amount, ask, ask_amount

recv_ms is our clock, ts_ms Deribit's. Each file starts with every option's quote as
Deribit sends it on subscribe, so a file stands alone and the few seconds between calls
lose no state, only the changes made in them. Index rows have instrument "index" and the
price in bid. ~100 quote changes a second for the three quarterlies: ~6,000 rows, a few
hundred KB gzipped, per file.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import gzip
import io
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pandas as pd

from .deribit_quotes import QuoteFeed
from .smile_recorder import API, quarterlies

COLUMNS = ["recv_ms", "ts_ms", "instrument", "bid", "bid_amount", "ask", "ask_amount"]
INDEX_CHANNEL = "deribit_price_index.eth_usd"


def data_root() -> Path:
    """QUOTE_RECORDER_DIR, else DB_DIR/quote_recorder (the persistent mount on Cloud
    Run), else data/cache/quote_recorder."""
    if os.environ.get("QUOTE_RECORDER_DIR"):
        return Path(os.environ["QUOTE_RECORDER_DIR"])
    if os.environ.get("DB_DIR"):
        return Path(os.environ["DB_DIR"]) / "quote_recorder"
    return Path("data/cache/quote_recorder")


async def quarterly_options(n_expiries: int = 3) -> list[str]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/get_instruments", params={"currency": "ETH", "kind": "option", "expired": "false"})
        r.raise_for_status()
        inst = r.json()["result"]
    exp = set(quarterlies(inst, n_expiries, time.time()))
    return [i["instrument_name"] for i in inst if i["instrument_name"].split("-")[1] in exp]


def _row(channel: str, d: dict) -> list:
    now = int(time.time() * 1000)
    if channel == INDEX_CHANNEL:
        return [now, d.get("timestamp"), "index", d.get("price"), None, None, None]
    return [now, d.get("timestamp"), d["instrument_name"], d.get("best_bid_price"), d.get("best_bid_amount"),
            d.get("best_ask_price"), d.get("best_ask_amount")]


async def record(seconds: float = 55.0, root: Path | None = None, n_expiries: int = 3,
                 feed_fn: Callable[..., QuoteFeed] = QuoteFeed,
                 names_fn: Callable[[int], object] = quarterly_options) -> dict:
    """Hold a quote feed for ``seconds`` and write what it saw as one file."""
    root = root or data_root()
    rows: list[list] = []
    names = await names_fn(n_expiries)
    t0 = time.time()
    feed = feed_fn(on_message=lambda ch, d: rows.append(_row(ch, d)))
    try:
        if not await feed.start(names, extra=[INDEX_CHANNEL]):
            raise RuntimeError(f"quote feed did not start: {feed.error}")
        while feed.alive and time.time() - t0 < seconds:
            await asyncio.sleep(0.25)
    finally:
        await feed.close()
    start = dt.datetime.fromtimestamp(t0, dt.timezone.utc)
    path = root / f"{start:%Y-%m-%d}" / f"{start:%H%M%S}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerows(rows)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(buf.getvalue().encode()))
    tmp.replace(path)  # readers never see a half-written file
    return {"file": str(path), "instruments": len(names), "rows": len(rows),
            "seconds": round(time.time() - t0, 1), "bytes": path.stat().st_size,
            "error": feed.error if feed.error and time.time() - t0 < seconds else None}


def days(root: Path | None = None) -> dict[str, dict]:
    """{day: {files, MB}} recorded under root."""
    root = root or data_root()
    if not root.exists():
        return {}
    out = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        fs = list(d.glob("*.csv.gz"))
        out[d.name] = {"files": len(fs), "MB": round(sum(f.stat().st_size for f in fs) / 1e6, 1)}
    return out


def load(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)
