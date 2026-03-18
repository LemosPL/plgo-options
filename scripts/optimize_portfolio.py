"""
Portfolio Optimizer -- ETH Options
=================================
Analyzes the current ETH portfolio payoff curve and searches for zero-cost
(or near-zero-cost) trade adjustments that improve the payoff profile.

Uses live Deribit bid/ask prices so suggestions are executable.

Usage:
    python scripts/optimize_portfolio.py [--budget 10000] [--expiry 2026-05-29]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# Fix Windows console encoding
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np

# Allow importing from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plgo_options.market_data.deribit_client import DeribitClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "plgo_options.db"
SPOT_RANGE = np.arange(500, 7100, 50, dtype=float)
DEFAULT_BUDGET = 10_000
MIN_DTE = 7  # ignore instruments expiring in < 7 days


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Position:
    trade_id: int
    side: str
    option_type: str
    strike: float
    expiry: str
    qty: float
    premium_usd: float

    @property
    def sign(self) -> float:
        return 1.0 if self.side.lower() in ("buy", "long") else -1.0

    @property
    def net_qty(self) -> float:
        return self.sign * self.qty

    @property
    def opt(self) -> str:
        return "C" if "call" in self.option_type.lower() else "P"

    @property
    def expiry_date(self) -> date:
        return date.fromisoformat(self.expiry.split("T")[0])


@dataclass
class DeribitInstrument:
    name: str
    expiry_code: str
    expiry_date: date
    strike: float
    opt: str
    bid: float | None
    ask: float | None
    mark: float | None
    mark_iv: float | None
    delta: float | None

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.mark

    @property
    def spread_pct(self) -> float:
        if self.bid and self.ask and self.ask > 0:
            return (self.ask - self.bid) / self.ask * 100
        return 100.0

    @property
    def dte(self) -> int:
        return max((self.expiry_date - date.today()).days, 0)


@dataclass
class TradeCandidate:
    instrument: DeribitInstrument
    side: str
    qty: float

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "buy" else -1.0

    @property
    def cost_per_eth(self) -> float:
        if self.side == "buy":
            return self.instrument.ask or self.instrument.mark or 0.0
        else:
            return -(self.instrument.bid or self.instrument.mark or 0.0)

    def cost_usd(self, eth_spot: float) -> float:
        return self.cost_per_eth * self.qty * eth_spot


@dataclass
class Suggestion:
    name: str
    category: str  # "spread", "risk_reversal", "collar", "put_protection", "roll"
    trades: list[TradeCandidate]
    net_cost_usd: float
    improvement_score: float
    details: dict = field(default_factory=dict)

    def format(self, rank: int) -> str:
        eth_spot = self.details.get("eth_spot", 1)
        lines = [f"\n  #{rank}  [{self.category.upper()}]"]
        lines.append(f"  {'-'*66}")
        lines.append(f"  {self.name}")
        lines.append(f"  Net cost: ${self.net_cost_usd:+,.0f}  |  Score: {self.improvement_score:+,.0f}")
        lines.append(f"  {'-'*66}")
        for t in self.trades:
            price_usd = abs(t.cost_per_eth) * eth_spot
            spread_info = f"(spread: {t.instrument.spread_pct:.0f}%)" if t.instrument.spread_pct < 100 else ""
            lines.append(
                f"    {t.side.upper():4s}  {t.qty:>6,.0f}x  {t.instrument.name:<28s}"
                f"  ${price_usd:>8,.2f}/ct  {spread_info}"
            )
        if self.details.get("impact"):
            lines.append(f"")
            for k, v in self.details["impact"].items():
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Payoff computation
# ---------------------------------------------------------------------------

def compute_payoff(spot: np.ndarray, positions: list[Position]) -> np.ndarray:
    total = np.zeros_like(spot, dtype=float)
    for p in positions:
        if p.opt == "C":
            intrinsic = np.maximum(spot - p.strike, 0.0)
        else:
            intrinsic = np.maximum(p.strike - spot, 0.0)
        total += p.net_qty * intrinsic
    return total


def compute_candidate_payoff(spot: np.ndarray, trades: list[TradeCandidate]) -> np.ndarray:
    total = np.zeros_like(spot, dtype=float)
    for t in trades:
        if t.instrument.opt == "C":
            intrinsic = np.maximum(spot - t.instrument.strike, 0.0)
        else:
            intrinsic = np.maximum(t.instrument.strike - spot, 0.0)
        total += t.sign * t.qty * intrinsic
    return total


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_improvement(
    current_payoff: np.ndarray,
    new_payoff: np.ndarray,
    spot: np.ndarray,
    eth_spot: float,
) -> dict:
    diff = new_payoff - current_payoff

    # Probability-weighted: log-normal centered on spot, ~60% annual vol
    # (higher vol to give more weight to tail scenarios)
    sigma_weight = 0.60
    log_spots = np.log(spot / eth_spot)
    weights = np.exp(-0.5 * (log_spots / sigma_weight) ** 2)
    weights /= weights.sum()

    # Zone masks
    deep_down = spot < eth_spot * 0.7
    near_down = (spot >= eth_spot * 0.7) & (spot < eth_spot * 0.95)
    around_spot = (spot >= eth_spot * 0.95) & (spot <= eth_spot * 1.05)
    near_up = (spot > eth_spot * 1.05) & (spot <= eth_spot * 1.5)
    far_up = spot > eth_spot * 1.5

    min_current = float(current_payoff.min())
    min_new = float(new_payoff.min())

    # Payoff at current spot
    at_spot_idx = np.argmin(np.abs(spot - eth_spot))
    at_spot_current = float(current_payoff[at_spot_idx])
    at_spot_new = float(new_payoff[at_spot_idx])
    at_spot_improvement = at_spot_new - at_spot_current

    # Weighted expected improvement
    expected_improvement = float(np.sum(diff * weights))

    # Zone improvements (probability-weighted within each zone)
    def zone_improvement(mask):
        if not mask.any():
            return 0.0
        zw = weights[mask] / weights[mask].sum()
        return float(np.sum(diff[mask] * zw))

    deep_down_imp = zone_improvement(deep_down)
    near_down_imp = zone_improvement(near_down)
    around_spot_imp = zone_improvement(around_spot)
    near_up_imp = zone_improvement(near_up)
    far_up_imp = zone_improvement(far_up)

    # Find breakeven improvement
    current_be = None
    new_be = None
    for i in range(len(spot) - 1):
        if current_payoff[i] <= 0 and current_payoff[i+1] > 0 and current_be is None:
            current_be = float(spot[i])
        if new_payoff[i] <= 0 and new_payoff[i+1] > 0 and new_be is None:
            new_be = float(spot[i])

    # Composite score: heavily weight the problem zones
    # The portfolio is negative below ~$3700, so improvement near spot and
    # in the $1500-$3500 range is most valuable
    composite = (
        0.10 * (min_new - min_current) +          # raise floor
        0.30 * at_spot_improvement +               # improve at current spot
        0.25 * near_down_imp +                     # improve near downside
        0.20 * expected_improvement +              # overall expected
        0.10 * around_spot_imp +                   # around current spot
        0.05 * near_up_imp                         # near upside
        # far upside not weighted (portfolio already benefits there)
    )

    return {
        "composite": composite,
        "min_improvement": min_new - min_current,
        "at_spot_improvement": at_spot_improvement,
        "expected_improvement": expected_improvement,
        "deep_down_imp": deep_down_imp,
        "near_down_imp": near_down_imp,
        "around_spot_imp": around_spot_imp,
        "near_up_imp": near_up_imp,
        "far_up_imp": far_up_imp,
        "current_min": min_current,
        "new_min": min_new,
        "current_be": current_be,
        "new_be": new_be,
        "at_spot_current": at_spot_current,
        "at_spot_new": at_spot_new,
    }


# ---------------------------------------------------------------------------
# Strategy generators
# ---------------------------------------------------------------------------

def generate_strategies(
    instruments: list[DeribitInstrument],
    eth_spot: float,
    budget: float,
    base_qty: float,
) -> list[Suggestion]:
    suggestions = []

    by_expiry: dict[str, list[DeribitInstrument]] = {}
    for inst in instruments:
        by_expiry.setdefault(inst.expiry_code, []).append(inst)

    for exp_code, insts in by_expiry.items():
        calls = sorted([i for i in insts if i.opt == "C" and i.bid and i.ask], key=lambda x: x.strike)
        puts = sorted([i for i in insts if i.opt == "P" and i.bid and i.ask], key=lambda x: x.strike)

        dte = insts[0].dte if insts else 0

        # === 1. Bull Call Spreads ===
        # Buy ATM/slightly OTM call, sell higher call -- adds upside participation
        for i, buy_c in enumerate(calls):
            if buy_c.strike < eth_spot * 0.8 or buy_c.strike > eth_spot * 1.5:
                continue
            for sell_c in calls[i+1:]:
                if sell_c.strike - buy_c.strike < 200:
                    continue
                if sell_c.strike > eth_spot * 3:
                    continue
                cost = (buy_c.ask - sell_c.bid) * base_qty * eth_spot
                if abs(cost) <= budget:
                    suggestions.append(Suggestion(
                        name=f"Bull Call {exp_code}: +{int(buy_c.strike)}C / -{int(sell_c.strike)}C",
                        category="spread",
                        trades=[TradeCandidate(buy_c, "buy", base_qty), TradeCandidate(sell_c, "sell", base_qty)],
                        net_cost_usd=cost, improvement_score=0,
                    ))

        # === 2. Bear Put Spreads ===
        # Buy put near/above spot, sell lower put -- adds downside protection
        for i, sell_p in enumerate(puts):
            if sell_p.strike < eth_spot * 0.4:
                continue
            for buy_p in puts[i+1:]:
                if buy_p.strike - sell_p.strike < 200:
                    continue
                if buy_p.strike > eth_spot * 1.5:
                    continue
                cost = (buy_p.ask - sell_p.bid) * base_qty * eth_spot
                if abs(cost) <= budget:
                    suggestions.append(Suggestion(
                        name=f"Bear Put {exp_code}: +{int(buy_p.strike)}P / -{int(sell_p.strike)}P",
                        category="spread",
                        trades=[TradeCandidate(buy_p, "buy", base_qty), TradeCandidate(sell_p, "sell", base_qty)],
                        net_cost_usd=cost, improvement_score=0,
                    ))

        # === 3. Risk Reversals (bullish: sell OTM put, buy OTM call) ===
        for sell_p in puts:
            if not sell_p.bid or sell_p.strike > eth_spot * 0.90:
                continue
            for buy_c in calls:
                if not buy_c.ask or buy_c.strike < eth_spot * 1.10:
                    continue
                if buy_c.strike > eth_spot * 2.5:
                    continue
                cost = (buy_c.ask - sell_p.bid) * base_qty * eth_spot
                if abs(cost) <= budget:
                    suggestions.append(Suggestion(
                        name=f"Risk Rev {exp_code}: +{int(buy_c.strike)}C / -{int(sell_p.strike)}P",
                        category="risk_reversal",
                        trades=[TradeCandidate(buy_c, "buy", base_qty), TradeCandidate(sell_p, "sell", base_qty)],
                        net_cost_usd=cost, improvement_score=0,
                    ))

        # === 4. Sell far OTM call to fund put purchase (downside protection) ===
        # This directly addresses the -$19M at current spot
        for sell_c in calls:
            if not sell_c.bid or sell_c.strike < eth_spot * 1.8:
                continue
            for buy_p in puts:
                if not buy_p.ask or buy_p.strike > eth_spot * 1.05 or buy_p.strike < eth_spot * 0.5:
                    continue
                cost = (buy_p.ask - sell_c.bid) * base_qty * eth_spot
                if abs(cost) <= budget:
                    suggestions.append(Suggestion(
                        name=f"Sell Call Buy Put {exp_code}: +{int(buy_p.strike)}P / -{int(sell_c.strike)}C",
                        category="put_protection",
                        trades=[TradeCandidate(buy_p, "buy", base_qty), TradeCandidate(sell_c, "sell", base_qty)],
                        net_cost_usd=cost, improvement_score=0,
                    ))

        # === 5. Sell call spread to fund put (3-leg) ===
        for sell_c in calls:
            if not sell_c.bid or sell_c.strike < eth_spot * 1.5:
                continue
            for buy_c_wing in calls:
                if buy_c_wing.strike <= sell_c.strike or not buy_c_wing.ask:
                    continue
                if buy_c_wing.strike > sell_c.strike * 1.5:
                    continue
                credit = sell_c.bid - buy_c_wing.ask
                if credit <= 0:
                    continue
                # Use credit to buy a put
                for buy_p in puts:
                    if not buy_p.ask or buy_p.strike > eth_spot * 1.1 or buy_p.strike < eth_spot * 0.5:
                        continue
                    cost = (buy_p.ask - credit) * base_qty * eth_spot
                    if abs(cost) <= budget:
                        suggestions.append(Suggestion(
                            name=f"Call Spread + Put {exp_code}: +{int(buy_p.strike)}P / -{int(sell_c.strike)}C / +{int(buy_c_wing.strike)}C",
                            category="collar",
                            trades=[
                                TradeCandidate(buy_p, "buy", base_qty),
                                TradeCandidate(sell_c, "sell", base_qty),
                                TradeCandidate(buy_c_wing, "buy", base_qty),
                            ],
                            net_cost_usd=cost, improvement_score=0,
                        ))

        # === 6. Put ratio spread (buy 1 ATM put, sell 2 OTM puts) ===
        for buy_p in puts:
            if not buy_p.ask or buy_p.strike < eth_spot * 0.85 or buy_p.strike > eth_spot * 1.1:
                continue
            for sell_p in puts:
                if not sell_p.bid or sell_p.strike >= buy_p.strike - 200:
                    continue
                if sell_p.strike < eth_spot * 0.4:
                    continue
                # Buy 1, sell 2 (ratio)
                cost = (buy_p.ask - 2 * sell_p.bid) * base_qty * eth_spot
                if abs(cost) <= budget:
                    suggestions.append(Suggestion(
                        name=f"Put Ratio {exp_code}: +{int(buy_p.strike)}P / -2x{int(sell_p.strike)}P",
                        category="spread",
                        trades=[
                            TradeCandidate(buy_p, "buy", base_qty),
                            TradeCandidate(sell_p, "sell", base_qty * 2),
                        ],
                        net_cost_usd=cost, improvement_score=0,
                    ))

    return suggestions


def generate_roll_suggestions(
    positions: list[Position],
    instruments: list[DeribitInstrument],
    eth_spot: float,
    budget: float,
) -> list[Suggestion]:
    suggestions = []

    inst_by_key: dict[tuple[str, float, str], DeribitInstrument] = {}
    for inst in instruments:
        inst_by_key[(inst.expiry_code, inst.strike, inst.opt)] = inst

    by_expiry: dict[str, list[DeribitInstrument]] = {}
    for inst in instruments:
        by_expiry.setdefault(inst.expiry_code, []).append(inst)

    for pos in positions:
        if abs(pos.net_qty) < 500:
            continue

        exp_date = pos.expiry_date
        exp_code = f"{exp_date.day}{exp_date.strftime('%b').upper()}{exp_date.strftime('%y')}"

        current_inst = inst_by_key.get((exp_code, pos.strike, pos.opt))
        if not current_inst or not current_inst.bid or not current_inst.ask:
            continue

        close_side = "sell" if pos.sign > 0 else "buy"
        open_side = "buy" if pos.sign > 0 else "sell"
        roll_qty = min(abs(pos.net_qty), 500)

        close_price = current_inst.bid if close_side == "sell" else current_inst.ask

        for target_exp, target_insts in by_expiry.items():
            for target in target_insts:
                if target.opt != pos.opt or not target.bid or not target.ask:
                    continue
                if target.strike == pos.strike and target.expiry_code == exp_code:
                    continue
                # Only roll to meaningful differences
                strike_diff = abs(target.strike - pos.strike)
                if strike_diff < 100 and target.expiry_code == exp_code:
                    continue

                open_price = target.ask if open_side == "buy" else target.bid
                net_cost = (open_price - close_price) * roll_qty * eth_spot
                if pos.sign < 0:
                    net_cost = -net_cost

                if abs(net_cost) > budget:
                    continue

                side_label = "Long" if pos.sign > 0 else "Short"
                suggestions.append(Suggestion(
                    name=f"Roll {side_label} {pos.opt} {int(pos.strike)} {exp_code} -> {int(target.strike)} {target.expiry_code}",
                    category="roll",
                    trades=[
                        TradeCandidate(current_inst, close_side, roll_qty),
                        TradeCandidate(target, open_side, roll_qty),
                    ],
                    net_cost_usd=net_cost, improvement_score=0,
                ))

    return suggestions


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def load_eth_positions(expiry_filter: str | None = None) -> list[Position]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = "SELECT * FROM trades WHERE status='active' AND asset='ETH'"
    params = []
    if expiry_filter:
        sql += " AND expiry = ?"
        params.append(expiry_filter)
    sql += " ORDER BY expiry, strike"

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    positions = []
    for r in rows:
        positions.append(Position(
            trade_id=r["id"],
            side=r["side"],
            option_type=r["option_type"],
            strike=r["strike"],
            expiry=r["expiry"],
            qty=r["qty"],
            premium_usd=r["premium_usd"] or 0.0,
        ))
    return positions


# ---------------------------------------------------------------------------
# Deribit data fetch
# ---------------------------------------------------------------------------

async def fetch_deribit_instruments(client: DeribitClient) -> list[DeribitInstrument]:
    summaries = await client._get("get_book_summary_by_currency", {
        "currency": "ETH",
        "kind": "option",
    })

    instruments = []
    today = date.today()
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
        if dte < MIN_DTE:
            continue

        strike = float(parts[2])
        opt = parts[3]

        bid = s.get("bid_price")
        ask = s.get("ask_price")
        mark = s.get("mark_price")
        mark_iv = s.get("mark_iv")

        instruments.append(DeribitInstrument(
            name=name,
            expiry_code=parts[1],
            expiry_date=exp_date,
            strike=strike,
            opt=opt,
            bid=bid if bid and bid > 0 else None,
            ask=ask if ask and ask > 0 else None,
            mark=mark if mark and mark > 0 else None,
            mark_iv=mark_iv,
            delta=None,
        ))

    return instruments


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

async def run_analysis(budget: float, expiry_filter: str | None, top_n: int = 15):
    client = DeribitClient()

    print("=" * 70)
    print("  ETH PORTFOLIO OPTIMIZER")
    print("=" * 70)

    print("\nFetching ETH spot price...")
    eth_spot = await client.get_eth_spot_price()
    print(f"  ETH Spot: ${eth_spot:,.2f}")

    # Load positions
    print("\nLoading ETH positions from database...")
    positions = load_eth_positions(expiry_filter)
    if not positions:
        print("  No active ETH positions found!")
        return

    # Summarize
    expiries = sorted(set(p.expiry for p in positions))
    print(f"  {len(positions)} legs across {len(expiries)} expiries:")
    for exp in expiries:
        exp_pos = [p for p in positions if p.expiry == exp]
        net_calls = sum(p.net_qty for p in exp_pos if p.opt == "C")
        net_puts = sum(p.net_qty for p in exp_pos if p.opt == "P")
        total_prem = sum(p.premium_usd for p in exp_pos)
        print(f"    {exp}: {len(exp_pos)} legs | net C={net_calls:+,.0f} | net P={net_puts:+,.0f} | prem=${total_prem:+,.0f}")

    # Current payoff
    spot = SPOT_RANGE
    current_payoff = compute_payoff(spot, positions)

    print(f"\n  CURRENT PAYOFF PROFILE (at expiry, intrinsic only):")
    print(f"    {'Spot':>8}  {'Payoff':>14}  {'Bar'}")
    print(f"    {'-'*8}  {'-'*14}  {'-'*40}")

    key_spots = [500, 1000, 1500, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200, 3500, 3800, 4000, 4500, 5000, 6000]
    max_abs = max(abs(current_payoff.max()), abs(current_payoff.min()))
    scale = 35 / max_abs if max_abs > 0 else 1

    for ks in key_spots:
        idx = np.argmin(np.abs(spot - ks))
        pnl = current_payoff[idx]
        bar_len = int(abs(pnl) * scale)
        if pnl >= 0:
            bar = " " * 15 + "+" * bar_len
        else:
            pad = max(15 - bar_len, 0)
            bar = " " * pad + "-" * bar_len + "|"
        marker = "  <-- YOU ARE HERE" if abs(ks - eth_spot) < 100 else ""
        print(f"    ${ks:>6,}  ${pnl:>13,.0f}  {bar}{marker}")

    min_idx = np.argmin(current_payoff)
    print(f"\n    Worst:  ${current_payoff.min():>13,.0f} at spot=${spot[min_idx]:,.0f}")
    print(f"    Best:   ${current_payoff.max():>13,.0f} at spot=${spot[np.argmax(current_payoff)]:,.0f}")

    # Find breakeven
    for i in range(len(spot) - 1):
        if current_payoff[i] <= 0 and current_payoff[i+1] > 0:
            be = spot[i] + (spot[i+1] - spot[i]) * (-current_payoff[i]) / (current_payoff[i+1] - current_payoff[i])
            print(f"    Breakeven: ~${be:,.0f}")
            break

    # Fetch Deribit instruments
    print(f"\nFetching Deribit instruments (DTE >= {MIN_DTE})...")
    deribit_instruments = await fetch_deribit_instruments(client)
    liquid = [i for i in deribit_instruments if i.bid and i.ask and i.spread_pct < 40]

    # Filter to strikes in a useful range (40% - 300% of spot)
    liquid = [i for i in liquid if eth_spot * 0.4 <= i.strike <= eth_spot * 3.0]

    exp_codes = sorted(set(i.expiry_code for i in liquid), key=lambda x: datetime.strptime(x, "%d%b%y"))
    print(f"  {len(liquid)} liquid instruments across {len(exp_codes)} expiries")
    print(f"  Expiries: {', '.join(exp_codes[:10])}{'...' if len(exp_codes) > 10 else ''}")

    # Filter to specific expiry if requested
    if expiry_filter:
        exp_date = date.fromisoformat(expiry_filter)
        exp_code = f"{exp_date.day}{exp_date.strftime('%b').upper()}{exp_date.strftime('%y')}"
        liquid = [i for i in liquid if i.expiry_code == exp_code]
        print(f"  Filtered to {len(liquid)} instruments for {exp_code}")

    # Base qty proportional to portfolio
    avg_qty = np.mean([abs(p.net_qty) for p in positions])
    base_qty = max(100, min(round(avg_qty / 5 / 100) * 100, 2000))
    print(f"  Base qty per leg: {base_qty:,.0f}")

    # Generate candidates
    print(f"\nGenerating strategies (budget: ${budget:,.0f})...")

    all_suggestions: list[Suggestion] = []

    # New structures
    new_strats = generate_strategies(liquid, eth_spot, budget, base_qty)
    by_cat = {}
    for s in new_strats:
        by_cat[s.category] = by_cat.get(s.category, 0) + 1
    print(f"  New structures: {len(new_strats)} " + " | ".join(f"{k}:{v}" for k, v in by_cat.items()))
    all_suggestions.extend(new_strats)

    # Rolls (limited)
    large_pos = [p for p in positions if abs(p.net_qty) >= 500]
    # Only try rolling with instruments from portfolio expiries (or close)
    portfolio_expiries = set()
    for p in positions:
        ed = p.expiry_date
        portfolio_expiries.add(f"{ed.day}{ed.strftime('%b').upper()}{ed.strftime('%y')}")
    roll_instruments = [i for i in liquid if i.expiry_code in portfolio_expiries or i.dte <= 90]

    if len(large_pos) <= 15 and len(roll_instruments) <= 150:
        roll_strats = generate_roll_suggestions(large_pos, roll_instruments, eth_spot, budget)
        print(f"  Rolls: {len(roll_strats)} candidates")
        all_suggestions.extend(roll_strats)
    else:
        print(f"  Skipping rolls ({len(large_pos)} positions x {len(roll_instruments)} instruments = too many)")

    if not all_suggestions:
        print("\n  No candidates found within budget!")
        return

    # Score
    print(f"\nScoring {len(all_suggestions)} candidates...")
    for s in all_suggestions:
        candidate_payoff = compute_candidate_payoff(spot, s.trades)
        new_payoff = current_payoff + candidate_payoff
        scores = score_improvement(current_payoff, new_payoff, spot, eth_spot)
        s.improvement_score = scores["composite"]
        s.details = {
            "eth_spot": eth_spot,
            "impact": {
                "At current spot": f"${scores['at_spot_current']:,.0f} -> ${scores['at_spot_new']:,.0f} ({scores['at_spot_improvement']:+,.0f})",
                "Worst case": f"${scores['current_min']:,.0f} -> ${scores['new_min']:,.0f} ({scores['min_improvement']:+,.0f})",
                "Downside (70-95% spot)": f"${scores['near_down_imp']:+,.0f}",
                "Upside (105-150% spot)": f"${scores['near_up_imp']:+,.0f}",
                "Breakeven": f"${scores['current_be']:,.0f} -> ${scores['new_be']:,.0f}" if scores['current_be'] and scores['new_be'] else "N/A",
            }
        }

    # Sort and deduplicate (remove very similar scores)
    all_suggestions.sort(key=lambda s: s.improvement_score, reverse=True)

    # Print results
    print(f"\n{'#'*70}")
    print(f"  TOP {top_n} SUGGESTIONS")
    print(f"  Budget: ${budget:,.0f}  |  ETH: ${eth_spot:,.2f}  |  Base qty: {base_qty}")
    print(f"{'#'*70}")

    seen_names = set()
    printed = 0
    for s in all_suggestions:
        if printed >= top_n:
            break
        # Skip very similar suggestions
        short_name = s.name.split(":")[0] if ":" in s.name else s.name
        if short_name in seen_names:
            continue
        seen_names.add(short_name)
        printed += 1
        print(s.format(printed))

    # Category breakdown
    print(f"\n{'#'*70}")
    print(f"  BEST BY CATEGORY")
    print(f"{'#'*70}")

    categories = sorted(set(s.category for s in all_suggestions))
    for cat in categories:
        cat_best = [s for s in all_suggestions if s.category == cat]
        if cat_best:
            best = cat_best[0]
            print(f"\n  Best {cat}: {best.name}")
            print(f"    Cost: ${best.net_cost_usd:+,.0f}  |  Score: {best.improvement_score:+,.0f}")

    return all_suggestions[:top_n]


def main():
    parser = argparse.ArgumentParser(description="ETH Portfolio Optimizer")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help=f"Max net cost in USD (default: {DEFAULT_BUDGET})")
    parser.add_argument("--expiry", type=str, default=None,
                        help="Filter to a specific expiry (e.g., 2026-05-29)")
    parser.add_argument("--top", type=int, default=15,
                        help="Number of top suggestions (default: 15)")
    args = parser.parse_args()

    asyncio.run(run_analysis(args.budget, args.expiry, args.top))


if __name__ == "__main__":
    main()
