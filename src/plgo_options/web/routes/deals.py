"""Deals / Risk-analysis endpoint.

Groups the flat option legs in the ``trades`` table into *deals* (multi-leg
structures) by ``(counterparty, trade_date)``, classifies each structure
(call spread, put spread, iron condor, straddle, ...), and returns everything
the frontend needs to render a risk view per deal:

  * payoff-at-expiry curve across a spot grid (using each leg's ENTRY premium),
  * a risk-neutral log-normal distribution of terminal spot (from the vol
    surface IV) — probability mass + density aligned to the grid,
  * probability-of-profit, expected P&L, max profit / max loss, breakevens,
  * deal-level greeks,
  * per-leg payoff curves + current "close cash" (signed MTM realised on exit),
    so the client can recompute any what-if subset (close legs to improve the
    profile at ~zero cost) locally without a round trip.

Pricing reuses the Pricing-tab term-structure IV via
``portfolio.build_market_context`` / ``_iv_from_surface`` so MTM stays
consistent across the app.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
import numpy as np

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.web.routes.portfolio import (
    DEFAULT_IV,
    bs_greeks,
    build_market_context,
    _bs_vec,
    _iso_to_date,
    _iv_from_surface,
    _safe_float,
)

router = APIRouter()

# Number of points in the payoff / probability spot grid.
GRID_POINTS = 161


# ---------------------------------------------------------------------------
# Strategy classification
# ---------------------------------------------------------------------------

def _classify(legs: list[dict]) -> tuple[str, str]:
    """Classify a set of legs into (short_label, description).

    Each leg dict needs ``opt`` ("C"/"P"), ``sign`` (+1 long / -1 short) and
    ``strike``.
    """
    n = len(legs)
    calls = [l for l in legs if l["opt"] == "C"]
    puts = [l for l in legs if l["opt"] == "P"]
    longs = [l for l in legs if l["sign"] > 0]
    shorts = [l for l in legs if l["sign"] < 0]

    if n == 1:
        l = legs[0]
        side = "Long" if l["sign"] > 0 else "Short"
        kind = "Call" if l["opt"] == "C" else "Put"
        return f"{side} {kind}", f"Single {side.lower()} {kind.lower()} @ {_fmt_k(l['strike'])}"

    if n == 2:
        a, b = sorted(legs, key=lambda x: x["strike"])
        # Vertical spreads (same option type, one long one short)
        if len(calls) == 2 and len(longs) == 1:
            bull = a["sign"] > 0  # long the lower strike => bullish
            label = "Bull Call Spread" if bull else "Bear Call Spread"
            return label, f"Call spread {_fmt_k(a['strike'])}/{_fmt_k(b['strike'])}"
        if len(puts) == 2 and len(longs) == 1:
            bull = b["sign"] > 0  # long the higher strike => bullish (debit) ... else bearish
            label = "Bull Put Spread" if a["sign"] < 0 else "Bear Put Spread"
            return label, f"Put spread {_fmt_k(a['strike'])}/{_fmt_k(b['strike'])}"
        # Straddle / strangle (one call one put)
        if len(calls) == 1 and len(puts) == 1:
            same_k = abs(calls[0]["strike"] - puts[0]["strike"]) < 1e-9
            if len(longs) == 2:
                return ("Long Straddle" if same_k else "Long Strangle",
                        "Long call + long put")
            if len(shorts) == 2:
                return ("Short Straddle" if same_k else "Short Strangle",
                        "Short call + short put")
            # Mixed: risk reversal / collar
            return "Risk Reversal / Collar", "Long one side, short the other"
        return "2-Leg Structure", "Two-leg combination"

    if n == 4 and len(calls) == 2 and len(puts) == 2:
        # Iron condor / butterfly: short the inner strikes, long the outer wings
        short_strikes = sorted(l["strike"] for l in shorts)
        long_strikes = sorted(l["strike"] for l in longs)
        if len(shorts) == 2 and len(longs) == 2:
            if abs(short_strikes[0] - short_strikes[1]) < 1e-9:
                return "Iron Butterfly", "Short straddle + protective wings"
            inner = (min(long_strikes) <= min(short_strikes)
                     and max(long_strikes) >= max(short_strikes))
            if inner:
                return "Iron Condor", "Short strangle + protective wings"
        return "4-Leg Structure", "Four-leg combination"

    nc, npu = len(calls), len(puts)
    return f"Custom ({n}-leg)", f"{nc} call leg(s), {npu} put leg(s)"


def _fmt_k(k: float) -> str:
    if k >= 100:
        return f"{int(round(k))}"
    return f"{k:g}"


# ---------------------------------------------------------------------------
# Probability model (risk-neutral log-normal terminal spot, r = 0)
# ---------------------------------------------------------------------------

def _lognormal_mass(grid: np.ndarray, s0: float, sigma: float, t: float):
    """Return (prob_mass, prob_density) over ``grid`` for terminal spot.

    Risk-neutral with r = 0: ln S_T ~ N(ln s0 - 0.5 sigma^2 t, (sigma sqrt(t))^2).
    ``prob_mass`` sums to ~1 (cell probabilities via CDF differences at the
    midpoints between grid points). ``prob_density`` is the pdf for plotting.
    """
    from scipy.stats import norm

    if t <= 0 or sigma <= 0 or s0 <= 0:
        return None, None
    sd = sigma * np.sqrt(t)
    mu = np.log(s0) - 0.5 * sigma ** 2 * t

    def cdf(x: np.ndarray) -> np.ndarray:
        x = np.maximum(x, 1e-9)
        return norm.cdf((np.log(x) - mu) / sd)

    # Cell edges = midpoints between grid points, extended at both ends.
    mids = (grid[:-1] + grid[1:]) / 2.0
    lo = np.concatenate([[max(grid[0] - (mids[0] - grid[0]), 1e-9)], mids])
    hi = np.concatenate([mids, [grid[-1] + (grid[-1] - mids[-1])]])
    lo[0] = 1e-9          # everything below the grid collapses into the first cell
    hi[-1] = grid[-1] * 10  # everything above collapses into the last cell
    mass = cdf(hi) - cdf(lo)

    # Lognormal pdf for display.
    g = np.maximum(grid, 1e-9)
    density = np.exp(-((np.log(g) - mu) ** 2) / (2 * sd ** 2)) / (g * sd * np.sqrt(2 * np.pi))
    return mass, density


def _breakevens(grid: np.ndarray, payoff: np.ndarray) -> list[float]:
    """Spot levels where the payoff curve crosses zero (linear interpolation)."""
    out: list[float] = []
    for i in range(len(grid) - 1):
        y0, y1 = payoff[i], payoff[i + 1]
        # Only count genuine sign crossings — a payoff that sits flat at zero
        # over a whole region (e.g. an unfunded wing) is not a breakeven.
        if y0 * y1 < 0:
            x0, x1 = grid[i], grid[i + 1]
            out.append(float(x0 + (x1 - x0) * (-y0) / (y1 - y0)))
    # De-duplicate near-identical crossings.
    deduped: list[float] = []
    for b in sorted(out):
        if not deduped or abs(b - deduped[-1]) > 1e-6:
            deduped.append(b)
    return deduped


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
async def list_deals(asset: str = "ETH", include_expired: bool = False):
    """Return all deals (grouped multi-leg structures) with risk analytics."""
    asset = asset.upper()

    # 1. Load active legs from the DB.
    try:
        db = await get_db()
        rows = await list_trades(db, include_expired=include_expired,
                                 include_deleted=False, asset=asset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read trades: {e}")

    today = date.today()

    # 2. Market context (spot + term-structure vol surface).
    ctx = await build_market_context(asset)
    spot = ctx["spot"] or 0.0
    smiles = ctx["smiles"]
    deribit_dates = ctx["deribit_dates"]

    # 3. Normalise legs and bucket them into deals by (counterparty, trade_date).
    groups: dict[tuple[str, str], list[dict]] = {}
    all_strikes: list[float] = []
    for r in rows:
        strike = _safe_float(r.get("strike"))
        qty = _safe_float(r.get("qty"))
        if strike <= 0 or qty <= 0:
            continue
        opt = "C" if "c" in str(r.get("option_type") or "").strip().lower()[:1] else "P"
        side = str(r.get("side") or "").strip().lower()
        sign = 1.0 if side in ("buy", "long") else -1.0
        expiry_raw = str(r.get("expiry") or "").strip()
        expiry_date = _iso_to_date(expiry_raw)
        days_rem = max((expiry_date - today).days, 0) if expiry_date else 0
        premium_per = _safe_float(r.get("premium_per"))
        # Entry premium cashflow (USD), already signed: + received, - paid.
        # Prefer premium_usd (always populated); fall back to per-contract * qty.
        premium_usd = _safe_float(r.get("premium_usd"))
        if premium_usd == 0 and premium_per != 0:
            premium_usd = -sign * qty * abs(premium_per)

        leg = {
            "id": r.get("id"),
            "counterparty": str(r.get("counterparty") or "").strip(),
            "trade_date": str(r.get("trade_date") or "").strip().split("T")[0],
            "opt": opt,
            "side": "Long" if sign > 0 else "Short",
            "sign": sign,
            "qty": qty,
            "signed_qty": sign * qty,
            "strike": strike,
            "expiry": expiry_raw.split("T")[0],
            "expiry_date": expiry_date,
            "days_rem": days_rem,
            "premium_per": premium_per,
            "premium_usd": premium_usd,
        }
        key = (leg["counterparty"], leg["trade_date"])
        groups.setdefault(key, []).append(leg)
        all_strikes.append(strike)

    if not groups:
        return {"asset": asset, "spot": spot, "deals": []}

    # 4. Build a shared spot grid spanning the strike range and current spot.
    lo_ref = min(all_strikes + ([spot] if spot > 0 else []))
    hi_ref = max(all_strikes + ([spot] if spot > 0 else []))
    grid_lo = max(lo_ref * 0.4, 1e-6)
    grid_hi = hi_ref * 1.6
    grid = np.linspace(grid_lo, grid_hi, GRID_POINTS)

    deals = []
    for (cpty, tdate), legs in groups.items():
        deals.append(_build_deal(cpty, tdate, legs, grid, spot, smiles,
                                 deribit_dates, today))

    # Sort by soonest expiry, then counterparty.
    deals.sort(key=lambda d: (d["expiry"] or "9999", d["counterparty"]))
    return {
        "asset": asset,
        "spot": spot,
        "grid": [round(float(x), 6) for x in grid],
        "deals": deals,
    }


def _build_deal(cpty, tdate, legs, grid, spot, smiles, deribit_dates, today):
    """Price one deal and assemble its analytics payload."""
    # Horizon = latest expiry among the legs (classic payoff-at-expiry diagram).
    horizon_days = max((l["days_rem"] for l in legs), default=0)
    horizon_date = max((l["expiry"] for l in legs if l["expiry"]), default="")
    expiries = sorted({l["expiry"] for l in legs if l["expiry"]})

    leg_payloads = []
    total_payoff = np.zeros_like(grid)
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    net_credit = 0.0       # entry cash: + received, - paid
    deal_mtm = 0.0         # current signed mark-to-market value (USD)

    for leg in legs:
        opt, sign, qty = leg["opt"], leg["sign"], leg["qty"]
        K = leg["strike"]
        signed_qty = leg["signed_qty"]
        prem_usd = leg["premium_usd"]  # signed entry cashflow: + received, - paid

        # Payoff at expiry for this leg, including the entry premium (true P&L).
        #   long  call: +qty*max(S-K,0) - premium_paid
        #   short call: -qty*max(S-K,0) + premium_received
        # signed_qty carries the long/short sign; prem_usd carries the cashflow.
        intrinsic = (np.maximum(grid - K, 0.0) if opt == "C"
                     else np.maximum(K - grid, 0.0))
        leg_payoff = signed_qty * intrinsic + prem_usd
        total_payoff = total_payoff + leg_payoff

        net_credit += prem_usd

        # IV for this leg from the term-structure surface (Pricing-tab method).
        iv = _iv_from_surface(leg["expiry"], K, smiles, deribit_dates, today)
        sigma = (iv / 100.0) if iv is not None else DEFAULT_IV
        t_leg = max(leg["days_rem"], 0) / 365.25

        # Current value per contract (USD) and close cash (signed MTM on exit).
        if t_leg > 0 and spot > 0:
            cur_val = float(_bs_vec(np.array([spot]), K, t_leg, 0.0, sigma, opt)[0])
            d, g, th, v = bs_greeks(spot, K, t_leg, 0.0, sigma, opt)
        else:
            cur_val = (max(spot - K, 0.0) if opt == "C" else max(K - spot, 0.0))
            d = g = th = v = 0.0
        d = d or 0.0; g = g or 0.0; th = th or 0.0; v = v or 0.0

        close_cash = signed_qty * cur_val  # cash realised when closing the leg
        deal_mtm += close_cash
        greeks["delta"] += signed_qty * d
        greeks["gamma"] += signed_qty * g
        greeks["theta"] += signed_qty * th
        greeks["vega"] += signed_qty * v

        leg_payloads.append({
            "id": leg["id"],
            "label": f"{leg['side']} {qty:g} {opt} {_fmt_k(K)} ({leg['expiry']})",
            "side": leg["side"],
            "opt": opt,
            "qty": qty,
            "strike": K,
            "expiry": leg["expiry"],
            "premium_per": round(prem_usd / qty, 4) if qty else 0.0,
            "premium_usd": round(prem_usd, 2),
            "iv_pct": round(sigma * 100, 1),
            "close_cash": round(close_cash, 2),
            "mtm": round(close_cash, 2),
            "payoff": [round(float(x), 2) for x in leg_payoff],
        })

    label, desc = _classify(legs)

    # Probability model at the deal horizon, using ATM IV at the spot.
    atm_iv = _iv_from_surface(horizon_date, spot, smiles, deribit_dates, today) if horizon_date else None
    sigma_atm = (atm_iv / 100.0) if atm_iv is not None else DEFAULT_IV
    t_h = max(horizon_days, 0) / 365.25
    mass, density = _lognormal_mass(grid, spot, sigma_atm, t_h)

    prob_profit = None
    expected_pnl = None
    if mass is not None:
        prob_profit = float(np.sum(mass[total_payoff > 0]))
        expected_pnl = float(np.sum(total_payoff * mass))

    return {
        "id": f"{cpty}|{tdate}",
        "counterparty": cpty,
        "trade_date": tdate,
        "strategy": label,
        "strategy_desc": desc,
        "expiry": horizon_date,
        "expiries": expiries,
        "days_to_expiry": horizon_days,
        "n_legs": len(legs),
        "net_credit": round(net_credit, 2),
        "mtm": round(deal_mtm, 2),
        "atm_iv_pct": round(sigma_atm * 100, 1),
        "greeks": {k: round(v, 4) for k, v in greeks.items()},
        "payoff": [round(float(x), 2) for x in total_payoff],
        "prob_mass": [float(x) for x in mass] if mass is not None else None,
        "prob_density": [float(x) for x in density] if density is not None else None,
        "prob_profit": prob_profit,
        "expected_pnl": expected_pnl,
        "max_profit": round(float(np.max(total_payoff)), 2),
        "max_loss": round(float(np.min(total_payoff)), 2),
        "breakevens": [round(b, 2) for b in _breakevens(grid, total_payoff)],
        "legs": leg_payloads,
    }
