"""Record Deribit ETH quarterly smiles, to test SVI-timed taker execution.

The idea under test: a low-frequency optimizer says what to hold on an expiry;
a taker crosses only when the touch is cheap (or rich) against the SVI fit by
more than the fee. That only beats crossing at once if such dislocations occur
with size behind them, and revert. This records the evidence.

One tick (Cloud Scheduler calls POST /api/smile-recorder/tick every 5 minutes;
scripts/smile_recorder.py does the same locally):

    1) the next N quarterly expiries (last-Friday expiries in Mar/Jun/Sep/Dec,
       rolling forward automatically as one expires)
    2) every order book of those expiries, depth 10, each expiry read in one
       concurrent burst (its strikes within ~1 s of each other)
    3) per expiry (pricing/smile_fit): parity forward + DF, OTM side, raw SVI
       fitted to the mid in price under the butterfly penalty
    4) per scored strike, a LEAVE-ONE-OUT fit (the strike's own quote removed,
       warm-started from the full fit), so a dislocated quote cannot drag the
       curve towards itself and hide its own edge
    5) taker edges net of Deribit's taker fee, in USD and vol points:
           edge_buy  = fit_loo - ask - fee      (> 0: the ask is cheap)
           edge_sell = bid - fit_loo - fee      (> 0: the bid is rich)

Each tick is one file, <root>/YYYY-MM-DD/HHMMSS.json.gz, holding the order books
as received (to refit later another way), one row per strike and one per
expiry. One new file per tick rather than appending, because on Cloud Run the
root is the GCS FUSE mount (DB_DIR), where an append rewrites the object.
Public Deribit endpoints only, no API key; nothing here places an order.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import gzip
import json
import math
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd
from plgo_options.pricing.smile_fit import (black_vec, book_frame, fit_forward, fit_svi, implied,
                                            svi_iv, vega)

API = "https://www.deribit.com/api/v2/public"
QUARTER_MONTHS = {3, 6, 9, 12}
EXPIRY_PAUSE = 4.0     # seconds between expiries: one burst of all three gets throttled
BOOK_KEYS = ("instrument_name", "timestamp", "bids", "asks", "best_bid_price", "best_ask_price",
             "best_bid_amount", "best_ask_amount", "mark_price", "mark_iv", "underlying_price",
             "index_price", "estimated_delivery_price", "open_interest", "stats")


def data_root() -> Path:
    """SMILE_RECORDER_DIR, else DB_DIR/smile_recorder (the persistent mount on
    Cloud Run, like the optimizer snapshots), else data/cache/smile_recorder."""
    if os.environ.get("SMILE_RECORDER_DIR"):
        return Path(os.environ["SMILE_RECORDER_DIR"])
    if os.environ.get("DB_DIR"):
        return Path(os.environ["DB_DIR"]) / "smile_recorder"
    return Path("data/cache/smile_recorder")


def quarterlies(instruments: list[dict], n: int, now: float) -> list[str]:
    """The next n quarterly expiry codes with at least a day to run."""
    out = {}
    for i in instruments:
        e = dt.datetime.fromtimestamp(i["expiration_timestamp"] / 1000, dt.timezone.utc)
        last_friday = e.weekday() == 4 and e.day + 7 > calendar.monthrange(e.year, e.month)[1]
        if e.month in QUARTER_MONTHS and last_friday and e.timestamp() - now >= 86400:
            out[i["instrument_name"].split("-")[1]] = e
    return sorted(out, key=out.get)[:n]


async def expiry_books(expiries: list[str] | None = None, n_expiries: int = 3,
                       depth: int = 10) -> tuple[dict[str, list[dict]], float, float]:
    """Every ETH option order book of the given expiries (default: the next n
    quarterlies), depth levels deep: ({expiry: books}, t0, t1)."""
    async with httpx.AsyncClient(timeout=20) as c:
        async def get(method: str, **params) -> dict | list:
            for attempt in range(5):
                r = await c.get(f"{API}/{method}", params=params)
                if r.status_code != 429:  # rate limited: back off and retry
                    r.raise_for_status()
                    return r.json()["result"]
                await asyncio.sleep(1 + attempt)
            raise RuntimeError(f"{method} {params}: rate limited")

        inst = await get("get_instruments", currency="ETH", kind="option", expired="false")
        if expiries is None:
            expiries = quarterlies(inst, n_expiries, time.time())
        names = [i["instrument_name"] for i in inst if i["instrument_name"].split("-")[1] in expiries]
        sem = asyncio.Semaphore(12)

        async def one(name: str) -> dict | None:
            # Deribit sometimes errors on one book (e.g. 400 internal_server_error on a deep
            # OTM strike) while serving its top level: fall back to the touch, else drop the
            # book rather than lose the whole expiry.
            async with sem:
                for d in dict.fromkeys((depth, 1)):
                    try:
                        return await get("get_order_book", instrument_name=name, depth=d)
                    except httpx.HTTPStatusError:
                        continue
                return None

        # One burst per expiry, with a pause for Deribit's rate-limit credits to refill:
        # read all at once, the first ~90 books come back in 0.5 s and the rest are
        # throttled over ~5 s, which skews strikes of the same expiry against each other.
        t0 = time.time()
        by_exp: dict[str, list[dict]] = {}
        for k, e in enumerate(expiries):
            if k:
                await asyncio.sleep(EXPIRY_PAUSE)
            books = await asyncio.gather(*(one(nm) for nm in names if nm.split("-")[1] == e))
            by_exp[e] = [{key: b.get(key) for key in BOOK_KEYS} for b in books if b]
        t1 = time.time()
    return by_exp, t0, t1


async def snapshot(n_expiries: int = 3, depth: int = 10) -> tuple[dict[str, list[dict]], float, float]:
    return await expiry_books(None, n_expiries, depth)


def analyse(expiry: str, books: list[dict], now: dt.datetime) -> tuple[list[dict], dict]:
    """One expiry's strike rows (quotes, sizes, fits, edges) and its fit row."""
    g = book_frame(books, expiry, now)
    depth = {}
    for b in books:  # visible size on each side, all levels read
        _, _, K, cp = b["instrument_name"].split("-")
        depth[(float(K), cp)] = (sum(a for _, a in b["bids"] or []), sum(a for _, a in b["asks"] or []))
    F, DF, _, _ = fit_forward(g)
    T = float(g["T"].iloc[0])
    spot = float(g.spot.median())
    otm = g[(g.cp == "C") == (g.K >= F)].sort_values("K").reset_index(drop=True)
    iv = lambda px, r: implied(px, F, r.K, T, r.cp, DF) if np.isfinite(px) else np.nan  # noqa: E731
    otm["bid_iv"] = [iv(r.bid, r) for r in otm.itertuples()]
    otm["ask_iv"] = [iv(r.ask, r) for r in otm.itertuples()]
    otm["scored"] = (np.isfinite(otm.bid) & np.isfinite(otm.ask) & (otm.ask > otm.bid)
                     & np.isfinite(otm.bid_iv) & np.isfinite(otm.ask_iv))
    s = otm[otm.scored]
    if len(s) < 6:
        raise RuntimeError(f"{expiry}: only {len(s)} two-sided strikes")
    K, CP = s.K.to_numpy(), s.cp.to_list()
    mid, half = ((s.bid + s.ask) / 2).to_numpy(), ((s.ask - s.bid) / 2).to_numpy()
    atm = float(np.median((s.bid_iv + s.ask_iv) / 2))
    p, ok = fit_svi(K, CP, mid, half, F, T, DF, atm)

    t_loo = time.time()
    loo = {}
    for j in range(len(K)):
        keep = np.arange(len(K)) != j
        loo[K[j]], _ = fit_svi(K[keep], [c for c, k in zip(CP, keep) if k], mid[keep], half[keep],
                               F, T, DF, atm, x0=p)
    loo_secs = time.time() - t_loo

    rows = []
    ts = now.isoformat(timespec="seconds")
    for r in otm.itertuples():
        v = float(svi_iv(p, [r.K], F, T)[0])
        pl = loo.get(r.K, p)  # one-sided strikes are not in the fit: the full fit is their LOO
        vl = float(svi_iv(pl, [r.K], F, T)[0])
        fit = float(black_vec(F, [r.K], T, v, [r.cp], DF)[0])
        fit_loo = float(black_vec(F, [r.K], T, vl, [r.cp], DF)[0])
        vg = float(vega(F, r.K, T, vl, DF))
        fee_buy = deribit_option_fee_usd(spot, r.ask) if np.isfinite(r.ask) else np.nan
        fee_sell = deribit_option_fee_usd(spot, r.bid) if np.isfinite(r.bid) else np.nan
        eb, es = fit_loo - r.ask - fee_buy, r.bid - fit_loo - fee_sell
        bd, ad = depth[(r.K, r.cp)]
        rows.append(dict(ts=ts, expiry=expiry, T_days=T * 365, spot=spot, forward=F, DF=DF, K=r.K, cp=r.cp,
                         scored=int(r.scored), bid=r.bid, ask=r.ask, bsize=r.bsize, asize=r.asize,
                         bid_depth=bd, ask_depth=ad, mark=r.mark, bid_iv=r.bid_iv * 100,
                         ask_iv=r.ask_iv * 100, mark_iv=r.mark_iv, fit=fit, fit_loo=fit_loo,
                         fit_iv=v * 100, fit_loo_iv=vl * 100, vega=vg, fee_buy=fee_buy, fee_sell=fee_sell,
                         edge_buy=eb, edge_sell=es, edge_buy_vp=eb / vg if vg > 0 else np.nan,
                         edge_sell_vp=es / vg if vg > 0 else np.nan))
    sr = [x for x in rows if x["scored"]]
    rms = lambda key: float(np.sqrt(np.mean([((x[key] - (x["bid"] + x["ask"]) / 2)  # noqa: E731
                                              / ((x["ask"] - x["bid"]) / 2)) ** 2 for x in sr])))
    stamps = [b["timestamp"] for b in books]
    fitrow = dict(ts=ts, expiry=expiry, T_days=T * 365, book_secs=(max(stamps) - min(stamps)) / 1000,
                  spot=spot, forward=F, DF=DF, n_scored=len(sr),
                  atm_iv=float(svi_iv(p, [F], F, T)[0]) * 100,
                  **dict(zip(["a", "b", "rho", "m", "sigma"], map(float, p))), arb_free=int(ok),
                  fit_err_mid=rms("fit"), mark_err_mid=rms("mark"), loo_secs=loo_secs)
    return rows, fitrow


