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
import numpy as np
from enum import Enum
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

from .models import Position, Candidate, load_positions_from_latest_xlsx, SpreadCandidate, StraddleCandidate, IronCondorCandidate
from .math_utils import bs_price, bs_vec, bs_greeks
from .option_smile import OptionSmile
from .snapshot import load_snapshot_dict
from .optimizer_utils import expiry_sort_key, safe_num

#from .portfolio import load_positions

Counterparties = ["Keyrock", "Flowdesk", "Deribit"]


class RiskMode(Enum):
    DELTA_ONLY = "delta_only"
    GAMMA_VEGA = "gamma_vega"
    FULL = "full"


class BaseOptimizer:
    """Holds all data needed for portfolio optimization."""

    def __init__(
        self,
        spot: float,
        spot_ladder: list[float],
        matrix_horizons: list[int],
        chart_horizons: list[int],
        vol_surface: list[dict],
        positions: list[Position],
        totals: dict,
        snapshot_path: Path,
        today: date,
        asset: str = "ETH",
    ):
        self.asset = asset.upper()
        self.spot = spot
        #self.spot = spot  # backward-compatible alias
        self.spot_ladder = spot_ladder
        self.matrix_horizons = matrix_horizons
        self.chart_horizons = chart_horizons
        self.vol_surface = vol_surface
        self.positions = positions
        self.totals = totals
        self.snapshot_path = snapshot_path
        self.today = today

    @classmethod
    def from_snapshot_dict(cls, data: dict, today: datetime.date) -> "BaseOptimizer":
        snapshot_data, positions = load_snapshot_dict(data)

        latest_positions = load_positions_from_latest_xlsx()
        if latest_positions:
            positions = latest_positions

        spot = snapshot_data.get("spot")
        return cls(
            spot=spot,
            spot_ladder=snapshot_data["spot_ladder"],
            matrix_horizons=snapshot_data["matrix_horizons"],
            chart_horizons=snapshot_data["chart_horizons"],
            vol_surface=snapshot_data["vol_surface"],
            positions=positions,
            totals=snapshot_data["totals"],
            snapshot_path=Path(snapshot_data.get("snapshot_path", "")),
            today=today,
            asset=snapshot_data.get("asset", "ETH"),
        )

    # ------------------------------------------------------------------
    # Build candidate instruments from vol surface
    # ------------------------------------------------------------------

    def _build_candidates(
        self,
        target_expiry: str | None = None,
        include_itm: bool = False,
        counterparties: list[str] | None = None,
    ) -> list[Candidate]:
        selected_counterparties = {
            c.strip()
            for c in (counterparties or [])
            if c and c.strip() and c.strip().upper() != "ALL"
        }
        """Generate tradeable instruments from the vol surface.

        Parameters
        ----------
        target_expiry : optional expiry code to restrict to (e.g. "29MAY26").
                        If None, uses ALL available expiries.
        """
        S = self.spot
        candidates = []

        # Build a lookup of currently held option positions so that in ALL-maturities
        # mode we only consider instruments we already have a position in.
        held_option_keys: set[tuple[str, float, str, str]] = set()
        held_expiry_codes: set[str] = set()
        for p in self.positions:
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
                key = (exp_code, p.strike, p.opt, p.counterparty)
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
                if True:#smile["expiry_code"] in held_expiry_codes:
                    matching_smiles.append(smile)

        option_smile = OptionSmile([{"expiry_code": smile["expiry_code"], "expiry_date": smile["expiry_date"],
                                     "strikes": smile["strikes"], "ivs": [iv/100.0 for iv in smile["ivs"]],}
            for smile in matching_smiles
        ])

        # Strike filter: 50%–200% of spot. TODO: replace with delta criteria
        strike_lo = S * 0.25
        strike_hi = S * 4.00

        candidate_by_key = {}

        held_keys_by_expiry = defaultdict(set)
        for exp_code, strike, opt, counterparty in held_option_keys:
            held_keys_by_expiry[datetime.strptime(exp_code, "%d%b%y")].add((exp_code, strike, opt, counterparty))
        held_keys_by_expiry = dict(sorted(held_keys_by_expiry.items(), key=lambda kv: kv[0]))

        tt = 0
        for smile in matching_smiles:
            expiry_code = smile["expiry_code"]
            expiry_date = smile["expiry_date"]
            dte = (datetime.strptime(expiry_date, "%Y-%m-%d") - self.today).days
            strikes = smile["strikes"]
            ivs = smile["ivs"]
            counterparty = "Deribit"

            for strike, iv_pct in zip(strikes, ivs):
                if strike < strike_lo or strike > strike_hi:
                    continue
                sigma = iv_pct / 100.0
                if sigma <= 0 or strike <= 0:
                    continue

                for opt in ("C", "P"):
                    if not include_itm:
                        if opt == "C" and (strike < S or strike > strike_hi):
                            continue
                        elif opt == "P" and (strike > S or strike < strike_lo):
                            continue

                    candidates.append(self.create_candidate(S, strike, 0.0, sigma, opt,
                                                            expiry_code, expiry_date, dte, counterparty))

            expiry = datetime.strptime(expiry_code, "%d%b%y")
            while list(held_keys_by_expiry.keys())[tt] <= expiry and tt < len(held_keys_by_expiry.keys())-1:
                held_option_keys = list(held_keys_by_expiry.values())[tt]
                for option_key in held_option_keys:
                    if option_key[0] == expiry_code or (tt < 1 and target_expiry is None):
                        exp_code = option_key[0]
                        expiry = datetime.strptime(exp_code, "%d%b%y")
                        strike = option_key[1]
                        opt = option_key[2]
                        counterparty = option_key[3]
                        dte = (expiry - self.today).days
                        sigma = option_smile.compute_vol(expiry, strike)
                        c = self.create_candidate(S, strike, 0., sigma, opt, exp_code, expiry, dte, counterparty)
                        candidate_by_key[(c.expiry_code, c.strike, c.opt, c.counterparty)] = c
                        candidates.append(c)
                tt += 1
        # ETH perpetual future: delta=1, no gamma/theta/vega, price = spot
        if target_expiry is None:
            perp_candidate = self.create_candidate(S, S, 0.0, 0.0, "F", "PERP", "",
                                                   0, "Deribit")
            perp_candidate.delta = 1
            candidates.append(perp_candidate)

        return candidates

    def _build_spread_candidates(self, candidates: list[Candidate], target_expiry: str | None = None) -> list[SpreadCandidate]:
        """
        Build vertical spread candidates from single-option candidates.

        Call spreads:
            long lower call, short higher call, both strikes > spot.

        Put spreads:
            long higher put, short lower put, both strikes < spot.

        One optimizer unit = one long spread.
        Negative optimizer quantity naturally means selling the spread.
        """
        option_candidates = [
            c for c in candidates
            if c.opt in ("C", "P")
               and c.strike is not None
               and c.bs_price_usd is not None
               and (target_expiry is None or c.expiry_code == target_expiry)
        ]

        spreads: list[SpreadCandidate] = []

        grouped: dict[tuple[str, str, str], list[Candidate]] = {}
        for c in option_candidates:
            key = (c.expiry_code, c.opt, c.counterparty)
            grouped.setdefault(key, []).append(c)

        for (expiry_code, opt, counterparty), legs in grouped.items():
            legs = sorted(legs, key=lambda c: float(c.strike))

            if opt == "C":
                calls = [c for c in legs if float(c.strike) > self.spot]

                for i, lower_call in enumerate(calls):
                    for higher_call in calls[i + 1:]:
                        spreads.append(
                            SpreadCandidate(
                                kind="CALL_SPREAD",
                                long_leg=lower_call,
                                short_leg=higher_call,
                            )
                        )

            elif opt == "P":
                puts = [c for c in legs if float(c.strike) < self.spot]

                # For a debit put spread:
                # long higher-strike put, short lower-strike put.
                descending_puts = sorted(puts, key=lambda c: float(c.strike), reverse=True)

                for i, higher_put in enumerate(descending_puts):
                    for lower_put in descending_puts[i + 1:]:
                        spreads.append(
                            SpreadCandidate(
                                kind="PUT_SPREAD",
                                long_leg=higher_put,
                                short_leg=lower_put,
                            )
                        )

        return spreads

    def _build_straddle_candidates(self, candidates: list[Candidate], target_expiry: str | None = None) -> list[StraddleCandidate]:
        """
        Build straddle candidates from single-option candidates.

        Straddles:
            long call + long put, same expiry, same strike, same counterparty.

        One optimizer unit = one long straddle.
        Negative optimizer quantity naturally means selling the straddle.
        """
        option_candidates = [
            c for c in candidates
            if c.opt in ("C", "P")
               and c.strike is not None
               and c.bs_price_usd is not None
               and (target_expiry is None or c.expiry_code == target_expiry)
        ]

        grouped: dict[tuple[str, float, str], dict[str, Candidate]] = {}
        for c in option_candidates:
            key = (c.expiry_code, float(c.strike), c.counterparty)
            grouped.setdefault(key, {})[c.opt] = c

        straddles: list[StraddleCandidate] = []
        for _key, legs_by_opt in grouped.items():
            call_leg = legs_by_opt.get("C")
            put_leg = legs_by_opt.get("P")

            if call_leg is None or put_leg is None:
                continue

            straddles.append(
                StraddleCandidate(
                    kind="STRADDLE",
                    call_leg=call_leg,
                    put_leg=put_leg,
                )
            )

        return straddles

    def _build_iron_condor_candidates(
            self,
            option_legs: list[Candidate],
            target_expiry: str | None = None,
            min_body_width: float = 100.0,
            max_body_width: float = 1200.0,
            max_wing_width: float = 1200.0,
            max_candidates_per_expiry: int = 80,
    ) -> list[IronCondorCandidate]:
        condors: list[IronCondorCandidate] = []

        expiry_codes = sorted(
            {c.expiry_code for c in option_legs if c.opt in ("C", "P")},
            key=expiry_sort_key,
        )

        for expiry_code in expiry_codes:
            if target_expiry is not None and expiry_code != target_expiry:
                continue

            expiry_legs = [
                c for c in option_legs
                if c.expiry_code == expiry_code
                   and c.opt in ("C", "P")
            ]

            puts = sorted(
                [c for c in expiry_legs if c.opt == "P"],
                key=lambda c: float(c.strike or 0.0),
            )
            calls = sorted(
                [c for c in expiry_legs if c.opt == "C"],
                key=lambda c: float(c.strike or 0.0),
            )

            if len(puts) < 2 or len(calls) < 2:
                continue

            expiry_condors: list[IronCondorCandidate] = []

            for put_low in puts:
                for put_high in puts:
                    if put_low.strike >= put_high.strike:
                        continue

                    put_wing_width = float(put_high.strike) - float(put_low.strike)
                    if put_wing_width > max_wing_width:
                        continue

                    for call_low in calls:
                        if call_low.strike <= put_high.strike:
                            continue

                        body_width = float(call_low.strike) - float(put_high.strike)
                        if body_width < min_body_width or body_width > max_body_width:
                            continue

                        for call_high in calls:
                            if call_high.strike <= call_low.strike:
                                continue

                            call_wing_width = float(call_high.strike) - float(call_low.strike)
                            if call_wing_width > max_wing_width:
                                continue

                            expiry_condors.append(
                                IronCondorCandidate(
                                    kind="IRON_CONDOR",
                                    put_low_leg=put_low,
                                    put_high_leg=put_high,
                                    call_low_leg=call_low,
                                    call_high_leg=call_high,
                                )
                            )

            expiry_condors.sort(
                key=lambda c: (
                    abs(float(c.delta or 0.0)),
                    -abs(float(c.vega or 0.0)),
                    abs(float(c.call_low_leg.strike) - float(c.put_high_leg.strike)),
                )
            )
            condors.extend(expiry_condors[:max_candidates_per_expiry])

        return condors

    def _estimate_vol_of_vol_daily(self) -> float:
        """Estimate daily vol-of-vol from the vol surface term structure.

        Uses the standard deviation of ATM IVs across expiries as a proxy
        for how much IV moves, scaled to daily.
        """
        atm_ivs = []
        dtes = []
        S = self.spot

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
        delta = sum((p.delta or 0) * p.net_qty*(1. if p.side=='Long' else -1.) for p in self.positions)
        gamma = sum((p.gamma or 0) * p.net_qty*(1. if p.side=='Long' else -1.) for p in self.positions)
        theta = sum((p.theta or 0) * p.net_qty*(1. if p.side=='Long' else -1.) for p in self.positions)
        vega = sum((p.vega or 0) * p.net_qty*(1. if p.side=='Long' else -1.) for p in self.positions)
        return delta, gamma, theta, vega

    def _portfolio_vega_by_expiry(self) -> dict[datetime, float]:
        """Return portfolio vega bucketed by expiry code."""
        vega_by_expiry: dict[datetime, float] = {}
        for p in self.positions:
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
            else:
                exp_code = "UNKNOWN"
            expiry = datetime.strptime(exp_code, "%d%b%y")
            vega_by_expiry[expiry] = (vega_by_expiry.get(expiry, 0.0) + (p.vega or 0.0) * p.net_qty
                                      * (1. if p.side=='Long' else -1.))
        vega_by_expiry = dict(sorted(vega_by_expiry.items(), key=lambda item: item[0]))

        return vega_by_expiry

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
            lambda_vega: float = 1.0,
            port_vega_by_expiry: dict[str, float] | None = None,
            vega_cross_expiry_corr: float = 0.35,
            risk_mode: RiskMode = RiskMode.FULL,
    ) -> float:
        """Weighted daily P&L standard deviation from greeks.

        Vega is treated as expiry-bucketed. Offsetting vega across expiries is
        only partial, controlled by vega_cross_expiry_corr in [0, 1]:
          - 0.0 => expiries independent, no cross-expiry netting
          - 1.0 => fully shared vol shock, equivalent to total-vega netting
        """
        S = self.spot

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

        if risk_mode == RiskMode.DELTA_ONLY:
            return np.sign(lambda_delta) * math.sqrt(np.abs(lambda_delta) * var_delta)
        elif risk_mode == RiskMode.GAMMA_VEGA:
            return np.sign(lambda_gamma) * math.sqrt(np.abs(lambda_gamma) * var_gamma + lambda_vega * var_vega)
        else:
            lam_var = lambda_delta * var_delta + lambda_gamma * var_gamma + lambda_vega * var_vega
            return np.sign(lam_var) * math.sqrt(np.abs(lam_var))
    # ------------------------------------------------------------------
    # Run optimization
    # ------------------------------------------------------------------

    def run(
        self,
        lam_factor: float = 1.0,
        target_expiry: str | None = None,
        unwind_discount: float = 0.2,
        new_position_penalty: float = 0.04,
        roll_dte_threshold: int | None = 7,
    ) -> dict:
        """Run the optimization and return proposed trades.

        Parameters
        ----------
        unwind_discount : float
            Multiplier on txn cost for closing existing positions (0.2 = 80% cheaper).
        new_position_penalty : float
            Extra cost per dollar notional for trades in instruments not already held.
        """
        print(f"Running base optimizer")

    def compute_costs(self, spot, candidates, perp_cost_bps, brokerage_txn_cost_pct, deribit_txn_cost_pct):
        c_cost_list = []
        for c in candidates:
            if c.opt == "F":
                cost = spot * perp_cost_bps / 10_000.0
            elif c.counterparty == "Deribit":
                cost = spot * deribit_txn_cost_pct / 100.0
            elif c.counterparty == "FlowDesk" or c.counterparty == "KeyRock":
                cost = brokerage_txn_cost_pct / 100.0
            else:
                cost = float('nan')
            c_cost_list.append(cost)
        c_cost_rate = np.array(c_cost_list)

        return c_cost_rate

    def create_candidate(self, S, strike, r, sigma, opt,
                         expiry_code, expiry_date, dte, counterparty):
        delta, gamma, theta, vega, price = bs_greeks(
            S, strike, dte/365.25, r, sigma, opt
        )
        return Candidate(
            expiry_code=expiry_code,
            expiry_date=expiry_date,
            dte=dte,
            strike=strike,
            opt=opt,
            iv_pct=sigma*100.,
            counterparty=counterparty,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            bs_price_usd=price,
        )

    def get_held_positions(self):
        # Build a map of existing positions keyed by (expiry_code, strike, opt)
        # so we know which candidates correspond to held instruments.

        held_positions: dict[tuple[str, float, str, str], float] = {}
        for p in self.positions:
            # Extract expiry code from instrument name (e.g. "ETH-29MAY26-3000-C")
            parts = p.instrument.split("-")
            if len(parts) >= 4:
                exp_code = parts[1]
            else:
                exp_code = ""
            key = (exp_code, p.strike, p.opt, p.counterparty)
            mult = 1.0 if p.side == "Long" else -1.0
            held_positions[key] = held_positions.get(key, 0.0) + mult*p.net_qty
        return held_positions
