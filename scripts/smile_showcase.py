#!/usr/bin/env python3
"""Client showcase: one Deribit ETH expiry's smile, fitted to every bid and ask.

Snapshots every order book of the expiry (calls and puts, read concurrently so
prices and sizes are from the same moment), then:

    1) forward + discount factor from put-call parity (deribit_smile_scanner.fit_forward)
    2) out-of-the-money side only: Black vols of each bid and each ask
    3) microprice per strike: (bid x ask size + ask x bid size) / (bid size + ask size)
    4) two raw-SVI fits, one to the mid and one to the microprice, each minimising
       |SVI price - target| / half-spread under a penalty that keeps Gatheral's
       butterfly density g(k) >= 0; the result is re-checked on k in [-2.5, 2.5]
       and the page only claims "no butterfly arbitrage" for a fit that passes

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
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.stats import norm

import deribit_smile_scanner as sc

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("smile_showcase.html")
API = "https://www.deribit.com/api/v2/public"
K_RANGE = 2.5          # |log(K/F)| over which the no-arbitrage check runs
PENALTY = 50.0         # weight on negative density / variance in the fit's residuals


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


def book_frame(books: list[dict], expiry: str, now: dt.datetime) -> pd.DataFrame:
    exp = sc.parse_exp(expiry)
    rows = []
    for b in books:
        _, _, K, cp = b["instrument_name"].split("-")
        S = b["estimated_delivery_price"]  # Deribit quotes options in ETH: USD = ETH price x S
        bid, ask = b.get("best_bid_price") or 0, b.get("best_ask_price") or 0
        rows.append(dict(K=float(K), cp=cp, T=(exp - now).total_seconds() / (365 * 86400),
                         bid=bid * S if bid else np.nan, ask=ask * S if ask else np.nan,
                         mark=b["mark_price"] * S, mark_iv=b["mark_iv"], F0=b["underlying_price"],
                         oi=b["open_interest"], vol24=(b.get("stats") or {}).get("volume") or 0.0,
                         bsize=b.get("best_bid_amount") or 0.0, asize=b.get("best_ask_amount") or 0.0,
                         spot=b["index_price"]))
    return pd.DataFrame(rows)


def svi_w(p, k):
    a, b, rho, m, s = p
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s * s))


def density(p, k):
    """Total variance w and Gatheral's g(k); g >= 0 everywhere = no butterfly arbitrage."""
    a, b, rho, m, s = p
    w = svi_w(p, k)
    w1 = b * (rho + (k - m) / np.sqrt((k - m) ** 2 + s * s))
    w2 = b * s * s / ((k - m) ** 2 + s * s) ** 1.5
    return w, (1 - k * w1 / (2 * w)) ** 2 - w1 ** 2 / 4 * (1 / w + 0.25) + w2 / 2


