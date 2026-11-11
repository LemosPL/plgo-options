"""Spot, history, realised vol and perp funding for tokens Deribit doesn't
list (the Sandbox screen; ZAMA first).

Spot is the median of every venue that answers, after dropping any quote more
than 5% off that median -- the lesson from FIL, where one frozen source looked
exactly like a quiet market (see DeribitClient.get_fil_spot_price). Binance is
left out: it returns 451 from Cloud Run us-central1.

History is Kraken's daily OHLC (up to 720 days), OKX daily candles as fallback.
"""
from __future__ import annotations

import asyncio
import math
import re
import statistics
import time
from datetime import datetime, timezone

import httpx

TIMEOUT = 10.0
SPOT_TTL = 30.0
HISTORY_TTL = 3600.0
OUTLIER = 0.05
VOL_WINDOWS = (30, 60, 90, 180)

_cache: dict[str, tuple[float, object]] = {}


def check_symbol(asset: str) -> str:
    a = str(asset or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", a):
        raise ValueError(f"bad token symbol {asset!r}")
    return a


def _sources(a: str):
    return (
        ("Coinbase", f"https://api.coinbase.com/v2/prices/{a}-USD/spot",
         lambda j: float(j["data"]["amount"])),
        ("Kraken", f"https://api.kraken.com/0/public/Ticker?pair={a}USD",
         lambda j: float(next(iter(j["result"].values()))["c"][0])),
        ("OKX", f"https://www.okx.com/api/v5/market/ticker?instId={a}-USDT",
         lambda j: float(j["data"][0]["last"])),
        ("Bybit", f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={a}USDT",
         lambda j: float(j["result"]["list"][0]["lastPrice"])),
    )


async def _quote(http: httpx.AsyncClient, name: str, url: str, extract) -> dict:
    try:
        r = await http.get(url)
        r.raise_for_status()
        px = extract(r.json())
        if not (px > 0 and math.isfinite(px)):
            raise ValueError(f"implausible price {px}")
        return {"source": name, "price": px}
    except Exception as e:  # noqa: BLE001 - a dead venue is reported, not fatal
        return {"source": name, "price": None, "error": f"{type(e).__name__}: {e}"[:160]}


def consensus(quotes: list[dict]) -> dict:
    """Median of the live quotes, outliers (> 5% off the median) excluded."""
    live = [q["price"] for q in quotes if q.get("price")]
    if not live:
        raise RuntimeError("no venue returned a price: " + "; ".join(f"{q['source']}: {q.get('error')}" for q in quotes))
    med = statistics.median(live)
    used = []
    for q in quotes:
        if q.get("price"):
            q["used"] = abs(q["price"] / med - 1.0) <= OUTLIER
            if q["used"]:
                used.append(q["price"])
    return {"spot": statistics.median(used), "quotes": quotes, "n_used": len(used)}


async def get_spot(asset: str) -> dict:
    a = check_symbol(asset)
    key = f"spot:{a}"
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        quotes = await asyncio.gather(*(_quote(http, *s) for s in _sources(a)))
    out = consensus(list(quotes))
    out["as_of"] = datetime.now(timezone.utc).isoformat()
    _cache[key] = (time.monotonic() + SPOT_TTL, out)
    return out


async def _kraken_daily(http: httpx.AsyncClient, a: str) -> list[tuple[str, float]]:
    r = await http.get(f"https://api.kraken.com/0/public/OHLC?pair={a}USD&interval=1440")
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    rows = next(v for k, v in j["result"].items() if k != "last")
    # The last row is today's still-open candle.
    return [(datetime.fromtimestamp(int(x[0]), timezone.utc).date().isoformat(), float(x[4])) for x in rows[:-1]]


async def _okx_daily(http: httpx.AsyncClient, a: str) -> list[tuple[str, float]]:
    r = await http.get(f"https://www.okx.com/api/v5/market/history-candles?instId={a}-USDT&bar=1Dutc&limit=300")
    r.raise_for_status()
    rows = [x for x in r.json()["data"] if x[8] == "1"]  # confirmed candles only
    return sorted((datetime.fromtimestamp(int(x[0]) / 1000, timezone.utc).date().isoformat(), float(x[4])) for x in rows)


def realised_vols(closes: list[float]) -> dict:
    """Annualised close-to-close vol (365-day year: tokens trade every day)."""
    lr = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
    out = {}
    for n in VOL_WINDOWS:
        if len(lr) >= n:
            out[f"{n}d"] = statistics.pstdev(lr[-n:]) * math.sqrt(365)
    if len(lr) >= 10:
        out["all"] = statistics.pstdev(lr) * math.sqrt(365)
    return out


async def get_history(asset: str) -> dict:
    a = check_symbol(asset)
    key = f"hist:{a}"
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    errors = []
    rows: list[tuple[str, float]] = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        for name, fn in (("Kraken", _kraken_daily), ("OKX", _okx_daily)):
            try:
                rows = await fn(http, a)
                if len(rows) >= 10:
                    src = name
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}: {e}"[:160])
    if len(rows) < 10:
        raise RuntimeError(f"no daily history for {a}: " + "; ".join(errors))
    closes = [c for _, c in rows]
    out = {"asset": a, "source": src, "dates": [d for d, _ in rows], "closes": closes,
           "first_date": rows[0][0], "vols": realised_vols(closes),
           "low": min(closes), "high": max(closes)}
    _cache[key] = (time.monotonic() + HISTORY_TTL, out)
    return out