def _clean(x):
    """JSON without NaN (null instead), floats rounded to 6 places."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if not math.isfinite(x) else round(float(x), 6)
    if isinstance(x, np.integer):
        return int(x)
    return x


def analyse_all(by_exp: dict[str, list[dict]], now: dt.datetime) -> dict:
    strikes, fits, errors = [], [], {}
    for expiry, books in by_exp.items():
        try:
            rows, fitrow = analyse(expiry, books, now)
        except Exception as e:  # one bad expiry must not lose the others; its books are kept anyway
            errors[expiry] = repr(e)
            continue
        strikes += rows
        fits.append(fitrow)
    return {"strikes": strikes, "fits": fits, "errors": errors}


async def record_tick(root: Path | None = None, n_expiries: int = 3, depth: int = 10) -> dict:
    """Snapshot, fit, write one tick file. Returns the fit rows and the file path."""
    root = root or data_root()
    by_exp, t0, t1 = await snapshot(n_expiries, depth)
    now = dt.datetime.fromtimestamp((t0 + t1) / 2, dt.timezone.utc)
    out = await asyncio.to_thread(analyse_all, by_exp, now)  # CPU: keep the app's event loop free
    path = root / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(_clean({"t0": t0, "t1": t1, "books": by_exp, **out}), separators=(",", ":"))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(blob.encode()))
    tmp.replace(path)  # readers never see a half-written tick
    for f in out["fits"]:
        sr = [r for r in out["strikes"] if r["expiry"] == f["expiry"] and r["scored"]]
        f["n_edge_buy"] = sum(r["edge_buy"] > 0 for r in sr)
        f["n_edge_sell"] = sum(r["edge_sell"] > 0 for r in sr)
    return {"asof": now.isoformat(timespec="seconds"), "file": str(path),
            "fits": _clean(out["fits"]), "errors": out["errors"]}


def tick_line(f: dict) -> str:
    return (f"{f['ts']} {f['expiry']:8} F {f['forward']:8.2f}  ATM {f['atm_iv']:5.2f}%  "
            f"fit err {f['fit_err_mid']:.3f} (mark {f['mark_err_mid']:.3f})  "
            f"taker edge >0: buy {f['n_edge_buy']}, sell {f['n_edge_sell']} of {f['n_scored']}")


def days(root: Path | None = None) -> dict[str, int]:
    root = root or data_root()
    if not root.exists():
        return {}
    return {d.name: len(list(d.glob("*.json.gz"))) for d in sorted(root.iterdir()) if d.is_dir()}


def load(root: Path | None = None, day: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Strike rows and fit rows of every recorded tick (or one day's)."""
    root = root or data_root()
    strikes, fits = [], []
    for f in sorted(root.glob(f"{day or '*'}/*.json.gz")):
        try:
            d = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        strikes += d["strikes"]
        fits += d["fits"]
    return pd.DataFrame(strikes), pd.DataFrame(fits)


def summarize(strikes: pd.DataFrame) -> list[dict]:
    """Per expiry: how often a taker edge clears the fee, by how much, with what size."""
    if strikes.empty:
        return []
    d = strikes[strikes.scored == 1]
    out = []
    for e, x in d.groupby("expiry", sort=False):
        b, s = x[x.edge_buy > 0], x[x.edge_sell > 0]
        size = pd.concat([b.asize, s.bsize])
        med = lambda col: float(col.median()) if len(col) else None  # noqa: E731
        out.append(dict(expiry=e, ticks=int(x.ts.nunique()), rows=len(x),
                        buy_pct=100 * len(b) / len(x), sell_pct=100 * len(s) / len(x),
                        med_buy_vp=med(b.edge_buy_vp), med_sell_vp=med(s.edge_sell_vp),
                        med_size=med(size)))
    return out
