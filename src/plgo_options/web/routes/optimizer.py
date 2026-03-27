"""Portfolio optimizer API — interprets user requests and generates trade suggestions."""

from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
import numpy as np
from scipy.stats import norm

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.pricing.options import bs_price
from plgo_options.pricing.vol_surface import VolSmile
from plgo_options.config import DERIBIT_BASE_URL, REQUEST_TIMEOUT, ANTHROPIC_API_KEY, ANTHROPIC_MODEL

router = APIRouter()
client = DeribitClient()

MIN_DTE = 7
SPOT_STEP = 50
DEFAULT_IV = 0.80  # 80% fallback


class OptimizeRequest(BaseModel):
    query: str = ""
    budget: float = 15000
    base_qty: float = 0
    min_dte: int = 7
    max_spread_pct: float = 40
    target_expiry: str | None = None
    target_payoff: list[dict] | None = None  # drawn target: [{x: spot, y: payoff}, ...]


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role: "user"|"bot", text: "..."}]
    workbench_legs: list[dict] = []  # legs currently in the workbench
    added_trades: list[dict] = []  # trades already added to portfolio from optimizer
    closed_trade_ids: list[int] = []  # trades "closed" in the optimizer working portfolio (for rolls)
    target_payoff: list[dict] | None = None  # drawn target: [{x: spot, y: payoff}, ...]


class CalculateRequest(BaseModel):
    legs: list[dict]
    closed_trade_ids: list[int] = []  # trades "closed" via rolls — exclude from base payoff


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _payoff_vec(spot_arr, strike, opt, qty):
    """Intrinsic payoff at expiry — kept as fallback."""
    if opt == "C":
        return qty * np.maximum(spot_arr - strike, 0.0)
    return qty * np.maximum(strike - spot_arr, 0.0)