def build(expiry: str) -> dict:
    books, t0, t1 = asyncio.run(snapshot(expiry))
    now = dt.datetime.fromtimestamp((t0 + t1) / 2, dt.timezone.utc)
    g = book_frame(books, expiry, now)
    F, DF, _, _ = sc.fit_forward(g)
    T = float(g["T"].iloc[0])

    def ivp(p, Ks):
        return np.sqrt(np.maximum(svi_w(p, np.log(np.asarray(Ks, float) / F)), 1e-9) / T)

    def black(K, v, cp):
        K = np.asarray(K, float)
        s = v * np.sqrt(T)
        d1 = (np.log(F / K) + 0.5 * s * s) / s
        d2 = d1 - s
        call = DF * (F * norm.cdf(d1) - K * norm.cdf(d2))
        put = DF * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        return np.where(np.asarray(cp) == "C", call, put)

    quotes = []
    for r in g.sort_values("K").itertuples():
        if (r.cp == "C") != (r.K >= F):  # OTM side only
            continue
        ivb = sc.implied(r.bid, F, r.K, T, r.cp, DF) if np.isfinite(r.bid) else np.nan
        iva = sc.implied(r.ask, F, r.K, T, r.cp, DF) if np.isfinite(r.ask) else np.nan
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
    kg = np.linspace(-K_RANGE, K_RANGE, 801)
    atm = float(np.median([(q["bid_iv"] + q["ask_iv"]) / 200 for q in scored]))

    def fit(target: np.ndarray) -> tuple[np.ndarray, bool]:
        def res(p):
            w, gk = density(p, kg)
            return np.concatenate([(black(K, ivp(p, K), CP) - target) / half,
                                   np.minimum(gk, 0) * PENALTY, np.minimum(w, 0) * PENALTY])
        lb, ub = [-1, 1e-4, -0.999, -1, 1e-3], [1, 5, 0.999, 1, 2]
        best = None
        for rho0 in (-0.4, 0.0, 0.3):
            for s0 in (0.2, 0.5, 0.8):
                r = least_squares(res, [atm * atm * T * 0.3, 0.2, rho0, 0.0, s0], bounds=(lb, ub))
                if best is None or r.cost < best.cost:
                    best = r
        w, gk = density(best.x, np.linspace(-K_RANGE, K_RANGE, 20001))
        return best.x, bool(gk.min() >= 0 and w.min() > 0)

    targets = {"mid": np.array([(q["bid_usd"] + q["ask_usd"]) / 2 for q in scored]),
               "micro": np.array([q["micro_usd"] for q in scored])}
    fits = {}
    for name, tgt in targets.items():
        p, ok = fit(tgt)
        fits[name] = dict(svi=dict(zip(["a", "b", "rho", "m", "sigma"], map(float, p))), arb_free=ok,
                          wing_slopes=dict(left=round(float(p[1] * (1 - p[2])), 4),
                                           right=round(float(p[1] * (1 + p[2])), 4)), p=p)
        for q in quotes:
            v = float(ivp(p, [q["K"]])[0])
            q[f"fit_iv_{name}"] = round(v * 100, 3)
            q[f"fit_usd_{name}"] = round(float(black([q["K"]], v, [q["cp"]])[0]), 4)
    for q in quotes:  # vega at the mid fit, USD per vol point per contract
        s = q["fit_iv_mid"] / 100 * np.sqrt(T)
        d1 = (np.log(F / q["K"]) + 0.5 * s * s) / s
        q["vega"] = round(float(DF * F * norm.pdf(d1) * np.sqrt(T) / 100), 4)

    pm, pu = fits["mid"]["p"], fits["micro"]["p"]
    Ks = np.exp(np.linspace(np.log(min(q["K"] for q in quotes) * 0.97),
                            np.log(max(q["K"] for q in quotes) * 1.02), 260))
    exp = sc.parse_exp(expiry)
    for f in fits.values():
        del f["p"]
    return dict(
        expiry=expiry, expiry_long=f"{exp.day} {exp:%b %Y}", expiry_ts=int(exp.timestamp() * 1000),
        asof=now.isoformat(timespec="seconds"), snapshot_secs=round(t1 - t0, 1),
        spot=round(float(g.spot.median()), 2), forward=round(float(F), 2), DF=round(float(DF), 5),
        T_days=round(T * 365, 1), atm_iv=round(float(ivp(pm, [F])[0]) * 100, 2),
        iv_80=round(float(ivp(pm, [0.8 * F])[0]) * 100, 2), iv_120=round(float(ivp(pm, [1.2 * F])[0]) * 100, 2),
        n_fit=len(scored), arb_range=[int(round(F * np.exp(-K_RANGE))), int(round(F * np.exp(K_RANGE)))],
        fits=fits, quotes=quotes,
        curve=[[round(float(k), 1), round(float(ivp(pm, [k])[0]) * 100, 3), round(float(ivp(pu, [k])[0]) * 100, 3)]
               for k in Ks])


def summary(d: dict) -> None:
    s = [q for q in d["quotes"] if "micro_usd" in q]
    print(f"{d['expiry']}  snapshot {d['asof']} ({d['snapshot_secs']} s)  index {d['spot']}  "
          f"forward {d['forward']}  DF {d['DF']}  ATM {d['atm_iv']}%  strikes scored {len(s)}")
    for t in ("mid", "micro"):
        tgt = [(q["bid_usd"] + q["ask_usd"]) / 2 if t == "mid" else q["micro_usd"] for q in s]
        err = lambda key: np.sqrt(np.mean([((q[key] - x) / ((q["ask_usd"] - q["bid_usd"]) / 2)) ** 2  # noqa: E731
                                           for q, x in zip(s, tgt)]))
        inside = sum(q["bid_usd"] <= q[f"fit_usd_{t}"] <= q["ask_usd"] for q in s)
        print(f"  vs {t:5}: fit error SVI {err(f'fit_usd_{t}'):.3f}, Deribit mark {err('mark_usd'):.3f} "
              f"(half-spreads, equal weights); SVI inside bid-ask {inside}/{len(s)}; "
              f"butterfly-free {d['fits'][t]['arb_free']}")


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
