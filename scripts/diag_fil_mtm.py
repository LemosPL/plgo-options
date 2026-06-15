"""Diagnostic: compare FIL Portfolio-PnL trades-table MTM vs the Pricing-tab math.

Calls the two real endpoints (portfolio_pnl + get_vol_surface) and, for each FIL
trade, recomputes MTM the way the Pricing tab does (linear strike interp + DTE
term-structure interp + pricerBs), then prints portfolio vs pricing side by side.
"""
import asyncio
import math
from datetime import datetime, date

from plgo_options.web.routes.portfolio import portfolio_pnl
from plgo_options.web.routes.market import get_vol_surface
from plgo_options.pricing.options import bs_price


def _lookup_iv_in_smile(smile, strike):
    strikes, ivs = smile["strikes"], smile["ivs"]
    if not strikes:
        return None
    if strike <= strikes[0]:
        return ivs[0]
    if strike >= strikes[-1]:
        return ivs[-1]
    for i in range(len(strikes) - 1):
        if strikes[i] <= strike <= strikes[i + 1]:
            t = (strike - strikes[i]) / (strikes[i + 1] - strikes[i])
            return ivs[i] + t * (ivs[i + 1] - ivs[i])
    return ivs[-1]


def _interp_iv(smiles, target_dte, strike):
    """Replicates JS _interpolateSmileIv: linear strike interp + DTE term interp."""
    s = sorted(smiles, key=lambda x: x["dte"])
    before = after = None
    for sm in s:
        if sm["dte"] <= target_dte:
            before = sm
        if sm["dte"] >= target_dte and after is None:
            after = sm
    if not before and not after:
        return None
    if not before:
        before = after
    if not after:
        after = before
    if before is after:
        return _lookup_iv_in_smile(before, strike)
    rng = after["dte"] - before["dte"]
    w = (target_dte - before["dte"]) / rng if rng > 0 else 0.5
    ivb = _lookup_iv_in_smile(before, strike)
    iva = _lookup_iv_in_smile(after, strike)
    if ivb is None or iva is None:
        return ivb if ivb is not None else iva
    return ivb * (1 - w) + iva * w


def pricing_tab_iv(smiles, expiry_code, target_dte, strike):
    """Replicates JS lookupSmileIv: exact expiry first, else DTE interpolation."""
    exact = next((sm for sm in smiles if sm["expiry_code"] == expiry_code), None)
    if exact:
        return _lookup_iv_in_smile(exact, strike), False
    return _interp_iv(smiles, target_dte, strike), True


def _code_from_expiry(expiry_iso):
    d = datetime.fromisoformat(expiry_iso[:10]).date() if "T" not in expiry_iso else datetime.fromisoformat(expiry_iso).date()
    return f"{d.day}{d.strftime('%b').upper()}{d.strftime('%y')}", d


async def main():
    vs = await get_vol_surface(asset="FIL")
    fil_spot = vs["eth_spot"]
    smiles = vs["smiles"]
    print(f"FIL spot = {fil_spot:.4f} | {len(smiles)} smiles: "
          f"{[(s['expiry_code'], s['dte']) for s in smiles]}")

    try:
        pnl = await portfolio_pnl(asset="FIL", include_expired=False)
    except Exception as e:
        print(f"portfolio_pnl(include_expired=False) -> {e}; retrying with expired")
        pnl = await portfolio_pnl(asset="FIL", include_expired=True)

    rows = pnl["trades"] if isinstance(pnl, dict) and "trades" in pnl else pnl.get("positions", [])
    today = date.today()

    hdr = f"{'instrument':<26}{'qty':>12}{'pf_iv':>7}{'px_iv':>7}{'pf_px':>10}{'px_px':>10}{'pf_mtm':>14}{'px_mtm':>14}{'dMTM%':>8}{'src':>6}"
    print(hdr)
    print("-" * len(hdr))

    tot_pf = tot_px = 0.0
    for r in rows:
        strike = float(r["strike"])
        opt = r["opt"]
        nqty = float(r["net_qty"])
        code, exp_d = _code_from_expiry(str(r["expiry"]))
        dte = max((exp_d - today).days, 0)
        T = dte / 365.25
        iv, synth = pricing_tab_iv(smiles, code, dte, strike)
        if iv is None:
            iv = r["iv_pct"]  # fall back to whatever portfolio used
        sigma = iv / 100.0
        px = bs_price(fil_spot, strike, T, 0.0, sigma, opt) if T > 0 and sigma > 0 else (
            max(fil_spot - strike, 0.0) if opt == "C" else max(strike - fil_spot, 0.0))
        px_mtm = nqty * px
        pf_mtm = r["current_mtm"]
        pf_px = r["mark_price_usd"]
        tot_pf += pf_mtm
        tot_px += px_mtm
        dpct = (px_mtm - pf_mtm) / abs(pf_mtm) * 100 if pf_mtm else 0.0
        inst = r.get("instrument", f"FIL-{code}-{strike}-{opt}")
        print(f"{inst:<26}{nqty:>12,.0f}{r['iv_pct']:>7.1f}{iv:>7.1f}"
              f"{pf_px:>10.4f}{px:>10.4f}{pf_mtm:>14,.0f}{px_mtm:>14,.0f}{dpct:>7.1f}%"
              f"{('TERM' if synth else 'exact'):>6}")

    print("-" * len(hdr))
    print(f"{'TOTAL':<26}{'':>12}{'':>7}{'':>7}{'':>10}{'':>10}{tot_pf:>14,.0f}{tot_px:>14,.0f}"
          f"{((tot_px-tot_pf)/abs(tot_pf)*100 if tot_pf else 0):>7.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
