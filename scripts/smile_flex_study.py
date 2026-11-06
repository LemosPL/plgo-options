#!/usr/bin/env python3
"""Is raw SVI too stiff? Raw SVI vs SVI + a smooth spline correction vs a smoothing
spline vs Deribit's mark, on the order books the smile recorder saved.

Raw SVI leaves residuals that alternate in sign in smooth runs across strikes
and persist tick after tick (26MAR27: +0.8 / -0.75 / +0.5 / -0.5 half-spreads),
which an SVI-timed taker would read as standing edges. The candidate fix:

    w(k) = SVI(k) + sum_i c_i B_i(k)

B_i cubic B-splines on uniform knots spanning the quoted strikes (so the
correction fades to zero past the last quote and the wings stay SVI), with a
penalty on second differences of c (smoothness) and the same butterfly penalty
as the raw fit, g(k) >= G_FLOOR computed numerically. Fitted in price to the mid, in
half-spreads, warm-started from the raw SVI fit.

The smoothing spline is the client page's (smile_showcase.py): cubic, implied vol
of the mid against log-moneyness, weights 1 / half-spread^2 in vol points,
smoothness by generalized cross-validation, scored as fitted ("spline") and with
smile_fit.fit_spline's butterfly repair ("spline arb-free"). It has no shape beyond
its data, so its arbitrage check covers the quoted strikes only (SVI: |k| <= 2.5),
from the spline's exact derivatives.

Scores, per expiry, all in half-spreads against the mid (the mark's score too):

    in-sample   RMS error of the curve fitted to every strike
    LOO         RMS error at each strike of the curve fitted WITHOUT it (sampled
                ticks): what an execution signal would see; overfitting shows here
    LOO inner   the same without the lowest and highest strike, where leaving the
                strike out forces an extrapolation (hardest on the spline)
    bias        RMS over strikes of each strike's mean residual across ticks:
                the standing, fake edges this exercise is meant to remove
    arb-free    share of fits with g(k) >= 0 and w > 0 on |k| <= 2.5

    .venv/bin/python -u scripts/smile_flex_study.py                     # every recorded tick
    .venv/bin/python -u scripts/smile_flex_study.py --loo-every 10 --bases 4,6,8 --lam 1,10
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import BSpline
from scipy.optimize import least_squares

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.pricing.smile_fit import (K_RANGE, PENALTY, black_vec, fit_spline, fit_svi,  # noqa: E402
                                            otm_quotes as quotes, svi_w, vega)

KG = np.linspace(-K_RANGE, K_RANGE, 801)
KG_CHECK = np.linspace(-K_RANGE, K_RANGE, 8001)
# The fit holds g(k) above a small margin, not just zero: in the far wings g is
# small, and the correction's tail tipped it to about -0.001 between grid points.
G_FLOOR = 0.01


def basis(kmin: float, kmax: float, n: int):
    """n cubic B-splines on uniform knots over [kmin, kmax]; each is 0 outside it."""
    t = np.linspace(kmin, kmax, n + 4)
    el = [BSpline.basis_element(t[i:i + 5], extrapolate=False) for i in range(n)]
    return lambda k: np.nan_to_num(np.column_stack([e(np.asarray(k, float)) for e in el]))


def g_numeric(w: np.ndarray, k: np.ndarray) -> np.ndarray:
    w1 = np.gradient(w, k)
    w2 = np.gradient(w1, k)
    return (1 - k * w1 / (2 * w)) ** 2 - w1 ** 2 / 4 * (1 / w + 0.25) + w2 / 2


class Flex:
    """SVI + smooth spline correction in total variance."""

    def __init__(self, q: dict, n: int, lam: float):
        k = np.log(q["K"] / q["F"])
        pad = 0.05 * (k.max() - k.min())
        self.B = basis(k.min() - pad, k.max() + pad, n)
        self.Bg = self.B(KG)
        self.n, self.lam, self.q = n, lam, q
        self.scale = q["atm"] ** 2 * q["T"]       # total variance ATM: the size a correction is measured against

    def w(self, x, k, Bk=None):
        Bk = self.B(k) if Bk is None else Bk
        return svi_w(x[:5], k) + Bk @ x[5:]

    def price(self, x, K, CP):
        q = self.q
        w = self.w(x, np.log(np.asarray(K) / q["F"]))
        return black_vec(q["F"], K, q["T"], np.sqrt(np.maximum(w, 1e-9) / q["T"]), CP, q["DF"])

    def fit(self, idx: np.ndarray, x0: np.ndarray):
        q = self.q
        K, CP = q["K"][idx], [q["CP"][i] for i in idx]
        mid, half = q["mid"][idx], q["half"][idx]
        rough = np.sqrt(self.lam) / self.scale

        def res(x):
            wg = self.w(x, KG, self.Bg)
            gk = g_numeric(np.maximum(wg, 1e-9), KG)
            return np.concatenate([(self.price(x, K, CP) - mid) / half, np.minimum(gk - G_FLOOR, 0) * PENALTY,
                                   np.minimum(wg, 0) * PENALTY, rough * np.diff(x[5:], 2),
                                   0.1 * rough * x[5:]])
        lb = [-1, 1e-4, -0.999, -1, 1e-3] + [-1] * self.n
        ub = [1, 5, 0.999, 1, 2] + [1] * self.n
        return least_squares(res, np.clip(x0, lb, ub), bounds=(lb, ub)).x

    def arb_free(self, x) -> bool:
        w = self.w(x, KG_CHECK)
        return bool(w.min() > 0 and g_numeric(w, KG_CHECK).min() >= 0)


def spline_fit(q: dict, idx: np.ndarray, arb_free: bool):
    """The showcase's smoothing spline through the mid vols of the strikes in idx:
    (spline, butterfly-free on the quoted range)."""
    K, iv = q["K"][idx], q["iv_mid"][idx]
    ok = np.isfinite(iv)
    vg = np.array([vega(q["F"], k_, q["T"], v, q["DF"]) for k_, v in zip(K[ok], iv[ok])])
    size = None if q.get("size") is None else q["size"][idx][ok]
    spl, _, free = fit_spline(np.log(K[ok] / q["F"]), iv[ok] * 100, q["half"][idx][ok] / vg, q["T"], arb_free,
                              size=size)
    return spl, free


def spline_price(q: dict, spl, K, CP):
    v = np.maximum(spl(np.log(np.asarray(K, float) / q["F"])) / 100, 1e-4)
    return black_vec(q["F"], K, q["T"], v, CP, q["DF"])


def score(q: dict, loo: bool, models: dict[str, tuple[int, float]]) -> list[dict]:
    """Per strike residuals (half-spreads vs mid) for raw SVI, each Flex and the mark."""
    n = len(q["K"])
    allidx = np.arange(n)
    p, ok = fit_svi(q["K"], q["CP"], q["mid"], q["half"], q["F"], q["T"], q["DF"], q["atm"])
    out = {"mark": dict(fit=(q["mark"] - q["mid"]) / q["half"], arb=True)}
    flex0 = Flex(q, 4, 1.0)
    out["svi"] = dict(fit=(flex0.price(np.r_[p, np.zeros(4)], q["K"], q["CP"]) - q["mid"]) / q["half"], arb=ok)
    if loo:
        r = np.empty(n)
        for j in range(n):
            keep = allidx != j
            pj, _ = fit_svi(q["K"][keep], [c for c, k in zip(q["CP"], keep) if k], q["mid"][keep],
                            q["half"][keep], q["F"], q["T"], q["DF"], q["atm"], x0=p)
            r[j] = (flex0.price(np.r_[pj, np.zeros(4)], q["K"][j:j + 1], q["CP"][j:j + 1])[0] - q["mid"][j]) / q["half"][j]
        out["svi"]["loo"] = r
    for name, (nb, lam) in models.items():
        m = Flex(q, nb, lam)
        x = m.fit(allidx, np.r_[p, np.zeros(nb)])
        out[name] = dict(fit=(m.price(x, q["K"], q["CP"]) - q["mid"]) / q["half"], arb=m.arb_free(x))
        if loo:
            r = np.empty(n)
            for j in range(n):
                xj = m.fit(allidx[allidx != j], x)
                r[j] = (m.price(xj, q["K"][j:j + 1], q["CP"][j:j + 1])[0] - q["mid"][j]) / q["half"][j]
            out[name]["loo"] = r
    for name, fix in (("spline", False), ("spline arb-free", True)):
        spl, free = spline_fit(q, allidx, fix)
        out[name] = dict(fit=(spline_price(q, spl, q["K"], q["CP"]) - q["mid"]) / q["half"], arb=free)
        if loo:
            r = np.empty(n)
            for j in range(n):
                sj, _ = spline_fit(q, allidx[allidx != j], fix)
                r[j] = (spline_price(q, sj, q["K"][j:j + 1], q["CP"][j:j + 1])[0] - q["mid"][j]) / q["half"][j]
            out[name]["loo"] = r
    out["mark"]["loo"] = out["mark"]["fit"] if loo else None
    rows = []
    for name, d in out.items():
        for j in range(n):
            rows.append(dict(model=name, K=q["K"][j], end=int(j in (0, n - 1)), fit=d["fit"][j], arb=int(d["arb"]),
                             loo=d["loo"][j] if loo else np.nan))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=ROOT / "data" / "cache" / "smile_recorder")
    ap.add_argument("--every", type=int, default=1, help="use every n-th tick for the in-sample scores")
    ap.add_argument("--loo-every", type=int, default=10, help="leave-one-out on every n-th tick used")
    ap.add_argument("--bases", default="4,6,8", help="spline basis sizes to try")
    ap.add_argument("--lam", default="1,10", help="roughness penalties to try")
    ap.add_argument("--out", type=Path, help="CSV of every residual")
    args = ap.parse_args()
    models = {f"svi+{nb}/λ{lam:g}": (nb, lam) for nb in map(int, args.bases.split(","))
              for lam in map(float, args.lam.split(","))}
    files = sorted(args.dir.expanduser().glob("*/*.json.gz"))[::args.every]
    print(f"{len(files)} ticks, leave-one-out on every {args.loo_every}th; models: svi, {', '.join(models)}, spline, spline arb-free, mark",
          flush=True)
    rows = []
    for i, f in enumerate(files):
        d = json.loads(gzip.decompress(f.read_bytes()))
        now = dt.datetime.fromtimestamp((d["t0"] + d["t1"]) / 2, dt.timezone.utc)
        for expiry, books in d["books"].items():
            q = quotes(expiry, books, now)
            if q is None:
                continue
            for r in score(q, i % args.loo_every == 0, models):
                rows.append(dict(tick=f.stem, day=f.parent.name, expiry=expiry, **r))
        if i % 20 == 0:
            print(f"  {i + 1}/{len(files)}", flush=True)
    df = pd.DataFrame(rows)
    if args.out:
        df.to_csv(args.out, index=False)
    order = ["svi", *models, "spline", "spline arb-free", "mark"]
    for expiry, x in df.groupby("expiry", sort=False):
        print(f"\n{expiry}  ({x.tick.nunique()} ticks, half-spreads vs mid)")
        print(f"{'model':16}{'in-sample':>10}{'LOO':>8}{'LOO inner':>11}{'bias':>8}{'arb-free':>10}")
        for m in order:
            y = x[x.model == m]
            ins = np.sqrt((y.fit ** 2).mean())
            loo = np.sqrt((y.loo.dropna() ** 2).mean())
            inner = np.sqrt((y[y.end == 0].loo.dropna() ** 2).mean())
            bias = np.sqrt((y.groupby("K").fit.mean() ** 2).mean())
            arb = y.groupby("tick").arb.first().mean()
            print(f"{m:16}{ins:>10.3f}{loo:>8.3f}{inner:>11.3f}{bias:>8.3f}{100 * arb:>9.0f}%")
    print("\nmark has no fit, so its LOO is its plain error. bias: RMS of each strike's mean "
          "residual over ticks, i.e. edges that never go away.")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # app imports can leave threads that hang interpreter exit
