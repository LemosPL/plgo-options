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
from datetime import datetime

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

#from plgo_options.optimization.optim_usecase import OptimizerRunParams, OptimizerUseCase


Counterparties = ["Keyrock", "Flowdesk", "Deribit"]

@dataclass
class Position:
    id: int
    instrument: str
    opt: str
    counterparty: str | None
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
    counterparty: str
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

    @staticmethod
    def _safe_num(value, default: float = 0.0) -> float:
        """Convert possibly-missing numeric values to float."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_snapshot_dict(cls, data: dict) -> OptimizerV2:
        """Load an OptimizerV2 instance from an in-memory snapshot dict."""
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
                counterparty=p.get("counterparty", "brokerage"),
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
            snapshot_path=Path(data.get("snapshot_path", "")),
        )

    @classmethod
    def from_snapshot(cls, path: Path) -> OptimizerV2:
        """Load an OptimizerV2 instance from a snapshot JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_snapshot_dict(data)

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

        # Build a lookup of currently held option positions so that in ALL-maturities
        # mode we only consider instruments we already have a position in.
        held_option_keys: set[tuple[str, float, str]] = set()
        held_expiry_codes: set[str] = set()
        for p in self.positions:
            print(f"Position: {p.instrument}")
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
                key = (exp_code, p.strike, p.opt)
                held_option_keys.add(key)
                held_expiry_codes.add(exp_code)

        # Filter smiles
        matching_smiles = []
        for smile in self.vol_surface:
            if smile["dte"] <= 0:
                continue
            if target_expiry:
                if smile["expiry_code"] == target_expiry:
                    matching_smiles.append(smile)
            else:
                # ALL-expiries mode: only include expiries where we currently hold positions
                if smile["expiry_code"] in held_expiry_codes:
                    matching_smiles.append(smile)

        # Strike filter: 50%–200% of spot
        strike_lo = S * 0.25
        strike_hi = S * 4.00

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

                #for#
                for opt in ("C", "P"):
                    for counterparty in Counterparties:
                        # In ALL-expiries mode, only keep option contracts we already hold.
                        # Perp is always included separately below.
                        if counterparty == "Deribit":
                            if opt == "C" and (strike < S or strike > S * 2):
                                continue
                            elif opt == "P" and (strike > S or strike < S * 0.5):
                                continue
                        elif (target_expiry is None and (expiry_code, strike, opt) not in held_option_keys):
                            continue

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
                            counterparty=counterparty,
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
            counterparty="deribit",
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

    def _portfolio_vega_by_expiry(self) -> dict[str, float]:
        """Return portfolio vega bucketed by expiry code."""
        vega_by_expiry: dict[str, float] = {}
        for p in self.positions:
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
            else:
                exp_code = "UNKNOWN"
            vega_by_expiry[exp_code] = vega_by_expiry.get(exp_code, 0.0) + (p.vega or 0.0) * p.net_qty
        return vega_by_expiry

    def _expiry_sort_key(self, expiry_code: str) -> tuple[int, str]:
        """Sort expiry buckets chronologically from codes like 29MAY26."""
        if expiry_code == "PERP":
            return (-1, expiry_code)

        try:
            expiry_date = datetime.strptime(expiry_code.upper(), "%d%b%y").date()
            return (expiry_date.toordinal(), expiry_code)
        except ValueError:
            return (10**9, expiry_code)

    def _active_position_expiry_codes(self) -> list[str]:
        """Return active expiry codes present in current positions, ordered by expiry."""
        expiry_codes: set[str] = set()
        for p in self.positions:
            parts = p.instrument.split("-")
            if len(parts) >= 4 and parts[1]:
                expiry_codes.add(parts[1])
        print(f"Active expiry codes: {expiry_codes}")
        return sorted(expiry_codes, key=self._expiry_sort_key)

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
            lambda_gamma: float = 1.0,
            lambda_vega: float = 100.0,
            port_vega_by_expiry: dict[str, float] | None = None,
            vega_cross_expiry_corr: float = 0.35,
    ) -> float:
        """Weighted daily P&L standard deviation from greeks.

        Vega is treated as expiry-bucketed. Offsetting vega across expiries is
        only partial, controlled by vega_cross_expiry_corr in [0, 1]:
          - 0.0 => expiries independent, no cross-expiry netting
          - 1.0 => fully shared vol shock, equivalent to total-vega netting
        """
        S = self.eth_spot

        # Delta P&L variance: (Δ · S · σ_daily)²
        var_delta = (port_delta * S * sigma_daily) ** 2

        # Gamma P&L variance: (½ · Γ · S² · σ_daily²)²
        var_gamma = (0.5 * port_gamma * S ** 2 * sigma_daily ** 2) ** 2

        if port_vega_by_expiry:
            rho = min(max(vega_cross_expiry_corr, 0.0), 1.0)
            bucket_vars = [
                (vega_bucket * vov_daily) ** 2
                for vega_bucket in port_vega_by_expiry.values()
            ]
            total_vega = sum(port_vega_by_expiry.values())
            shared_var_vega = (total_vega * vov_daily) ** 2
            var_vega = (1.0 - rho) * sum(bucket_vars) + rho * shared_var_vega
        else:
            # Backward-compatible fallback
            var_vega = (port_vega * vov_daily) ** 2

        return math.sqrt(
            lambda_delta * var_delta
            + lambda_gamma * var_gamma
            + lambda_vega * var_vega
        )

    # ------------------------------------------------------------------
    # Run optimization
    # ------------------------------------------------------------------

    def run(
        self,
        risk_aversion: float = 1.0,
        brokerage_txn_cost_pct: float = 5.0,
        deribit_txn_cost_pct: float = 0.15,
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
        print(f"Running optimization with risk aversion {risk_aversion:.2f}...")
        S = self.eth_spot
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
        c_delta = np.array(
            [0.0 if c.delta is None else float(c.delta) for c in candidates],
            dtype=float,
        )
        c_gamma = np.array(
            [0.0 if c.gamma is None else float(c.gamma) for c in candidates],
            dtype=float,
        )
        c_theta = np.array(
            [self._safe_num(c.theta) for c in candidates],
            dtype=float,
        )
        c_vega = np.array(
            [self._safe_num(c.vega) for c in candidates],
            dtype=float,
        )
        c_price = np.array(
            [max(self._safe_num(c.bs_price_usd), 0.0) for c in candidates],
            dtype=float,
        )
        c_iv_pct = np.array(
            [max(self._safe_num(c.iv_pct), 0.0) for c in candidates],
            dtype=float,
        )
        c_strike = np.array(
            [self._safe_num(c.strike) for c in candidates],
            dtype=float,
        )
        c_dte = np.array(
            [int(self._safe_num(c.dte)) for c in candidates],
            dtype=int,
        )

        # ----------------------------------------------------------
        # Strike weighting: ensure OTM strikes get a minimum weight.
        #
        # IMPORTANT:
        # Weighting is done within each expiry bucket, not globally
        # across all candidates. Otherwise, an all-expiry run can
        # distort the effective greeks of a given expiry versus
        # running that expiry on its own.
        # ----------------------------------------------------------
        min_strike_weight_pct = 0.10  # 10% of the max weight per expiry
        strike_weight = np.ones(n)

        expiry_to_indices: dict[str, list[int]] = {}
        for i, c in enumerate(candidates):
            expiry_to_indices.setdefault(c.expiry_code, []).append(i)

        for exp_code, idxs in expiry_to_indices.items():
            idx_arr = np.array(idxs, dtype=int)

            greek_mag_bucket = np.sqrt(
                (c_delta[idx_arr] * S) ** 2
                + (c_gamma[idx_arr] * S**2) ** 2
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
        PERP_COST_BPS = 5.0  # 5 basis points = 0.05%
        c_cost_rate = np.array([
            PERP_COST_BPS / 10_000.0 if c.opt == "F" else brokerage_txn_cost_pct / 100.0
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

        expiry_codes = sorted({c.expiry_code for c in candidates if c.expiry_code != "PERP"})
        c_vega_by_expiry = {
            exp_code: np.array([
                c_vega[i] if candidates[i].expiry_code == exp_code else 0.0
                for i in range(n)
            ])
            for exp_code in expiry_codes
        }

        # Current portfolio risk (before optimization)
        risk_before = self._compute_risk(
            port_delta, port_gamma, port_theta, port_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_gamma, lambda_vega,
            port_vega_by_expiry=port_vega_by_expiry,
            vega_cross_expiry_corr=vega_cross_expiry_corr,
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
            new_vega_by_expiry = {
                exp_code: port_vega_by_expiry.get(exp_code, 0.0) + np.dot(x, c_vega_by_expiry[exp_code])
                for exp_code in expiry_codes
            }

            risk = self._compute_risk(
                new_delta, new_gamma, new_theta, new_vega,
                sigma_daily, vov_daily, lambda_delta, lambda_gamma, lambda_vega,
                port_vega_by_expiry=new_vega_by_expiry,
                vega_cross_expiry_corr=vega_cross_expiry_corr,
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

            return cost - risk_aversion * risk_reduction

        # Bounds: unwind-only
        bounds = []
        for c, held_qty, price in zip(candidates, c_held_qty, c_price):
            if c.opt == "F":
                # ETH-PERPETUAL stays unrestricted
                bounds.append((-max_collateral / max(price, 1.0), max_collateral / max(price, 1.0)))
            elif c.counterparty == "Deribit":
                bounds.append((-max_collateral / max(price, 1.0), max_collateral / max(price, 1.0)))
            elif held_qty > 0:
                # long option: can only sell to reduce/close
                bounds.append((-abs(held_qty), 0.0))
            elif held_qty < 0:
                # short option: can only buy to reduce/close
                bounds.append((0.0, abs(held_qty)))
            else:
                # no existing option position: no trade
                bounds.append((0.0, 0.0))

        # Start from zero (no trades)
        x0 = np.zeros(n)

        result = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=bounds,
            options={"maxiter": 2000, "ftol": 1e-10},
        )

        # Extract proposed trades (filter out tiny quantities)
        trades = []
        for i, qty in enumerate(result.x):
            if abs(qty) < 0.5:
                continue
            c = candidates[i]
            rounded_qty = round(qty)
            if rounded_qty == 0:
                continue
            cost_rate = float(c_cost_rate[i])

            # Determine if this is an unwind or new position
            held_qty = c_held_qty[i]
            is_unwind = bool((rounded_qty * held_qty) < 0)
            # Cap unwind qty to the actual held position size
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
            cost_new_part = (
                cost_rate + (new_position_penalty if is_new_instrument else 0.0)
            ) * new_notional

            instrument_name = (
                "ETH-PERPETUAL" if c.opt == "F"
                else f"ETH-{c.expiry_code}-{int(strike_i)}-{c.opt}"
            )

            # Emit separate rows for unwind vs new-position portions
            if unwind_qty >= 1:
                unwind_signed = int(unwind_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({
                    "counterparty": c.counterparty,
                    "instrument": instrument_name,
                    "expiry": c.expiry_date,
                    "dte": dte_i,
                    "strike": strike_i,
                    "opt": c.opt,
                    "qty": unwind_signed,
                    "side": "Buy" if unwind_signed > 0 else "Sell",
                    "iv_pct": round(iv_pct_i, 1),
                    "bs_price_usd": round(price_i, 2),
                    "notional": round(unwind_notional, 2),
                    "cost_bps": round(cost_rate * 10_000, 1),
                    "trade_cost": round(cost_unwind_part, 2),
                    "delta_contribution": round(unwind_signed * delta_i, 4),
                    "gamma_contribution": round(unwind_signed * gamma_i, 6),
                    "vega_contribution": round(unwind_signed * vega_i, 4),
                    "is_unwind": True,
                    "unwind_qty": int(unwind_qty),
                    "new_qty": 0,
                })

            if new_qty >= 1:
                new_signed = int(new_qty) * (1 if rounded_qty > 0 else -1)
                trades.append({
                    "counterparty": c.counterparty,
                    "instrument": instrument_name,
                    "expiry": c.expiry_date,
                    "dte": dte_i,
                    "strike": strike_i,
                    "opt": c.opt,
                    "qty": new_signed,
                    "side": "Buy" if new_signed > 0 else "Sell",
                    "iv_pct": round(iv_pct_i, 1),
                    "bs_price_usd": round(price_i, 2),
                    "notional": round(new_notional, 2),
                    "cost_bps": round(cost_rate * 10_000, 1),
                    "trade_cost": round(cost_new_part, 2),
                    "delta_contribution": round(new_signed * delta_i, 4),
                    "gamma_contribution": round(new_signed * gamma_i, 6),
                    "vega_contribution": round(new_signed * vega_i, 4),
                    "is_unwind": False,
                    "unwind_qty": 0,
                    "new_qty": int(new_qty),
                })

        print(np.array(trades))

        # Post-optimization portfolio greeks
        opt_x = result.x
        new_delta = port_delta + np.dot(opt_x, c_delta)
        new_gamma = port_gamma + np.dot(opt_x, c_gamma)
        new_theta = port_theta + np.dot(opt_x, c_theta)
        new_vega = port_vega + np.dot(opt_x, c_vega)
        new_vega_by_expiry = {
            exp_code: port_vega_by_expiry.get(exp_code, 0.0) + np.dot(opt_x, c_vega_by_expiry[exp_code])
            for exp_code in expiry_codes
        }

        risk_after = self._compute_risk(
            new_delta, new_gamma, new_theta, new_vega,
            sigma_daily, vov_daily, lambda_delta, lambda_gamma, lambda_vega,
            port_vega_by_expiry=new_vega_by_expiry,
            vega_cross_expiry_corr=vega_cross_expiry_corr,
        )

        total_cost = sum(t["trade_cost"] for t in trades)

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
    theta = (
        -(S * pdf_d1 * sigma) / (2 * sqrtT)
        - r * K * math.exp(-r * T) * (norm.cdf(d2) if opt == "C" else norm.cdf(-d2))
    ) / 365.25

    return delta, gamma, theta, vega, price