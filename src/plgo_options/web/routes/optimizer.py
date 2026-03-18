"""Portfolio optimizer API — interprets user requests and generates trade suggestions."""

from __future__ import annotations

import re
import time
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
import numpy as np

from plgo_options.data.database import get_db
from plgo_options.data.trade_repository import list_trades
from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.config import DERIBIT_BASE_URL, REQUEST_TIMEOUT

router = APIRouter()
client = DeribitClient()

MIN_DTE = 7
SPOT_STEP = 50


class OptimizeRequest(BaseModel):
    query: str = ""                     # user's natural language request
    budget: float = 15000
    base_qty: float = 0
    min_dte: int = 7
    max_spread_pct: float = 40
    target_expiry: str | None = None


class CalculateRequest(BaseModel):
    """Re-calculate costs for edited workbench legs."""
    legs: list[dict]  # [{instrument, side, qty, strike, opt, expiry_code}]


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _payoff_vec(spot_arr, strike, opt, qty):
    if opt == "C":
        return qty * np.maximum(spot_arr - strike, 0.0)
    return qty * np.maximum(strike - spot_arr, 0.0)


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

    positions = []
    positions_display = []
    for t in db_trades:
        side = str(t["side"]).lower()
        opt_raw = str(t["option_type"]).lower()
        opt = "C" if "call" in opt_raw else "P"
        sign = 1.0 if side in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        if strike <= 0 or qty <= 0:
            continue
        positions.append({"opt": opt, "strike": strike, "net_qty": sign * qty})
        positions_display.append({
            "id": t["id"], "counterparty": t.get("counterparty", ""),
            "side": t["side"], "opt": opt, "strike": strike,
            "expiry": t["expiry"], "net_qty": sign * qty, "qty": qty,
            "premium_usd": _safe_float(t.get("premium_usd")),
        })

    # 4. Spot ladder & current payoff
    lo = max(500, int(spot * 0.2))
    hi = int(spot * 3.5)
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for p in positions:
        current_payoff += _payoff_vec(spot_arr, p["strike"], p["opt"], p["net_qty"])

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
                current_payoff, spot_idx, breakeven, objective, zone_lo, zone_hi))

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
                      current_payoff, spot_idx, current_be, objective, zone_lo, zone_hi):
    """Build and score a suggestion."""
    candidate = np.zeros_like(spot_arr)
    for leg in legs:
        inst = leg["inst"]
        sign = 1.0 if leg["side"] == "buy" else -1.0
        if inst["opt"] == "C":
            candidate += sign * leg["qty"] * np.maximum(spot_arr - inst["strike"], 0.0)
        else:
            candidate += sign * leg["qty"] * np.maximum(inst["strike"] - spot_arr, 0.0)

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

    return {
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

    # Current payoff
    lo = max(500, int(spot * 0.2))
    hi = int(spot * 3.5)
    spot_arr = np.arange(lo, hi + SPOT_STEP, SPOT_STEP, dtype=float)
    current_payoff = np.zeros_like(spot_arr)
    for t in db_trades:
        side = str(t["side"]).lower()
        opt = "C" if "call" in str(t["option_type"]).lower() else "P"
        sign = 1.0 if side in ("buy", "long") else -1.0
        strike = _safe_float(t["strike"])
        qty = _safe_float(t["qty"])
        if strike > 0 and qty > 0:
            current_payoff += _payoff_vec(spot_arr, strike, opt, sign * qty)

    # Workbench payoff
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
        inst_name = leg.get("instrument", "")
        side = leg.get("side", "buy")
        qty = float(leg.get("qty", 0))
        strike = float(leg.get("strike", 0))
        opt = leg.get("opt", "C")
        sign = 1.0 if side == "buy" else -1.0

        # Get live price
        mkt = summaries.get(inst_name, {})
        bid = mkt.get("bid_price") or 0
        ask = mkt.get("ask_price") or 0
        price_eth = ask if side == "buy" else bid

        leg_cost = sign * price_eth * qty * spot
        total_cost += leg_cost
        leg_costs.append({
            "instrument": inst_name, "side": side, "qty": qty,
            "strike": strike, "opt": opt,
            "bid_usd": round(bid * spot, 2), "ask_usd": round(ask * spot, 2),
            "price_usd": round(price_eth * spot, 2),
            "leg_cost": round(leg_cost, 2),
            "spread_pct": round((ask - bid) / ask * 100, 1) if ask > 0 else 0,
            "mark_iv": mkt.get("mark_iv"),
        })

        if strike > 0 and qty > 0:
            wb_payoff += _payoff_vec(spot_arr, strike, opt, sign * qty)

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
