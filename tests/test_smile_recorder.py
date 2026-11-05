"""Smile recorder: expiry selection, leave-one-out edges on a known smile, tick round trip."""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import io
import json
import zipfile

import numpy as np

from plgo_options.market_data import smile_recorder as rec
from plgo_options.pricing.smile_fit import black, parse_exp, svi_iv
from plgo_options.web.routes import smile_recorder as routes

NOW = dt.datetime(2026, 11, 3, 15, 0, tzinfo=dt.timezone.utc)
EXPIRY = "25DEC26"
F, S = 2300.0, 2290.0
SVI = np.array([0.03, 0.05, -0.3, 0.02, 0.2])     # a, b, rho, m, sigma: ~50% ATM, a put skew
STRIKES = [1400, 1600, 1800, 1900, 2000, 2100, 2200, 2300, 2400, 2500, 2600, 2800, 3000, 3200, 3500]


def _inst(code: str) -> dict:
    return {"instrument_name": f"ETH-{code}-2000-C",
            "expiration_timestamp": int(parse_exp(code).timestamp() * 1000)}


def test_quarterlies_pick_last_friday_of_quarter_months_and_roll():
    insts = [_inst(c) for c in ("6NOV26", "27NOV26", "18DEC26", "25DEC26", "29JAN27",
                                "26MAR27", "25JUN27", "24SEP27")]
    assert rec.quarterlies(insts, 3, NOW.timestamp()) == ["25DEC26", "26MAR27", "25JUN27"]
    day_before = parse_exp("25DEC26").timestamp() - 3600   # under a day left: roll on
    assert rec.quarterlies(insts, 3, day_before) == ["26MAR27", "25JUN27", "24SEP27"]


def _books(half_usd: float = 4.0, shift: dict | None = None) -> list[dict]:
    """Both sides of every strike, quoted at the SVI price +/- half_usd; shift moves
    one quote (K, cp) by a USD amount to make a dislocation."""
    T = (parse_exp(EXPIRY) - NOW).total_seconds() / (365 * 86400)
    out = []
    for K in STRIKES:
        v = float(svi_iv(SVI, [K], F, T)[0])
        for cp in ("C", "P"):
            fair = black(F, K, T, v, cp) + (shift or {}).get((K, cp), 0.0)
            bid, ask = max(fair - half_usd, 0.5), fair + half_usd
            out.append({"instrument_name": f"ETH-{EXPIRY}-{K}-{cp}", "timestamp": 1_000,
                        "bids": [[bid / S, 20.0]], "asks": [[ask / S, 30.0]],
                        "best_bid_price": bid / S, "best_ask_price": ask / S,
                        "best_bid_amount": 20.0, "best_ask_amount": 30.0,
                        "mark_price": fair / S, "mark_iv": v * 100, "underlying_price": F,
                        "index_price": S, "estimated_delivery_price": S, "open_interest": 0.0,
                        "stats": {"volume": 0.0}})
    return out


def test_fit_recovers_a_clean_smile_and_no_edge_clears_the_spread():
    rows, fit = rec.analyse(EXPIRY, _books(), NOW)
    scored = [r for r in rows if r["scored"]]
    assert fit["arb_free"] == 1 and fit["n_scored"] == len(STRIKES)
    assert abs(fit["forward"] - F) < 0.5 and fit["fit_err_mid"] < 0.05
    for r in scored:  # quoted symmetrically around the curve: crossing costs half a spread plus fee
        assert abs(r["edge_buy"] + 4.0 + r["fee_buy"]) < 0.3
        assert abs(r["edge_sell"] + 4.0 + r["fee_sell"]) < 0.3


def test_leave_one_out_exposes_a_cheap_ask_the_full_fit_partly_hides():
    rows, _ = rec.analyse(EXPIRY, _books(shift={(1800, "P"): -7.0}), NOW)
    r = next(x for x in rows if x["K"] == 1800)
    assert r["edge_buy"] > 1.5                           # ask 3 below the curve, less the fee
    assert r["fit_loo"] - r["fit"] > 0.1                 # the full fit was dragged towards the quote
    others = [x for x in rows if x["scored"] and x["K"] != 1800]
    assert all(x["edge_buy"] < 0 and x["edge_sell"] < 0 for x in others)


def test_tick_written_once_and_read_back(tmp_path, monkeypatch):
    async def fake_snapshot(n, depth):
        t = NOW.timestamp()
        return {EXPIRY: _books(), "26MAR27": _books()[:4]}, t - 0.5, t + 0.5  # second expiry too thin

    monkeypatch.setattr(rec, "snapshot", fake_snapshot)
    out = asyncio.run(rec.record_tick(tmp_path))
    assert [f["expiry"] for f in out["fits"]] == [EXPIRY] and "26MAR27" in out["errors"]
    assert out["fits"][0]["n_edge_buy"] == 0
    assert rec.days(tmp_path) == {"2026-11-03": 1}
    blob = json.loads(gzip.decompress((tmp_path / "2026-11-03" / "150000.json.gz").read_bytes()))
    assert set(blob["books"]) == {EXPIRY, "26MAR27"}            # books kept even for the failed expiry
    strikes, fits = rec.load(tmp_path)
    assert len(fits) == 1 and (strikes.scored == 1).sum() == len(STRIKES)
    (s,) = rec.summarize(strikes)
    assert s["expiry"] == EXPIRY and s["buy_pct"] == 0 and s["med_buy_vp"] is None

    monkeypatch.setenv("SMILE_RECORDER_DIR", str(tmp_path))
    st = asyncio.run(routes.status())
    assert st["ticks"] == 1 and st["last"][0]["expiry"] == EXPIRY
    resp = asyncio.run(routes.day_zip("2026-11-03"))

    async def body():
        return b"".join([c async for c in resp.body_iterator])
    assert zipfile.ZipFile(io.BytesIO(asyncio.run(body()))).namelist() == ["2026-11-03/150000.json.gz"]


def test_a_book_deribit_will_not_serve_falls_back_to_the_touch_then_is_dropped(monkeypatch):
    # Deribit answers 400 internal_server_error on some deep OTM books at depth > 1
    import httpx

    names = ["ETH-25DEC26-2500-C", "ETH-25DEC26-6000-C", "ETH-25DEC26-7000-C"]

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        if req.url.path.endswith("get_instruments"):
            return httpx.Response(200, json={"result": [{"instrument_name": n} for n in names]})
        n, depth = p["instrument_name"], p["depth"]
        if n.endswith("7000-C") or (n.endswith("6000-C") and depth != "1"):
            return httpx.Response(400, json={"error": {"code": 11094, "message": "internal_server_error"}})
        return httpx.Response(200, json={"result": {"instrument_name": n, "bids": [], "asks": [[0.001, 1.0]]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(rec.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    books, _, _ = asyncio.run(rec.expiry_books(["25DEC26"]))
    assert [b["instrument_name"] for b in books["25DEC26"]] == names[:2]
