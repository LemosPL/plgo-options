"""Verify the strike-edit maths behind the Optimizer v4 trade table.

Mirrors app.js optv4TradePnlNow + the After-curve delta in
optv4AfterSelectedAtHorizon, so the formula can be checked without a JS runtime.

What matters:
  * An untouched leg (and a qty-only edit) must produce EXACTLY the same numbers
    as before strike editing existed - the strike override is only passed when
    the strike actually moved.
  * The premium must be repriced at the new strike. This curve is
    qty * (BS value - premium paid); reusing the original premium at a new strike
    would offset the entire curve by the premium difference instead of reshaping
    it, which would show up as a phantom P&L jump at every spot.
  * Direction sanity: raising a SHORT call's strike must reduce the upside loss;
    raising a LONG call's strike must reduce its upside gain.

Run:  .venv\\Scripts\\python.exe scripts/diag_strike_edit.py
"""
import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, r, sigma, typ):
    """Black-Scholes, matching app.js bsPrice."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if typ == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if typ == "C":
        return S * norm_cdf(d1) - K * disc * norm_cdf(d2)
    return K * disc * norm_cdf(-d2) - S * norm_cdf(-d1)


def mid_price_at_strike(t, K, S0):
    """app.js optv4MidPriceAtStrike."""
    opt = t["opt"]
    if opt in ("F", "PERP"):
        return t["bs_price_usd"]
    sigma = t["iv_pct"] / 100.0
    T = max(t["dte"], 0) / 365.25
    if T <= 0 or sigma <= 0:
        return max(S0 - K, 0.0) if opt == "C" else max(K - S0, 0.0)
    return bs_price(S0, K, T, 0.0, sigma, opt)


def trade_pnl(t, spots, S0, h=0, qty_override=None, strike_override=None):
    """app.js optv4TradePnlNow."""
    qty = qty_override if qty_override is not None else t["qty"]
    moved = strike_override is not None and strike_override > 0
    K = strike_override if moved else t["strike"]
    opt = t["opt"]
    price = mid_price_at_strike(t, K, S0) if moved else t["bs_price_usd"]
    if opt in ("F", "PERP"):
        return [qty * (s - S0) for s in spots]
    sigma = t["iv_pct"] / 100.0
    T = max(t["dte"] - h, 0) / 365.25
    out = []
    for s in spots:
        val = bs_price(s, K, T, 0.0, sigma, opt) if (T > 0 and sigma > 0) else (
            max(s - K, 0.0) if opt == "C" else max(K - s, 0.0))
        out.append(qty * (val - price))
    return out


def after_delta(t, spots, S0, h, q_eff, k_eff):
    """app.js optv4AfterSelectedAtHorizon's per-leg delta."""
    q0, k0 = t["_qty0"], t["_strike0"]
    if q_eff == q0 and k_eff == k0:
        return [0.0] * len(spots)          # exact no-op
    c_eff = trade_pnl(t, spots, S0, h, q_eff, k_eff if k_eff != k0 else None)
    c0 = trade_pnl(t, spots, S0, h, q0)
    return [a - b for a, b in zip(c_eff, c0)]


def main() -> int:
    S0 = 4500.0
    spots = [3000.0, 3750.0, 4500.0, 5250.0, 6000.0, 7000.0]
    failures = []

    def leg(qty, K=4500.0, opt="C"):
        t = {"opt": opt, "strike": K, "dte": 90, "iv_pct": 60.0, "qty": qty}
        t["bs_price_usd"] = bs_price(S0, K, 90 / 365.25, 0.0, 0.60, opt)
        t["_qty0"], t["_strike0"] = qty, K
        return t

    # 1. Untouched leg -> exactly zero delta.
    t = leg(-100.0)
    d = after_delta(t, spots, S0, 0, t["_qty0"], t["_strike0"])
    print(f"1. untouched leg delta: max|d| = {max(abs(x) for x in d):.9f}")
    if any(abs(x) > 1e-12 for x in d):
        failures.append("untouched leg is not an exact no-op")

    # 2. Qty-only edit must be identical to the pre-change behaviour (which never
    #    passed a strike override), i.e. still netted against bs_price_usd.
    d_qty = after_delta(t, spots, S0, 0, -50.0, t["_strike0"])
    legacy = [a - b for a, b in zip(trade_pnl(t, spots, S0, 0, -50.0),
                                    trade_pnl(t, spots, S0, 0, -100.0))]
    same = all(abs(a - b) < 1e-9 for a, b in zip(d_qty, legacy))
    print(f"2. qty-only edit matches legacy path: {same}")
    if not same:
        failures.append("qty-only edit changed behaviour")

    # 3. Premium is repriced at the new strike.
    p_old = t["bs_price_usd"]
    p_new = mid_price_at_strike(t, 5000.0, S0)
    print(f"3. premium repriced: K=4500 -> ${p_old:,.2f}; K=5000 -> ${p_new:,.2f}"
          f"  (moved {p_old - p_new:,.2f})")
    if not (p_new < p_old):
        failures.append("raising a call strike did not lower its premium")

    # 4. Direction: SHORT call, strike up -> less upside loss.
    short = leg(-100.0)
    base = trade_pnl(short, spots, S0, 0, -100.0)
    moved = trade_pnl(short, spots, S0, 0, -100.0, 5000.0)
    print("4. short call 4500 -> 5000 (P&L at each spot):")
    for s, a, b in zip(spots, base, moved):
        print(f"     spot {s:7,.0f}   before {a:12,.0f}   after {b:12,.0f}   change {b - a:+11,.0f}")
    if not (moved[-1] > base[-1]):
        failures.append("short call: raising the strike did not reduce upside loss")

    # 5. Direction: LONG call, strike up -> less upside gain.
    lng = leg(100.0)
    b2 = trade_pnl(lng, spots, S0, 0, 100.0)
    m2 = trade_pnl(lng, spots, S0, 0, 100.0, 5000.0)
    print(f"5. long call 4500 -> 5000 at spot 7000: {b2[-1]:,.0f} -> {m2[-1]:,.0f}"
          f"  ({m2[-1] - b2[-1]:+,.0f})")
    if not (m2[-1] < b2[-1]):
        failures.append("long call: raising the strike did not reduce upside gain")

    # 6. Had the premium NOT been repriced, the curve would be offset by the
    #    premium difference at EVERY spot - show that this is what we avoided.
    naive = [100.0 * (bs_price(s, 5000.0, 90 / 365.25, 0.0, 0.60, "C") - p_old) for s in spots]
    off = [a - b for a, b in zip(naive, m2)]
    spread = max(off) - min(off)
    print(f"6. not repricing would add a flat ${off[0]:,.0f} offset at every spot "
          f"(spread across spots {spread:.6f} => a pure level shift, not a reshape)")
    if spread > 1e-6:
        failures.append("offset check malformed")

    # 7. Horizon decay still applies with a moved strike.
    m90 = trade_pnl(short, spots, S0, 90, -100.0, 5000.0)
    intrinsic_ok = abs(m90[2] - (-100.0 * (max(4500.0 - 5000.0, 0.0) - mid_price_at_strike(short, 5000.0, S0)))) < 1e-9
    print(f"7. at h=dte the moved leg collapses to intrinsic: {intrinsic_ok}")
    if not intrinsic_ok:
        failures.append("horizon decay wrong for a moved strike")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
