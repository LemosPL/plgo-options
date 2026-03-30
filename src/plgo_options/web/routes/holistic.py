"""Holistic Portfolio Optimizer — one-click full optimization.

Phase 1: Run SLSQP optimizer (all maturities) for mathematical optimal.
Phase 2: Filter noise, categorize trades (unwinds, rolls, new positions),
         and call Claude for an executive summary.
"""

from __future__ import annotations

import traceback

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plgo_options.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from plgo_options.optimization.snapshot import save_snapshot
from plgo_options.optimization.optimizer import OptimizerV2
from plgo_options.web.routes.portfolio import portfolio_pnl

router = APIRouter()


class HolisticParams(BaseModel):
    risk_aversion: float = 1.0
    txn_cost_pct: float = 5.0
    max_collateral: float = 4_000_000.0
    min_qty: int = 50  # filter out noise — trades below this qty are dropped
    min_dte: int = 14  # minimum days to expiry — ignore near-expiry instruments


# ---------------------------------------------------------------------------
# Filter tiny trades that are just optimizer noise
# ---------------------------------------------------------------------------

def _filter_trades(
    trades: list[dict],
    min_qty: int = 1,
    min_dte: int = 0,
) -> list[dict]:
    """Remove trades that are noise: below min_qty or below min_dte."""
    result = []
    for t in trades:
        if abs(t.get("qty", 0)) < min_qty:
            continue
        # Filter by DTE — only for NEW trades (not unwinds of existing positions)
        if min_dte > 0 and not t.get("is_unwind") and (t.get("dte", 999) < min_dte):
            continue
        result.append(t)
    return result


# ---------------------------------------------------------------------------
# Roll detection: pair unwind trades with matching new trades
# ---------------------------------------------------------------------------

def _detect_rolls(trades: list[dict], eth_spot: float) -> dict:
    """Categorize SLSQP trades into unwinds, rolls, new_positions.

    Roll detection: an unwind paired with a new trade when they share
    opt type, similar strike (within 20% of spot), and different expiry.
    """
    unwinds = [t for t in trades if t.get("is_unwind")]
    new_trades = [t for t in trades if not t.get("is_unwind")]

    paired_unwinds: set[int] = set()
    paired_new: set[int] = set()
    rolls: list[dict] = []
    strike_tolerance = eth_spot * 0.20

    for ui, u in enumerate(unwinds):
        if ui in paired_unwinds:
            continue
        best_match = None
        best_score = float("inf")

        for ni, n in enumerate(new_trades):
            if ni in paired_new:
                continue
            if u["opt"] != n["opt"]:
                continue
            if u.get("expiry") == n.get("expiry"):
                continue
            strike_diff = abs(u["strike"] - n["strike"])
            if strike_diff > strike_tolerance:
                continue
            u_sign = 1 if u["qty"] > 0 else -1
            n_sign = 1 if n["qty"] > 0 else -1
            if u_sign != n_sign:
                continue
            if strike_diff < best_score:
                best_score = strike_diff
                best_match = ni

        if best_match is not None:
            paired_unwinds.add(ui)
            paired_new.add(best_match)
            close_trade = unwinds[ui]
            open_trade = new_trades[best_match]
            close_cost = close_trade.get("trade_cost", 0)
            open_cost = open_trade.get("trade_cost", 0)
            rolls.append({
                "close": close_trade,
                "open": open_trade,
                "net_cost": round(close_cost + open_cost, 2),
            })

    pure_unwinds = [unwinds[i] for i in range(len(unwinds)) if i not in paired_unwinds]
    new_positions = [new_trades[i] for i in range(len(new_trades)) if i not in paired_new]

    return {
        "unwinds": pure_unwinds,
        "rolls": rolls,
        "new_positions": new_positions,
    }


# ---------------------------------------------------------------------------
# Claude summary generation
# ---------------------------------------------------------------------------

