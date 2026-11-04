#!/usr/bin/env python3
"""Client showcase: one Deribit ETH expiry's smile, fitted to every bid and ask.

Snapshots every order book of the expiry (calls and puts, read concurrently so
prices and sizes are from the same moment), then:

    1) forward + discount factor from put-call parity (deribit_smile_scanner.fit_forward)
    2) out-of-the-money side only: Black vols of each bid and each ask
    3) microprice per strike: (bid x ask size + ask x bid size) / (bid size + ask size)
    4) two cubic smoothing splines of implied vol against log-moneyness, one
       through the mid vols and one through the microprice vols, each strike
       weighted by 1 / half-spread^2 in vol points, smoothness chosen by
       generalized cross-validation; drawn only across the scored strikes (a
       spline has no shape beyond its data). Where it has butterfly arbitrage
       (Gatheral's g(k) < 0) between them, it is refitted with a no-arbitrage
       penalty (smile_fit.fit_spline)

and writes a self-contained page from scripts/smile_showcase.html (charts,
fit error vs Deribit's mark, cheap/rich vs the curve). Public endpoints only,
no API key. Standalone, not wired into the app.

    .venv/bin/python scripts/smile_showcase.py                     # 25DEC26
    .venv/bin/python scripts/smile_showcase.py --expiry 27MAR26
    .venv/bin/python scripts/smile_showcase.py --out /tmp/smile.html

Output defaults to data/cache/smile_showcase/eth-smile-<expiry>.html (ignored by git).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.pricing.smile_fit import (black_vec, book_frame, fit_forward, fit_spline, implied,  # noqa: E402
                                           parse_exp, spline_density, vega)

TEMPLATE = Path(__file__).with_name("smile_showcase.html")
API = "https://www.deribit.com/api/v2/public"


async def snapshot(expiry: str) -> tuple[list[dict], float, float]:
    async with httpx.AsyncClient(timeout=20) as c:
        inst = (await c.get(f"{API}/get_instruments",
                            params={"currency": "ETH", "kind": "option", "expired": "false"})).json()["result"]
        names = [i["instrument_name"] for i in inst if f"-{expiry}-" in i["instrument_name"]]
        if not names:
            raise SystemExit(f"no live ETH options for expiry {expiry}")
        sem = asyncio.Semaphore(12)

        async def one(name: str) -> dict:
            async with sem:
                for attempt in range(4):
                    r = await c.get(f"{API}/get_order_book", params={"instrument_name": name, "depth": 1})
                    if r.status_code != 429:  # rate limited: back off and retry
                        r.raise_for_status()
                        return r.json()["result"]
                    await asyncio.sleep(1 + attempt)
                raise RuntimeError(f"{name}: rate limited")

        t0 = time.time()
        books = await asyncio.gather(*(one(n) for n in names))
        return books, t0, time.time()


def build(expiry: str) -> dict:
    books, t0, t1 = asyncio.run(snapshot(expiry))
    now = dt.datetime.fromtimestamp((t0 + t1) / 2, dt.timezone.utc)
    g = book_frame(books, expiry, now)
    F, DF, _, _ = fit_forward(g)
    T = float(g["T"].iloc[0])

    def black(K, v, cp):
        return black_vec(F, K, T, v, cp, DF)

    quotes = []
    for r in g.sort_values("K").itertuples():
        if (r.cp == "C") != (r.K >= F):  # OTM side only
            continue
        ivb = implied(r.bid, F, r.K, T, r.cp, DF) if np.isfinite(r.bid) else np.nan
        iva = implied(r.ask, F, r.K, T, r.cp, DF) if np.isfinite(r.ask) else np.nan
        pc = lambda x: None if not np.isfinite(x) else round(float(x) * 100, 2)  # noqa: E731
        q = dict(K=r.K, cp=r.cp, bid_iv=pc(ivb), ask_iv=pc(iva), mark_iv=round(float(r.mark_iv), 2),
                 bid_usd=None if not np.isfinite(r.bid) else round(float(r.bid), 4),
                 ask_usd=None if not np.isfinite(r.ask) else round(float(r.ask), 4),
                 mark_usd=round(float(r.mark), 4), oi=float(r.oi), vol24=float(r.vol24),
                 bsize=float(r.bsize), asize=float(r.asize))
        two_sided = q["bid_usd"] is not None and q["ask_usd"] is not None and q["ask_usd"] > q["bid_usd"]
        if two_sided and r.bsize > 0 and r.asize > 0:
            q["micro_usd"] = round((q["bid_usd"] * r.asize + q["ask_usd"] * r.bsize) / (r.bsize + r.asize), 4)
        quotes.append(q)

    scored = [q for q in quotes if "micro_usd" in q]
    K = np.array([q["K"] for q in scored])
    CP = [q["cp"] for q in scored]
    half = np.array([(q["ask_usd"] - q["bid_usd"]) / 2 for q in scored])
    atm = float(np.median([(q["bid_iv"] + q["ask_iv"]) / 200 for q in scored]))
    k = np.log(K / F)
    kk = np.linspace(k.min(), k.max(), 8001)

    targets = {"mid": np.array([(q["bid_usd"] + q["ask_usd"]) / 2 for q in scored]),
               "micro": np.array([q["micro_usd"] for q in scored])}
    fits, splines = {}, {}
    for name, tgt in targets.items():
        iv_t = np.array([implied(px, F, Kj, T, c, DF) for px, Kj, c in zip(tgt, K, CP)])
        ok_t = np.isfinite(iv_t)
        vg = np.array([vega(F, Kj, T, v, DF) for Kj, v in zip(K, np.where(ok_t, iv_t, atm))])
        half_vp = half / vg                         # half-spread in vol points
        raw, _, _ = fit_spline(k[ok_t], iv_t[ok_t] * 100, half_vp[ok_t], T, arb_free=False)
        spl, repaired, arb_free = fit_spline(k[ok_t], iv_t[ok_t] * 100, half_vp[ok_t], T)
        bad = kk[spline_density(raw, kk, T)[1] < 0]         # where the plain spline had arbitrage
        splines[name] = spl
        fits[name] = dict(n=int(ok_t.sum()), arb_free=arb_free, repaired=repaired,
                          arb_bad=[int(round(F * np.exp(bad.min()))), int(round(F * np.exp(bad.max())))]
                          if len(bad) else None,
                          max_move_vp=round(float(np.abs(spl(k) - raw(k)).max()), 2))
        for q in quotes:
            inside = k.min() - 1e-9 <= np.log(q["K"] / F) <= k.max() + 1e-9
            v = float(spl(np.log(q["K"] / F))) if inside else None
            q[f"spl_iv_{name}"] = round(v, 3) if v is not None else None
            q[f"spl_usd_{name}"] = round(float(black([q["K"]], v / 100, [q["cp"]])[0]), 4) if v is not None else None
    for q in quotes:  # vega at the mid spline (the mark's vol past it), USD per vol point per contract
        q["vega"] = round(float(vega(F, q["K"], T, (q["spl_iv_mid"] or q["mark_iv"]) / 100, DF)), 4)

    sm = lambda x: round(float(splines["mid"](np.log(x / F))), 2)  # noqa: E731
    Ks = np.exp(np.linspace(np.log(K.min()), np.log(K.max()), 260))
    exp = parse_exp(expiry)
    return dict(
        expiry=expiry, expiry_long=f"{exp.day} {exp:%b %Y}", expiry_ts=int(exp.timestamp() * 1000),
        asof=now.isoformat(timespec="seconds"), snapshot_secs=round(t1 - t0, 1),
        spot=round(float(g.spot.median()), 2), forward=round(float(F), 2), DF=round(float(DF), 5),
        T_days=round(T * 365, 1), atm_iv=sm(F), iv_80=sm(0.8 * F), iv_120=sm(1.2 * F),
        n_fit=len(scored), fit_range=[float(K.min()), float(K.max())], fits=fits, quotes=quotes,
        curve=[[round(float(x), 1), *[round(float(splines[t](np.log(x / F))), 3) for t in ("mid", "micro")]]
               for x in Ks])


def summary(d: dict) -> None:
    s = [q for q in d["quotes"] if "micro_usd" in q]
    print(f"{d['expiry']}  snapshot {d['asof']} ({d['snapshot_secs']} s)  index {d['spot']}  "
          f"forward {d['forward']}  DF {d['DF']}  ATM {d['atm_iv']}%  strikes scored {len(s)}")
    for t in ("mid", "micro"):
        tgt = [(q["bid_usd"] + q["ask_usd"]) / 2 if t == "mid" else q["micro_usd"] for q in s]
        err = lambda key: np.sqrt(np.mean([((q[key] - x) / ((q["ask_usd"] - q["bid_usd"]) / 2)) ** 2  # noqa: E731
                                           for q, x in zip(s, tgt) if q[key] is not None]))
        inside = sum(q[f"spl_usd_{t}"] is not None and q["bid_usd"] <= q[f"spl_usd_{t}"] <= q["ask_usd"] for q in s)
        print(f"  vs {t:5}: fit error spline {err(f'spl_usd_{t}'):.3f}, Deribit mark {err('mark_usd'):.3f} "
              f"(half-spreads, equal weights); spline inside bid-ask {inside}/{len(s)}; "
              f"butterfly-free {d['fits'][t]['arb_free']}, repaired {d['fits'][t]['repaired']} "
              f"(plain spline's arbitrage {d['fits'][t]['arb_bad']}, max move {d['fits'][t]['max_move_vp']} vp)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--expiry", default="25DEC26", help="Deribit expiry code, e.g. 25DEC26")
    p.add_argument("--out", type=Path, help="output HTML (default data/cache/smile_showcase/eth-smile-<expiry>.html)")
    args = p.parse_args()
    d = build(args.expiry.upper())
    summary(d)
    out = args.out or ROOT / "data" / "cache" / "smile_showcase" / f"eth-smile-{d['expiry'].lower()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text().replace("/*DATA*/null", json.dumps(d, separators=(",", ":")))
    out.write_text(html.replace("__EXPIRY_LONG__", d["expiry_long"]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
