"""Optimizer v2 — loads snapshot and runs portfolio optimization.

Objective: maximize U = -cost - λ·risk
  cost = txn_cost_pct/100 × Σ|notional_i| for new trades
  risk = daily portfolio P&L standard deviation (greeks-based)

Risk model (greeks-based, 1-day horizon):
  Var(P&L) ≈ (Δ_port · S · σ_daily)²
           + (½ · Γ_port · S² · σ_daily²)²      [gamma P&L variance]
           + (Vega_port · σ_vol_daily)²           [vega P&L variance]
           + θ_port²                              [deterministic but included]

  σ_daily   = ATM_IV / √252
  σ_vol_daily estimated from vol surface term structure
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm


@dataclass
class Position:
    id: int
    instrument: str
    opt: str
    side: str
    strike: float
    expiry: str
    days_remaining: int
    net_qty: float
    iv_pct: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    mark_price_usd: float
    current_mtm: float
    payoff_by_horizon: dict[str, list[float]]
    mtm_by_horizon: list[float]


@dataclass
class Candidate:
    """A tradeable instrument from the vol surface."""
    expiry_code: str
    expiry_date: str
    dte: int
    strike: float
    opt: str  # "C" or "P"
    iv_pct: float
    # BS greeks per contract (computed at current spot)
    delta: float
    gamma: float
    theta: float
    vega: float
    bs_price_usd: float


class OptimizerV2:
    """Holds all data needed for portfolio optimization."""

    def __init__(
        self,
        eth_spot: float,
        spot_ladder: list[float],
        matrix_horizons: list[int],
        chart_horizons: list[int],
        vol_surface: list[dict],
        positions: list[Position],
        totals: dict,
        snapshot_path: Path,
    ):
        self.eth_spot = eth_spot
        self.spot_ladder = spot_ladder
        self.matrix_horizons = matrix_horizons
        self.chart_horizons = chart_horizons
        self.vol_surface = vol_surface
        self.positions = positions
        self.totals = totals
        self.snapshot_path = snapshot_path

    @classmethod
    def from_snapshot(cls, path: Path) -> OptimizerV2:
        """Load an OptimizerV2 instance from a snapshot JSON file."""
        with open(path) as f:
            data = json.load(f)

        positions = [
            Position(
                id=p["id"],
                instrument=p["instrument"],
                opt=p["opt"],
                side=p["side"],
                strike=p["strike"],
                expiry=p["expiry"],
                days_remaining=p["days_remaining"],
                net_qty=p["net_qty"],
                iv_pct=p["iv_pct"],
                delta=p.get("delta"),
                gamma=p.get("gamma"),
                theta=p.get("theta"),
                vega=p.get("vega"),
                mark_price_usd=p["mark_price_usd"],
                current_mtm=p["current_mtm"],
                payoff_by_horizon=p["payoff_by_horizon"],
                mtm_by_horizon=p["mtm_by_horizon"],
            )
            for p in data["positions"]
        ]

        return cls(
            eth_spot=data["eth_spot"],
            spot_ladder=data["spot_ladder"],
            matrix_horizons=data["matrix_horizons"],
            chart_horizons=data["chart_horizons"],
            vol_surface=data["vol_surface"],
            positions=positions,
            totals=data["totals"],
            snapshot_path=path,
        )

    # ------------------------------------------------------------------
    # Build candidate instruments from vol surface
    # ------------------------------------------------------------------

    def _build_candidates(self, target_expiry: str | None = None) -> list[Candidate]:
        """Generate tradeable instruments from the vol surface.

        Parameters
        ----------
        target_expiry : optional expiry code to restrict to (e.g. "29MAY26").
                        If None, uses ALL available expiries.
        """
        S = self.eth_spot
        candidates = []

        # Filter smiles
        matching_smiles = []
        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            if target_expiry:
                if smile["expiry_code"] == target_expiry:
                    matching_smiles.append(smile)
            else:
                # No filter → use all expiries
                matching_smiles.append(smile)

        # Strike filter: 50%–200% of spot
        strike_lo = S * 0.50
        strike_hi = S * 2.00

        for smile in matching_smiles:
            expiry_code = smile["expiry_code"]
            expiry_date = smile["expiry_date"]
            dte = smile["dte"]
            T = dte / 365.25
            strikes = smile["strikes"]
            ivs = smile["ivs"]

            for strike, iv_pct in zip(strikes, ivs):
                if strike < strike_lo or strike > strike_hi:
                    continue
                sigma = iv_pct / 100.0
                if sigma <= 0 or strike <= 0:
                    continue

                for opt in ("C", "P"):
                    delta, gamma, theta, vega, price = _bs_greeks(
                        S, strike, T, 0.0, sigma, opt
                    )
                    candidates.append(Candidate(
                        expiry_code=expiry_code,
                        expiry_date=expiry_date,
                        dte=dte,
                        strike=strike,
                        opt=opt,
                        iv_pct=iv_pct,
                        delta=delta,
                        gamma=gamma,
                        theta=theta,
                        vega=vega,
                        bs_price_usd=price,
                    ))

        # ETH perpetual future: delta=1, no gamma/theta/vega, price = spot
        candidates.append(Candidate(
            expiry_code="PERP",
            expiry_date="",
            dte=0,
            strike=S,
            opt="F",
            iv_pct=0.0,
            delta=1.0,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            bs_price_usd=S,
        ))

        return candidates

    # ------------------------------------------------------------------
    # Estimate vol-of-vol from the term structure
    # ------------------------------------------------------------------

    def _estimate_vol_of_vol_daily(self) -> float:
        """Estimate daily vol-of-vol from the vol surface term structure.

        Uses the standard deviation of ATM IVs across expiries as a proxy
        for how much IV moves, scaled to daily.
        """
        atm_ivs = []
        dtes = []
        S = self.eth_spot

        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            strikes = smile["strikes"]
            ivs = smile["ivs"]
            # Find nearest-to-ATM strike
            best_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - S))
            atm_ivs.append(ivs[best_idx])
            dtes.append(smile["dte"])

        if len(atm_ivs) < 2:
            return 1.0  # fallback: 1 vol point daily

        # Std dev of ATM IVs across the term structure (in vol points, e.g. 5.0)
        # This is a rough proxy — true vol-of-vol would come from historical data
        iv_std = float(np.std(atm_ivs))
        # Scale: assume this spread represents ~30-day variation
        daily_vov = iv_std / math.sqrt(30)
        return max(daily_vov, 0.5)  # floor at 0.5 vol points/day

    # ------------------------------------------------------------------
    # Portfolio greeks
    # ------------------------------------------------------------------

    def _portfolio_greeks(self) -> tuple[float, float, float, float]:
        """Return (delta, gamma, theta, vega) of the current portfolio."""
        delta = sum((p.delta or 0) * p.net_qty for p in self.positions)
        gamma = sum((p.gamma or 0) * p.net_qty for p in self.positions)
        theta = sum((p.theta or 0) * p.net_qty for p in self.positions)
        vega = sum((p.vega or 0) * p.net_qty for p in self.positions)
        return delta, gamma, theta, vega

    # ------------------------------------------------------------------
    # Risk computation
    # ------------------------------------------------------------------

    def _compute_risk(
        self,
        port_delta: float,
        port_gamma: float,
        port_theta: float,
        port_vega: float,
        sigma_daily: float,
        vov_daily: float,
        lambda_delta: float = 1.0,
        lambda_vega: float = 100.0,
    ) -> float:
        """Weighted daily P&L standard deviation from greeks."""
        S = self.eth_spot

        # Delta P&L variance: (Δ · S · σ_daily)²
        var_delta = (port_delta * S * sigma_daily) ** 2

        # Gamma P&L variance: (½ · Γ · S² · σ_daily²)²
        var_gamma = (0.5 * port_gamma * S**2 * sigma_daily**2) ** 2

        # Vega P&L variance: (Vega · σ_vol_daily)²
        var_vega = (port_vega * vov_daily) ** 2

        return math.sqrt(
            lambda_delta * var_delta
            + lambda_delta * var_gamma
            + lambda_vega * var_vega
        )

    # ------------------------------------------------------------------
    # Run optimization
    # ------------------------------------------------------------------

    def run(
        self,
        risk_aversion: float = 1.0,
        txn_cost_pct: float = 5.0,
        max_collateral: float = 4_000_000.0,
        target_expiry: str | None = None,
        lambda_delta: float = 1.0,
        lambda_vega: float = 100.0,
        unwind_discount: float = 0.2,
        new_position_penalty: float = 0.04,
        fixed_trade_cost: float = 200.0,  # USD penalty per distinct instrument traded
    ) -> dict:
        """Run the optimization and return proposed trades.

        Parameters
        ----------
        unwind_discount : float
            Multiplier on txn cost for closing existing positions (0.2 = 80% cheaper).
        new_position_penalty : float
            Extra cost per dollar notional for trades in instruments not already held.
        fixed_trade_cost : float
            Fixed USD penalty per distinct instrument traded (sparsity incentive).
        """
        S = self.eth_spot
        candidates = self._build_candidates(target_expiry=target_expiry)

        if not candidates:
            return {
                "status": "no_candidates",
                "message": "No tradeable instruments found on the vol surface.",
            }

        # Current portfolio greeks
        port_delta, port_gamma, port_theta, port_vega = self._portfolio_greeks()

        # Market parameters
        # ATM IV for daily spot vol
        atm_ivs = []
        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            strikes = smile["strikes"]
            ivs = smile["ivs"]
            best_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - S))
            atm_ivs.append(ivs[best_idx])
        atm_iv = float(np.mean(atm_ivs)) / 100.0 if atm_ivs else 0.80
        sigma_daily = atm_iv / math.sqrt(252)

        # Vol-of-vol
        vov_daily = self._estimate_vol_of_vol_daily()

        # ------------------------------------------------------------------
        # Build a map of existing positions keyed by (expiry_code, strike, opt)
        # so we know which candidates correspond to held instruments.
        # A negative net_qty means we're short → buying unwinds.
        # A positive net_qty means we're long → selling unwinds.
        # ------------------------------------------------------------------
        held_positions: dict[tuple[str, float, str], float] = {}
        for p in self.positions:
            # Extract expiry code from instrument name (e.g. "ETH-29MAY26-3000-C")
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
            else:
                exp_code = ""
            key = (exp_code, p.strike, p.opt)
            held_positions[key] = held_positions.get(key, 0.0) + p.net_qty

        # Pre-compute candidate greek arrays (per contract)
        n = len(candidates)
        c_delta = np.array([c.delta for c in candidates])
        c_gamma = np.array([c.gamma for c in candidates])
        c_theta = np.array([c.theta for c in candidates])
        c_vega = np.array([c.vega for c in candidates])
        c_price = np.array([c.bs_price_usd for c in candidates])

        # Per-candidate cost rate: 5bps for perp, txn_cost_pct for options
        PERP_COST_BPS = 5.0  # 5 basis points = 0.05%
        c_cost_rate = np.array([
            PERP_COST_BPS / 10_000.0 if c.opt == "F" else txn_cost_pct / 100.0
            for c in candidates
        ])

        # ------------------------------------------------------------------
        # Per-candidate: existing position qty and "is_held" flag
        # ------------------------------------------------------------------
        c_held_qty = np.array([
            held_positions.get((c.expiry_code, c.strike, c.opt), 0.0)
            for c in candidates
        ])
        c_is_held = np.array([abs(q) > 0 for q in c_held_qty], dtype=float)

        # Current portfolio risk (before optimization)
        risk_before = self._compute_risk(
            port_delta, port_gamma, port_theta, port_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_vega,
        )

        def objective(x: np.ndarray) -> float:
            """Negative utility: cost − λ·risk_reduction.

            At x=0 (no trades), this returns exactly 0.
            A trade is only proposed if λ·risk_reduction > cost.
            """
            # New portfolio greeks = current + sum(x_i * candidate_greeks_i)
            new_delta = port_delta + np.dot(x, c_delta)
            new_gamma = port_gamma + np.dot(x, c_gamma)
            new_theta = port_theta + np.dot(x, c_theta)
            new_vega = port_vega + np.dot(x, c_vega)

            risk = self._compute_risk(
                new_delta, new_gamma, new_theta, new_vega,
                sigma_daily, vov_daily,
            )

            # Risk reduction relative to doing nothing
            risk_reduction = risk_before - risk

            # ----------------------------------------------------------
            # Split each trade into "unwind" and "new" portions.
            #
            # For a held position with qty H and optimizer trade x:
            #   - If x goes in the opposite direction of H (reducing exposure),
            #     that part is an unwind → cheaper cost.
            #   - Any remainder is a new position → higher cost.
            #
            # unwind_qty = min(|x|, |H|) when sign(x) != sign(H)
            # new_qty    = |x| - unwind_qty
            # ----------------------------------------------------------
            # Unwind portion: x opposes held qty
            opposite = (x * c_held_qty) < 0  # True where trade closes position
            unwind_abs = np.where(
                opposite,
                np.minimum(np.abs(x), np.abs(c_held_qty)),
                0.0,
            )
            new_abs = np.abs(x) - unwind_abs

            # Cost for unwind portion (discounted)
            cost_unwind = np.sum(
                c_cost_rate * unwind_discount * unwind_abs * c_price
            )
            # Cost for new portion (full rate + penalty for non-held instruments)
            cost_new = np.sum(
                (c_cost_rate + new_position_penalty * (1.0 - c_is_held))
                * new_abs * c_price
            )

            cost = cost_unwind + cost_new

            # Fixed per-trade penalty: smooth approximation of 𝟙(x_i ≠ 0)
            # tanh(|x_i| / ε) ≈ 0 when x_i ≈ 0, ≈ 1 when |x_i| >> ε
            if fixed_trade_cost > 0:
                epsilon = 0.3  # transition sharpness (≈ 0.3 contracts)
                cost += fixed_trade_cost * float(np.sum(
                    np.tanh(np.abs(x) / epsilon)
                ))

            return cost - risk_aversion * risk_reduction

        # Bounds: limit trade sizes, respect collateral
        # Max contracts per instrument: collateral / price (rough)
        max_qty = np.array([
            max_collateral / max(p, 1.0) for p in c_price
        ])
        bounds = [(-mq, mq) for mq in max_qty]

        # Start from zero (no trades)
        x0 = np.zeros(n)

        # Value of doing nothing — any solution MUST beat this
        do_nothing_value = objective(x0)  # == 0.0 with the risk_reduction formulation

        result = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=bounds,
            options={"maxiter": 2000, "ftol": 1e-10},
        )

        best_x = result.x.copy()
        best_val = result.fun

        # Multi-start refinement — only if the solver actually proposed trades.
        # If it stayed near zero, scaling zero is pointless.
        if np.max(np.abs(result.x)) > 0.5:
            for scale in [0.25, 0.5, 0.75]:
                res2 = minimize(
                    objective,
                    result.x * scale,
                    method="SLSQP",
                    bounds=bounds,
                    options={"maxiter": 500, "ftol": 1e-9},
                )
                if res2.fun < best_val:
                    best_val = res2.fun
                    best_x = res2.x.copy()

        # ── HARD GUARD: reject solutions worse than doing nothing ──
        if best_val >= do_nothing_value - 1e-6:
            best_x = np.zeros(n)

        # Extract proposed trades (filter out tiny quantities)
        trades = []
        for i, qty in enumerate(best_x):
            if abs(qty) < 0.5:
                continue
            c = candidates[i]
            rounded_qty = round(qty)
            if rounded_qty == 0:
                continue
            notional = abs(rounded_qty) * c.bs_price_usd
            cost_rate = float(c_cost_rate[i])

            # Determine if this is an unwind or new position
            held_qty = c_held_qty[i]
            is_unwind = bool((rounded_qty * held_qty) < 0)
            # Cap unwind qty to the actual held position size
            unwind_qty = min(abs(rounded_qty), abs(held_qty)) if is_unwind else 0
            new_qty = abs(rounded_qty) - unwind_qty

            # Compute cost split: unwind portion at discounted rate, remainder at full rate
            unwind_notional = unwind_qty * c.bs_price_usd
            new_notional = new_qty * c.bs_price_usd
            is_new_instrument = abs(held_qty) == 0
            cost_unwind_part = cost_rate * unwind_discount * unwind_notional
            cost_new_part = (
                cost_rate + (new_position_penalty if is_new_instrument else 0.0)
            ) * new_notional
            trade_cost = cost_unwind_part + cost_new_part

            instrument_name = (
                "ETH-PERPETUAL" if c.opt == "F"
                else f"ETH-{c.expiry_code}-{int(c.strike)}-{c.opt}"
            )

            # Emit separate rows for unwind vs new-position portions
            if unwind_qty >= 1:
                unwind_signed = int(unwind_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({
                    "instrument": instrument_name,
                    "expiry": c.expiry_date,
                    "dte": c.dte,
                    "strike": c.strike,
                    "opt": c.opt,
                    "qty": unwind_signed,
                    "side": "Buy" if unwind_signed > 0 else "Sell",
                    "iv_pct": round(c.iv_pct, 1),
                    "bs_price_usd": round(c.bs_price_usd, 2),
                    "notional": round(unwind_notional, 2),
                    "cost_bps": round(cost_rate * 10_000, 1),
                    "trade_cost": round(cost_unwind_part, 2),
                    "delta_contribution": round(unwind_signed * c.delta, 4),
                    "gamma_contribution": round(unwind_signed * c.gamma, 6),
                    "vega_contribution": round(unwind_signed * c.vega, 4),
                    "is_unwind": True,
                    "unwind_qty": int(unwind_qty),
                    "new_qty": 0,
                })

            if new_qty >= 1:
                new_signed = int(new_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({
                    "instrument": instrument_name,
                    "expiry": c.expiry_date,
                    "dte": c.dte,
                    "strike": c.strike,
                    "opt": c.opt,
                    "qty": new_signed,
                    "side": "Buy" if new_signed > 0 else "Sell",
                    "iv_pct": round(c.iv_pct, 1),
                    "bs_price_usd": round(c.bs_price_usd, 2),
                    "notional": round(new_notional, 2),
                    "cost_bps": round(cost_rate * 10_000, 1),
                    "trade_cost": round(cost_new_part, 2),
                    "delta_contribution": round(new_signed * c.delta, 4),
                    "gamma_contribution": round(new_signed * c.gamma, 6),
                    "vega_contribution": round(new_signed * c.vega, 4),
                    "is_unwind": False,
                    "unwind_qty": 0,
                    "new_qty": int(new_qty),
                })

        # Post-optimization portfolio greeks
        opt_x = best_x
        new_delta = port_delta + np.dot(opt_x, c_delta)
        new_gamma = port_gamma + np.dot(opt_x, c_gamma)
        new_theta = port_theta + np.dot(opt_x, c_theta)
        new_vega = port_vega + np.dot(opt_x, c_vega)

        risk_after = self._compute_risk(
            new_delta, new_gamma, new_theta, new_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_vega,
        )

        total_cost = sum(t["trade_cost"] for t in trades)

        # Add fixed cost per distinct instrument traded
        n_distinct_instruments = len({t["instrument"] for t in trades})
        total_fixed_cost = fixed_trade_cost * n_distinct_instruments
        total_cost_with_fixed = total_cost + total_fixed_cost

        # ------------------------------------------------------------------
        # Compute before/after payoff curves and P&L matrix
        # ------------------------------------------------------------------
        spot_arr = np.array(self.spot_ladder, dtype=float)
        horizons = sorted(set(self.chart_horizons + [0]))

        # Before payoff: aggregate from existing positions
        before_payoff = {}
        for h in horizons:
            h_key = str(h)
            total = np.zeros(len(spot_arr))
            for p in self.positions:
                curve = p.payoff_by_horizon.get(h_key)
                if curve:
                    total += np.array(curve)
            before_payoff[h_key] = np.round(total, 2).tolist()

        # Trade payoff contribution: for each proposed trade, compute BS
        # values across the spot ladder at each horizon
        trade_payoff_delta = {}
        for h in horizons:
            h_key = str(h)
            total = np.zeros(len(spot_arr))
            for t in trades:
                if t["opt"] == "F":
                    # Perpetual future: P&L = qty * (spot - entry)
                    vals = spot_arr - t["strike"]
                else:
                    dte_at_h = max(t["dte"] - h, 0)
                    T_h = dte_at_h / 365.25
                    sigma = t["iv_pct"] / 100.0
                    vals = _bs_vec(spot_arr, t["strike"], T_h, 0.0, sigma, t["opt"])
                total += t["qty"] * vals
            trade_payoff_delta[h_key] = np.round(total, 2).tolist()

        # After payoff = before + trades
        after_payoff = {}
        for h_key in before_payoff:
            before = np.array(before_payoff[h_key])
            delta_arr = np.array(trade_payoff_delta[h_key])
            after_payoff[h_key] = np.round(before + delta_arr, 2).tolist()

        return {
            "status": "ok",
            "snapshot_path": str(self.snapshot_path),
            "eth_spot": S,
            "spot_ladder": self.spot_ladder,
            "chart_horizons": horizons,
            "params": {
                "risk_aversion": risk_aversion,
                "txn_cost_pct": txn_cost_pct,
                "max_collateral": max_collateral,
                "atm_iv_pct": round(atm_iv * 100, 1),
                "sigma_daily": round(sigma_daily, 4),
                "vov_daily": round(vov_daily, 2),
            },
            "before": {
                "delta": round(port_delta, 2),
                "gamma": round(port_gamma, 4),
                "theta": round(port_theta, 2),
                "vega": round(port_vega, 2),
                "daily_risk": round(risk_before, 2),
                "payoff_by_horizon": before_payoff,
            },
            "after": {
                "delta": round(new_delta, 2),
                "gamma": round(new_gamma, 4),
                "theta": round(new_theta, 2),
                "vega": round(new_vega, 2),
                "daily_risk": round(risk_after, 2),
                "payoff_by_horizon": after_payoff,
            },
            "trades": trades,
            "total_trade_cost": round(total_cost_with_fixed, 2),
            "total_proportional_cost": round(total_cost, 2),
            "total_fixed_cost": round(total_fixed_cost, 2),
            "n_distinct_instruments": n_distinct_instruments,
            "fixed_trade_cost_per_instrument": fixed_trade_cost,
            "utility_improvement": round(risk_before - risk_after - total_cost_with_fixed, 2),
            "candidates_evaluated": n,
            "optimizer_converged": result.success,
            "rejected_as_worse_than_nothing": bool(np.all(best_x == 0) and result.fun >= do_nothing_value - 1e-6),
        }


# ---------------------------------------------------------------------------
# BS greeks helper
# ---------------------------------------------------------------------------

def _bs_vec(
    spots: np.ndarray, K: float, T: float, r: float, sigma: float, opt: str,
) -> np.ndarray:
    """Vectorised Black-Scholes across an array of spot prices."""
    if T <= 0:
        return np.maximum(spots - K, 0.0) if opt == "C" else np.maximum(K - spots, 0.0)
    d1 = (np.log(spots / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "C":
        return spots * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - spots * norm.cdf(-d1)


def _bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float, opt: str,
) -> tuple[float, float, float, float, float]:
    """Return (delta, gamma, theta, vega, price) for a single contract."""
    if T <= 0 or sigma <= 0:
        price = max(S - K, 0.0) if opt == "C" else max(K - S, 0.0)
        delta = (1.0 if S > K else 0.0) if opt == "C" else (-1.0 if S < K else 0.0)
        return delta, 0.0, 0.0, 0.0, price

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = norm.pdf(d1)

    # Price
    if opt == "C":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1.0

    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100.0  # per 1 vol point
    theta = (-(S * pdf_d1 * sigma) / (2 * sqrtT)
             - r * K * math.exp(-r * T) * (norm.cdf(d2) if opt == "C" else norm.cdf(-d2))) / 365.25

    return delta, gamma, theta, vega, price

