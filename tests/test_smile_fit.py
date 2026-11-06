"""Smoothing spline: the no-arbitrage refit removes arbitrage inherited from the quotes."""

from __future__ import annotations

import numpy as np

from plgo_options.pricing.smile_fit import fit_spline, spline_density, svi_iv

F, T = 2300.0, 51 / 365
SVI = np.array([0.03, 0.05, -0.3, 0.02, 0.2])
STRIKES = np.array([800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200,
                    3500, 3800, 4000, 4600, 4800, 5500, 6000, 6500], float)


def _smile(bump: dict | None = None):
    k = np.log(STRIKES / F)
    iv = svi_iv(SVI, STRIKES, F, T) * 100 + np.array([(bump or {}).get(K, 0.0) for K in STRIKES])
    half_vp = np.where(STRIKES >= 4600, 12.0, 1.0)      # wide, near-empty quotes in the call wing
    return k, iv, half_vp


def test_clean_smile_is_left_alone():
    k, iv, hv = _smile()
    spl, repaired, ok = fit_spline(k, iv, hv, T)
    assert ok and not repaired


# 25DEC26 mid vols on 4 Nov 08:00 (forward 2315, 51 days): strike, mid vol, half-spread in vol
# points. The 4600-5500 calls were near-empty wide markets whose mids priced above the 4000 call.
SNAP = np.array([
    [800, 120.47, 13.19], [1000, 91.19, 8.55], [1200, 81.45, 2.89], [1300, 74.86, 3.77],
    [1400, 70.97, 2.61], [1500, 67.19, 2.04], [1600, 64.49, 1.44], [1700, 60.67, 1.49],
    [1800, 57.81, 1.50], [1900, 55.32, 1.17], [2000, 53.29, 0.94], [2100, 51.91, 0.80],
    [2200, 50.41, 0.89], [2300, 49.42, 1.01], [2400, 48.82, 0.67], [2500, 48.45, 1.06],
    [2600, 48.33, 0.58], [2700, 48.78, 0.88], [2800, 49.35, 1.04], [2900, 50.12, 0.92],
    [3000, 50.66, 1.50], [3100, 51.39, 1.38], [3200, 52.76, 1.31], [3300, 53.87, 1.32],
    [3400, 54.87, 1.76], [3500, 56.02, 2.10], [3600, 57.63, 1.76], [3800, 60.18, 2.37],
    [4000, 62.90, 2.67], [4600, 88.45, 11.59], [4800, 99.77, 15.60], [5500, 105.78, 14.26],
    [6000, 91.72, 6.96], [6500, 97.20, 6.87]])


def test_arbitraged_wing_is_refitted_without_moving_the_body():
    K, iv, hv = SNAP.T
    k = np.log(K / 2315.0)
    raw, _, raw_ok = fit_spline(k, iv, hv, T, arb_free=False)
    assert not raw_ok
    spl, repaired, ok = fit_spline(k, iv, hv, T)
    assert repaired and ok
    w, g = spline_density(spl, np.linspace(k[0], k[-1], 40001), T)
    assert w.min() > 0 and g.min() >= 0
    body = K <= 3600
    assert (np.abs(spl(k[body]) - raw(k[body])) / hv[body]).max() < 0.1   # half-spreads
    assert spl(np.log(4800 / 2315.0)) < raw(np.log(4800 / 2315.0)) - 5  # the wing comes down


def test_a_thin_quote_pulls_the_spline_less_than_a_deep_one():
    k, iv, hv = _smile({2400: 1.5})                     # 2400's mid 1.5 vol points off the smile
    j = int(np.flatnonzero(STRIKES == 2400)[0])
    true = svi_iv(SVI, STRIKES, F, T)[j] * 100
    deep = np.full(len(k), 300.0)
    thin = deep.copy()
    thin[j] = 1.0                                        # one contract on its thinner side
    pull = {name: fit_spline(k, iv, hv, T, size=s)[0](k[j]) - true
            for name, s in (("spread only", None), ("deep", deep), ("thin", thin))}
    assert abs(pull["deep"] - pull["spread only"]) < 0.02      # equal sizes: the same fit
    assert 0 < pull["thin"] < 0.9 * pull["deep"]           # log weights: a gentle discount, ~8x less weight


def test_quotes_carry_sizes_only_when_every_strike_has_them():
    import datetime as dt

    from plgo_options.pricing.smile_fit import black, otm_quotes

    S, now = 2290.0, dt.datetime(2026, 11, 3, 15, tzinfo=dt.timezone.utc)
    tt = (dt.datetime(2026, 12, 25, 8, tzinfo=dt.timezone.utc) - now).total_seconds() / (365 * 86400)

    def books(bsize):
        out = []
        for K in STRIKES:
            v = float(svi_iv(SVI, [K], F, tt)[0])
            for cp in ("C", "P"):
                fair = black(F, K, tt, v, cp)
                out.append({"instrument_name": f"ETH-25DEC26-{K:g}-{cp}", "best_bid_price": max(fair - 2, 0.5) / S,
                            "best_ask_price": (fair + 2) / S, "best_bid_amount": bsize, "best_ask_amount": 40.0,
                            "mark_price": fair / S, "mark_iv": v * 100, "underlying_price": F, "index_price": S,
                            "estimated_delivery_price": S, "open_interest": 0.0, "stats": {}})
        return out
    q = otm_quotes("25DEC26", books(15.0), now)
    assert (q["size"] == 15.0).all()                    # the thinner side
    assert otm_quotes("25DEC26", books(None), now)["size"] is None   # the book summary: no sizes