def _generate_summary(
    actions: dict,
    before: dict,
    after: dict,
    eth_spot: float,
    total_cost: float,
) -> str:
    """Call Claude for an executive summary of the optimization result."""
    try:
        import anthropic
    except ImportError:
        return "Optimization complete. Install `anthropic` for AI-generated summary."

    if not ANTHROPIC_API_KEY:
        return "Optimization complete. Set ANTHROPIC_API_KEY for AI-generated summary."

    n_unwinds = len(actions["unwinds"])
    n_rolls = len(actions["rolls"])
    n_new = len(actions["new_positions"])

    trade_summary_lines = []
    for u in actions["unwinds"]:
        trade_summary_lines.append(
            f"  UNWIND: {u['side']} {abs(u['qty']):,} {u['instrument']} "
            f"(cost ${u.get('trade_cost', 0):,.0f})"
        )
    for r in actions["rolls"]:
        c, o = r["close"], r["open"]
        trade_summary_lines.append(
            f"  ROLL: Close {abs(c['qty']):,} {c['instrument']} -> "
            f"Open {abs(o['qty']):,} {o['instrument']} "
            f"(net cost ${r['net_cost']:,.0f})"
        )
    for n in actions["new_positions"]:
        trade_summary_lines.append(
            f"  NEW: {n['side']} {abs(n['qty']):,} {n['instrument']} "
            f"(cost ${n.get('trade_cost', 0):,.0f})"
        )

    trade_block = "\n".join(trade_summary_lines) if trade_summary_lines else "  (no trades proposed)"

    prompt = f"""You are analyzing an ETH options portfolio optimization result for an institutional-scale portfolio.

Current ETH spot: ${eth_spot:,.2f}

BEFORE optimization:
  Delta: {before['delta']:.2f}, Gamma: {before['gamma']:.4f}, Theta: {before['theta']:.2f}, Vega: {before['vega']:.2f}
  Daily Risk (1-sigma): ${before['daily_risk']:,.0f}

AFTER optimization:
  Delta: {after['delta']:.2f}, Gamma: {after['gamma']:.4f}, Theta: {after['theta']:.2f}, Vega: {after['vega']:.2f}
  Daily Risk (1-sigma): ${after['daily_risk']:,.0f}

Risk reduction: ${before['daily_risk'] - after['daily_risk']:,.0f}
Total trade cost: ${total_cost:,.0f}

Proposed actions ({n_unwinds} unwinds, {n_rolls} rolls, {n_new} new positions):
{trade_block}

Provide a concise executive summary (3-5 sentences) explaining:
1. The overall optimization strategy and what it achieves
2. The key risk changes (which greeks improved most)
3. Whether the cost-benefit tradeoff looks favorable

Be direct and professional. Focus on WHY these trades make sense, not just WHAT they are. Use dollar values where helpful."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        return (
            f"Optimization complete ({n_unwinds} unwinds, {n_rolls} rolls, "
            f"{n_new} new positions). AI summary unavailable: {e}"
        )


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/run")
async def run_holistic(params: HolisticParams):
    """Run holistic portfolio optimization: SLSQP + categorization + Claude summary."""

    # 1. Gather portfolio data
    try:
        pnl_data = await portfolio_pnl()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to gather portfolio data: {e}")

    # 2. Save snapshot
    try:
        path = save_snapshot(pnl_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save snapshot: {e}")

    # 3. Run SLSQP optimizer (all maturities)
    try:
        optimizer = OptimizerV2.from_snapshot(path)
        slsqp_result = optimizer.run(
            risk_aversion=params.risk_aversion,
            txn_cost_pct=params.txn_cost_pct,
            max_collateral=params.max_collateral,
            target_expiry=None,  # all maturities
            min_dte=params.min_dte,
        )
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        raise HTTPException(status_code=500, detail=f"Optimization failed: {e}")

    if slsqp_result.get("status") != "ok":
        return slsqp_result

    # 4. Categorize trades first, then filter noise from each category
    eth_spot = slsqp_result["eth_spot"]
    trades = slsqp_result.get("trades", [])
    actions = _detect_rolls(trades, eth_spot)

    # 5. Apply min_qty filter to each category (including both legs of rolls)
    mq = params.min_qty
    actions["unwinds"] = _filter_trades(actions["unwinds"], mq)
    actions["new_positions"] = _filter_trades(actions["new_positions"], mq)
    actions["rolls"] = [
        r for r in actions["rolls"]
        if abs(r["close"].get("qty", 0)) >= mq or abs(r["open"].get("qty", 0)) >= mq
    ]

    # 6. Generate Claude summary
    summary = _generate_summary(
        actions=actions,
        before=slsqp_result["before"],
        after=slsqp_result["after"],
        eth_spot=eth_spot,
        total_cost=slsqp_result.get("total_trade_cost", 0),
    )

    # 7. Build response
    return {
        "status": "ok",
        "executive_summary": summary,
        "eth_spot": eth_spot,
        "spot_ladder": slsqp_result["spot_ladder"],
        "chart_horizons": slsqp_result["chart_horizons"],
        "before": slsqp_result["before"],
        "after": slsqp_result["after"],
        "actions": actions,
        "total_trade_cost": slsqp_result["total_trade_cost"],
        "risk_reduction": round(
            slsqp_result["before"]["daily_risk"] - slsqp_result["after"]["daily_risk"], 2
        ),
        "utility_improvement": slsqp_result["utility_improvement"],
        "candidates_evaluated": slsqp_result["candidates_evaluated"],
        "optimizer_converged": slsqp_result["optimizer_converged"],
        "params": slsqp_result["params"],
    }
