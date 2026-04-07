from __future__ import annotations

import math
import numpy as np
from pathlib import Path
from datetime import date, datetime
from scipy.optimize import minimize

from .base_optimizer import BaseOptimizer, RiskMode
from .models import Position, Candidate
from .math_utils import bs_price, bs_vec, bs_greeks
from .pulp_solver import PulpSolver
from .snapshot import load_snapshot_dict
from .optimizer_utils import expiry_sort_key, safe_num


class OptimizerV3(BaseOptimizer):
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
        today: datetime.date,
    ):
        super().__init__(eth_spot, spot_ladder, matrix_horizons, chart_horizons, vol_surface, positions, totals,
                         snapshot_path, today)
        self.cost = None
        self.risk_reduction = None

        self.lambda_delta = float('nan')
        self.lambda_gamma = float('nan')
        self.lambda_vega = float('nan')

    def run(
        self,
        risk_aversion: float = 1.0,
        brokerage_txn_cost_pct: float = 5.0,
        deribit_txn_cost_pct: float = 0.1,
        max_collateral: float = 4_000_000.0,
        target_expiry: str | None = None,
        lambda_delta: float = 1.0,
        lambda_gamma: float = 1.0,
        lambda_vega: float = 100.0,
        unwind_discount: float = 0.2,
        new_position_penalty: float = 0.04,
        vega_cross_expiry_corr: float = 0.0,
    ) -> dict:
        """Run the optimization and return proposed trades.

                Parameters
                ----------
                unwind_discount : float
                    Multiplier on txn cost for closing existing positions (0.2 = 80% cheaper).
                new_position_penalty : float
                    Extra cost per dollar notional for trades in instruments not already held.
                vega_cross_expiry_corr : float
                    Correlation of vol shocks across expiries. Lower means less cross-expiry
                    vega netting; higher means more shared vega risk.
                """
        self.lambda_delta = lambda_delta
        self.lambda_gamma = lambda_gamma
        self.lambda_vega = lambda_vega
        print(f"Running optimization with risk aversion {risk_aversion:.2f}...")
        spot = self.eth_spot
        candidates = self._build_candidates(target_expiry=target_expiry)

        if not candidates:
            return {
                "status": "no_candidates",
                "message": "No tradeable instruments found on the vol surface.",
            }

        # Current portfolio greeks
        port_delta, port_gamma, port_theta, port_vega = self._portfolio_greeks()
        port_vega_by_expiry = self._portfolio_vega_by_expiry()

        # Market parameters
        # ATM IV for daily spot vol
        atm_ivs = []
        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            strikes = smile["strikes"]
            ivs = smile["ivs"]
            best_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
            atm_ivs.append(ivs[best_idx])
        atm_iv = float(np.mean(atm_ivs)) / 100.0 if atm_ivs else 0.80
        sigma_daily = atm_iv / math.sqrt(252)
        vov_daily = self._estimate_vol_of_vol_daily()  # Vol-of-vol

        # ------------------------------------------------------------------
        # A negative net_qty means we're short → buying unwinds.
        # A positive net_qty means we're long → selling unwinds.
        # ------------------------------------------------------------------
        held_positions = self.get_held_positions()

        n = len(candidates)
        # Pre-compute candidate greek arrays (per contract)
        c_delta = np.array([0.0 if c.delta is None else float(c.delta) for c in candidates], dtype=float)
        c_gamma = np.array([0.0 if c.gamma is None else float(c.gamma) for c in candidates], dtype=float)
        c_theta = np.array([safe_num(c.theta) for c in candidates], dtype=float)
        c_vega = np.array([safe_num(c.vega) for c in candidates], dtype=float)
        c_price = np.array([max(safe_num(c.bs_price_usd), 0.0) for c in candidates], dtype=float)
        c_iv_pct = np.array([max(safe_num(c.iv_pct), 0.0) for c in candidates], dtype=float)
        c_strike = np.array([safe_num(c.strike) for c in candidates], dtype=float)
        c_dte = np.array([int(safe_num(c.dte)) for c in candidates], dtype=int)

        # ----------------------------------------------------------
        # Strike weighting: ensure OTM strikes get a minimum weight.
        # Weighting is done within each expiry bucket, not globally across all candidates.
        # Otherwise, an all-expiry run can distort the effective greeks of a given expiry versus running that expiry on its own.
        # ----------------------------------------------------------
        min_strike_weight_pct = 0.10  # 10% of the max weight per expiry
        strike_weight = np.ones(n)

        expiry_to_indices: dict[str, list[int]] = {}
        for i, c in enumerate(candidates):
            expiry_to_indices.setdefault(c.expiry_code, []).append(i)

        for exp_code, idxs in expiry_to_indices.items():
            idx_arr = np.array(idxs, dtype=int)

            greek_mag_bucket = np.sqrt(
                (c_delta[idx_arr] * spot) ** 2
                + (c_gamma[idx_arr] * spot ** 2) ** 2
                + c_vega[idx_arr] ** 2
            )

            bucket_max = greek_mag_bucket.max() if greek_mag_bucket.size > 0 else 0.0
            if bucket_max <= 0:
                raw_weight_bucket = np.ones(len(idx_arr))
            else:
                raw_weight_bucket = greek_mag_bucket / bucket_max

            strike_weight[idx_arr] = np.maximum(raw_weight_bucket, min_strike_weight_pct)

        # Apply strike weights to the greeks the optimizer sees
        c_delta = c_delta * strike_weight
        c_gamma = c_gamma * strike_weight
        c_theta = c_theta * strike_weight
        c_vega = c_vega * strike_weight

        # Per-candidate cost rate: 5bps for perp, txn_cost_pct for options
        perp_cost_bps = 2.0  # 5 basis points = 0.05%
        c_cost_rate = self.compute_costs(spot, candidates, perp_cost_bps, brokerage_txn_cost_pct, deribit_txn_cost_pct)

        # ------------------------------------------------------------------
        # Per-candidate: existing position qty and "is_held" flag
        # ------------------------------------------------------------------
        c_held_qty = np.array([held_positions.get((c.expiry_code, c.strike, c.opt, c.counterparty), 0.0)
            for c in candidates
        ])
        c_is_held = np.array([abs(q) > 0 for q in c_held_qty], dtype=float)

        expiry_codes = sorted({c.expiry_code for c in candidates if c.expiry_code != "PERP"})
        c_vega_by_expiry = {
            exp_code: np.array([c_vega[i] if candidates[i].expiry_code == exp_code else 0.0 for i in range(n)])
            for exp_code in expiry_codes
        }

        new_port_vega_by_expiry = {exp_code: port_vega_by_expiry.get(exp_code, 0.0) for exp_code in expiry_codes}

        # Current portfolio risk (before optimization)
        risk_before = self._compute_risk(port_delta, port_gamma, port_theta, port_vega, sigma_daily, vov_daily,
                                         self.lambda_delta, self.lambda_gamma, self.lambda_vega,
                                         port_vega_by_expiry=new_port_vega_by_expiry,
                                         vega_cross_expiry_corr=vega_cross_expiry_corr, risk_mode=RiskMode.GAMMA_VEGA)

        bounds = []  # Bounds: unwind-only
        for c, held_qty, price in zip(candidates, c_held_qty, c_price):
            if c.opt == "F":  # ETH-PERPETUAL stays unrestricted
                bounds.append((-max_collateral / max(price, 1.0), max_collateral / max(price, 1.0)))
            elif c.counterparty == "Deribit":
                bounds.append((-max_collateral / max(price, 1.0), max_collateral / max(price, 1.0)))
            elif held_qty > 0:  # long option: can only sell to reduce/close
                bounds.append((-abs(held_qty), 0.0))
            elif held_qty < 0:  # short option: can only buy to reduce/close
                bounds.append((0.0, abs(held_qty)))
            else:  # no existing option position: no trade
                bounds.append((0.0, 0.0))

        x0 = np.zeros(n)  # Start from zero (no trades)
        obj = lambda x:self.objective(x, port_delta, port_gamma, port_theta, port_vega, c_delta, c_gamma, c_theta,
                                      c_vega, c_vega_by_expiry, c_held_qty, c_is_held, c_price, c_cost_rate,
                                      sigma_daily, vov_daily, port_vega_by_expiry, vega_cross_expiry_corr, expiry_codes,
                                      risk_aversion, risk_before, unwind_discount, new_position_penalty,
                                      risk_mode=RiskMode.GAMMA_VEGA)
        res0 = obj(x0)
        result = minimize(obj, x0, method="SLSQP", bounds=bounds, options={"maxiter": 2000, "ftol": 1e-10})
        if not result.success:
            print(f"Optimization failed: {result.message}")
        obj_result = obj(result.x)
        print(f"cost: {self.cost:.2f}, risk_reduction: {self.risk_reduction:.2f}")

        trades = self.extract_trades(result.x, candidates, c_dte, c_strike, c_iv_pct, c_price, c_delta, c_gamma, c_theta,
                                     c_vega, c_held_qty, unwind_discount, new_position_penalty, strike_weight, c_cost_rate)

        new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry = (
            self.compute_greeks(result.x, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry,
                                c_delta, c_gamma, c_theta, c_vega, c_vega_by_expiry))

        risk_after = self._compute_risk(
            new_delta, new_gamma, new_theta, new_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_gamma, lambda_vega,
            port_vega_by_expiry=new_vega_by_expiry,
            vega_cross_expiry_corr=vega_cross_expiry_corr, risk_mode=RiskMode.GAMMA_VEGA,
        )

        total_cost = sum(t["trade_cost"] for t in trades)

        # ------------------------------------------------------------------
        # Compute before/after payoff curves and P&L matrix
        # ------------------------------------------------------------------
        spot_arr = np.array(self.spot_ladder, dtype=float)
        horizons = sorted(set(self.chart_horizons + [0]))
        before_payoff, after_payoff = self.build_payoffs(horizons, spot_arr, trades)

        return {
            "status": "ok",
            "snapshot_path": str(self.snapshot_path),
            "eth_spot": spot,
            "spot_ladder": self.spot_ladder,
            "chart_horizons": horizons,
            "params": {
                "risk_aversion": risk_aversion,
                "brokerage_txn_cost_pct": brokerage_txn_cost_pct,
                "deribit_txn_cost_pct": deribit_txn_cost_pct,
                "max_collateral": max_collateral,
                "atm_iv_pct": round(atm_iv * 100, 1),
                "sigma_daily": round(sigma_daily, 4),
                "vov_daily": round(vov_daily, 2),
                "vega_cross_expiry_corr": round(vega_cross_expiry_corr, 2),
                "lambda_delta": lambda_delta,
                "lambda_gamma": lambda_gamma,
                "lambda_vega": lambda_vega,
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
            "total_trade_cost": round(total_cost, 2),
            "utility_improvement": round(risk_before - risk_after - total_cost, 2),
            "candidates_evaluated": n,
            "optimizer_converged": result.success,
        }

    def objective(self, x: np.ndarray,
                  port_delta, port_gamma, port_theta, port_vega,
                  c_delta, c_gamma, c_theta, c_vega, c_vega_by_expiry, c_held_qty, c_is_held, c_price, c_cost_rate,
                  sigma_daily, vov_daily,
                  port_vega_by_expiry, vega_cross_expiry_corr, expiry_codes,
                  risk_aversion, risk_before, unwind_discount, new_position_penalty,
                  risk_mode) -> float:
        """Negative utility: cost − λ·risk_reduction. At x=0 (no trades), this returns exactly 0.
        A trade is only proposed if λ·risk_reduction > cost."""
        new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry = (
            self.compute_greeks(x, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry,
                                c_delta, c_gamma, c_theta, c_vega, c_vega_by_expiry))

        risk = self._compute_risk(
            new_delta, new_gamma, new_theta, new_vega,
            sigma_daily, vov_daily, self.lambda_delta, self.lambda_gamma, self.lambda_vega,
            port_vega_by_expiry=new_vega_by_expiry,
            vega_cross_expiry_corr=vega_cross_expiry_corr, risk_mode=risk_mode,
        )

        risk_reduction = risk_before - risk  # Risk reduction relative to doing nothing

        # ----------------------------------------------------------
        # Split each trade into "unwind" and "new" portions.
        #
        # For a held position with qty H and optimizer trade x:
        #   - If x goes in the opposite direction of H (reducing exposure),
        #     that part is an unwind → cheaper cost.
        #   - Any remainder is a new position → higher cost.
        # ----------------------------------------------------------
        opposite = (x * c_held_qty) < 0  # True where trade closes position
        unwind_abs = np.where(opposite, np.minimum(np.abs(x), np.abs(c_held_qty)), 0.0)
        new_abs = np.abs(x) - unwind_abs

        cost_unwind = np.sum(c_cost_rate * unwind_discount * unwind_abs * c_price)
        cost_new = np.sum((c_cost_rate + new_position_penalty * (1.0 - c_is_held)) * new_abs * c_price)
        self.cost = cost_unwind + cost_new

        self.risk_reduction = risk_reduction
        return self.cost - risk_aversion * risk_reduction

    def compute_greeks(self, x: np.ndarray,port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry,
                       c_delta, c_gamma, c_theta, c_vega, c_vega_by_expiry):
        new_delta = port_delta + np.dot(x, c_delta)
        new_gamma = port_gamma + np.dot(x, c_gamma)
        new_theta = port_theta + np.dot(x, c_theta)
        new_vega = port_vega + np.dot(x, c_vega)
        new_vega_by_expiry = {
            exp_code: port_vega_by_expiry.get(exp_code, 0.0) + np.dot(x, c_vega_by_expiry[exp_code])
            for exp_code in c_vega_by_expiry.keys()
        }

        return new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry

    def compute_greeks_lp(self, x, candidates, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry):
        new_delta = port_delta + np.dot(x, np.array([c.delta for c in candidates]))
        new_gamma = port_gamma + np.dot(x, np.array([c.gamma for c in candidates]))
        new_theta = port_theta + np.dot(x, np.array([c.theta for c in candidates]))
        new_vega = port_vega + np.dot(x, np.array([c.vega for c in candidates]))

        expiry_codes = sorted({c.expiry_code for c in candidates if c.expiry_code != "PERP"})
        c_vega_by_expiry = {
            exp_code: np.array([c.vega if c.expiry_code == exp_code else 0.0 for c in candidates])
            for exp_code in expiry_codes
        }

        for exp_code in c_vega_by_expiry.keys():
            #c_vega_by_expiry[exp_code] = np.sum(c_vega_by_expiry[exp_code], axis=0)
            diff = np.sum(np.dot(x, c_vega_by_expiry[exp_code]))
            k=1

        new_vega_by_expiry = {
            exp_code: port_vega_by_expiry.get(exp_code, 0.0) + np.dot(x, c_vega_by_expiry[exp_code])
            for exp_code in c_vega_by_expiry.keys()
        }

        return new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry

    def extract_trades(self, x, candidates, c_dte, c_strike, c_iv_pct, c_price, c_delta, c_gamma, c_theta, c_vega,
                       c_held_qty, unwind_discount, new_position_penalty, strike_weight, c_cost_rate):
        trades = []
        for i, qty in enumerate(x):  # Extract proposed trades (filter out tiny quantities)
            if abs(qty) < 0.5:
                continue
            c = candidates[i]
            rounded_qty = round(qty)
            if rounded_qty == 0:
                continue
            cost_rate = float(c_cost_rate[i])

            # Determine if this is an unwind or new position, and cap unwind qty to the actual held position size
            held_qty = c_held_qty[i]
            is_unwind = bool((rounded_qty * held_qty) < 0)
            unwind_qty = min(abs(rounded_qty), abs(held_qty)) if is_unwind else 0
            new_qty = abs(rounded_qty) - unwind_qty

            # Compute cost split: unwind portion at discounted rate, remainder at full rate
            price_i = float(c_price[i])
            iv_pct_i = float(c_iv_pct[i])
            strike_i = float(c_strike[i])
            dte_i = int(c_dte[i])
            delta_i = float(c_delta[i] / strike_weight[i]) if strike_weight[i] != 0 else 0.0
            gamma_i = float(c_gamma[i] / strike_weight[i]) if strike_weight[i] != 0 else 0.0
            vega_i = float(c_vega[i] / strike_weight[i]) if strike_weight[i] != 0 else 0.0

            unwind_notional = unwind_qty * price_i
            new_notional = new_qty * price_i
            is_new_instrument = abs(held_qty) == 0
            cost_unwind_part = cost_rate * unwind_discount * unwind_notional
            cost_new_part = (cost_rate + (new_position_penalty if is_new_instrument else 0.0)) * new_notional

            instrument_name = ("ETH-PERPETUAL" if c.opt == "F" else f"ETH-{c.expiry_code}-{int(strike_i)}-{c.opt}")

            if unwind_qty >= 1:  # Emit separate rows for unwind vs new-position portions
                unwind_signed = int(unwind_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({"counterparty": c.counterparty, "instrument": instrument_name, "expiry": c.expiry_date,
                               "dte": dte_i, "strike": strike_i, "opt": c.opt, "qty": unwind_signed,
                               "side": "Buy" if unwind_signed > 0 else "Sell", "iv_pct": round(iv_pct_i, 1),
                               "bs_price_usd": round(price_i, 2), "notional": round(unwind_notional, 2),
                               "cost_bps": round(cost_rate * 10_000, 1), "trade_cost": round(cost_unwind_part, 2),
                               "delta_contribution": round(unwind_signed * delta_i, 4),
                               "gamma_contribution": round(unwind_signed * gamma_i, 6),
                               "vega_contribution": round(unwind_signed * vega_i, 4),
                               "is_unwind": True, "unwind_qty": int(unwind_qty), "new_qty": 0})

            if new_qty >= 1:
                new_signed = int(new_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({"counterparty": c.counterparty, "instrument": instrument_name, "expiry": c.expiry_date,
                               "dte": dte_i, "strike": strike_i, "opt": c.opt, "qty": new_signed,
                               "side": "Buy" if new_signed > 0 else "Sell", "iv_pct": round(iv_pct_i, 1),
                               "bs_price_usd": round(price_i, 2), "notional": round(new_notional, 2),
                               "cost_bps": round(cost_rate * 10_000, 1), "trade_cost": round(cost_new_part, 2),
                               "delta_contribution": round(new_signed * delta_i, 4),
                               "gamma_contribution": round(new_signed * gamma_i, 6),
                               "vega_contribution": round(new_signed * vega_i, 4),
                               "is_unwind": False, "unwind_qty": 0, "new_qty": int(new_qty)})
        return trades

    def build_payoffs(self, horizons, spot_arr, trades):
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
            for trade in trades:
                if trade["opt"] == "F":
                    # Perpetual future: P&L = qty * (spot - entry)
                    vals = spot_arr - trade["strike"]
                else:
                    dte_at_h = max(trade["dte"] - h, 0)
                    T_h = dte_at_h / 365.25
                    sigma = trade["iv_pct"] / 100.0
                    vals = bs_vec(spot_arr, trade["strike"], T_h, 0.0, sigma, trade["opt"])
                total += trade["qty"] * vals
            trade_payoff_delta[h_key] = np.round(total, 2).tolist()

        # After payoff = before + trades
        after_payoff = {}
        for h_key in before_payoff:
            before = np.array(before_payoff[h_key])
            delta_arr = np.array(trade_payoff_delta[h_key])
            after_payoff[h_key] = np.round(before + delta_arr, 2).tolist()

        return before_payoff, after_payoff

    def run_lp(self,        risk_aversion: float = 1.0,
        brokerage_txn_cost_pct: float = 5.0,
        deribit_txn_cost_pct: float = 0.1,
        max_collateral: float = 4_000_000.0,
        target_expiry: str | None = None,
        lambda_delta: float = 1.0,
        lambda_gamma: float = 1.0,
        lambda_vega: float = 100.0,
        unwind_discount: float = 0.2,
        new_position_penalty: float = 0.04,
        vega_cross_expiry_corr: float = 0.0,):

        # Liquidate all existing positions (outside of the target expiry range?)
        held_positions = self.get_held_positions()
        candidates = self._build_candidates(target_expiry=None)

        # Build a quick lookup for candidate quotes by (expiry_code, strike, opt, counterparty)
        candidate_by_key = {(c.expiry_code, c.strike, c.opt, c.counterparty): c for c in candidates}
        trades = []
        unwind_discount = 1.

        x = np.array([0.0] * len(candidates))
        i = -1
        for c in candidates:#(exp_code, strike_i, opt_i, counterparty_i), held_qty in held_positions.items():
            i += 1
            held_qty = held_positions.get((c.expiry_code, c.strike, c.opt, c.counterparty), 0)
            if held_qty == 0:
                continue
            #candidate = candidate_by_key.get((exp_code, strike_i, opt_i, counterparty_i))  # matching candidate quote if exists
            # Fallbacks if the instrument is not in candidates
            price_i = float(c.bs_price_usd) if c and c.bs_price_usd is not None else 0.0
            dte_i = int(c.dte) if c and c.dte is not None else 0
            cost_rate = float(self.compute_costs(
                self.eth_spot, [c] if c else [], perp_cost_bps=2.0, brokerage_txn_cost_pct=0.5,
                deribit_txn_cost_pct=0.1,
            )[0]) if c else 0.0

            if dte_i > 15:
                continue
            instrument_name = ("ETH-PERPETUAL" if c.opt == "F" else f"ETH-{c.expiry_code}-{int(c.strike)}-{c.opt}")

            # Close the full held quantity: long position  -> sell to unwind, short position -> buy to unwind
            unwind_signed = -int(round(held_qty))
            unwind_qty = abs(unwind_signed)
            unwind_notional = unwind_qty * price_i
            cost_unwind_part = cost_rate * unwind_discount * unwind_notional
            x[i] = unwind_signed

            trades.append({
                "counterparty": c.counterparty,
                "instrument": instrument_name,
                "expiry": c.expiry_date if c else "",
                "dte": c.dte if c else 0,
                "strike": c.strike if c else 0.0,
                "opt": c.opt,
                "qty": unwind_signed,
                "side": "Buy" if unwind_signed > 0 else "Sell",
                "iv_pct": round(c.iv_pct, 1),
                "bs_price_usd": round(c.bs_price_usd, 2),
                "notional": round(unwind_notional, 2),
                "cost_bps": round(cost_rate * 10_000, 1),
                "trade_cost": round(cost_unwind_part, 2),
                "delta_contribution": round(unwind_signed * float(c.delta), 4),
                "gamma_contribution": round(unwind_signed * float(c.gamma), 6),
                "vega_contribution": round(unwind_signed * c.vega, 4),
                "is_unwind": True,
                "unwind_qty": unwind_qty,
                "new_qty": 0,
            })

        #return trades

        port_delta, port_gamma, port_theta, port_vega = self._portfolio_greeks()
        port_vega_by_expiry = self._portfolio_vega_by_expiry()

        new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry = (
            self.compute_greeks_lp(x, candidates, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry))

        spot = self.eth_spot
        # Market parameters
        # ATM IV for daily spot vol
        atm_ivs = []
        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            strikes = smile["strikes"]
            ivs = smile["ivs"]
            best_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
            atm_ivs.append(ivs[best_idx])
        atm_iv = float(np.mean(atm_ivs)) / 100.0 if atm_ivs else 0.80
        sigma_daily = atm_iv / math.sqrt(252)
        vov_daily = self._estimate_vol_of_vol_daily()  # Vol-of-vol

        # Current portfolio risk (before optimization)
        expiry_codes = sorted({c.expiry_code for c in candidates if c.expiry_code != "PERP"})
        c_vega_by_expiry = {
            exp_code: np.array([c.vega if c.expiry_code == exp_code else 0.0 for c in candidates])
            for exp_code in expiry_codes
        }

        new_port_vega_by_expiry = {exp_code: port_vega_by_expiry.get(exp_code, 0.0) for exp_code in expiry_codes}

        risk_before = self._compute_risk(port_delta, port_gamma, port_theta, port_vega, sigma_daily, vov_daily,
                                         lambda_delta, lambda_gamma, lambda_vega,
                                         port_vega_by_expiry=new_port_vega_by_expiry,
                                         vega_cross_expiry_corr=vega_cross_expiry_corr, risk_mode=RiskMode.GAMMA_VEGA)

        risk_after = self._compute_risk(
            new_delta, new_gamma, new_theta, new_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_gamma, lambda_vega,
            port_vega_by_expiry=new_vega_by_expiry,
            vega_cross_expiry_corr=vega_cross_expiry_corr, risk_mode=RiskMode.GAMMA_VEGA,
        )

        total_cost = sum(t["trade_cost"] for t in trades)

        # ------------------------------------------------------------------
        # Compute before/after payoff curves and P&L matrix
        # ------------------------------------------------------------------
        spot_arr = np.array(self.spot_ladder, dtype=float)
        horizons = sorted(set(self.chart_horizons + [0]))
        before_payoff, after_payoff = self.build_payoffs(horizons, spot_arr, trades)

        return {
            "status": "ok",
            "snapshot_path": str(self.snapshot_path),
            "eth_spot": spot,
            "spot_ladder": self.spot_ladder,
            "chart_horizons": horizons,
            "params": {
                "risk_aversion": risk_aversion,
                "brokerage_txn_cost_pct": brokerage_txn_cost_pct,
                "deribit_txn_cost_pct": deribit_txn_cost_pct,
                "max_collateral": max_collateral,
                "atm_iv_pct": round(atm_iv * 100, 1),
                "sigma_daily": round(sigma_daily, 4),
                "vov_daily": round(vov_daily, 2),
                "vega_cross_expiry_corr": round(vega_cross_expiry_corr, 2),
                "lambda_delta": lambda_delta,
                "lambda_gamma": lambda_gamma,
                "lambda_vega": lambda_vega,
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
            "total_trade_cost": round(total_cost, 2),
            "utility_improvement": round(risk_before - risk_after - total_cost, 2),
            "candidates_evaluated": len(candidates),
            "optimizer_converged": True,
        }

        # Build candidates for the target expiry range: 10% / ATM / 10% iron condors, and ETH-PERPETUAL
        expiry_codes = sorted(
            {s["expiry_code"] for s in self.vol_surface if s.get("dte", 0) > 0},
            key=expiry_sort_key,
        )

        picked = self._pick_two_monthly_expiries(expiry_codes)
        if len(picked) < 2:
            raise ValueError("Need at least 2 monthly expiries with DTE > 28 days")

        front_expiry, front_dte = picked[0]
        back_expiry, back_dte = picked[1]
        print(f"Selected expiries: {front_expiry} ({front_dte}d), {back_expiry} ({back_dte}d)")

        # Build candidates for the target expiry range: front expiry structure + back expiry structure
        front_candidates = self._build_candidates(target_expiry=front_expiry)
        back_candidates = self._build_candidates(target_expiry=back_expiry)

        if not front_candidates or not back_candidates:
            raise ValueError("Could not build candidates for one or both selected expiries")

        front_condor = self._pick_iron_condor_legs(front_candidates, front_expiry)
        back_condor = self._pick_iron_condor_legs(back_candidates, back_expiry)

        selected_candidates = front_condor + back_condor

        price_by_expiry = {
            front_expiry: self._condor_price(front_condor),
            back_expiry: self._condor_price(back_condor),
        }
        # Now the LP works only on those 8-ish legs
        candidates = selected_candidates
        # Solve the LP: maximize x*front_ic_qty + (1-x)*back_ic_qty under collateral constraints
        collateral_by_counterparty = {"FlowDesk": 8750000, "KeyRock": 7926168}
        cost_by_counterparty= {"FlowDesk": 0.01, "KeyRock": 0.05}
        solver = PulpSolver()
        solver.solve(price_by_expiry, cost_by_counterparty, collateral_by_counterparty)

        # Build trades

        return

    def _pick_two_monthly_expiries(self, expiry_codes: list[str], min_dte: int = 29) -> list[tuple[str, int]]:
        today = date.today()
        valid: list[tuple[int, str]] = []

        for code in expiry_codes:
            try:
                exp_date = datetime.strptime(code, "%d%b%y").date()
            except ValueError:
                continue

            dte = (exp_date - today).days
            if dte >= min_dte:
                valid.append((dte, code))

        valid.sort(key=lambda x: (x[0], expiry_sort_key(x[1])))
        return [(code, dte) for dte, code in valid[:2]]

    def _pick_iron_condor_legs(
            self,
            candidates: list[Candidate],
            target_expiry: str,
            wing_target: float = 10.0,
            body_target: float = 50.0,
    ) -> list[Candidate]:
        expiry_candidates = [c for c in candidates if c.expiry_code == target_expiry and c.opt in ("C", "P")]
        if not expiry_candidates:
            raise ValueError(f"No option candidates found for expiry {target_expiry}")

        puts = [c for c in expiry_candidates if c.opt == "P"]
        calls = [c for c in expiry_candidates if c.opt == "C"]

        if not puts or not calls:
            raise ValueError(f"Need both puts and calls to build iron condor for {target_expiry}")

        def score(c: Candidate, target: float) -> float:
            # Use iv_pct as the "percentage" signal if that's how your surface is encoded.
            # If you prefer delta-based selection, replace this with abs(abs(c.delta) - target/100).
            return abs(abs(float(c.delta or 0.0)) * 100.0 - target)

        put_wing = min(puts, key=lambda c: score(c, wing_target))
        put_body = min(puts, key=lambda c: score(c, body_target))
        call_body = min(calls, key=lambda c: score(c, body_target))
        call_wing = min(calls, key=lambda c: score(c, wing_target))

        # Deduplicate if the surface is sparse and the same strike is chosen twice
        chosen = []
        seen = set()
        for leg in (put_wing, put_body, call_body, call_wing):
            key = (leg.expiry_code, leg.strike, leg.opt, leg.counterparty)
            if key not in seen:
                chosen.append(leg)
                seen.add(key)

        return chosen

    def _condor_price(self, legs: list[Candidate]) -> float:
        # Net premium of the structure:
        # long legs paid, short legs received
        total = 0.0
        for leg in legs:
            qty_sign = 1.0
            if leg.opt in ("C", "P"):
                # use candidate side implied by the current trade setup:
                # if you later attach explicit long/short intent, replace this
                qty_sign = 1.0
            total += qty_sign * leg.bs_price_usd
        return total