async def get_funding(asset: str) -> list[dict]:
    """Current perp funding, annualised. Positive = shorts are paid, i.e.
    hedging a long token position with a short perp earns carry."""
    a = check_symbol(asset)
    key = f"fund:{a}"
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        try:
            r = await http.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={a}USDT")
            t = r.json()["result"]["list"][0]
            h = float(t.get("fundingIntervalHour") or 8)
            out.append({"venue": "Bybit", "rate": float(t["fundingRate"]), "interval_h": h,
                        "annual": float(t["fundingRate"]) * 24 / h * 365,
                        "open_interest_usd": float(t.get("openInterestValue") or 0)})
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await http.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={a}-USDT-SWAP")
            t = r.json()["data"][0]
            h = (int(t["nextFundingTime"]) - int(t["fundingTime"])) / 3_600_000 or 8
            out.append({"venue": "OKX", "rate": float(t["fundingRate"]), "interval_h": h,
                        "annual": float(t["fundingRate"]) * 24 / h * 365})
        except Exception:  # noqa: BLE001
            pass
    _cache[key] = (time.monotonic() + 300.0, out)
    return out


# Hourly closes for the Forwards Monitor chart: Kraken (720 bars = 30 days),
# OKX 1H as fallback. Cached a few minutes; the last bar is the open hour.
HOURLY_TTL = 300.0


async def _kraken_hourly(http: httpx.AsyncClient, a: str) -> list[tuple[int, float]]:
    r = await http.get(f"https://api.kraken.com/0/public/OHLC?pair={a}USD&interval=60")
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    rows = next(v for k, v in j["result"].items() if k != "last")
    return [(int(x[0]), float(x[4])) for x in rows]


async def _okx_hourly(http: httpx.AsyncClient, a: str) -> list[tuple[int, float]]:
    r = await http.get(f"https://www.okx.com/api/v5/market/history-candles?instId={a}-USDT&bar=1H&limit=300")
    r.raise_for_status()
    return sorted((int(x[0]) // 1000, float(x[4])) for x in r.json()["data"])


async def get_hourly(asset: str) -> dict:
    a = check_symbol(asset)
    key = f"hourly:{a}"
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    errors, rows, src = [], [], None
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        for name, fn in (("Kraken", _kraken_hourly), ("OKX", _okx_hourly)):
            try:
                rows = await fn(http, a)
                if len(rows) >= 24:
                    src = name
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}: {e}"[:160])
    if len(rows) < 24:
        raise RuntimeError(f"no hourly history for {a}: " + "; ".join(errors))
    out = {"asset": a, "source": src, "ts": [t for t, _ in rows], "closes": [c for _, c in rows]}
    _cache[key] = (time.monotonic() + HOURLY_TTL, out)
    return out