def _bs_vec(spots, K, T, r, sigma, opt):
    """Vectorised Black-Scholes across an array of spot prices."""
    if T <= 0:
        return np.maximum(spots - K, 0.0) if opt == "C" else np.maximum(K - spots, 0.0)
    d1 = (np.log(spots / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "C":
        return spots * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - spots * norm.cdf(-d1)


def _bs_payoff_vec(spot_arr, strike, opt, qty, T, sigma):
    """BS-aware payoff for a position across spot ladder."""
    if sigma is None or sigma <= 0 or T is None:
        return _payoff_vec(spot_arr, strike, opt, qty)
    return qty * _bs_vec(spot_arr, strike, T, 0.0, sigma, opt)


async def _fetch_smiles() -> dict[str, VolSmile]:
    """Fetch all ETH option IVs from Deribit and build vol smiles per expiry."""
    summaries = await client._get("get_book_summary_by_currency", {
        "currency": "ETH", "kind": "option",
    })
    expiry_data: dict[str, dict[float, list[float]]] = {}
    for s in summaries:
        name = s.get("instrument_name", "")
        mark_iv = s.get("mark_iv")
        if not name or mark_iv is None or mark_iv <= 0:
            continue
        parts = name.split("-")
        if len(parts) < 4:
            continue
        expiry_data.setdefault(parts[1], {}).setdefault(float(parts[2]), []).append(mark_iv)

    smiles: dict[str, VolSmile] = {}
    for exp, strike_ivs in expiry_data.items():
        strikes = sorted(strike_ivs.keys())
        ivs = [float(np.mean(strike_ivs[k])) for k in strikes]
        if len(strikes) >= 2:
            smiles[exp] = VolSmile(strikes, ivs)
    return smiles


def _match_expiry_to_smile(expiry_iso: str, smiles: dict[str, VolSmile]) -> str | None:
    """Match an ISO date expiry to a Deribit expiry code in the smiles dict."""
    try:
        if "T" in expiry_iso:
            pos_date = datetime.fromisoformat(expiry_iso).date()
        else:
            pos_date = datetime.strptime(expiry_iso[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    best, best_diff = None, 999
    for exp_code in smiles:
        try:
            ddate = datetime.strptime(exp_code, "%d%b%y").date()
        except ValueError:
            continue
        diff = abs((ddate - pos_date).days)
        if diff < best_diff:
            best_diff, best = diff, exp_code
    return best if best_diff <= 7 else None


def _get_sigma(strike, expiry_str, smiles, expiry_code=None):
    """Get IV (as decimal) for a position from the vol surface."""
    matched = expiry_code
    if not matched:
        matched = _match_expiry_to_smile(expiry_str, smiles)
    if matched and matched in smiles:
        return smiles[matched].iv_at(strike) / 100.0
    return DEFAULT_IV


def _get_dte(expiry_str):
    """Get days to expiry from an expiry string."""
    today = date.today()
    try:
        if "T" in expiry_str:
            exp_date = datetime.fromisoformat(expiry_str).date()
        else:
            exp_date = datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
        return max((exp_date - today).days, 0)
    except (ValueError, TypeError):
        return 0


def _time_to_expiry_years(expiry_code: str) -> float:
    """Exact T in years from now to Deribit expiry (08:00 UTC).
    Same formula as pricing.py for consistent pricing.
    """
    try:
        dt = datetime.strptime(expiry_code, "%d%b%y")
        expiry_dt = dt.replace(hour=8, minute=0, second=0, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = expiry_dt - now
        return max(delta.total_seconds() / (365.25 * 86400), 1e-6)
    except (ValueError, TypeError):
        return 0.0


def _T_from_iso(expiry_str: str) -> float:
    """Exact T in years from an ISO date expiry string."""
    try:
        if "T" in expiry_str:
            exp_date = datetime.fromisoformat(expiry_str).date()
        else:
            exp_date = datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
        expiry_dt = datetime(exp_date.year, exp_date.month, exp_date.day,
                             8, 0, 0, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = expiry_dt - now
        return max(delta.total_seconds() / (365.25 * 86400), 1e-6)
    except (ValueError, TypeError):
        return 0.0


def _find_breakeven(spot_arr, payoff):
    for i in range(len(spot_arr) - 1):
        if payoff[i] <= 0 and payoff[i + 1] > 0:
            frac = -payoff[i] / (payoff[i + 1] - payoff[i])
            return float(spot_arr[i] + frac * SPOT_STEP)
    return None


# ---------------------------------------------------------------------------
# Parse user query into objective parameters
# ---------------------------------------------------------------------------

def _parse_query(query: str, spot: float) -> dict:
    """Parse natural language query into optimization parameters."""
    q = query.lower().strip()
    result = {
        "objective": "balanced",
        "focus_zone_lo": None,
        "focus_zone_hi": None,
        "description": "",
    }

    # Extract price ranges like "from 0 to 2000", "below 1800", "above 3500"
    range_match = re.search(r'(?:from|between)\s*\$?(\d+)\s*(?:to|and|-)\s*\$?(\d+)', q)
    below_match = re.search(r'below\s*\$?(\d+)', q)
    above_match = re.search(r'above\s*\$?(\d+)', q)

    if range_match:
        result["focus_zone_lo"] = float(range_match.group(1))
        result["focus_zone_hi"] = float(range_match.group(2))
    elif below_match:
        result["focus_zone_lo"] = 0
        result["focus_zone_hi"] = float(below_match.group(1))
    elif above_match:
        result["focus_zone_lo"] = float(above_match.group(1))
        result["focus_zone_hi"] = spot * 3

    # Determine objective from keywords
    if any(w in q for w in ["downside", "protect", "crash", "floor", "worst"]):
        result["objective"] = "protect_downside"
        result["description"] = "Protecting downside / raising floor"
        if not result["focus_zone_lo"]:
            result["focus_zone_lo"] = 0
            result["focus_zone_hi"] = spot * 0.95
    elif any(w in q for w in ["upside", "participation", "more upside"]):
        result["objective"] = "increase_upside"
        result["description"] = "Adding upside participation"
        if not result["focus_zone_lo"]:
            result["focus_zone_lo"] = spot * 1.05
            result["focus_zone_hi"] = spot * 3
    elif any(w in q for w in ["lock", "gain", "secure"]):
        if "upside" in q or "protect" in q:
            result["objective"] = "lock_gains"
            result["description"] = "Locking gains + protecting upside"
            result["focus_zone_lo"] = spot * 0.85
            result["focus_zone_hi"] = spot * 1.5
        else:
            result["objective"] = "lock_gains"
            result["description"] = "Locking gains / protecting against reversal"
    elif any(w in q for w in ["breakeven", "break even", "break-even"]):
        result["objective"] = "lower_breakeven"
        result["description"] = "Lowering the breakeven point"
    elif any(w in q for w in ["improve", "better"]):
        result["objective"] = "improve_zone"
        if result["focus_zone_lo"] is not None:
            result["description"] = f"Improving payoff between ${int(result['focus_zone_lo']):,} and ${int(result['focus_zone_hi']):,}"
        else:
            result["objective"] = "balanced"
            result["description"] = "Balanced improvement"
    else:
        result["description"] = "Balanced improvement"

    return result


# ---------------------------------------------------------------------------
# Core endpoint
# ---------------------------------------------------------------------------

@router.post("/suggest")
async def suggest_trades(req: OptimizeRequest):
    """Parse user request, fetch market data, generate suggestions."""

    # 1. Spot
    try:
        spot = await client.get_eth_spot_price()
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch spot: {e}")

    # 2. Parse query
    parsed = _parse_query(req.query, spot)
    objective = parsed["objective"]
    zone_lo = parsed.get("focus_zone_lo")
    zone_hi = parsed.get("focus_zone_hi")

    # 3. Load positions
    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=False, include_deleted=False, asset="ETH")
    except Exception as e:
        raise HTTPException(500, f"Failed to read trades: {e}")

    if not db_trades:
        raise HTTPException(404, "No active ETH trades found")

    # Fetch vol surface for BS pricing
    try:
        smiles = await _fetch_smiles()
    except Exception:
        smiles = {}

    positions = []
    positions_display = []
    for t in db_trades:
        side = str(t["side"]).lower()
        opt_raw = str(t["option_type"]).lower()
        opt = "C" if "call" in opt_raw else "P"
        sign = 1.0 if side in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        expiry_str = str(t.get("expiry", ""))
        if strike <= 0 or qty <= 0:
            continue
        dte = _get_dte(expiry_str)
        sigma = _get_sigma(strike, expiry_str, smiles)
        positions.append({"opt": opt, "strike": strike, "net_qty": sign * qty,
                          "expiry": expiry_str, "dte": dte, "sigma": sigma})
        positions_display.append({
            "id": t["id"], "counterparty": t.get("counterparty", ""),
            "side": t["side"], "opt": opt, "strike": strike,
            "expiry": t["expiry"], "net_qty": sign * qty, "qty": qty,
            "premium_usd": _safe_float(t.get("premium_usd")),
        })

    # 4. Spot ladder & current payoff (BS-aware with vol surface)
    lo = max(500, int(spot * 0.2))
    hi = int(spot * 3.5)
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for p in positions:
        T = max(p["dte"], 0) / 365.25
        current_payoff += _bs_payoff_vec(spot_arr, p["strike"], p["opt"], p["net_qty"], T, p["sigma"])

    breakeven = _find_breakeven(spot_arr, current_payoff)
    spot_idx = int(np.argmin(np.abs(spot_arr - spot)))
    current_profile = {
        "at_spot": float(current_payoff[spot_idx]),
        "min": float(current_payoff.min()),
        "min_at": float(spot_arr[np.argmin(current_payoff)]),
        "max": float(current_payoff.max()),
        "max_at": float(spot_arr[np.argmax(current_payoff)]),
        "breakeven": breakeven,
    }

    # 5. Fetch instruments
    try:
        summaries = await client._get("get_book_summary_by_currency", {
            "currency": "ETH", "kind": "option",
        })
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    today = date.today()
    instruments = []
    for s in summaries:
        name = s.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "ETH":
            continue
        try:
            exp_date = datetime.strptime(parts[1], "%d%b%y").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < req.min_dte:
            continue
        strike = float(parts[2])
        opt = parts[3]
        bid = s.get("bid_price")
        ask = s.get("ask_price")
        mark = s.get("mark_price")
        if not bid or bid <= 0 or not ask or ask <= 0:
            continue
        spread = (ask - bid) / ask * 100
        if spread > req.max_spread_pct:
            continue
        if strike < spot * 0.3 or strike > spot * 3.5:
            continue
        if req.target_expiry and parts[1] != req.target_expiry:
            continue
        instruments.append({
            "name": name, "expiry_code": parts[1], "expiry_date": exp_date.isoformat(),
            "dte": dte, "strike": strike, "opt": opt,
            "bid": bid, "ask": ask, "mark": mark, "mid": (bid + ask) / 2,
            "spread_pct": round(spread, 1), "mark_iv": s.get("mark_iv"),
        })

    if not instruments:
        raise HTTPException(404, "No liquid instruments found")

    base_qty = req.base_qty
    if base_qty <= 0:
        avg_qty = np.mean([abs(p["net_qty"]) for p in positions])
        base_qty = max(100, min(round(avg_qty / 5 / 100) * 100, 2000))

    # 6. Generate suggestions
    budget = req.budget
    by_expiry: dict[str, list[dict]] = {}
    for inst in instruments:
        by_expiry.setdefault(inst["expiry_code"], []).append(inst)

    suggestions = []
    for exp_code, insts in by_expiry.items():
        calls = sorted([i for i in insts if i["opt"] == "C"], key=lambda x: x["strike"])
        puts = sorted([i for i in insts if i["opt"] == "P"], key=lambda x: x["strike"])
        dte = insts[0]["dte"]

        def try_add(name, cat, legs, cost):
            if abs(cost) > budget:
                return
            suggestions.append(_score_suggestion(
                name, cat, legs, cost, dte, spot, spot_arr,
                current_payoff, spot_idx, breakeven, objective, zone_lo, zone_hi,
                target_payoff=req.target_payoff))

        # Bull Call Spreads
        for i, bc in enumerate(calls):
            if bc["strike"] < spot * 0.8 or bc["strike"] > spot * 1.5:
                continue
            for sc in calls[i + 1:]:
                if sc["strike"] - bc["strike"] < 200 or sc["strike"] > spot * 3:
                    continue
                cost = (bc["ask"] - sc["bid"]) * base_qty * spot
                try_add(f"Bull Call {exp_code}: +{int(bc['strike'])}C / -{int(sc['strike'])}C",
                        "bull_call_spread",
                        [{"inst": bc, "side": "buy", "qty": base_qty},
                         {"inst": sc, "side": "sell", "qty": base_qty}], cost)

        # Bear Put Spreads
        for i, sp in enumerate(puts):
            if sp["strike"] < spot * 0.3:
                continue
            for bp in puts[i + 1:]:
                if bp["strike"] - sp["strike"] < 200 or bp["strike"] > spot * 1.5:
                    continue
                cost = (bp["ask"] - sp["bid"]) * base_qty * spot
                try_add(f"Bear Put {exp_code}: +{int(bp['strike'])}P / -{int(sp['strike'])}P",
                        "bear_put_spread",
                        [{"inst": bp, "side": "buy", "qty": base_qty},
                         {"inst": sp, "side": "sell", "qty": base_qty}], cost)

        # Risk Reversals (bullish)
        for sp in puts:
            if sp["strike"] > spot * 0.90:
                continue
            for bc in calls:
                if bc["strike"] < spot * 1.10 or bc["strike"] > spot * 2.5:
                    continue
                cost = (bc["ask"] - sp["bid"]) * base_qty * spot
                try_add(f"Risk Rev {exp_code}: +{int(bc['strike'])}C / -{int(sp['strike'])}P",
                        "risk_reversal",
                        [{"inst": bc, "side": "buy", "qty": base_qty},
                         {"inst": sp, "side": "sell", "qty": base_qty}], cost)

        # Sell call buy put (downside protection)
        for sc in calls:
            if sc["strike"] < spot * 1.8:
                continue
            for bp in puts:
                if bp["strike"] > spot * 1.05 or bp["strike"] < spot * 0.5:
                    continue
                cost = (bp["ask"] - sc["bid"]) * base_qty * spot
                try_add(f"Put Protect {exp_code}: +{int(bp['strike'])}P / -{int(sc['strike'])}C",
                        "put_protection",
                        [{"inst": bp, "side": "buy", "qty": base_qty},
                         {"inst": sc, "side": "sell", "qty": base_qty}], cost)

        # Collar
        for sc in calls:
            if sc["strike"] < spot * 1.5:
                continue
            for bcw in calls:
                if bcw["strike"] <= sc["strike"] or bcw["strike"] > sc["strike"] * 1.5:
                    continue
                credit = sc["bid"] - bcw["ask"]
                if credit <= 0:
                    continue
                for bp in puts:
                    if bp["strike"] > spot * 1.1 or bp["strike"] < spot * 0.5:
                        continue
                    cost = (bp["ask"] - credit) * base_qty * spot
                    try_add(f"Collar {exp_code}: +{int(bp['strike'])}P / -{int(sc['strike'])}C / +{int(bcw['strike'])}C",
                            "collar",
                            [{"inst": bp, "side": "buy", "qty": base_qty},
                             {"inst": sc, "side": "sell", "qty": base_qty},
                             {"inst": bcw, "side": "buy", "qty": base_qty}], cost)

        # Put ratio spread
        for bp in puts:
            if bp["strike"] < spot * 0.85 or bp["strike"] > spot * 1.1:
                continue
            for sp in puts:
                if sp["strike"] >= bp["strike"] - 200 or sp["strike"] < spot * 0.3:
                    continue
                cost = (bp["ask"] - 2 * sp["bid"]) * base_qty * spot
                try_add(f"Put Ratio {exp_code}: +{int(bp['strike'])}P / -2x{int(sp['strike'])}P",
                        "put_ratio",
                        [{"inst": bp, "side": "buy", "qty": base_qty},
                         {"inst": sp, "side": "sell", "qty": base_qty * 2}], cost)

        # Bear reversal
        for sc in calls:
            if sc["strike"] < spot * 1.10:
                continue
            for bp in puts:
                if bp["strike"] > spot * 0.90:
                    continue
                cost = (bp["ask"] - sc["bid"]) * base_qty * spot
                try_add(f"Bear Rev {exp_code}: +{int(bp['strike'])}P / -{int(sc['strike'])}C",
                        "bear_reversal",
                        [{"inst": bp, "side": "buy", "qty": base_qty},
                         {"inst": sc, "side": "sell", "qty": base_qty}], cost)

    suggestions.sort(key=lambda s: s["score"], reverse=True)

    available_expiries = sorted(
        set(i["expiry_code"] for i in instruments),
        key=lambda x: datetime.strptime(x, "%d%b%y"),
    )

    return {
        "spot": spot,
        "spot_ladder": spot_arr.tolist(),
        "current_payoff": np.round(current_payoff, 2).tolist(),
        "current_profile": current_profile,
        "positions": positions_display,
        "base_qty": base_qty,
        "budget": budget,
        "objective": objective,
        "parsed_query": parsed,
        "num_positions": len(positions),
        "num_instruments": len(instruments),
        "num_suggestions": len(suggestions),
        "available_expiries": available_expiries,
        "suggestions": suggestions[:60],
    }


def _score_suggestion(name, category, legs, net_cost, dte, spot, spot_arr,
                      current_payoff, spot_idx, current_be, objective, zone_lo, zone_hi,
                      target_payoff=None):
    """Build and score a suggestion using BS pricing with market IV."""
    candidate = np.zeros_like(spot_arr)
    T = max(dte, 0) / 365.25
    for leg in legs:
        inst = leg["inst"]
        sign = 1.0 if leg["side"] == "buy" else -1.0
        # Use mark IV from Deribit for this instrument
        mark_iv = inst.get("mark_iv")
        sigma = mark_iv / 100.0 if mark_iv and mark_iv > 0 else DEFAULT_IV
        candidate += _bs_payoff_vec(spot_arr, inst["strike"], inst["opt"], sign * leg["qty"], T, sigma)

    new_payoff = current_payoff + candidate
    diff = new_payoff - current_payoff

    at_spot_imp = float(diff[spot_idx])
    min_imp = float(new_payoff.min() - current_payoff.min())

    new_be = _find_breakeven(spot_arr, new_payoff)
    be_imp = (current_be - new_be) if (current_be and new_be) else 0.0

    # Zone-weighted improvements
    sigma_w = 0.60
    log_spots = np.log(spot_arr / spot)
    weights = np.exp(-0.5 * (log_spots / sigma_w) ** 2)
    weights /= weights.sum()

    down_mask = spot_arr < spot * 0.95
    up_mask = spot_arr > spot * 1.05
    down_imp = float(np.sum(diff[down_mask] * weights[down_mask]) / weights[down_mask].sum()) if down_mask.any() else 0.0
    up_imp = float(np.sum(diff[up_mask] * weights[up_mask]) / weights[up_mask].sum()) if up_mask.any() else 0.0

    # Focus zone improvement
    zone_imp = 0.0
    if zone_lo is not None and zone_hi is not None:
        zmask = (spot_arr >= zone_lo) & (spot_arr <= zone_hi)
        if zmask.any():
            zw = weights[zmask] / weights[zmask].sum() if weights[zmask].sum() > 0 else np.ones(zmask.sum()) / zmask.sum()
            zone_imp = float(np.sum(diff[zmask] * zw))

    # Score
    if objective == "improve_zone" and zone_lo is not None:
        score = 0.50 * zone_imp + 0.20 * min_imp + 0.15 * at_spot_imp + 0.10 * be_imp + 0.05 * down_imp
    elif objective == "protect_downside":
        score = 0.35 * down_imp + 0.25 * min_imp + 0.20 * at_spot_imp + 0.10 * be_imp + 0.10 * zone_imp
    elif objective == "increase_upside":
        score = 0.50 * up_imp + 0.20 * at_spot_imp + 0.15 * zone_imp + 0.10 * min_imp + 0.05 * be_imp
    elif objective == "lock_gains":
        score = 0.30 * down_imp + 0.25 * min_imp + 0.20 * be_imp + 0.15 * at_spot_imp + 0.10 * zone_imp
    elif objective == "lower_breakeven":
        score = 0.40 * be_imp + 0.25 * at_spot_imp + 0.15 * down_imp + 0.10 * min_imp + 0.10 * up_imp
    else:
        score = 0.25 * at_spot_imp + 0.20 * down_imp + 0.20 * min_imp + 0.15 * be_imp + 0.10 * up_imp + 0.10 * zone_imp

    # Target-matching: when user drew a target profile, score how close we get
    target_match_pct = None
    if target_payoff and len(target_payoff) >= 2:
        # Interpolate target to spot_arr using numpy
        tgt_x = np.array([p["x"] for p in target_payoff], dtype=float)
        tgt_y = np.array([p["y"] for p in target_payoff], dtype=float)
        # Interpolate target values at each spot_arr point (clamp outside range)
        target_at_spots = np.interp(spot_arr, tgt_x, tgt_y)
        # Only score where target is defined (between min and max target x)
        mask = (spot_arr >= tgt_x.min()) & (spot_arr <= tgt_x.max())
        if mask.any():
            # Distance: how far current payoff is from target vs how far new payoff is
            curr_dist = np.abs(current_payoff[mask] - target_at_spots[mask])
            new_dist = np.abs(new_payoff[mask] - target_at_spots[mask])
            # Improvement = reduction in distance (positive = closer to target)
            improvement = float(np.mean(curr_dist) - np.mean(new_dist))
            # Match percentage: how close new_payoff is to target (100% = perfect match)
            max_range = max(float(np.ptp(target_at_spots[mask])), 1.0)
            target_match_pct = max(0.0, 100.0 * (1.0 - float(np.mean(new_dist)) / max_range))
            # Override scoring — target match dominates
            score = improvement + 0.10 * at_spot_imp + 0.05 * min_imp

    formatted_legs = []
    for leg in legs:
        inst = leg["inst"]
        price_eth = inst["ask"] if leg["side"] == "buy" else inst["bid"]
        formatted_legs.append({
            "instrument": inst["name"], "side": leg["side"], "qty": leg["qty"],
            "strike": inst["strike"], "opt": inst["opt"],
            "expiry_code": inst["expiry_code"], "dte": inst["dte"],
            "price_eth": round(price_eth, 6), "price_usd": round(price_eth * spot, 2),
            "bid_usd": round(inst["bid"] * spot, 2), "ask_usd": round(inst["ask"] * spot, 2),
            "spread_pct": inst["spread_pct"], "mark_iv": inst.get("mark_iv"),
        })

    result = {
        "name": name, "category": category, "legs": formatted_legs,
        "net_cost_usd": round(net_cost, 2), "dte": dte,
        "score": round(score, 2),
        "impact": {
            "at_spot": round(at_spot_imp, 2), "min_improvement": round(min_imp, 2),
            "new_min": round(float(new_payoff.min()), 2),
            "downside": round(down_imp, 2), "upside": round(up_imp, 2),
            "zone": round(zone_imp, 2),
            "breakeven_improvement": round(be_imp, 2),
            "new_breakeven": round(new_be, 2) if new_be else None,
        },
        "new_payoff": np.round(new_payoff, 2).tolist(),
    }
    if target_match_pct is not None:
        result["target_match_pct"] = round(target_match_pct, 1)
    return result


@router.post("/calculate")
async def calculate_workbench(req: CalculateRequest):
    """Re-calculate payoff impact for edited workbench legs against current portfolio."""
    try:
        spot = await client.get_eth_spot_price()
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch spot: {e}")

    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=False, include_deleted=False, asset="ETH")
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    # Fetch vol surface for BS pricing
    try:
        smiles = await _fetch_smiles()
    except Exception:
        smiles = {}

    # Current payoff (BS-aware) — exclude trades "closed" via rolls
    closed_ids = set(req.closed_trade_ids)
    lo = max(500, int(spot * 0.2))
    hi = int(spot * 3.5)
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for t in db_trades:
        if t["id"] in closed_ids:
            continue  # excluded from working portfolio (rolled)
        side = str(t["side"]).lower()
        opt = "C" if "call" in str(t["option_type"]).lower() else "P"
        sign = 1.0 if side in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        expiry_str = str(t.get("expiry", ""))
        if strike > 0 and qty > 0:
            T = _T_from_iso(expiry_str)
            sigma = _get_sigma(strike, expiry_str, smiles)
            current_payoff += _bs_payoff_vec(spot_arr, strike, opt, sign * qty, T, sigma)

    # Workbench payoff (BS-aware)
    wb_payoff = np.zeros_like(spot_arr)
    total_cost = 0.0
    leg_costs = []

    # Fetch live prices for the instruments
    summaries = {}
    try:
        raw = await client._get("get_book_summary_by_currency", {"currency": "ETH", "kind": "option"})
        for s in raw:
            summaries[s.get("instrument_name", "")] = s
    except Exception:
        pass

    for leg in req.legs:
        side = leg.get("side", "buy")
        qty = float(leg.get("qty", 0))
        strike = float(leg.get("strike", 0))
        opt = leg.get("opt", "C")
        expiry_code = leg.get("expiry_code", "")
        sign = 1.0 if side == "buy" else -1.0

        # Rebuild instrument name from actual strike/expiry/opt (don't trust stale name)
        if strike > 0 and expiry_code:
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            inst_name = f"ETH-{expiry_code}-{strike_str}-{opt}"
        else:
            inst_name = leg.get("instrument", "")

        # Exact T in years (seconds precision, 08:00 UTC expiry — same as Pricer)
        T = _time_to_expiry_years(expiry_code) if expiry_code else 0.0
        dte = int(T * 365.25)

        # Use BS pricing with vol surface IV — same method as Strategy Builder / Pricer
        sigma = _get_sigma(strike, "", smiles, expiry_code=expiry_code)
        mark_iv = sigma * 100.0

        if strike > 0 and spot > 0 and T > 0:
            bs_val_usd = bs_price(spot, strike, T, 0.0, sigma, opt)
        else:
            # At/past expiry — intrinsic
            bs_val_usd = max(spot - strike, 0.0) if opt == "C" else max(strike - spot, 0.0)

        bs_val_eth = bs_val_usd / spot if spot > 0 else 0

        # Deribit bid/ask for reference only
        mkt = summaries.get(inst_name, {})
        deribit_bid = mkt.get("bid_price") or 0
        deribit_ask = mkt.get("ask_price") or 0
        # Use Deribit mark_iv if available for display, but price from BS
        if mkt.get("mark_iv") and mkt["mark_iv"] > 0:
            mark_iv = mkt["mark_iv"]

        # Use Deribit bid/ask for cost (same as Suggestions table):
        # Buy → pay the ask, Sell → receive the bid.
        # Fall back to BS price only when Deribit data is unavailable.
        bid_display = round(deribit_bid * spot, 2) if deribit_bid > 0 else round(bs_val_usd * 0.995, 2)
        ask_display = round(deribit_ask * spot, 2) if deribit_ask > 0 else round(bs_val_usd * 1.005, 2)
        spread_pct = round((deribit_ask - deribit_bid) / deribit_ask * 100, 1) if deribit_ask > 0 else 1.0

        if deribit_bid > 0 and deribit_ask > 0:
            # Use market prices: buy at ask, sell at bid
            price_usd = ask_display if side == "buy" else bid_display
        else:
            # No Deribit data — fall back to BS
            price_usd = bs_val_usd

        leg_cost = sign * price_usd * qty
        total_cost += leg_cost

        leg_costs.append({
            "instrument": inst_name, "side": side, "qty": qty,
            "strike": strike, "opt": opt,
            "bid_usd": bid_display, "ask_usd": ask_display,
            "price_usd": round(price_usd, 2),
            "leg_cost": round(leg_cost, 2),
            "spread_pct": spread_pct,
            "mark_iv": mark_iv,
        })

        if strike > 0 and qty > 0:
            wb_payoff += _bs_payoff_vec(spot_arr, strike, opt, sign * qty, T, sigma)

    new_payoff = current_payoff + wb_payoff
    breakeven = _find_breakeven(spot_arr, current_payoff)
    new_be = _find_breakeven(spot_arr, new_payoff)
    spot_idx = int(np.argmin(np.abs(spot_arr - spot)))

    return {
        "spot": spot,
        "total_cost": round(total_cost, 2),
        "leg_costs": leg_costs,
        "spot_ladder": spot_arr.tolist(),
        "current_payoff": np.round(current_payoff, 2).tolist(),
        "new_payoff": np.round(new_payoff, 2).tolist(),
        "pnl_at_spot": round(float(new_payoff[spot_idx]), 2),
        "new_min": round(float(new_payoff.min()), 2),
        "new_breakeven": round(new_be, 2) if new_be else None,
        "floor_change": round(float(new_payoff.min() - current_payoff.min()), 2),
        "be_change": round(breakeven - new_be, 2) if breakeven and new_be else None,
    }


# ---------------------------------------------------------------------------
# match_target: Find multi-leg strategies that best match a drawn profile
# Split into DOWNSIDE (below spot) and UPSIDE (above spot) for better matching.
# ---------------------------------------------------------------------------

async def _handle_match_target(inp: dict, target_payoff: list[dict]) -> tuple[str, dict | None]:
    """
    Decompose the gap between current portfolio and drawn target into
    tradeable option legs.  Solves TWO independent zones:
      1. Downside strategy  (spot_arr <= spot) — mostly puts
      2. Upside strategy    (spot_arr > spot)  — mostly calls
    Uses LASSO (if available) or numpy least-squares with iterative pruning.
    Returns both strategies + a combined strategy.
    """
    try:
        from sklearn.linear_model import Lasso
        HAS_SKLEARN = True
    except ImportError:
        HAS_SKLEARN = False

    if not target_payoff or len(target_payoff) < 2:
        return "No target profile provided.", None

    try:
        spot = await client.get_eth_spot_price()
    except Exception as e:
        return f"Failed to fetch spot: {e}", None

    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=False, include_deleted=False, asset="ETH")
    except Exception as e:
        return f"Failed to read trades: {e}", None

    if not db_trades:
        return "No active ETH trades found.", None

    smiles = {}
    try:
        smiles = await _fetch_smiles()
    except Exception:
        pass

    positions = []
    for t in db_trades:
        side_str = str(t["side"]).lower()
        opt_raw = str(t["option_type"]).lower()
        opt = "C" if "call" in opt_raw else "P"
        sign = 1.0 if side_str in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        expiry_str = str(t.get("expiry", ""))
        if strike <= 0 or qty <= 0:
            continue
        dte = _get_dte(expiry_str)
        sigma = _get_sigma(strike, expiry_str, smiles)
        positions.append({"opt": opt, "strike": strike, "net_qty": sign * qty,
                          "expiry": expiry_str, "dte": dte, "sigma": sigma})

    lo = max(500, int(spot * 0.2))
    hi = int(spot * 3.5)
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for p in positions:
        T = max(p["dte"], 0) / 365.25
        current_payoff += _bs_payoff_vec(spot_arr, p["strike"], p["opt"], p["net_qty"], T, p["sigma"])

    tgt_x = np.array([p["x"] for p in target_payoff], dtype=float)
    tgt_y = np.array([p["y"] for p in target_payoff], dtype=float)
    target_interp = np.interp(spot_arr, tgt_x, tgt_y)
    gap = target_interp - current_payoff

    drawn_mask = (spot_arr >= tgt_x.min()) & (spot_arr <= tgt_x.max())
    if drawn_mask.sum() < 5:
        return "Target profile range too narrow.", None

    # ── Analyze each existing position against the target ──
    # For each position: would REMOVING it bring us closer to the target?
    position_analysis = []
    current_gap_mse = float(np.mean(gap[drawn_mask] ** 2))
    for i, p in enumerate(positions):
        T = max(p["dte"], 0) / 365.25
        pos_payoff = _bs_payoff_vec(spot_arr, p["strike"], p["opt"], p["net_qty"], T, p["sigma"])
        without_payoff = current_payoff - pos_payoff
        without_gap = target_interp - without_payoff
        without_mse = float(np.mean(without_gap[drawn_mask] ** 2))
        improvement_pct = (current_gap_mse - without_mse) / max(current_gap_mse, 1.0) * 100
        side_label = "Long" if p["net_qty"] > 0 else "Short"
        opt_label = "Call" if p["opt"] == "C" else "Put"
        action = "KEEP"
        if improvement_pct > 5:
            action = "CLOSE"
        elif improvement_pct < -20:
            action = "ESSENTIAL"
        if p["dte"] < 60 and action != "CLOSE":
            action = "ROLL"
        position_analysis.append({
            "side": side_label, "opt": opt_label,
            "strike": p["strike"], "dte": p["dte"],
            "qty": abs(p["net_qty"]),
            "action": action, "impact": round(improvement_pct, 1),
            "counterparty": p.get("counterparty", ""),
        })

    # ── Fetch tradeable instruments ──
    try:
        summaries = await client._get("get_book_summary_by_currency", {
            "currency": "ETH", "kind": "option",
        })
    except Exception as e:
        return f"Deribit error: {e}", None

    today = date.today()
    min_dte = inp.get("min_dte", 14)
    max_spread = inp.get("max_spread_pct", 40)
    candidates = []
    for s in summaries:
        name = s.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "ETH":
            continue
        try:
            exp_date = datetime.strptime(parts[1], "%d%b%y").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < min_dte:
            continue
        strike = float(parts[2])
        opt = parts[3]
        bid = s.get("bid_price") or 0
        ask = s.get("ask_price") or 0
        if bid <= 0 or ask <= 0:
            continue
        spread = (ask - bid) / ask * 100
        if spread > max_spread:
            continue
        if strike < spot * 0.3 or strike > spot * 3.5:
            continue
        candidates.append({
            "name": name, "expiry_code": parts[1], "dte": dte,
            "strike": strike, "opt": opt,
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "mark_iv": s.get("mark_iv"),
        })

    if not candidates:
        return "No liquid instruments found.", None

    # ── Pre-compute full payoff matrix for all candidates ──
    full_payoff = np.zeros((len(spot_arr), len(candidates)))
    for j, c in enumerate(candidates):
        sigma_c = _get_sigma(c["strike"], c["expiry_code"], smiles) or 0.8
        T = max(c["dte"], 1) / 365.25
        full_payoff[:, j] = _bs_vec(spot_arr, c["strike"], T, 0.0, sigma_c, c["opt"])

    # ── Helper: solve one zone ──
    def _solve_zone(zone_mask, zone_name, prefer_opt=None, min_legs=3, max_legs=10):
        """Run LASSO on a subset of spot_arr. Returns list of (cand_idx, weight)."""
        zm = drawn_mask & zone_mask
        if zm.sum() < 3:
            return []

        # Filter candidates strictly by zone — no mixing
        cand_idxs = []
        for j, c in enumerate(candidates):
            if prefer_opt == "P":
                # Downside zone: ONLY puts (any strike) — no calls
                if c["opt"] == "P":
                    cand_idxs.append(j)
            elif prefer_opt == "C":
                # Upside zone: ONLY calls (any strike) — no puts
                if c["opt"] == "C":
                    cand_idxs.append(j)
            else:
                cand_idxs.append(j)

        if len(cand_idxs) < 2:
            # Fallback: include all if zone filter is too restrictive
            cand_idxs = list(range(len(candidates)))

        pm = full_payoff[np.ix_(np.where(zm)[0], cand_idxs)]
        zone_gap = gap[zm]

        # Normalize: scale both payoff matrix columns and gap
        gap_scale = max(np.abs(zone_gap).max(), 1.0)
        # Also scale each column of pm by its own max to equalize features
        col_scale = np.abs(pm).max(axis=0)
        col_scale[col_scale < 1e-6] = 1.0  # avoid div by zero
        pm_scaled = pm / col_scale[np.newaxis, :]
        gap_scaled = zone_gap / gap_scale

        best_w = None
        best_score = -1e18

        # Method 1: LASSO (if sklearn available) — best sparsity control
        if HAS_SKLEARN:
            alphas = [1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002,
                      0.001, 0.0005, 0.0002, 0.0001, 0.00005, 0.00001]
            for alpha in alphas:
                try:
                    lasso = Lasso(alpha=alpha, max_iter=20000, positive=False, fit_intercept=False)
                    lasso.fit(pm_scaled, gap_scaled)
                    w = lasso.coef_ * gap_scale / col_scale
                    n_active = int(np.sum(np.abs(w) > 0.5))
                    if n_active < min_legs or n_active > max_legs:
                        continue
                    residual = zone_gap - pm @ w
                    score = 1.0 - np.std(residual) / max(np.std(zone_gap), 1.0)
                    if score > best_score:
                        best_score = score
                        best_w = w.copy()
                except Exception:
                    continue

            if best_w is None:
                for alpha in alphas:
                    try:
                        lasso = Lasso(alpha=alpha, max_iter=20000, positive=False, fit_intercept=False)
                        lasso.fit(pm_scaled, gap_scaled)
                        w = lasso.coef_ * gap_scale / col_scale
                        if int(np.sum(np.abs(w) > 0.5)) >= 1:
                            best_w = w.copy()
                            break
                    except Exception:
                        continue

        # Method 2: Numpy least-squares with iterative pruning (no sklearn needed)
        if best_w is None:
            try:
                from numpy.linalg import lstsq
                # Solve full least-squares
                w_raw, _, _, _ = lstsq(pm, zone_gap, rcond=None)
                # Iteratively prune smallest legs until we have target count
                target_n = min(max_legs, max(min_legs, 6))
                w_pruned = w_raw.copy()
                for _ in range(max(len(w_pruned) - target_n, 0)):
                    abs_w = np.abs(w_pruned)
                    abs_w[abs_w < 0.01] = 1e18  # don't re-prune zeros
                    drop_idx = np.argmin(abs_w)
                    w_pruned[drop_idx] = 0.0
                    # Re-solve with remaining non-zero indices
                    active = np.where(np.abs(w_pruned) > 0.01)[0]
                    if len(active) < min_legs:
                        break
                    w_sub, _, _, _ = lstsq(pm[:, active], zone_gap, rcond=None)
                    w_pruned = np.zeros(pm.shape[1])
                    w_pruned[active] = w_sub
                best_w = w_pruned
            except Exception:
                return []

        if best_w is None:
            return []

        result = []
        for local_j, w_val in enumerate(best_w):
            if abs(w_val) < 0.5:
                continue
            result.append((cand_idxs[local_j], w_val))
        return result

    # ── Solve downside and upside independently ──
    spot_idx = int(np.argmin(np.abs(spot_arr - spot)))
    downside_mask = np.zeros(len(spot_arr), dtype=bool)
    downside_mask[:spot_idx + 1] = True  # at or below spot
    upside_mask = np.zeros(len(spot_arr), dtype=bool)
    upside_mask[spot_idx:] = True  # at or above spot (overlap at spot)

    down_weights = _solve_zone(downside_mask, "Downside", prefer_opt="P", min_legs=2, max_legs=8)
    up_weights = _solve_zone(upside_mask, "Upside", prefer_opt="C", min_legs=2, max_legs=8)

    # ── Convert weights to legs ──
    # Scale quantities to match portfolio size
    avg_pos_qty = max(np.mean([abs(p["net_qty"]) for p in positions]), 100) if positions else 1000
    MIN_LEG_QTY = max(round(avg_pos_qty * 0.1 / 100) * 100, 100)  # at least 10% of avg position, min 100
    MAX_LEG_QTY = max(round(avg_pos_qty * 3 / 100) * 100, 1000)   # at most 3x avg position, min 1000

    def _weights_to_legs(weights_list):
        if not weights_list:
            return []

        # Get raw absolute weights
        raw_abs = [abs(w) for _, w in weights_list]
        max_w = max(raw_abs)
        min_w = min(raw_abs)

        # Scale: ensure smallest leg >= MIN_LEG_QTY and largest <= MAX_LEG_QTY
        if max_w < 1e-6:
            return []
        # First scale up so the smallest leg hits MIN_LEG_QTY
        scale_up = MIN_LEG_QTY / min_w if min_w > 0 else 1.0
        # Then cap so largest doesn't exceed MAX_LEG_QTY
        if max_w * scale_up > MAX_LEG_QTY:
            scale_up = MAX_LEG_QTY / max_w
        # Final check: ensure minimum is still reasonable
        scale = max(scale_up, MIN_LEG_QTY / max_w)

        legs = []
        for cand_idx, w_val in weights_list:
            c = candidates[cand_idx]
            scaled_w = abs(w_val) * scale
            # Round to nearest 50, minimum MIN_LEG_QTY
            qty = max(round(scaled_w / 50) * 50, 50)
            if qty < 50:
                continue
            side = "buy" if w_val > 0 else "sell"
            legs.append({
                "instrument": c["name"], "side": side, "qty": qty,
                "strike": c["strike"], "opt": c["opt"],
                "expiry_code": c["expiry_code"], "dte": c["dte"],
                "price_eth": c["ask"] if side == "buy" else c["bid"],
                "price_usd": round((c["ask"] if side == "buy" else c["bid"]) * spot, 2),
                "bid_usd": round(c["bid"] * spot, 2),
                "ask_usd": round(c["ask"] * spot, 2),
                "spread_pct": round((c["ask"] - c["bid"]) / c["ask"] * 100, 1),
                "mark_iv": c.get("mark_iv"),
            })
        legs.sort(key=lambda l: (l["expiry_code"], l["strike"], l["opt"]))
        return legs

    down_legs = _weights_to_legs(down_weights)
    up_legs = _weights_to_legs(up_weights)
    combined_legs = _weights_to_legs(down_weights + up_weights)

    # ── Compute payoff and cost for a set of legs ──
    # zone_mask: score ONLY on the zone where this strategy operates
    def _compute_strategy(legs_list, name_label, zone_mask=None):
        if not legs_list:
            return None
        net_cost = 0.0
        for l in legs_list:
            s = -1 if l["side"] == "buy" else 1
            net_cost += s * l["price_usd"] * l["qty"]

        new_pf = current_payoff.copy()
        for l in legs_list:
            sigma_l = _get_sigma(l["strike"], l["expiry_code"], smiles) or 0.8
            T = max(l["dte"], 1) / 365.25
            s = 1.0 if l["side"] == "buy" else -1.0
            new_pf += s * l["qty"] * _bs_vec(spot_arr, l["strike"], T, 0.0, sigma_l, l["opt"])

        # Score on the relevant zone only (not the full curve)
        if zone_mask is not None:
            score_mask = drawn_mask & zone_mask
        else:
            score_mask = drawn_mask
        if score_mask.sum() < 2:
            score_mask = drawn_mask

        new_gap = np.abs(new_pf[score_mask] - target_interp[score_mask])
        old_gap = np.abs(current_payoff[score_mask] - target_interp[score_mask])
        tgt_range = max(float(np.ptp(target_interp[score_mask])), 1.0)
        match_pct = max(0.0, 100.0 * (1.0 - float(np.mean(new_gap)) / tgt_range))
        improvement_pct = max(0.0, 100.0 * (1.0 - float(np.mean(new_gap)) / max(float(np.mean(old_gap)), 1.0)))
        new_min = float(new_pf.min())

        return {
            "suggestion": {
                "name": f"{name_label} ({len(legs_list)} legs, {match_pct:.0f}% match)",
                "category": "target_match",
                "legs": legs_list,
                "net_cost_usd": round(net_cost, 2),
                "dte": max(l["dte"] for l in legs_list),
                "score": round(match_pct * 100, 2),
                "target_match_pct": round(match_pct, 1),
                "impact": {
                    "at_spot": round(float(new_pf[spot_idx] - current_payoff[spot_idx]), 2),
                    "min_improvement": round(new_min - float(current_payoff.min()), 2),
                    "new_min": round(new_min, 2),
                    "downside": 0, "upside": 0, "zone": 0,
                    "breakeven_improvement": 0, "new_breakeven": None,
                },
                "new_payoff": np.round(new_pf, 2).tolist(),
            },
            "match_pct": match_pct,
            "improvement_pct": improvement_pct,
            "net_cost": net_cost,
            "new_min": new_min,
            "new_pf": new_pf,
        }

    # ── Build descriptive strategy names from legs ──
    def _describe_legs(legs_list, zone_label):
        """Generate a descriptive strategy name from legs like 'Downside: Put Spread 2400/2200'."""
        if not legs_list:
            return zone_label
        strikes = sorted(set(l["strike"] for l in legs_list))
        types = set(l["opt"] for l in legs_list)
        sides = set(l["side"] for l in legs_list)
        expiry = legs_list[0].get("expiry_code", "")

        if len(legs_list) == 2 and len(types) == 1 and len(sides) == 2:
            # Spread: buy + sell same type, different strikes
            t = "Put" if "P" in types else "Call"
            return f"{zone_label}: {t} Spread {int(min(strikes))}/{int(max(strikes))} {expiry}"
        elif len(legs_list) == 2 and types == {"C", "P"} and len(sides) == 1:
            # Strangle or straddle
            if len(strikes) == 1:
                return f"{zone_label}: Straddle {int(strikes[0])} {expiry}"
            return f"{zone_label}: Strangle {int(min(strikes))}/{int(max(strikes))} {expiry}"
        elif len(legs_list) == 1:
            l = legs_list[0]
            t = "Put" if l["opt"] == "P" else "Call"
            s = "Long" if l["side"] == "buy" else "Short"
            return f"{zone_label}: {s} {t} {int(l['strike'])} {expiry}"
        else:
            strike_str = "/".join(str(int(s)) for s in strikes[:4])
            return f"{zone_label}: {len(legs_list)}-leg {strike_str} {expiry}"

    down_name = _describe_legs(down_legs, "Downside Protection")
    up_name = _describe_legs(up_legs, "Upside Shaping")
    combined_name = _describe_legs(combined_legs, "Combined Target Match")

    # Build all three strategies — score each on its OWN zone
    strat_down = _compute_strategy(down_legs, down_name, zone_mask=downside_mask)
    strat_up = _compute_strategy(up_legs, up_name, zone_mask=upside_mask)
    strat_combined = _compute_strategy(combined_legs, combined_name, zone_mask=None)  # full curve

    suggestions = []
    summary_parts = []

    for label, strat, legs_list in [
        ("DOWNSIDE (below spot)", strat_down, down_legs),
        ("UPSIDE (above spot)", strat_up, up_legs),
        ("COMBINED (full target)", strat_combined, combined_legs),
    ]:
        if not strat:
            summary_parts.append(f"\n=== {label} ===\nNo matching strategy found for this zone.\n")
            continue
        suggestions.append(strat["suggestion"])
        leg_lines = []
        for l in legs_list:
            sl = "Buy" if l["side"] == "buy" else "Sell"
            leg_lines.append(f"  {sl} {l['qty']} ETH {l['strike']}{l['opt']} {l['expiry_code']} @ ${l['price_usd']:.2f}")
        summary_parts.append(
            f"\n=== {label}: {strat['match_pct']:.1f}% match, {len(legs_list)} legs ===\n"
            f"Legs:\n" + "\n".join(leg_lines) + "\n"
            f"ACTUAL COST: ${strat['net_cost']:,.0f}\n"
            f"Floor change: {strat['new_min'] - float(current_payoff.min()):+,.0f}\n"
            f"P&L at spot change: {strat['suggestion']['impact']['at_spot']:+,.0f}\n"
        )

    if not suggestions:
        return "Could not find strategies matching the target profile.", None

    # ── Build position analysis summary ──
    close_positions = [p for p in position_analysis if p["action"] == "CLOSE"]
    roll_positions = [p for p in position_analysis if p["action"] == "ROLL"]
    essential_positions = [p for p in position_analysis if p["action"] == "ESSENTIAL"]

    pos_analysis_text = "\n=== EXISTING POSITION ANALYSIS vs TARGET ===\n"
    if close_positions:
        pos_analysis_text += "POSITIONS TO CLOSE (removing them improves target match):\n"
        for p in close_positions:
            pos_analysis_text += f"  CLOSE: {p['side']} {p['qty']} {p['opt']} @ {p['strike']} ({p['dte']}d) — removing improves match by {p['impact']:+.1f}%\n"
    if roll_positions:
        pos_analysis_text += "POSITIONS TO ROLL (close to expiry, should extend):\n"
        for p in roll_positions:
            pos_analysis_text += f"  ROLL: {p['side']} {p['qty']} {p['opt']} @ {p['strike']} ({p['dte']}d) — DTE too short\n"
    if essential_positions:
        pos_analysis_text += "ESSENTIAL POSITIONS (keep — removing them worsens match significantly):\n"
        for p in essential_positions:
            pos_analysis_text += f"  KEEP: {p['side']} {p['qty']} {p['opt']} @ {p['strike']} ({p['dte']}d) — removing worsens match by {abs(p['impact']):.1f}%\n"
    if not close_positions and not roll_positions:
        pos_analysis_text += "All existing positions are helping the target match. No closures or rolls recommended.\n"

    summary = (
        f"=== TARGET MATCH ANALYSIS ===\n"
        + pos_analysis_text
        + f"\n=== NEW TRADE STRATEGIES (3 options) ===\n"
        + "\n".join(summary_parts)
        + "\nIMPORTANT: Present the COMPLETE plan to the user: which positions to CLOSE, which to ROLL, and which NEW trades to add."
        + "\nReport exact costs from the data above. The user can add Downside and Upside strategies separately."
        + "\nIf there are positions to ROLL, call `suggest_rolls` to get priced roll suggestions."
    )

    spot_idx_local = int(np.argmin(np.abs(spot_arr - spot)))
    pnl_at_spot_local = float(current_payoff[spot_idx_local]) if len(current_payoff) > 0 else 0
    worst_local = float(current_payoff.min()) if len(current_payoff) > 0 else 0
    worst_at_local = float(spot_arr[np.argmin(current_payoff)]) if len(current_payoff) > 0 else 0
    best_local = float(current_payoff.max()) if len(current_payoff) > 0 else 0
    best_at_local = float(spot_arr[np.argmax(current_payoff)]) if len(current_payoff) > 0 else 0
    breakeven_local = _find_breakeven(spot_arr, current_payoff)

    result = {
        "suggestions": suggestions,
        "num_suggestions": len(suggestions),
        "spot_ladder": spot_arr.tolist(),
        "current_payoff": np.round(current_payoff, 2).tolist(),
        "current_profile": {
            "at_spot": pnl_at_spot_local, "min": worst_local, "min_at": worst_at_local,
            "max": best_local, "max_at": best_at_local, "breakeven": breakeven_local,
        },
        "positions": [{"side": "Long" if p["net_qty"] > 0 else "Short",
                        "type": "Call" if p["opt"] == "C" else "Put",
                        "strike": p["strike"], "qty": abs(p["net_qty"]),
                        "net_qty": p["net_qty"], "dte": p["dte"]}
                       for p in positions],
        "spot": spot,
        "available_expiries": sorted(set(c["expiry_code"] for c in candidates),
                                      key=lambda x: datetime.strptime(x, "%d%b%y")),
    }

    return summary, result


@router.post("/chat")
async def chat(req: ChatRequest):
    """Conversational optimizer — powered by Claude for real inference."""
    try:
        import anthropic
    except ImportError:
        return {"type": "question", "text": "**Setup required:** `pip install anthropic python-dotenv` on this server.", "context": {}}

    import json as _json

    if not ANTHROPIC_API_KEY:
        return {"type": "question", "text": "**Setup required:** Set `ANTHROPIC_API_KEY` in your `.env` file.", "context": {}}

    msg = req.message.strip()
    history = req.history

    # ── Load live portfolio context ──
    try:
        spot = await client.get_eth_spot_price()
    except Exception:
        spot = 0

    try:
        db = await get_db()
        db_trades = await list_trades(db, include_expired=False, include_deleted=False, asset="ETH")
    except Exception:
        db_trades = []

    # Fetch vol surface for BS pricing
    try:
        smiles = await _fetch_smiles()
    except Exception:
        smiles = {}

    closed_ids = set(req.closed_trade_ids)
    # Filter out "closed" (rolled) trades from portfolio context
    active_trades = [t for t in db_trades if t["id"] not in closed_ids]
    num_positions = len(active_trades)
    positions = []
    positions_detail = []  # rich detail for Claude
    expiry_groups: dict[str, int] = {}
    today = date.today()
    for t in active_trades:
        side = str(t["side"]).lower()
        opt = "C" if "call" in str(t.get("option_type", "")).lower() else "P"
        sign = 1.0 if side in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        expiry_str = str(t.get("expiry", ""))
        exp_short = expiry_str.split("T")[0]
        try:
            exp_date = datetime.fromisoformat(expiry_str).date() if "T" in expiry_str else datetime.strptime(exp_short, "%Y-%m-%d").date()
            dte = (exp_date - today).days
        except (ValueError, TypeError):
            dte = 0
        if strike > 0 and qty > 0:
            sigma = _get_sigma(strike, expiry_str, smiles)
            positions.append({"opt": opt, "strike": strike, "net_qty": sign * qty,
                              "expiry": expiry_str, "counterparty": t.get("counterparty", ""),
                              "dte": dte, "sigma": sigma})
            side_label = "Long" if sign > 0 else "Short"
            # Compute mark-to-market value at current spot
            entry_premium = _safe_float(t.get("premium_usd"))
            T_mtm = max(dte, 0) / 365.25
            if spot > 0 and T_mtm > 0 and sigma > 0:
                current_price_per = bs_price(spot, strike, T_mtm, 0.0, sigma, opt)
                current_value = sign * current_price_per * qty  # positive = asset value for longs
                mtm_pnl = current_value - (sign * entry_premium)  # P&L = current value - cost paid
            else:
                current_value = 0.0
                mtm_pnl = 0.0
            positions_detail.append({
                "id": t["id"], "counterparty": t.get("counterparty", ""),
                "side": side_label, "type": "Call" if opt == "C" else "Put",
                "strike": strike, "qty": qty, "net_qty": sign * qty,
                "expiry": exp_short, "dte": dte,
                "premium_usd": entry_premium,
                "current_value_usd": round(current_value, 2),
                "mtm_pnl_usd": round(mtm_pnl, 2),
            })
        expiry_groups[exp_short] = expiry_groups.get(exp_short, 0) + 1

    net_calls = sum(p["net_qty"] for p in positions if p["opt"] == "C")
    net_puts = sum(p["net_qty"] for p in positions if p["opt"] == "P")

    # ── Detect portfolio structures (spreads, condors, collars, etc.) ──
    from collections import defaultdict as _defaultdict
    _struct_groups: dict[tuple, list[dict]] = _defaultdict(list)
    for pd in positions_detail:
        _sk = (pd["counterparty"], pd["expiry"], pd["qty"])
        _struct_groups[_sk].append(pd)

    portfolio_structures = []
    _used_struct_ids: set[int] = set()

    def _detect_struct(stype, slegs):
        for sl in slegs:
            _used_struct_ids.add(sl["id"])
        strikes_desc = "/".join(str(int(sl["strike"])) for sl in sorted(slegs, key=lambda x: x["strike"]))
        sides_desc = " + ".join(f"{sl['side']} {sl['type']} {int(sl['strike'])}" for sl in sorted(slegs, key=lambda x: x["strike"]))
        ids_str = ", ".join(f"#{sl['id']}" for sl in slegs)
        struct_mtm = sum(sl.get("mtm_pnl_usd", 0) for sl in slegs)
        struct_value = sum(sl.get("current_value_usd", 0) for sl in slegs)
        mtm_tag = f"MTM P&L: ${struct_mtm:+,.0f}" if struct_mtm != 0 else "MTM P&L: $0"
        if struct_mtm > 1000:
            mtm_tag += " [PROFITABLE — harvest candidate]"
        elif struct_mtm < -1000:
            mtm_tag += " [UNDERWATER]"
        portfolio_structures.append(
            f"  {stype.replace('_', ' ').upper()} [{ids_str}]: {sides_desc} | exp {slegs[0]['expiry']} ({slegs[0]['dte']}d) | qty {slegs[0]['qty']} ETH | {mtm_tag}"
        )

    for _sk, _pool in _struct_groups.items():
        _avail = [l for l in _pool if l["id"] not in _used_struct_ids]
        _bp = sorted([l for l in _avail if l["type"] == "Put" and l["side"] == "Long"], key=lambda x: x["strike"])
        _sp = sorted([l for l in _avail if l["type"] == "Put" and l["side"] == "Short"], key=lambda x: x["strike"])
        _bc = sorted([l for l in _avail if l["type"] == "Call" and l["side"] == "Long"], key=lambda x: x["strike"])
        _sc = sorted([l for l in _avail if l["type"] == "Call" and l["side"] == "Short"], key=lambda x: x["strike"])
        # Iron Condors
        while _bp and _sp and _sc and _bc and all(l["id"] not in _used_struct_ids for l in [_bp[0], _sp[0], _sc[0], _bc[-1]]):
            _detect_struct("iron_condor", [_bp.pop(0), _sp.pop(0), _sc.pop(0), _bc.pop(-1)])
        # Put spreads
        _bp2 = [l for l in _bp if l["id"] not in _used_struct_ids]
        _sp2 = [l for l in _sp if l["id"] not in _used_struct_ids]
        while _bp2 and _sp2:
            _b = _bp2.pop(0)
            _best_i = min(range(len(_sp2)), key=lambda i: abs(_sp2[i]["strike"] - _b["strike"]))
            _detect_struct("put_spread", [_b, _sp2.pop(_best_i)])
        # Call spreads
        _bc2 = [l for l in _bc if l["id"] not in _used_struct_ids]
        _sc2 = [l for l in _sc if l["id"] not in _used_struct_ids]
        while _bc2 and _sc2:
            _b = _bc2.pop(0)
            _best_i = min(range(len(_sc2)), key=lambda i: abs(_sc2[i]["strike"] - _b["strike"]))
            _detect_struct("call_spread", [_b, _sc2.pop(_best_i)])
        # Collars (long put + short call)
        _bp3 = [l for l in _avail if l["type"] == "Put" and l["side"] == "Long" and l["id"] not in _used_struct_ids]
        _sc3 = [l for l in _avail if l["type"] == "Call" and l["side"] == "Short" and l["id"] not in _used_struct_ids]
        while _bp3 and _sc3:
            _detect_struct("collar", [_bp3.pop(0), _sc3.pop(0)])
        # Risk reversals (short put + long call)
        _sp3 = [l for l in _avail if l["type"] == "Put" and l["side"] == "Short" and l["id"] not in _used_struct_ids]
        _bc3 = [l for l in _avail if l["type"] == "Call" and l["side"] == "Long" and l["id"] not in _used_struct_ids]
        while _sp3 and _bc3:
            _detect_struct("risk_reversal", [_bc3.pop(0), _sp3.pop(0)])

    # Remaining naked legs
    for pd in positions_detail:
        if pd["id"] not in _used_struct_ids:
            mtm = pd.get("mtm_pnl_usd", 0)
            mtm_tag = f"MTM P&L: ${mtm:+,.0f}"
            if mtm > 1000:
                mtm_tag += " [PROFITABLE — harvest candidate]"
            elif mtm < -1000:
                mtm_tag += " [UNDERWATER]"
            portfolio_structures.append(
                f"  NAKED [#{pd['id']}]: {pd['side']} {pd['type']} {int(pd['strike'])} | exp {pd['expiry']} ({pd['dte']}d) | qty {pd['qty']} ETH | {mtm_tag}"
            )

    # Compute total harvestable profit
    _total_mtm = sum(pd.get("mtm_pnl_usd", 0) for pd in positions_detail)
    _harvestable = sum(pd.get("mtm_pnl_usd", 0) for pd in positions_detail if pd.get("mtm_pnl_usd", 0) > 0)
    _underwater = sum(pd.get("mtm_pnl_usd", 0) for pd in positions_detail if pd.get("mtm_pnl_usd", 0) < 0)
    portfolio_structures.append(f"\n  TOTAL PORTFOLIO MTM P&L: ${_total_mtm:+,.0f}")
    portfolio_structures.append(f"  HARVESTABLE PROFIT (close profitable positions): ${_harvestable:+,.0f}")
    portfolio_structures.append(f"  UNDERWATER POSITIONS: ${_underwater:+,.0f}")

    # Add DTE warnings
    _dte_warnings = []
    for pd in positions_detail:
        if pd["dte"] < 30:
            _dte_warnings.append(f"  WARNING: Trade #{pd['id']} ({pd['side']} {pd['type']} {int(pd['strike'])}) expires in {pd['dte']}d — ROLL CANDIDATE")
        elif pd["dte"] < 60:
            _dte_warnings.append(f"  WATCH: Trade #{pd['id']} ({pd['side']} {pd['type']} {int(pd['strike'])}) expires in {pd['dte']}d — consider rolling")

    structures_text = "\n".join(portfolio_structures) if portfolio_structures else "No structures detected."
    if _dte_warnings:
        structures_text += "\n\n=== EXPIRY ALERTS ===\n" + "\n".join(_dte_warnings)

    lo = max(500, int(spot * 0.2)) if spot > 0 else 500
    hi = int(spot * 3.5) if spot > 0 else 8000
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for p in positions:
        T = max(p["dte"], 0) / 365.25
        current_payoff += _bs_payoff_vec(spot_arr, p["strike"], p["opt"], p["net_qty"], T, p["sigma"])

    spot_idx = int(np.argmin(np.abs(spot_arr - spot))) if spot > 0 else 0
    pnl_at_spot = float(current_payoff[spot_idx]) if len(current_payoff) > 0 else 0
    worst = float(current_payoff.min()) if len(current_payoff) > 0 else 0
    worst_at = float(spot_arr[np.argmin(current_payoff)]) if len(current_payoff) > 0 else 0
    best = float(current_payoff.max()) if len(current_payoff) > 0 else 0
    breakeven = _find_breakeven(spot_arr, current_payoff)

    # P&L at key spot levels
    key_spots = [int(spot * m) for m in [0.3, 0.5, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0, 3.0]]
    pnl_ladder = {}
    for ks in key_spots:
        ki = int(np.argmin(np.abs(spot_arr - ks)))
        pnl_ladder[f"${ks:,}"] = round(float(current_payoff[ki]), 2)

    # Workbench context
    wb_legs = req.workbench_legs
    added_trades = req.added_trades
    wb_total_cost = 0.0
    wb_payoff = np.zeros_like(spot_arr)
    for leg in wb_legs:
        sign = 1.0 if leg.get("side") == "buy" else -1.0
        strike = _safe_float(leg.get("strike"))
        qty = _safe_float(leg.get("qty"))
        opt_type = leg.get("opt", "C")
        price_usd = _safe_float(leg.get("price_usd"))
        expiry_code = leg.get("expiry_code", "")
        dte_leg = int(leg.get("dte", 0))
        mark_iv = _safe_float(leg.get("mark_iv"))
        if strike > 0 and qty > 0:
            T = max(dte_leg, 0) / 365.25
            if mark_iv > 0:
                sigma = mark_iv / 100.0
            elif expiry_code and expiry_code in smiles:
                sigma = smiles[expiry_code].iv_at(strike) / 100.0
            else:
                sigma = DEFAULT_IV
            wb_payoff += _bs_payoff_vec(spot_arr, strike, opt_type, sign * qty, T, sigma)
        wb_total_cost += sign * price_usd * qty

    new_payoff = current_payoff + wb_payoff
    new_worst = float(new_payoff.min()) if len(new_payoff) > 0 else 0
    new_pnl_at_spot = float(new_payoff[spot_idx]) if len(new_payoff) > 0 else 0
    new_breakeven = _find_breakeven(spot_arr, new_payoff)

    context = {
        "spot": spot, "num_positions": num_positions,
        "net_calls": net_calls, "net_puts": net_puts,
        "pnl_at_spot": pnl_at_spot, "worst": worst, "worst_at": worst_at,
        "breakeven": breakeven,
    }

    # ── Fast path: detect pasted trades and add directly ──
    # Pattern: "Long/Short Put/Call ETH-DDMMMYY-STRIKE-C/P ... qty"
    trade_pattern = re.findall(
        r'(Long|Short)\s+(Put|Call)\s+ETH-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])\s+[\d-]+\s+\d+\s+\d+\s+[-\d.]+\s+(\d+)',
        msg, re.IGNORECASE
    )
    if not trade_pattern:
        # Also try simpler format: "Long Put ETH-26JUN26-2400-P qty 3000"
        trade_pattern = re.findall(
            r'(Long|Short)\s+(Put|Call)\s+ETH-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP]).*?(\d{3,})',
            msg, re.IGNORECASE
        )

    # Pre-built suggestion data from pasted trades (used by Claude for analysis)
    fast_path_data = None
    fast_path_summary = None

    if trade_pattern and len(trade_pattern) >= 2:
        # Parse trades and build suggestions directly for speed
        parsed_legs = []
        for match in trade_pattern:
            side_raw, type_raw, exp_code, strike_str, opt, qty_str = match
            parsed_legs.append({
                "side": "buy" if side_raw.lower() == "long" else "sell",
                "opt": opt.upper(),
                "strike": float(strike_str),
                "expiry_code": exp_code.upper(),
                "qty": float(qty_str),
            })

        # Group into put spreads / call spreads (pair buy+sell by type)
        strategies = []
        buys = [l for l in parsed_legs if l["side"] == "buy"]
        sells = [l for l in parsed_legs if l["side"] == "sell"]

        # Try to pair each buy with a sell of same type
        used_sells = set()
        for b in buys:
            for j, s in enumerate(sells):
                if j not in used_sells and s["opt"] == b["opt"] and s["expiry_code"] == b["expiry_code"]:
                    spread_type = "Put Spread" if b["opt"] == "P" else "Call Spread"
                    hi = max(b["strike"], s["strike"])
                    lo = min(b["strike"], s["strike"])
                    strategies.append({
                        "name": f"{spread_type} {b['expiry_code']}: {int(hi)}/{int(lo)}",
                        "legs": [b, s],
                    })
                    used_sells.add(j)
                    break

        # Any unpaired legs go as individual strategies
        for j, s in enumerate(sells):
            if j not in used_sells:
                strategies.append({"name": f"{'Sell' if s['side'] == 'sell' else 'Buy'} {s['expiry_code']} {int(s['strike'])}{s['opt']}", "legs": [s]})
        unpaired_buys = [b for b in buys if not any(b in strat["legs"] for strat in strategies)]
        for b in unpaired_buys:
            strategies.append({"name": f"Buy {b['expiry_code']} {int(b['strike'])}{b['opt']}", "legs": [b]})

        # If no clean pairing, just put all legs in one strategy
        if not strategies:
            strategies = [{"name": "Custom Strategy", "legs": parsed_legs}]

        # Fetch live prices from Deribit
        try:
            summaries_raw = await client._get("get_book_summary_by_currency", {
                "currency": "ETH", "kind": "option",
            })
            summaries = {s.get("instrument_name", ""): s for s in summaries_raw}
        except Exception:
            summaries = {}

        all_suggestions = []
        for strat in strategies:
            formatted_legs = []
            total_cost = 0.0
            for leg in strat["legs"]:
                strike_str = str(int(leg["strike"])) if leg["strike"] == int(leg["strike"]) else str(leg["strike"])
                inst_name = f"ETH-{leg['expiry_code']}-{strike_str}-{leg['opt']}"
                mkt = summaries.get(inst_name, {})
                bid = mkt.get("bid_price") or 0
                ask = mkt.get("ask_price") or 0
                mark_iv = mkt.get("mark_iv")
                price_eth = ask if leg["side"] == "buy" else bid
                try:
                    exp_date = datetime.strptime(leg["expiry_code"], "%d%b%y").date()
                    dte = (exp_date - date.today()).days
                except ValueError:
                    dte = 0
                sign = 1.0 if leg["side"] == "buy" else -1.0
                leg_cost = sign * price_eth * leg["qty"] * spot
                total_cost += leg_cost
                spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else 0
                formatted_legs.append({
                    "instrument": inst_name, "side": leg["side"], "qty": leg["qty"],
                    "strike": leg["strike"], "opt": leg["opt"],
                    "expiry_code": leg["expiry_code"], "dte": dte,
                    "price_eth": round(price_eth, 6), "price_usd": round(price_eth * spot, 2),
                    "bid_usd": round(bid * spot, 2), "ask_usd": round(ask * spot, 2),
                    "spread_pct": spread_pct, "mark_iv": mark_iv,
                })

            # Compute payoff (BS-aware with market IV)
            candidate = np.zeros_like(spot_arr)
            for fl in formatted_legs:
                s = 1.0 if fl["side"] == "buy" else -1.0
                T_fl = max(fl["dte"], 0) / 365.25
                fl_iv = fl.get("mark_iv")
                fl_sigma = fl_iv / 100.0 if fl_iv and fl_iv > 0 else DEFAULT_IV
                candidate += _bs_payoff_vec(spot_arr, fl["strike"], fl["opt"], s * fl["qty"], T_fl, fl_sigma)
            new_pf = current_payoff + candidate
            new_be_strat = _find_breakeven(spot_arr, new_pf)

            all_suggestions.append({
                "name": strat["name"], "category": "custom", "legs": formatted_legs,
                "net_cost_usd": round(total_cost, 2),
                "dte": formatted_legs[0]["dte"] if formatted_legs else 0,
                "score": 0,
                "impact": {
                    "at_spot": round(float(new_pf[spot_idx]) - pnl_at_spot, 2),
                    "min_improvement": round(float(new_pf.min()) - worst, 2),
                    "new_min": round(float(new_pf.min()), 2),
                    "downside": 0, "upside": 0, "zone": 0,
                    "breakeven_improvement": round(breakeven - new_be_strat, 2) if breakeven and new_be_strat else 0,
                    "new_breakeven": round(new_be_strat, 2) if new_be_strat else None,
                },
                "new_payoff": np.round(new_pf, 2).tolist(),
            })

        fast_path_data = {
            "spot": spot, "spot_ladder": spot_arr.tolist(),
            "current_payoff": np.round(current_payoff, 2).tolist(),
            "current_profile": {
                "at_spot": pnl_at_spot, "min": worst, "min_at": worst_at,
                "max": best, "max_at": float(spot_arr[np.argmax(current_payoff)]),
                "breakeven": breakeven,
            },
            "positions": [], "base_qty": 3000, "budget": 50000,
            "objective": "custom", "parsed_query": {"description": "User-provided trades"},
            "num_positions": num_positions, "num_instruments": len(parsed_legs),
            "num_suggestions": len(all_suggestions),
            "available_expiries": sorted(set(l["expiry_code"] for l in parsed_legs)),
            "suggestions": all_suggestions,
        }

        # Build a detailed summary for Claude to analyze
        summary_lines = []
        for s in all_suggestions:
            cost_str = f"${abs(round(s['net_cost_usd'])):,}"
            if s['net_cost_usd'] < 0:
                cost_str += " credit"
            else:
                cost_str = f"${round(s['net_cost_usd']):,} debit"
            legs_desc = ", ".join(
                f"{l['side'].upper()} {l['qty']:.0f}x {l['instrument']} @ ${l['price_usd']:,.2f} (IV: {l['mark_iv'] or 'N/A'}%, spread: {l['spread_pct']}%)"
                for l in s["legs"]
            )
            summary_lines.append(
                f"Strategy: {s['name']}\n"
                f"  Legs: {legs_desc}\n"
                f"  Net cost: {cost_str}\n"
                f"  Impact: P&L at spot {s['impact']['at_spot']:+,.0f}, "
                f"floor change {s['impact']['min_improvement']:+,.0f} (new floor: ${s['impact']['new_min']:,.0f}), "
                f"BE change {s['impact']['breakeven_improvement']:+,.0f}"
                + (f" (new BE: ${s['impact']['new_breakeven']:,.0f})" if s['impact']['new_breakeven'] else "")
            )
        fast_path_summary = "\n".join(summary_lines)

    # ── Auto-run match_target when user drew a target profile ──
    target_match_summary = None
    if req.target_payoff and len(req.target_payoff) >= 2 and not fast_path_data:
        try:
            mt_text, mt_data = await _handle_match_target({}, req.target_payoff)
            if mt_data and mt_data.get("suggestions"):
                fast_path_data = mt_data
                target_match_summary = mt_text
        except Exception as e:
            import traceback as _tb
            print(f"Auto match_target error:\n{_tb.format_exc()}")
            target_match_summary = f"Target matching failed: {e}"

    # ── Build system prompt ──
    fmt_spot = f"${spot:,.2f}"
    fmt_net_calls = f"{net_calls:+,.0f}"
    fmt_net_puts = f"{net_puts:+,.0f}"
    fmt_pnl = f"${pnl_at_spot:,.0f}"
    fmt_worst = f"${worst:,.0f}"
    fmt_worst_at = f"${worst_at:,.0f}"
    fmt_best = f"${best:,.0f}"
    fmt_be = f"${breakeven:,.0f}" if breakeven else "N/A"
    fmt_expiries = ', '.join(sorted(expiry_groups.keys())[:8])
    fmt_ladder = _json.dumps(pnl_ladder)
    fmt_positions = _json.dumps(positions_detail, default=str)

    system_prompt = """YOU MUST FOLLOW THESE RULES. THEY ARE NON-NEGOTIABLE:

1. You have 3 tools: `scan_trades`, `build_strategy`, `suggest_rolls`. USE THEM.
2. When the user gives you specific trades (strikes, expiries, sides, qtys) — IMMEDIATELY call `build_strategy` to add them to the Suggestions table. Do NOT just describe them in text.
3. When the user asks to "add", "test", "try", "show" trades — call `build_strategy` or `scan_trades`. NEVER say you cannot manage the workbench or suggestions. You CAN — via your tools.
4. NEVER use emojis.
5. NEVER say you lack portfolio data — it is provided below.
6. NEVER discuss topics outside ETH options/perps trading for this portfolio.
7. You CAN add strategies to the Suggestions table via build_strategy. You CANNOT directly add to the Workbench — the user must click the "+" button on a suggestion to move it to the workbench. When the user says "add to workbench", call build_strategy to put it in the Suggestions table, then tell them to click "+" to move it to the workbench.

=== CRITICAL: COST AND P&L ACCURACY ===
8. NEVER invent, estimate, or guess cost numbers. ONLY use the exact cost/P&L values returned by your tools (build_strategy, scan_trades, suggest_rolls). The tool results contain computed costs from live Deribit prices — those are the ONLY source of truth.
9. If the user asks for a "cost neutral" or "$0 cost" strategy and your tool returns a non-zero cost — REPORT THE ACTUAL COST from the tool result. Then explain that the strategy does not achieve $0 cost and suggest adjustments. NEVER claim a strategy costs $0 when the tool says otherwise.
10. When presenting strategies to the user, ALWAYS quote the "Net cost" value exactly as returned by the tool. Do NOT round aggressively, do NOT replace with "$0" or "near-zero" unless the actual value is within +/- $1,000.
11. NEVER write P&L impact tables with made-up numbers. Only include P&L figures if they come directly from tool results (the "impact" field). If you don't have tool results yet, say "call build_strategy to get exact numbers" instead of guessing.
12. If you want to propose multiple strategies, call build_strategy for EACH one separately so you get real costs for each. Do NOT describe strategies with cost estimates before calling the tool.

When parsing pasted trades:
- Instrument "ETH-26JUN26-2400-P" means expiry_code="26JUN26", strike=2400, opt="P"
- Group into strategies: Long+Short puts at different strikes = "Put Spread"
- Call build_strategy once per logical strategy (a spread = 2 legs in one call)

"""

    system_prompt += f"""You are the PLGO Options Trading Assistant — a senior options strategist for Protocol Labs' ETH derivatives portfolio.

=== LIVE MARKET ===
ETH Spot: {fmt_spot}

=== PORTFOLIO SUMMARY ===
Positions: {num_positions} | Net calls: {fmt_net_calls} ETH | Net puts: {fmt_net_puts} ETH
Expiries: {fmt_expiries}
P&L at spot: {fmt_pnl} | Worst: {fmt_worst} (at {fmt_worst_at}) | Best: {fmt_best} | BE: {fmt_be}
P&L ladder: {fmt_ladder}

=== ALL POSITIONS ===
{fmt_positions}

=== PORTFOLIO STRUCTURES (trades are paired — NEVER split these) ===
{structures_text}

IMPORTANT: When suggesting rolls, cost reductions, or closing trades, ALWAYS operate on entire structures.
A put spread (Long Put + Short Put) must be rolled/closed as a UNIT — never close just one leg.
An iron condor has 4 legs — roll all 4 together. Reference structures by their trade IDs.
When analyzing risk or suggesting improvements, describe what each STRUCTURE does, not individual legs.
"""

    if wb_legs:
        fmt_wb = _json.dumps(wb_legs, default=str)
        fmt_wb_cost = f"${wb_total_cost:,.2f}"
        fmt_wb_pnl = f"${new_pnl_at_spot:,.0f}"
        fmt_wb_worst = f"${new_worst:,.0f}"
        fmt_wb_be = f"${new_breakeven:,.0f}" if new_breakeven else "N/A"
        system_prompt += f"""
=== WORKBENCH ===
{fmt_wb}
Cost: {fmt_wb_cost} | P&L: {fmt_wb_pnl} | Worst: {fmt_wb_worst} | BE: {fmt_wb_be}
"""

    if added_trades:
        system_prompt += f"""
=== RECENTLY ADDED ===
{_json.dumps(added_trades, default=str)}
"""

    if closed_ids:
        system_prompt += f"""
=== ROLLED/CLOSED TRADES (optimizer working portfolio) ===
The following trade IDs have been "rolled" in the optimizer's working portfolio (NOT in the real DB):
{_json.dumps(list(closed_ids))}
These are excluded from the portfolio payoff. Do NOT suggest rolling them again.
"""

    if fast_path_summary:
        system_prompt += f"""
=== USER'S PASTED TRADES (already priced and added to Suggestions table) ===
{fast_path_summary}

The trades above have ALREADY been parsed, priced with live Deribit data, and added to the Suggestions table.
Do NOT call build_strategy for these trades again — they are already there.
Instead, focus on ANALYZING them: risk profile, greeks impact, how they interact with the existing portfolio,
max loss scenarios, breakeven analysis, and any concerns or improvements you see.
Answer any specific questions the user asked about these trades.
"""

    system_prompt += """
=== TOOLS ===
- `build_strategy(name, legs)`: Build specific trades and add to Suggestions table. Use when you know exact legs or user pastes trades. Fetches live Deribit prices.
- `scan_trades(query, filters)`: Search Deribit broadly. Results go to Suggestions table.
- `suggest_rolls(objective)`: Analyze which existing positions to roll. Returns priced roll suggestions (close existing + open at new expiry/strike). Use when user mentions "roll", "extend", "adjust expiry", "restructure existing", or when near-term positions are expiring. Results appear as Roll Cards in the Suggestions table with category "roll".

=== STRATEGY NAMING ===
Every strategy you create via build_strategy MUST have a descriptive name that includes:
1. The ZONE it addresses: "Downside:", "Upside:", or "Cost Reduction:" prefix
2. The STRUCTURE type: "Put Spread", "Call Spread", "Collar", "Butterfly", "Roll", etc.
3. KEY STRIKES and EXPIRY: e.g. "2400/2200 27JUN25"
Examples: "Downside: Put Spread 2400/2200 27JUN25", "Upside: Call Spread 3200/3800 26SEP25", "Cost Reduction: Sell Call 4000 27JUN25"
This naming lets the user identify each trade block in the workbench at a glance.

=== PROACTIVE PORTFOLIO ANALYSIS ===
CRITICAL: When analyzing the portfolio or suggesting improvements, you MUST consider ALL of these actions, not just new trades:

1. **ROLL existing structures** — Call `suggest_rolls` PROACTIVELY when:
   - Any structure has DTE < 60 days (getting close to expiry)
   - Strikes are far from current spot (deeply ITM or OTM structures losing effectiveness)
   - The user asks to improve the portfolio, reduce cost, or match a target
   - Rolling can reduce cost (e.g., rolling to farther expiry collects more premium)
   Think: "Before adding new trades, can I improve the portfolio by rolling what already exists?"

2. **CLOSE existing structures** — Recommend closing when:
   - A structure is at max profit (short options near zero value)
   - A structure has positive MTM P&L marked [PROFITABLE — harvest candidate] in the structures list
   - A structure is working against the target shape
   - Closing frees up margin or reduces risk for cheaper restructuring
   CRITICAL: Always close ENTIRE structures, never individual legs.
   - A "Call Spread [#39, #38]" must be closed by reversing BOTH legs.
   - Always reference the trade IDs: "Close Call Spread [#39, #38] 3700/5700"
   - Use build_strategy with the reverse legs (sell what's long, buy what's short) so it appears as an executable step.

3. **NEW trades** — Add new structures only when rolling/closing isn't sufficient.

The BEST analysis combines all three: "Roll structure [#12,#13] to extend duration, close [#15,#16] since it conflicts with the target, and add a new put spread to fill the gap."

=== ROLL WORKFLOW ===
When rolling or closing/restructuring positions:
1. Use `suggest_rolls` with the appropriate objective
2. Explain which positions you recommend rolling and WHY (DTE risk, cost efficiency, strike adjustment)
3. Show the roll suggestions so the user can click "+" to add them to the workbench
4. The roll suggestions show: original trade (counterparty, ID, strike, expiry) -> close leg + open leg at new expiry
5. The user can then click Calculate to get live prices and see the net roll cost

=== RESPONSE STYLE ===
When analyzing/optimizing:
1. DIAGNOSE — reference problem STRUCTURES by their trade IDs (e.g., "Put Spread [#12, #13] at 2400/2200")
2. EVALUATE EXISTING — for each structure, state: keep as-is, roll, or close? Why?
3. CALL TOOLS — call suggest_rolls for structures that should be rolled, build_strategy for new trades, BEFORE writing analysis
4. STRATEGY — present using ONLY the cost and impact numbers returned by your tools. Quote them exactly.
5. IMPACT TABLE — ONLY include P&L figures from tool results. If the tool did not return a P&L ladder, do NOT make one up.
6. GREEKS — brief delta/theta/vega impact (qualitative is fine)

IMPORTANT: The order is TOOL FIRST, THEN ANALYSIS. Never write cost estimates before calling the tool. The user trusts your numbers — wrong costs erode trust.
IMPORTANT: NEVER suggest only new trades without first evaluating whether existing structures should be rolled or closed. The user expects a complete portfolio management view.

Use **bold** for key numbers. Use markdown tables. Reference positions by ID. Be structured and actionable.
"""

    # ── Inject drawn target profile if provided ──
    if req.target_payoff:
        target_lines = []
        for pt in req.target_payoff:
            target_lines.append(f"  ETH ${pt['x']:,.0f} -> P&L ${pt['y']:,.0f}")
        system_prompt += f"""
=== DRAWN TARGET PAYOFF PROFILE ===
The user has drawn a target payoff curve on the chart.

Target points (Spot Price -> Desired P&L):
{chr(10).join(target_lines)}

The system has ALREADY run target matching optimization and added NEW TRADE strategies to the Suggestions table.
It has ALSO analyzed every existing position to determine which ones HELP vs HURT the target match.

{target_match_summary or "Target matching is processing."}

YOUR JOB — COMPLETE PORTFOLIO PLAN (follow this order):

**STEP 1 — EXISTING POSITIONS:** The analysis above tells you which positions to CLOSE, ROLL, or KEEP.
- For CLOSE candidates: explain WHY closing them improves the target match. These are positions working AGAINST the desired curve shape.
- For ROLL candidates: call `suggest_rolls` to get priced roll suggestions. These have short DTE and need to be extended.
- For ESSENTIAL positions: briefly note they are helping and should be kept.
You MUST call `suggest_rolls` if there are any ROLL candidates in the analysis. Do NOT skip this step.

**STEP 2 — BELOW SPOT (Downside):** Analyze the curve BELOW current spot. What does the user want on the downside? Explain the Downside new-trade strategy: legs, cost, match quality.

**STEP 3 — ABOVE SPOT (Upside):** Analyze the curve ABOVE current spot. Explain the Upside new-trade strategy: legs, cost, match quality.

**STEP 4 — COMPLETE ACTION PLAN:** Present the FULL plan as a numbered list:
  1. Close positions: [list with IDs]
  2. Roll positions: [list with IDs and target expiry]
  3. New trades - Downside: [strategy name and cost]
  4. New trades - Upside: [strategy name and cost]
  5. Total estimated cost of the full restructuring

Present each action as a distinct **named trade block** so the user can add them individually to the workbench.
Do NOT call build_strategy or scan_trades — the new trade strategies are already in the Suggestions table.
You CAN and SHOULD call `suggest_rolls` if there are positions to roll.
"""

    # ── Build messages ──
    messages = []
    for h in history:
        role = "user" if h.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": h.get("text", "")})
    messages.append({"role": "user", "content": msg})

    # ── Tool definitions ──
    tools = [
        {
            "name": "scan_trades",
            "description": "Scan the market for option structures that improve the portfolio. Returns ranked suggestions with cost and impact metrics. Use this when the user wants trade ideas, hedging structures, or improvements to their portfolio.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what to optimize for (e.g. 'protect downside below 1500', 'improve payoff from 0 to 2000')"
                    },
                    "budget": {
                        "type": "number",
                        "description": "Maximum cost in USD for the trade. Default 15000.",
                        "default": 15000
                    },
                    "min_floor_improvement": {
                        "type": "number",
                        "description": "If set, only return trades where worst-case P&L improves by at least this amount. Use when user wants floor/worst-case improvement.",
                        "default": 0
                    },
                    "min_downside_improvement": {
                        "type": "number",
                        "description": "If set, only return trades where downside zone improves. Use when user wants downside protection.",
                        "default": 0
                    },
                    "min_upside_improvement": {
                        "type": "number",
                        "description": "If set, only return trades where upside zone improves.",
                        "default": 0
                    },
                    "min_breakeven_improvement": {
                        "type": "number",
                        "description": "If set, only return trades where breakeven moves lower by at least this amount.",
                        "default": 0
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return. Default 10.",
                        "default": 10
                    },
                    "target_expiry": {
                        "type": "string",
                        "description": "Filter to specific expiry code (e.g. '27JUN25'). Leave empty for all."
                    },
                    "min_dte": {
                        "type": "integer",
                        "description": "Minimum days to expiry for instruments. Default 7. Set higher when user wants longer-dated trades.",
                        "default": 7
                    },
                },
                "required": ["query"]
            }
        },
        {
            "name": "suggest_rolls",
            "description": "Analyze which existing portfolio positions could be rolled (closed and reopened at a different strike/expiry). Returns priced roll suggestions showing close leg + open leg with live Deribit prices and net roll cost. Results appear as Roll Cards in the Suggestions table. Use when user mentions rolling, closing and reopening, extending duration, adjusting existing positions, or restructuring.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "What the roll should achieve: 'raise_floor', 'lower_breakeven', 'reduce_cost', 'extend_duration', 'adjust_strikes'",
                        "enum": ["raise_floor", "lower_breakeven", "reduce_cost", "extend_duration", "adjust_strikes"]
                    },
                    "min_dte": {
                        "type": "integer",
                        "description": "Minimum DTE for the new (rolled) positions. Default 60.",
                        "default": 60
                    },
                    "max_roll_cost": {
                        "type": "number",
                        "description": "Maximum net cost of the roll in USD. Default 15000.",
                        "default": 15000
                    },
                },
                "required": ["objective"]
            }
        },
        {
            "name": "build_strategy",
            "description": "Build a custom strategy from specific legs and add it to the Suggestions table. Use this when YOU recommend specific trades (strike, type, side, expiry) and want them to appear in the user's UI so they can add to workbench and test. This fetches live Deribit prices for each leg.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Strategy name (e.g. 'Floor Raiser: Bear Put Spread', 'Repair Corridor')"
                    },
                    "legs": {
                        "type": "array",
                        "description": "Array of trade legs",
                        "items": {
                            "type": "object",
                            "properties": {
                                "side": {"type": "string", "enum": ["buy", "sell"]},
                                "opt": {"type": "string", "enum": ["C", "P"], "description": "C=Call, P=Put"},
                                "strike": {"type": "number"},
                                "expiry_code": {"type": "string", "description": "Deribit expiry code e.g. '27JUN25'"},
                                "qty": {"type": "number", "description": "Quantity in ETH. Default to portfolio's average leg size."}
                            },
                            "required": ["side", "opt", "strike", "expiry_code"]
                        }
                    }
                },
                "required": ["name", "legs"]
            }
        },
        {
            "name": "match_target",
            "description": (
                "Find a multi-leg option strategy that best matches the user's drawn target payoff profile. "
                "Uses optimization to decompose the gap between current portfolio and target into tradeable legs. "
                "MUST be called whenever the user provides a target payoff profile (drawn on the chart). "
                "Returns a strategy with legs, cost, and target match percentage."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "min_dte": {
                        "type": "integer", "default": 14,
                        "description": "Minimum days to expiry for candidate options"
                    },
                    "max_spread_pct": {
                        "type": "number", "default": 40,
                        "description": "Maximum bid-ask spread percentage"
                    }
                },
                "required": []
            }
        }
    ]

    # ── Call Claude ──
    try:
        aclient = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = aclient.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4000,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )
    except Exception as e:
        # If we have fast-path data, still return the suggestions even if Claude failed
        if fast_path_data:
            return {"type": "suggestions", "text": f"**Error calling AI for analysis:** {e}\n\nYour trades have been added to the Suggestions table.", "context": context, "data": fast_path_data}
        return {"type": "question", "text": f"**Error calling AI:** {e}", "context": context}

    # ── Process response — handle multiple tool calls ──
    # Start with fast-path data if we pre-built suggestions from pasted trades
    result_data = fast_path_data

    async def _handle_scan_trades(inp: dict) -> tuple[str, dict | None]:
        """Execute scan_trades tool and return (result_text, suggestion_data)."""
        try:
            suggest_req = OptimizeRequest(
                query=inp.get("query", msg),
                budget=inp.get("budget", 15000),
                target_expiry=inp.get("target_expiry") or None,
                min_dte=inp.get("min_dte", 7),
                target_payoff=req.target_payoff,
            )
            result = await suggest_trades(suggest_req)
            all_sug = result["suggestions"]

            min_floor = inp.get("min_floor_improvement", 0)
            min_down = inp.get("min_downside_improvement", 0)
            min_up = inp.get("min_upside_improvement", 0)
            min_be = inp.get("min_breakeven_improvement", 0)

            if min_floor > 0:
                all_sug = [s for s in all_sug if s["impact"]["min_improvement"] > min_floor]
            if min_down > 0:
                all_sug = [s for s in all_sug if s["impact"]["downside"] > min_down]
            if min_up > 0:
                all_sug = [s for s in all_sug if s["impact"]["upside"] > min_up]
            if min_be > 0:
                all_sug = [s for s in all_sug if s["impact"].get("breakeven_improvement", 0) > min_be]

            max_r = inp.get("max_results", 10)
            top_n = all_sug[:max_r]
            result["suggestions"] = top_n
            result["num_suggestions"] = len(top_n)

            lines = []
            for i, s in enumerate(top_n):
                lines.append(
                    f"#{i+1} {s['name']} | cost: ${s['net_cost_usd']:,.0f} | "
                    f"floor: {s['impact']['min_improvement']:+,.0f} | "
                    f"downside: {s['impact']['downside']:+,.0f} | "
                    f"upside: {s['impact']['upside']:+,.0f} | "
                    f"BE: {s['impact'].get('breakeven_improvement', 0):+,.0f}"
                )
            text = (
                f"Found {len(all_sug)} structures passing filters (from {result['num_suggestions']} total). "
                f"Showing top {len(top_n)}:\n" + "\n".join(lines)
                + "\n\nIMPORTANT: The costs above are computed from live market data. Report these exact costs to the user."
            )
            return text, result
        except Exception as e:
            return f"Error scanning: {e}", None

    async def _handle_suggest_rolls(inp: dict) -> tuple[str, dict | None]:
        """Structure-aware roll analysis. Detects spreads/condors/collars and rolls them as units."""
        objective = inp.get("objective", "raise_floor")
        min_dte_target = inp.get("min_dte", 60)
        max_cost = inp.get("max_roll_cost", 50000)

        # ── 1. Build enriched position list ──
        all_legs = []
        today_d = date.today()
        for t in db_trades:
            if t["id"] in closed_ids:
                continue
            expiry_str = str(t.get("expiry", ""))
            try:
                exp_date = (datetime.fromisoformat(expiry_str).date() if "T" in expiry_str
                            else datetime.strptime(expiry_str[:10], "%Y-%m-%d").date())
                dte = (exp_date - today_d).days
            except (ValueError, TypeError):
                dte = 999
            side = str(t["side"]).lower()
            opt_raw = str(t.get("option_type", "")).lower()
            opt = "C" if "call" in opt_raw else "P"
            strike = _safe_float(t["strike"])
            qty = _safe_float(t["qty"])
            sign = 1.0 if side in ("buy", "long") else -1.0
            if strike > 0 and qty > 0:
                all_legs.append({
                    "id": t["id"], "counterparty": t.get("counterparty", ""),
                    "side": t["side"], "opt": opt, "strike": strike,
                    "qty": qty, "net_qty": sign * qty,
                    "expiry": expiry_str[:10], "dte": dte,
                    "premium_usd": _safe_float(t.get("premium_usd")),
                })

        if not all_legs:
            return "No active positions found to analyze.", None

        # ── 2. Detect structures by pairing legs ──
        # Group by (counterparty, expiry, qty) then extract structures one at a time
        from collections import defaultdict
        struct_groups: dict[tuple, list[dict]] = defaultdict(list)
        for leg in all_legs:
            key = (leg["counterparty"], leg["expiry"], leg["qty"])
            struct_groups[key].append(leg)

        structures = []
        used_ids: set[int] = set()

        def _make_struct(stype, slegs, cpty, expiry, qty):
            """Helper to build a structure dict from matched legs."""
            avg_dte = sum(l["dte"] for l in slegs) / len(slegs)
            strikes_desc = "/".join(str(int(l["strike"])) for l in sorted(slegs, key=lambda x: x["strike"]))
            sides_desc = "+".join(("L" if l["net_qty"] > 0 else "S") + l["opt"]
                                  for l in sorted(slegs, key=lambda x: x["strike"]))
            for l in slegs:
                used_ids.add(l["id"])
            structures.append({
                "type": stype, "legs": slegs,
                "ids": [l["id"] for l in slegs],
                "counterparty": cpty, "expiry": expiry,
                "dte": int(avg_dte), "qty": qty,
                "description": f"{stype.replace('_', ' ').title()} {strikes_desc} ({sides_desc})",
            })

        for key, pool in struct_groups.items():
            cpty, expiry, qty = key
            # Work with copies so we can consume legs as we match them
            avail = [l for l in pool if l["id"] not in used_ids]

            # Separate into buckets
            buy_puts = sorted([l for l in avail if l["opt"] == "P" and l["net_qty"] > 0], key=lambda x: x["strike"])
            sell_puts = sorted([l for l in avail if l["opt"] == "P" and l["net_qty"] < 0], key=lambda x: x["strike"])
            buy_calls = sorted([l for l in avail if l["opt"] == "C" and l["net_qty"] > 0], key=lambda x: x["strike"])
            sell_calls = sorted([l for l in avail if l["opt"] == "C" and l["net_qty"] < 0], key=lambda x: x["strike"])

            # Pass 1: Iron Condors (buy put + sell put + sell call + buy call)
            while (buy_puts and sell_puts and sell_calls and buy_calls
                   and buy_puts[0]["id"] not in used_ids and sell_puts[0]["id"] not in used_ids
                   and sell_calls[0]["id"] not in used_ids and buy_calls[-1]["id"] not in used_ids):
                bp = buy_puts.pop(0)
                sp = sell_puts.pop(0)
                sc = sell_calls.pop(0)
                bc = buy_calls.pop(-1)
                _make_struct("iron_condor", [bp, sp, sc, bc], cpty, expiry, qty)

            # Pass 2: Put spreads (buy put + sell put, pair by proximity)
            bp_remaining = [l for l in buy_puts if l["id"] not in used_ids]
            sp_remaining = [l for l in sell_puts if l["id"] not in used_ids]
            while bp_remaining and sp_remaining:
                bp = bp_remaining.pop(0)
                # Find closest sell put by strike
                best_idx, best_dist = 0, abs(sp_remaining[0]["strike"] - bp["strike"])
                for i, sp_c in enumerate(sp_remaining):
                    dist = abs(sp_c["strike"] - bp["strike"])
                    if dist < best_dist:
                        best_idx, best_dist = i, dist
                sp = sp_remaining.pop(best_idx)
                _make_struct("put_spread", [bp, sp], cpty, expiry, qty)

            # Pass 3: Call spreads (buy call + sell call)
            bc_remaining = [l for l in buy_calls if l["id"] not in used_ids]
            sc_remaining = [l for l in sell_calls if l["id"] not in used_ids]
            while bc_remaining and sc_remaining:
                bc = bc_remaining.pop(0)
                best_idx, best_dist = 0, abs(sc_remaining[0]["strike"] - bc["strike"])
                for i, sc_c in enumerate(sc_remaining):
                    dist = abs(sc_c["strike"] - bc["strike"])
                    if dist < best_dist:
                        best_idx, best_dist = i, dist
                sc = sc_remaining.pop(best_idx)
                _make_struct("call_spread", [bc, sc], cpty, expiry, qty)

            # Pass 4: Collars / Risk Reversals (remaining cross-type pairs)
            bp_left = [l for l in avail if l["opt"] == "P" and l["net_qty"] > 0 and l["id"] not in used_ids]
            sc_left = [l for l in avail if l["opt"] == "C" and l["net_qty"] < 0 and l["id"] not in used_ids]
            while bp_left and sc_left:
                _make_struct("collar", [bp_left.pop(0), sc_left.pop(0)], cpty, expiry, qty)

            sp_left = [l for l in avail if l["opt"] == "P" and l["net_qty"] < 0 and l["id"] not in used_ids]
            bc_left = [l for l in avail if l["opt"] == "C" and l["net_qty"] > 0 and l["id"] not in used_ids]
            while sp_left and bc_left:
                _make_struct("risk_reversal", [bc_left.pop(0), sp_left.pop(0)], cpty, expiry, qty)

        # Remaining unmatched legs are naked positions
        for leg in all_legs:
            if leg["id"] not in used_ids:
                side_label = "Long" if leg["net_qty"] > 0 else "Short"
                structures.append({
                    "type": "naked", "legs": [leg], "ids": [leg["id"]],
                    "counterparty": leg["counterparty"], "expiry": leg["expiry"],
                    "dte": leg["dte"], "qty": leg["qty"],
                    "description": f"{side_label} {leg['opt']} @ {int(leg['strike'])}",
                })

        # ── 3. Select structures to roll based on objective ──
        to_roll = []
        if objective == "extend_duration":
            to_roll = [s for s in structures if s["dte"] < min_dte_target]
        elif objective == "raise_floor":
            # Prefer structures with puts or short-dated
            to_roll = [s for s in structures if s["dte"] < min_dte_target * 2
                        or any(l["opt"] == "P" for l in s["legs"])]
            if not to_roll:
                to_roll = sorted(structures, key=lambda s: s["dte"])[:8]
        elif objective == "reduce_cost":
            # Structures with net debit (long-heavy)
            to_roll = [s for s in structures if sum(l["net_qty"] for l in s["legs"]) > 0][:8]
        elif objective == "lower_breakeven":
            to_roll = sorted(structures, key=lambda s: s["dte"])[:8]
        elif objective == "adjust_strikes":
            to_roll = [s for s in structures if s["dte"] < min_dte_target or any(
                (l["opt"] == "P" and l["strike"] < spot * 0.5) or
                (l["opt"] == "C" and l["strike"] > spot * 2.5)
                for l in s["legs"]
            )]
            if not to_roll:
                to_roll = sorted(structures, key=lambda s: s["dte"])[:8]

        if not to_roll:
            return "No structures found matching the roll criteria.", None

        # ── 4. Fetch Deribit data ──
        try:
            summaries_raw = await client._get("get_book_summary_by_currency", {
                "currency": "ETH", "kind": "option",
            })
            summaries_map = {s.get("instrument_name", ""): s for s in summaries_raw}
        except Exception:
            summaries_map = {}

        avail_expiries = {}
        for name in summaries_map:
            parts = name.split("-")
            if len(parts) == 4 and parts[0] == "ETH":
                try:
                    exp_date = datetime.strptime(parts[1], "%d%b%y").date()
                    dte = (exp_date - today_d).days
                    if dte >= min_dte_target:
                        avail_expiries[parts[1]] = dte
                except ValueError:
                    pass
        target_expiries = sorted(avail_expiries.keys(), key=lambda x: avail_expiries[x])

        if not target_expiries:
            return "No suitable target expiries found on Deribit.", None

        roll_target_codes = target_expiries[:3]

        # ── Helper: resolve expiry ISO -> Deribit code ──
        def _iso_to_exp_code(iso_str):
            for exp_code_key in summaries_map:
                parts = exp_code_key.split("-")
                if len(parts) == 4:
                    try:
                        edt = datetime.strptime(parts[1], "%d%b%y").date()
                        orig_edt = datetime.strptime(iso_str[:10], "%Y-%m-%d").date()
                        if abs((edt - orig_edt).days) <= 3:
                            return parts[1]
                    except (ValueError, TypeError):
                        pass
            try:
                od = datetime.strptime(iso_str[:10], "%Y-%m-%d").date()
                return od.strftime("%d%b%y").upper()
            except (ValueError, TypeError):
                return None

        # ── Helper: price a single leg ──
        def _price_leg(strike, opt, exp_code, side, qty, dte_val):
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            inst_name = f"ETH-{exp_code}-{strike_str}-{opt}"
            mkt = summaries_map.get(inst_name, {})
            bid = mkt.get("bid_price") or 0
            ask = mkt.get("ask_price") or 0
            price_eth = ask if side == "buy" else bid
            mark_iv = mkt.get("mark_iv")

            if price_eth <= 0:
                T_val = max(dte_val, 0) / 365.25 if dte_val else _time_to_expiry_years(exp_code)
                sigma_val = _get_sigma(strike, "", smiles, expiry_code=exp_code)
                if spot > 0 and T_val > 0:
                    price_eth = bs_price(spot, strike, T_val, 0.0, sigma_val, opt) / spot
                else:
                    return None

            spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else 0
            sign = 1.0 if side == "buy" else -1.0
            cost = sign * price_eth * qty * spot
            return {
                "instrument": inst_name, "side": side, "qty": qty,
                "strike": strike, "opt": opt, "expiry_code": exp_code,
                "dte": dte_val if dte_val else int(_time_to_expiry_years(exp_code) * 365.25),
                "price_eth": round(price_eth, 6), "price_usd": round(price_eth * spot, 2),
                "bid_usd": round(bid * spot, 2), "ask_usd": round(ask * spot, 2),
                "spread_pct": spread_pct, "mark_iv": mark_iv,
                "cost": cost,
            }

        # ── 5. Build roll suggestions per structure ──
        lo_val = max(500, int(spot * 0.2))
        hi_val = int(spot * 3.5)
        spot_arr_roll = np.arange(lo_val, hi_val + SPOT_STEP, SPOT_STEP, dtype=float)
        spot_idx_roll = int(np.argmin(np.abs(spot_arr_roll - spot)))

        roll_suggestions = []

        for struct in to_roll[:8]:
            orig_exp_code = _iso_to_exp_code(struct["expiry"])
            if not orig_exp_code:
                continue

            for tgt_exp in roll_target_codes:
                tgt_dte = avail_expiries[tgt_exp]
                close_legs = []
                open_legs = []
                total_close_cost = 0.0
                total_open_cost = 0.0
                skip = False

                for leg in struct["legs"]:
                    orig_side_lower = leg["side"].lower()
                    close_side = "sell" if orig_side_lower in ("buy", "long") else "buy"
                    new_side = "buy" if orig_side_lower in ("buy", "long") else "sell"

                    # Close the original leg
                    cl = _price_leg(leg["strike"], leg["opt"], orig_exp_code, close_side, leg["qty"], leg["dte"])
                    if cl is None:
                        skip = True
                        break

                    # New strike: keep structure shape by default, adjust only per objective
                    new_strike = leg["strike"]
                    if objective == "adjust_strikes":
                        # Shift proportionally toward ATM
                        ratio = leg["strike"] / spot
                        if ratio < 0.5:
                            new_strike = round(max(leg["strike"], spot * 0.6) / 100) * 100
                        elif ratio > 2.0:
                            new_strike = round(min(leg["strike"], spot * 1.8) / 100) * 100

                    ol = _price_leg(new_strike, leg["opt"], tgt_exp, new_side, leg["qty"], tgt_dte)
                    if ol is None:
                        skip = True
                        break

                    close_legs.append(cl)
                    open_legs.append(ol)
                    total_close_cost += cl["cost"]
                    total_open_cost += ol["cost"]

                if skip:
                    continue

                net_roll_cost = total_close_cost + total_open_cost
                if abs(net_roll_cost) > max_cost:
                    continue

                # Compute payoff impact: remove all original legs, add all new legs
                roll_effect = np.zeros_like(spot_arr_roll)
                for leg in struct["legs"]:
                    T_orig = max(leg["dte"], 0) / 365.25
                    sigma_orig = _get_sigma(leg["strike"], leg["expiry"], smiles)
                    roll_effect -= _bs_payoff_vec(spot_arr_roll, leg["strike"], leg["opt"], leg["net_qty"], T_orig, sigma_orig)
                for ol in open_legs:
                    T_new = _time_to_expiry_years(tgt_exp)
                    sigma_new = _get_sigma(ol["strike"], "", smiles, expiry_code=tgt_exp)
                    new_sign = 1.0 if ol["side"] == "buy" else -1.0
                    roll_effect += _bs_payoff_vec(spot_arr_roll, ol["strike"], ol["opt"], new_sign * ol["qty"], T_new, sigma_new)

                new_payoff_roll = current_payoff + roll_effect
                new_be_roll = _find_breakeven(spot_arr_roll, new_payoff_roll)

                # Score: weighted combination of improvements
                floor_imp = float(new_payoff_roll.min() - current_payoff.min())
                spot_imp = float(new_payoff_roll[spot_idx_roll] - current_payoff[spot_idx_roll])
                be_imp = (breakeven - new_be_roll) if breakeven and new_be_roll else 0.0

                # Penalize rolls that worsen the floor
                if objective == "raise_floor":
                    score = 0.50 * floor_imp + 0.30 * spot_imp + 0.20 * be_imp
                elif objective == "lower_breakeven":
                    score = 0.50 * be_imp + 0.30 * spot_imp + 0.20 * floor_imp
                elif objective == "reduce_cost":
                    score = -net_roll_cost + 0.20 * floor_imp + 0.10 * spot_imp
                else:
                    score = 0.30 * floor_imp + 0.30 * spot_imp + 0.20 * be_imp + 0.20 * (-abs(net_roll_cost) / 1000)

                # Build combined legs list for workbench
                all_close_open = [{"_leg_type": "close", **cl} for cl in close_legs] + [{"_leg_type": "open", **ol} for ol in open_legs]
                # Remove cost field (internal)
                for l in all_close_open:
                    l.pop("cost", None)
                    l.pop("_leg_type", None)

                # Structure name
                struct_label = struct["type"].replace("_", " ").title()
                strikes_str = "/".join(str(int(l["strike"])) for l in sorted(struct["legs"], key=lambda x: x["strike"]))
                sug_name = f"Roll {struct_label} {strikes_str} ({orig_exp_code} -> {tgt_exp})"
                if struct["counterparty"]:
                    sug_name += f" [{struct['counterparty']}]"

                # Original trade info shows the full structure
                orig_trade_info = {
                    "id": struct["ids"][0],  # primary ID
                    "all_ids": struct["ids"],
                    "counterparty": struct["counterparty"],
                    "structure_type": struct["type"],
                    "description": struct["description"],
                    "side": struct["legs"][0]["side"],
                    "opt": struct["legs"][0]["opt"],
                    "strike": struct["legs"][0]["strike"],
                    "qty": struct["qty"],
                    "net_qty": struct["legs"][0]["net_qty"],
                    "expiry": struct["expiry"],
                    "dte": struct["dte"],
                    "num_legs": len(struct["legs"]),
                    "legs_detail": [{
                        "id": l["id"], "side": l["side"], "opt": l["opt"],
                        "strike": l["strike"], "net_qty": l["net_qty"],
                    } for l in struct["legs"]],
                }

                roll_suggestions.append({
                    "name": sug_name,
                    "category": "roll",
                    "is_roll": True,
                    "original_trade_id": struct["ids"][0],
                    "original_trade_ids": struct["ids"],
                    "original_trade": orig_trade_info,
                    "close_legs": close_legs,
                    "open_legs": open_legs,
                    "legs": all_close_open,
                    "net_cost_usd": round(net_roll_cost, 2),
                    "close_cost_usd": round(total_close_cost, 2),
                    "open_cost_usd": round(total_open_cost, 2),
                    "dte": tgt_dte,
                    "score": round(score, 2),
                    "impact": {
                        "at_spot": round(spot_imp, 2),
                        "min_improvement": round(floor_imp, 2),
                        "new_min": round(float(new_payoff_roll.min()), 2),
                        "downside": 0, "upside": 0, "zone": 0,
                        "breakeven_improvement": round(be_imp, 2) if be_imp else 0,
                        "new_breakeven": round(new_be_roll, 2) if new_be_roll else None,
                    },
                    "new_payoff": np.round(new_payoff_roll, 2).tolist(),
                })

        if not roll_suggestions:
            struct_summary = "\n".join(
                f"  - {s['description']} | {s['counterparty']} | exp {s['expiry']} ({s['dte']}d) | {len(s['legs'])} legs"
                for s in to_roll[:8]
            )
            lines = [
                f"Analyzed {len(to_roll)} structures for rolling but no suitable rolls found within ${max_cost:,.0f} budget.",
                f"\nStructures analyzed:\n{struct_summary}",
                f"\nTarget expiries: {', '.join(roll_target_codes)}",
            ]
            return "\n".join(lines), None

        # Sort by score
        roll_suggestions.sort(key=lambda s: s["score"], reverse=True)
        top = roll_suggestions[:12]

        # Build result data
        all_exp_codes = set()
        for s in top:
            for l in s.get("close_legs", []):
                all_exp_codes.add(l["expiry_code"])
            for l in s.get("open_legs", []):
                all_exp_codes.add(l["expiry_code"])

        roll_result = {
            "spot": spot,
            "spot_ladder": spot_arr.tolist(),
            "current_payoff": np.round(current_payoff, 2).tolist(),
            "current_profile": {
                "at_spot": float(current_payoff[spot_idx]),
                "min": float(current_payoff.min()),
                "min_at": float(spot_arr[np.argmin(current_payoff)]),
                "max": float(current_payoff.max()),
                "max_at": float(spot_arr[np.argmax(current_payoff)]),
                "breakeven": breakeven,
            },
            "positions": positions_detail,
            "base_qty": 0,
            "budget": max_cost,
            "objective": objective,
            "parsed_query": {"description": f"Roll suggestions — {objective.replace('_', ' ')}"},
            "num_positions": num_positions,
            "num_instruments": len(to_roll),
            "num_suggestions": len(top),
            "available_expiries": sorted(all_exp_codes, key=lambda x: datetime.strptime(x, "%d%b%y")),
            "suggestions": top,
        }

        # Build summary for Claude — structure-aware
        lines = []
        # Report detected structures
        struct_types = defaultdict(int)
        for s in structures:
            struct_types[s["type"]] += 1
        struct_summary = ", ".join(f"{v} {k.replace('_', ' ')}(s)" for k, v in sorted(struct_types.items()))
        lines.append(f"Portfolio structure analysis: {struct_summary}")
        lines.append(f"Found {len(roll_suggestions)} roll opportunities (showing top {len(top)}):\n")

        for i, s in enumerate(top):
            orig = s["original_trade"]
            floor_imp = s["impact"]["min_improvement"]
            floor_warn = " ** WORSENS FLOOR **" if floor_imp < -1000 else ""
            lines.append(
                f"#{i+1} {s['name']}\n"
                f"    Structure: {orig['description']} ({orig['num_legs']} legs, cpty: {orig['counterparty']})\n"
                f"    Close: ${s['close_cost_usd']:,.0f} | Open: ${s['open_cost_usd']:,.0f} | Net: ${s['net_cost_usd']:,.0f}\n"
                f"    Floor: {floor_imp:+,.0f} | Spot P&L: {s['impact']['at_spot']:+,.0f} | Score: {s['score']:.0f}{floor_warn}"
            )

        lines.append("\nThese roll suggestions preserve the original structure shape (spreads roll as spreads, condors as condors).")
        lines.append("IMPORTANT: The costs above are computed from LIVE Deribit bid/ask prices. You MUST report these exact costs to the user. Do NOT claim a roll is 'cost neutral' or '$0' unless the Net cost shown above is within +/- $1,000.")
        lines.append("Ask the user if they want to add any to the workbench.")
        return "\n".join(lines), roll_result

    async def _handle_build_strategy(inp: dict) -> tuple[str, dict | None]:
        """Build a custom strategy from specific legs, fetch live prices, return as suggestion data."""
        strategy_name = inp.get("name", "Custom Strategy")
        legs_input = inp.get("legs", [])
        if not legs_input:
            return "No legs provided.", None

        # Fetch live prices from Deribit
        try:
            summaries_raw = await client._get("get_book_summary_by_currency", {
                "currency": "ETH", "kind": "option",
            })
            summaries = {s.get("instrument_name", ""): s for s in summaries_raw}
        except Exception:
            summaries = {}

        # Default qty from portfolio average
        avg_qty = 1000
        if positions:
            avg_qty = max(100, min(round(np.mean([abs(p["net_qty"]) for p in positions]) / 5 / 100) * 100, 2000))

        formatted_legs = []
        total_cost = 0.0
        for leg in legs_input:
            strike = leg["strike"]
            opt_type = leg["opt"]
            side = leg["side"]
            exp_code = leg["expiry_code"]
            qty = leg.get("qty", avg_qty)

            # Build instrument name
            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            inst_name = f"ETH-{exp_code}-{strike_str}-{opt_type}"

            # Look up live price
            mkt = summaries.get(inst_name, {})
            bid = mkt.get("bid_price") or 0
            ask = mkt.get("ask_price") or 0
            mark = mkt.get("mark_price") or 0
            mark_iv = mkt.get("mark_iv")
            price_eth = ask if side == "buy" else bid

            # Compute DTE
            try:
                exp_date = datetime.strptime(exp_code, "%d%b%y").date()
                dte = (exp_date - date.today()).days
            except ValueError:
                dte = 0

            # Reject expired instruments
            if dte <= 0:
                return f"Rejected: {inst_name} is expired (DTE={dte}). Use a future expiry.", None

            # Fallback to BS pricing when no market data (OTC positions, illiquid strikes)
            if bid <= 0 and ask <= 0:
                T_fb = max(dte, 1) / 365.25
                sigma_fb = _get_sigma(strike, exp_code, smiles, expiry_code=exp_code) if smiles else DEFAULT_IV
                if spot > 0 and T_fb > 0:
                    bs_px = bs_price(spot, strike, T_fb, 0.0, sigma_fb, opt_type)
                    price_eth = bs_px / spot if spot > 0 else 0
                    bid = price_eth * 0.95  # estimate bid/ask from BS
                    ask = price_eth * 1.05
                    mark_iv = sigma_fb * 100
                else:
                    return f"Rejected: {inst_name} has no market data and cannot be priced.", None

            sign = 1.0 if side == "buy" else -1.0
            leg_cost = sign * price_eth * qty * spot
            total_cost += leg_cost

            spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else 0

            formatted_legs.append({
                "instrument": inst_name, "side": side, "qty": qty,
                "strike": strike, "opt": opt_type,
                "expiry_code": exp_code, "dte": dte,
                "price_eth": round(price_eth, 6), "price_usd": round(price_eth * spot, 2),
                "bid_usd": round(bid * spot, 2), "ask_usd": round(ask * spot, 2),
                "spread_pct": spread_pct, "mark_iv": mark_iv,
            })

        # Compute payoff impact (BS-aware with market IV)
        lo_val = max(500, int(spot * 0.2))
        hi_val = int(spot * 3.5)
        spot_arr_local = np.arange(lo_val, hi_val + SPOT_STEP, SPOT_STEP, dtype=float)
        candidate = np.zeros_like(spot_arr_local)
        for fl in formatted_legs:
            s = 1.0 if fl["side"] == "buy" else -1.0
            T_fl = max(fl["dte"], 0) / 365.25
            fl_iv = fl.get("mark_iv")
            fl_sigma = fl_iv / 100.0 if fl_iv and fl_iv > 0 else DEFAULT_IV
            candidate += _bs_payoff_vec(spot_arr_local, fl["strike"], fl["opt"], s * fl["qty"], T_fl, fl_sigma)

        new_payoff_local = current_payoff + candidate
        spot_idx_local = int(np.argmin(np.abs(spot_arr_local - spot)))
        new_be = _find_breakeven(spot_arr_local, new_payoff_local)

        suggestion = {
            "name": strategy_name, "category": "custom", "legs": formatted_legs,
            "net_cost_usd": round(total_cost, 2), "dte": formatted_legs[0]["dte"] if formatted_legs else 0,
            "score": 0,
            "impact": {
                "at_spot": round(float(new_payoff_local[spot_idx_local]) - float(current_payoff[spot_idx_local]), 2),
                "min_improvement": round(float(new_payoff_local.min()) - float(current_payoff.min()), 2),
                "new_min": round(float(new_payoff_local.min()), 2),
                "downside": 0, "upside": 0, "zone": 0,
                "breakeven_improvement": round(breakeven - new_be, 2) if breakeven and new_be else 0,
                "new_breakeven": round(new_be, 2) if new_be else None,
            },
            "new_payoff": np.round(new_payoff_local, 2).tolist(),
        }

        # Build result in same format as suggest_trades
        result_data = {
            "spot": spot,
            "spot_ladder": spot_arr_local.tolist(),
            "current_payoff": np.round(current_payoff, 2).tolist(),
            "current_profile": {
                "at_spot": float(current_payoff[spot_idx_local]),
                "min": float(current_payoff.min()),
                "min_at": float(spot_arr_local[np.argmin(current_payoff)]),
                "max": float(current_payoff.max()),
                "max_at": float(spot_arr_local[np.argmax(current_payoff)]),
                "breakeven": breakeven,
            },
            "positions": positions_detail,
            "base_qty": avg_qty,
            "budget": abs(total_cost) + 1000,
            "objective": "custom",
            "parsed_query": {"description": strategy_name},
            "num_positions": len(positions),
            "num_instruments": len(formatted_legs),
            "num_suggestions": 1,
            "available_expiries": sorted(set(l["expiry_code"] for l in formatted_legs)),
            "suggestions": [suggestion],
        }

        cost_str = f"${abs(round(total_cost)):,}"
        if total_cost < 0:
            cost_str += " credit"
        leg_summary = " / ".join(
            f"{'Buy' if l['side'] == 'buy' else 'Sell'} {l['instrument']} x{l['qty']:,.0f} @ ${l['price_usd']:,.2f}"
            for l in formatted_legs
        )
        summary = (
            f"Built strategy: {strategy_name}\n"
            f"Legs: {leg_summary}\n"
            f"=== ACTUAL COST (from live Deribit prices): {cost_str} ===\n"
            f"Floor change: {suggestion['impact']['min_improvement']:+,.0f}\n"
            f"P&L at spot change: {suggestion['impact']['at_spot']:+,.0f}\n"
            f"New worst-case P&L: ${suggestion['impact']['new_min']:,.0f}\n"
            f"IMPORTANT: You MUST report this exact cost to the user. Do NOT say the cost is $0 or 'neutral' if the above cost is not near zero.\n"
            f"The strategy is now in the Suggestions table. The user can click + to add it to their workbench."
        )
        return summary, result_data

    def _serialize_content(content_blocks) -> list[dict]:
        """Convert SDK content blocks to plain dicts for message history."""
        result = []
        for b in content_blocks:
            if b.type == "text":
                result.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return result

    # Loop until Claude gives a text response (max 5 tool rounds)
    text = ""
    current_response = response
    for _round in range(5):
        if current_response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in current_response.content if b.type == "tool_use"]
        tool_results_content = []

        for tb in tool_blocks:
            if tb.name == "scan_trades":
                tr_text, tr_data = await _handle_scan_trades(tb.input)
                if tr_data:
                    if result_data and "suggestions" in result_data:
                        # Merge: keep existing suggestions, add new ones
                        result_data["suggestions"].extend(tr_data.get("suggestions", []))
                        result_data["num_suggestions"] += tr_data.get("num_suggestions", 0)
                    else:
                        result_data = tr_data
                tool_results_content.append({
                    "type": "tool_result", "tool_use_id": tb.id, "content": tr_text
                })
            elif tb.name == "suggest_rolls":
                roll_text, roll_data = await _handle_suggest_rolls(tb.input)
                if roll_data:
                    if result_data and "suggestions" in result_data:
                        result_data["suggestions"].extend(roll_data.get("suggestions", []))
                        result_data["num_suggestions"] += roll_data.get("num_suggestions", 0)
                        # Merge available expiries
                        existing = set(result_data.get("available_expiries", []))
                        existing.update(roll_data.get("available_expiries", []))
                        result_data["available_expiries"] = sorted(existing, key=lambda x: datetime.strptime(x, "%d%b%y"))
                    else:
                        result_data = roll_data
                tool_results_content.append({
                    "type": "tool_result", "tool_use_id": tb.id, "content": roll_text
                })
            elif tb.name == "build_strategy":
                bs_text, bs_data = await _handle_build_strategy(tb.input)
                if bs_data:
                    if result_data and "suggestions" in result_data:
                        # Merge into existing suggestions
                        result_data["suggestions"].extend(bs_data.get("suggestions", []))
                        result_data["num_suggestions"] += bs_data.get("num_suggestions", 0)
                    else:
                        result_data = bs_data
                tool_results_content.append({
                    "type": "tool_result", "tool_use_id": tb.id, "content": bs_text
                })
            elif tb.name == "match_target":
                # If target match was already auto-run, don't duplicate
                if target_match_summary and result_data and result_data.get("suggestions"):
                    mt_text = "Target matching was already performed automatically. The strategies are already in the Suggestions table — do NOT add more."
                    tool_results_content.append({
                        "type": "tool_result", "tool_use_id": tb.id, "content": mt_text
                    })
                else:
                    try:
                        mt_text, mt_data = await _handle_match_target(tb.input, req.target_payoff or [])
                    except Exception as e:
                        import traceback
                        tb_str = traceback.format_exc()
                        print(f"match_target error:\n{tb_str}")
                        mt_text = f"Error in match_target: {e}"
                        mt_data = None
                    if mt_data:
                        if result_data and "suggestions" in result_data:
                            result_data["suggestions"].extend(mt_data.get("suggestions", []))
                            result_data["num_suggestions"] += mt_data.get("num_suggestions", 0)
                        else:
                            result_data = mt_data
                    tool_results_content.append({
                        "type": "tool_result", "tool_use_id": tb.id, "content": mt_text
                    })
            else:
                tool_results_content.append({
                    "type": "tool_result", "tool_use_id": tb.id, "content": "Unknown tool."
                })

        # Serialize content blocks to dicts so they survive re-serialization
        messages.append({"role": "assistant", "content": _serialize_content(current_response.content)})
        messages.append({"role": "user", "content": tool_results_content})

        try:
            current_response = aclient.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4000,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )
        except Exception as e:
            text = f"**Error:** {e}"
            break

    # Extract text from final response
    if current_response and current_response.stop_reason != "tool_use":
        text_blocks = [b.text for b in current_response.content if hasattr(b, "text")]
        text = "\n".join(text_blocks) if text_blocks else "Here are the results."
    elif not text:
        text = "I ran out of processing steps. Try a simpler request."

    # Always include base portfolio data so the frontend can load portfolio on any message (incl. "hello")
    # Build available_expiries from portfolio expiries + Deribit if possible
    _fallback_expiries = sorted(set(expiry_groups.keys()))
    try:
        _fb_summaries = await client._get("get_book_summary_by_currency", {"currency": "ETH", "kind": "option"})
        _fb_exp_set = set()
        _today_fb = date.today()
        for _fbs in _fb_summaries:
            _fbp = _fbs.get("instrument_name", "").split("-")
            if len(_fbp) == 4 and _fbp[0] == "ETH":
                try:
                    _fbd = datetime.strptime(_fbp[1], "%d%b%y").date()
                    if (_fbd - _today_fb).days >= 7:
                        _fb_exp_set.add(_fbp[1])
                except ValueError:
                    pass
        if _fb_exp_set:
            _fallback_expiries = sorted(_fb_exp_set, key=lambda x: datetime.strptime(x, "%d%b%y"))
    except Exception:
        pass

    if not result_data:
        result_data = {
            "suggestions": [],
            "num_suggestions": 0,
            "spot_ladder": spot_arr.tolist(),
            "current_payoff": np.round(current_payoff, 2).tolist(),
            "current_profile": {
                "at_spot": pnl_at_spot, "min": worst, "min_at": worst_at,
                "max": best, "max_at": float(spot_arr[np.argmax(current_payoff)]) if len(current_payoff) > 0 else 0,
                "breakeven": breakeven,
            },
            "positions": positions_detail,
            "spot": spot,
            "available_expiries": _fallback_expiries,
        }
    # Ensure available_expiries exists on all response paths
    if "available_expiries" not in result_data:
        result_data["available_expiries"] = _fallback_expiries
    resp = {"type": "suggestions", "text": text, "context": context, "data": result_data}
    return resp


@router.get("/expiries")
async def get_available_expiries():
    try:
        summaries = await client._get("get_book_summary_by_currency", {
            "currency": "ETH", "kind": "option",
        })
    except Exception as e:
        raise HTTPException(502, f"Deribit error: {e}")

    today = date.today()
    expiries: dict[str, int] = {}
    for s in summaries:
        name = s.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "ETH":
            continue
        try:
            exp_date = datetime.strptime(parts[1], "%d%b%y").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte >= MIN_DTE:
            expiries[parts[1]] = dte

    return sorted(
        [{"code": k, "dte": v, "date": datetime.strptime(k, "%d%b%y").date().isoformat()} for k, v in expiries.items()],
        key=lambda x: x["dte"],
    )
