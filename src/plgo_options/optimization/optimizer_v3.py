from __future__ import annotations

import math
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date, datetime

from matplotlib import figure
from scipy.ndimage import gaussian_filter1d

from .base_optimizer import BaseOptimizer, RiskMode
from .collateral_optimization import CollateralOptimization
from .elastic_net import GeneralizedLasso
from .models import Position, Candidate
from .math_utils import bs_vec
from .option_smile import OptionSmile
from .pulp_solver import PulpSolver
from .snapshot import load_snapshot_dict
from .optimizer_utils import expiry_sort_key, safe_num, get_expiry_code
from .misc_utils import build_parametric_target_profile

import matplotlib.pyplot as plt

from ..pricing import options


class OptimizerV3(BaseOptimizer):
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
        asset: str = "ETH"
    ):
        super().__init__(spot, spot_ladder, matrix_horizons, chart_horizons, vol_surface, positions, totals,
                         snapshot_path, today, asset=asset)
        self.cost = None
        self.risk_reduction = None

    def _estimate_trade_cost(
        self,
        qty: float,
        price: float,
        held_qty: float = 0.0,
        unwind_discount: float = 0.2,
        new_position_penalty: float = 0.04,
        is_held: bool = False,
    ) -> float:
        """
        Estimate transaction cost for a single leg.
        """
        abs_qty = abs(float(qty))
        price = max(float(price), 0.0)

        opposite = (qty * held_qty) < 0
        unwind_abs = min(abs(float(held_qty)), abs_qty) if opposite else 0.0
        new_abs = abs_qty - unwind_abs

        unwind_cost = unwind_abs * price * unwind_discount
        new_cost = new_abs * price * (1.0 + (0.0 if is_held else new_position_penalty))

        return unwind_cost + new_cost

    # Current portfolio payoff from held positions, at expiry
    def terminal_payoff_for_position(self, spot_arr, p: Position) -> np.ndarray:
        qty = float(getattr(p, "net_qty", 0.0) or 0.0)
        side = str(getattr(p, "side", "")).lower()
        signed_qty = qty if side == "long" else -qty

        strike = float(getattr(p, "strike", 0.0) or 0.0)
        opt = str(getattr(p, "opt", "") or "")
        if opt == "C":
            return signed_qty * np.maximum(spot_arr - strike, 0.0)
        if opt == "P":
            return signed_qty * np.maximum(strike - spot_arr, 0.0)
        if opt == "F":
            return signed_qty * (spot_arr - strike)
        return np.zeros_like(spot_arr)

    def _get_roll_positions(self, roll_dte_threshold: int | None, roll_itm_only: bool = False) -> list[Position]:
        if roll_dte_threshold is None:
            return []

        roll_positions = []
        for p in self.positions:
            opt = str(getattr(p, "opt", "") or "")
            if opt not in ("C", "P", "F"):
                continue
            try:
                expiry_dt = datetime.combine(p.expiry_date, datetime.min.time())
                dte = (expiry_dt - self.today).days
            except Exception:
                dte = int(getattr(p, "days_remaining", 0) or 0)
            if dte > roll_dte_threshold:
                continue
            if roll_itm_only and opt in ("C", "P"):
                strike = float(getattr(p, "strike", 0.0) or 0.0)
                is_itm = (opt == "C" and strike < self.spot) or (opt == "P" and strike > self.spot)
                if not is_itm:
                    continue
            roll_positions.append(p)

        return roll_positions

    def _build_roll_unwind_trades(self, token, roll_positions: list[Position]) -> list[dict]:
        trades = []

        for p in roll_positions:
            qty = float(getattr(p, "net_qty", 0.0) or 0.0)
            raw_side = str(getattr(p, "side_raw", getattr(p, "side", ""))).lower()
            if raw_side in ("sell", "short"):
                qty = -qty
            if qty == 0:
                continue

            unwind_qty = -int(round(qty))
            if unwind_qty == 0:
                continue

            opt = str(getattr(p, "opt", "") or "")
            strike = float(getattr(p, "strike", 0.0) or 0.0)
            expiry_code = get_expiry_code(getattr(p, "expiry_date", getattr(p, "expiry", "")))

            if token == "ETH":
                instrument_name = (
                    "ETH-PERPETUAL" if opt == "F"
                    else f"ETH-{expiry_code}-{int(strike)}-{opt}"
                )
            elif token == "FIL":
                instrument_name = (
                    "FIL-PERPETUAL" if opt == "F"
                    else f"FIL-{expiry_code}-{strike}-{opt}"
                )
            else:
                raise ValueError(f"Unsupported token: {token}")

            trades.append({
                "counterparty": getattr(p, "counterparty", ""),
                "instrument": instrument_name,
                "strategy": "ROLL_UNWIND",
                "strategy_instrument": instrument_name,
                "expiry": getattr(p, "expiry_date", getattr(p, "expiry", "")),
                "dte": int(getattr(p, "days_remaining", 0) or 0),
                "strike": strike,
                "opt": opt,
                "qty": unwind_qty,
                "side": "Buy" if unwind_qty > 0 else "Sell",
                "iv_pct": round(float(getattr(p, "iv_pct", 0.0) or 0.0), 1),
                "bs_price_usd": round(float(getattr(p, "mark_price_usd", 0.0) or 0.0), 2),
                "vega": round(float(getattr(p, "vega", 0.0) or 0.0), 4),
                "notional": round(abs(float(unwind_qty)) * float(getattr(p, "mark_price_usd", 0.0) or 0.0), 2),
                "is_unwind": True,
                "unwind_qty": abs(int(unwind_qty)),
                "new_qty": 0,
                "estimated_cost": 0.0,
                "normalized_benefit": 0.0,
                "net_benefit": 0.0,
                "delta_contribution": round(float(unwind_qty * (getattr(p, "delta", 0.0) or 0.0)), 4),
                "gamma_contribution": round(float(unwind_qty * (getattr(p, "gamma", 0.0) or 0.0)), 6),
                "vega_contribution": round(float(unwind_qty * (getattr(p, "vega", 0.0) or 0.0)), 4),
            })

        return trades

    def _build_roll_replacement_trades(
        self,
        roll_positions: list[Position],
        option_legs: list[Candidate],
        target_expiry: str | None,
        min_abs_delta: float = 0.05,
    ) -> list[dict]:
        if target_expiry is None:
            return []

        trades = []

        for p in roll_positions:
            old_delta = float(getattr(p, "delta", 0.0) or 0.0)
            old_opt = str(getattr(p, "opt", "") or "")
            old_strike = float(getattr(p, "strike", 0.0) or 0.0)
            raw_side = str(getattr(p, "side_raw", getattr(p, "side", ""))).lower()
            old_qty = abs(float(getattr(p, "net_qty", 0.0) or 0.0))
            if raw_side in ("sell", "short"):
                old_qty = -old_qty
            if old_qty == 0.0 or old_opt not in ("C", "P"):
                continue

            # Only force replacement for currently ITM rolled positions.
            if old_opt == "C" and old_strike >= self.spot:
                continue
            if old_opt == "P" and old_strike <= self.spot:
                continue

            desired_delta_exposure = old_qty * old_delta
            if abs(desired_delta_exposure) <= 0.0:
                continue

            same_opt_candidates = [
                c for c in option_legs
                if c.expiry_code == target_expiry
                and c.opt == old_opt
                and abs(float(c.delta or 0.0)) >= min_abs_delta
            ]

            # Require target replacement to also be ITM.
            if old_opt == "C":
                same_opt_candidates = [
                    c for c in same_opt_candidates
                    if float(c.strike or 0.0) < self.spot
                ]
            else:
                same_opt_candidates = [
                    c for c in same_opt_candidates
                    if float(c.strike or 0.0) > self.spot
                ]

            if not same_opt_candidates:
                continue

            replacement = min(
                same_opt_candidates,
                key=lambda c: abs(abs(float(c.delta or 0.0)) - abs(old_delta)),
            )

            replacement_delta = float(replacement.delta or 0.0)
            if abs(replacement_delta) < min_abs_delta:
                continue

            old_price = float(getattr(p, "mark_price_usd", 0.0) or 0.0)
            new_price = max(float(replacement.bs_price_usd or 0.0), 1e-9)

            old_premium_abs = abs(old_qty * old_price)
            replacement_abs_qty = int(round(old_premium_abs / new_price))

            replacement_qty = int(math.copysign(replacement_abs_qty, old_qty))

            instrument_name = f"{self.asset}-{replacement.expiry_code}-{np.round(replacement.strike, self.asset_precision)}-{replacement.opt}"

            trades.append({
                "counterparty": replacement.counterparty,
                "instrument": instrument_name,
                "strategy": "ROLL_REPLACEMENT",
                "strategy_instrument": instrument_name,
                "expiry": replacement.expiry_date,
                "dte": replacement.dte,
                "strike": replacement.strike,
                "opt": replacement.opt,
                "qty": replacement_qty,
                "side": "Buy" if replacement_qty > 0 else "Sell",
                "iv_pct": round(float(replacement.iv_pct or 0.0), 1),
                "bs_price_usd": round(float(replacement.bs_price_usd or 0.0), 2),
                "vega": round(float(replacement.vega or 0.0), 4),
                "estimated_cost": 0.0,
                "normalized_benefit": 0.0,
                "net_benefit": 0.0,
                "delta_contribution": round(float(replacement_qty * replacement_delta), 4),
                "gamma_contribution": round(float(replacement_qty * (replacement.gamma or 0.0)), 6),
                "vega_contribution": round(float(replacement_qty * (replacement.vega or 0.0)), 4),
                "rolled_from": getattr(p, "instrument", ""),
            })

        return trades

    def _build_roll_summary(
        self,
        roll_positions: list[Position],
        roll_unwind_trades: list[dict],
        roll_replacement_trades: list[dict],
    ) -> dict:
        current_mtm = float(
            sum(float(getattr(p, "current_mtm", 0.0) or 0.0) for p in roll_positions)
        )

        close_value = float(
            sum(
                float(t.get("qty", 0.0) or 0.0)
                * float(t.get("bs_price_usd", 0.0) or 0.0)
                for t in roll_unwind_trades
            )
        )

        open_value = float(
            sum(
                float(t.get("qty", 0.0) or 0.0)
                * float(t.get("bs_price_usd", 0.0) or 0.0)
                for t in roll_replacement_trades
            )
        )

        return {
            "rolled_positions_count": len(roll_positions),
            "current_mtm_before_roll": round(current_mtm, 2),
            "close_value": round(close_value, 2),
            "open_value": round(open_value, 2),
            "net_roll_cash": round(close_value - open_value, 2),
        }

    def _build_option_smile(self) -> OptionSmile | None:
        smile_slices = [
            {
                "expiry_code": smile["expiry_code"],
                "expiry_date": smile["expiry_date"],
                "strikes": smile["strikes"],
                "ivs": [iv / 100.0 for iv in smile["ivs"]],
            }
            for smile in self.vol_surface
            if smile.get("dte", 0) > 0
        ]

        if not smile_slices:
            return None

        return OptionSmile(smile_slices, today=self.today)

    def _trade_value_curve(
        self,
        trade: dict,
        spot_arr: np.ndarray,
    ) -> np.ndarray:
        qty = float(trade.get("qty", 0.0) or 0.0)
        strike = float(trade.get("strike", 0.0) or 0.0)
        opt = str(trade.get("opt", "") or "")
        dte = int(trade.get("dte", 0) or 0)
        iv_pct = float(trade.get("iv_pct", 0.0) or 0.0)

        if opt == "F":
            return qty * (spot_arr - strike)

        if opt not in ("C", "P"):
            return np.zeros_like(spot_arr, dtype=float)

        T = max(dte, 0) / 365.25
        sigma = iv_pct / 100.0
        price_curve = bs_vec(spot_arr, strike, T, 0.0, sigma, opt)
        entry_price = float(trade.get("bs_price_usd", 0.0) or 0.0)

        return qty * (price_curve - entry_price)

    def _trade_premium_summary(self, trades: list[dict]) -> dict:
        option_trades = [
            trade for trade in trades
            if trade.get("opt") in ("C", "P")
        ]

        gross_premium_bought = sum(
            float(trade.get("qty", 0.0) or 0.0) * float(trade.get("bs_price_usd", 0.0) or 0.0)
            for trade in option_trades
            if float(trade.get("qty", 0.0) or 0.0) > 0
        )

        gross_premium_sold = sum(
            abs(float(trade.get("qty", 0.0) or 0.0)) * float(trade.get("bs_price_usd", 0.0) or 0.0)
            for trade in option_trades
            if float(trade.get("qty", 0.0) or 0.0) < 0
        )

        net_premium_generated = gross_premium_sold - gross_premium_bought

        return {
            "gross_premium_sold": round(float(gross_premium_sold), 2),
            "gross_premium_bought": round(float(gross_premium_bought), 2),
            "net_premium_generated": round(float(net_premium_generated), 2),
        }

    def _trade_premium_summary(self, trades: list[dict]) -> dict:
        option_trades = [
            trade for trade in trades
            if trade.get("opt") in ("C", "P")
        ]

        gross_premium_bought = sum(
            float(trade.get("qty", 0.0) or 0.0) * float(trade.get("bs_price_usd", 0.0) or 0.0)
            for trade in option_trades
            if float(trade.get("qty", 0.0) or 0.0) > 0
        )

        gross_premium_sold = sum(
            abs(float(trade.get("qty", 0.0) or 0.0)) * float(trade.get("bs_price_usd", 0.0) or 0.0)
            for trade in option_trades
            if float(trade.get("qty", 0.0) or 0.0) < 0
        )

        net_premium_generated = gross_premium_sold - gross_premium_bought

        return {
            "gross_premium_sold": round(float(gross_premium_sold), 2),
            "gross_premium_bought": round(float(gross_premium_bought), 2),
            "net_premium_generated": round(float(net_premium_generated), 2),
        }

    def _build_box_premium_neutralizer_trades(
            self,
            token,
            trades: list[dict],
            option_legs: list[Candidate],
            target_expiry: str | None,
            min_abs_premium: float = 10_000.0,
    ) -> list[dict]:
        if target_expiry is None:
            return []

        net_premium_generated = float(self._trade_premium_summary(trades)["net_premium_generated"])
        if abs(net_premium_generated) < min_abs_premium:
            return []

        expiry_legs = [
            c for c in option_legs
            if c.expiry_code == target_expiry
               and c.opt in ("C", "P")
               and float(c.bs_price_usd or 0.0) > 0.0
        ]

        calls_by_strike = {float(c.strike): c for c in expiry_legs if c.opt == "C"}
        puts_by_strike = {float(c.strike): c for c in expiry_legs if c.opt == "P"}
        common_strikes = sorted(set(calls_by_strike) & set(puts_by_strike))

        if len(common_strikes) < 2:
            return []

        best_box = None
        for low_strike in common_strikes:
            for high_strike in common_strikes:
                if high_strike <= low_strike:
                    continue

                low_call = calls_by_strike[low_strike]
                low_put = puts_by_strike[low_strike]
                high_call = calls_by_strike[high_strike]
                high_put = puts_by_strike[high_strike]

                # Long box: +C_low -P_low -C_high +P_high.
                box_debit = (
                        float(low_call.bs_price_usd or 0.0)
                        - float(low_put.bs_price_usd or 0.0)
                        - float(high_call.bs_price_usd or 0.0)
                        + float(high_put.bs_price_usd or 0.0)
                )

                if box_debit <= 0.0:
                    continue

                target_width = max(self.spot * 0.5, 1.0)
                width = high_strike - low_strike
                score = abs(width - target_width) + abs((low_strike + high_strike) / 2.0 - self.spot) * 0.25

                if best_box is None or score < best_box[0]:
                    best_box = (score, box_debit, low_call, low_put, high_call, high_put)

        if best_box is None:
            return []

        _score, box_debit, low_call, low_put, high_call, high_put = best_box
        box_qty = int(round(abs(net_premium_generated) / box_debit))
        if box_qty == 0:
            return []

        # Net credit already generated => buy long box to spend it.
        # Net debit generated => sell box to fund it.
        direction = 1 if net_premium_generated > 0.0 else -1

        legs = [
            (low_call, direction * box_qty),
            (low_put, -direction * box_qty),
            (high_call, -direction * box_qty),
            (high_put, direction * box_qty),
        ]

        if token == "ETH":
            strategy_instrument = (
                f"BOX_NEUTRALIZER: "
                f"ETH-{target_expiry}-{int(low_call.strike)} / "
                f"ETH-{target_expiry}-{int(high_call.strike)}"
            )
        elif token == "FIL":
            strategy_instrument = (
                f"BOX_NEUTRALIZER: "
                f"FIL-{target_expiry}-{int(low_call.strike)} / "
                f"FIL-{target_expiry}-{int(high_call.strike)}"
            )
        else:
            raise ValueError(f"Unsupported token: {token}")

        box_trades = []
        for leg, leg_qty in legs:
            strike = int(leg.strike) if token == "ETH" else np.round(leg.strike, 2)

            instrument_name = f"{token}-{leg.expiry_code}-{strike}-{leg.opt}"
            box_trades.append({
                "counterparty": leg.counterparty,
                "instrument": instrument_name,
                "strategy": "BOX_NEUTRALIZER",
                "strategy_instrument": strategy_instrument,
                "expiry": leg.expiry_date,
                "dte": leg.dte,
                "strike": leg.strike,
                "opt": leg.opt,
                "qty": leg_qty,
                "side": "Buy" if leg_qty > 0 else "Sell",
                "iv_pct": round(float(leg.iv_pct or 0.0), 1),
                "bs_price_usd": round(float(leg.bs_price_usd or 0.0), 2),
                "vega": round(float(leg.vega or 0.0), 4),
                "notional": round(abs(float(leg_qty)) * float(leg.bs_price_usd or 0.0), 2),
                "is_unwind": False,
                "unwind_qty": 0,
                "new_qty": abs(int(leg_qty)),
                "estimated_cost": 0.0,
                "normalized_benefit": 0.0,
                "net_benefit": 0.0,
                "delta_contribution": round(float(leg_qty * (leg.delta or 0.0)), 4),
                "gamma_contribution": round(float(leg_qty * (leg.gamma or 0.0)), 6),
                "vega_contribution": round(float(leg_qty * (leg.vega or 0.0)), 4),
            })

        return box_trades

    def _risk_neutral_spot_weights(
            self,
            spot_arr: np.ndarray,
            option_smile: OptionSmile,
            target_expiry: str,
    ) -> np.ndarray:
        """
        Infer risk-neutral terminal spot weights from the target-expiry smile.

        Uses Breeden-Litzenberger:
            q(K) = exp(rT) * d²C(K,T) / dK²

        With r = 0 here, q(K) is approximated by the numerical second
        derivative of call prices across the strike/state grid.
        """
        matching_slice = next(
            (
                smile_slice
                for smile_slice in option_smile.slices
                if smile_slice.expiry_code == target_expiry
            ),
            None,
        )

        if matching_slice is None:
            return np.ones_like(spot_arr, dtype=float)

        strikes = np.asarray(spot_arr, dtype=float)
        if strikes.size < 3:
            return np.ones_like(strikes, dtype=float)

        maturity = matching_slice.maturity
        T = option_smile._year_fraction(maturity)
        r = 0.0

        call_prices = np.array(
            [
                options.bs_price(
                    self.spot,
                    strike,
                    T,
                    r,
                    option_smile.compute_vol(maturity, strike=strike),
                    "C",
                )
                for strike in strikes
            ],
            dtype=float,
        )

        raw_density = np.gradient(np.gradient(call_prices, strikes), strikes)
        density = gaussian_filter1d(raw_density, sigma=1.5, mode="nearest")
        density = np.clip(density, 0.0, None)

        if not np.any(np.isfinite(density)) or float(np.sum(density)) <= 0.0:
            return np.ones_like(strikes, dtype=float)

        weights = density / np.mean(density[density > 0.0])
        return np.clip(weights, 1e-1, None)

    def bs_value_for_position(
            self,
            spot_arr,
            p: Position,
            option_smile: OptionSmile | None = None,
            horizon_days: int = 0,
    ) -> np.ndarray:
        """
        Reprice an existing position across the spot ladder using Black-Scholes.

        Uses sticky-strike volatility:
            sigma = smile_vol(position_expiry, position_strike)

        That sigma is then held fixed while evaluating BS over different spots.
        """
        qty = float(getattr(p, "net_qty", 0.0) or 0.0)
        side = str(getattr(p, "side", "")).lower()
        signed_qty = qty if side == "long" else -qty

        strike = float(getattr(p, "strike", 0.0) or 0.0)
        opt = str(getattr(p, "opt", "") or "")
        if opt == "F":
            return signed_qty * (spot_arr - strike)
        if opt not in ("C", "P"):
            return np.zeros_like(spot_arr, dtype=float)

        if option_smile is not None:
            maturity = datetime.combine(p.expiry_date, datetime.min.time())
            T = option_smile._year_fraction(maturity)  # T = dte_at_horizon / 365.25
            sigma = option_smile.compute_vol(maturity, strike=strike)
        else:
            T = float('nan')
            sigma = float(getattr(p, "iv_pct", 0.0) or 0.0) / 100.0

        r = 0.0
        return signed_qty * bs_vec(spot_arr, strike, T, r, sigma, opt)

    @staticmethod
    def nice_spot_ticks(spot: float) -> np.ndarray:
        tick_multipliers = np.array([0.4, 0.6, 0.8, 1.0, 1.2, 1.8, 2.8], dtype=float)
        raw_ticks = spot * tick_multipliers

        if spot >= 1000:
            step = 100.0
        elif spot >= 100:
            step = 10.0
        elif spot >= 10:
            step = 1.0
        elif spot >= 1:
            step = 0.1
        else:
            step = 0.01

        return np.round(raw_ticks / step) * step

    def run_lp(self,
                 lam_factor: float = 0.5,
                 mu_factor: float = 0.0,
                 bid_ask_atm_pct: float = 0.03,
                 bid_ask_min_delta: float = 0.05,
                 min_trade_delta: float = 0.10,
                 target_expiry: str | None = None,
                 unwind_discount: float = 0.2,
                 new_position_penalty: float = 0.04,
                 is_replay: bool = False,
                 roll_dte_threshold: int | None = 7,
                 roll_itm_only: bool = False,
                 collateral_budget_pct: float | None = None,
                 counterparties: list[str] | None = None,
                 asset: str | None = None,
                 max_exposure_by_counterparty: dict | None = None,
                 collateral_tier_free_pct: "dict[str, float] | float" = 0.0,
                 collateral_tier_mu: "dict[str, float] | float | None" = None,
            ):
        if asset is not None:
            self.asset = asset.upper()
            if self.asset == "ETH":
                self.asset_precision = 0
            elif self.asset == "FIL":
                self.asset_precision = 2
            else:
                raise ValueError(f"Unsupported asset: {self.asset}")
        print(f"asset: {self.asset}")

        target_profile = build_parametric_target_profile(self.asset, spot_ladder=self.spot_ladder,
                                                         current_spot=self.spot)

        held_positions = self.get_held_positions()
        roll_positions = self._get_roll_positions(roll_dte_threshold, roll_itm_only=roll_itm_only)
        roll_position_ids = {id(p) for p in roll_positions}

        option_legs = self._build_candidates(target_expiry=target_expiry, include_itm=False, counterparties=counterparties)
        # Spreads are built from target_expiry vanilla legs only (before unwind injection),
        # so they're always new positions (existing_qty=0, unwind_only=False via getattr defaults).
        # SpreadCandidate is frozen so we concatenate after the existing_qty stamp loop below.
        spread_candidates = self._build_spread_candidates(option_legs, target_expiry=target_expiry)

        option_smile = self._build_option_smile()
        if option_smile is None:
            return {"status": "no_smile", "message": "No valid vol surface slices available."}

        # Inject candidates for held positions whose counterparty is not in the requested list,
        # so they can be unwound even when counterparties=["Flowdesk", "KeyRock"].
        option_leg_keys = {(c.expiry_code, c.strike, c.opt, c.counterparty) for c in option_legs}
        held_expiries = set()
        for p in self.positions:
            if id(p) in roll_position_ids:
                continue
            exp_code = getattr(p, "expiry_code", "") or ""
            if not exp_code:
                parts = p.instrument.split("-")
                exp_code = parts[1] if len(parts) >= 4 else ""
            if not exp_code:
                continue
            held_expiries.add(exp_code)
            cp = getattr(p, "counterparty", "")
            strike = float(getattr(p, "strike", 0.0) or 0.0)
            opt = str(getattr(p, "opt", "") or "")
            if opt not in ("C", "P") or (exp_code, strike, opt, cp) in option_leg_keys:
                continue
            try:
                expiry_dt = datetime.combine(p.expiry_date, datetime.min.time())
                dte = (expiry_dt - self.today).days
                if dte < 0:
                    continue
                sigma = option_smile.compute_vol(expiry_dt, strike)
                expiry_date_str = p.expiry_date.strftime("%Y-%m-%d")
                c = self.create_candidate(self.spot, strike, 0., sigma, opt, exp_code, expiry_date_str, dte, cp)
                c.unwind_only = True
                option_legs.append(c)
                option_leg_keys.add((exp_code, strike, opt, cp))
            except Exception as e:
                print(f"  [inject candidate error] {exp_code} {strike} {opt} {cp}: {e}")
                continue


        for c in option_legs:
            c.existing_qty = held_positions.get((c.expiry_code, c.strike, c.opt, c.counterparty), 0.0)
        candidates = option_legs + spread_candidates

        n_with_existing = sum(1 for c in candidates if getattr(c, "existing_qty", 0.0) != 0.0)
        n_unwind_only = sum(1 for c in candidates if getattr(c, "unwind_only", False))
        print(f"candidates: {len(candidates)} total, {n_with_existing} with existing_qty≠0, {n_unwind_only} unwind_only")

        target_strikes = np.asarray(target_profile.index, dtype=float)
        target_payoff_arr = np.asarray(target_profile["Payoff($)"], dtype=float)

        spot_arr = np.array(self.spot_ladder, dtype=float)
        target_interp = np.interp(spot_arr, target_strikes, target_payoff_arr)

        if target_expiry is not None:
            spot_weights = self._risk_neutral_spot_weights(
                spot_arr=spot_arr,
                option_smile=option_smile,
                target_expiry=target_expiry,
            )
        else:
            spot_weights = np.ones_like(spot_arr, dtype=float)
        spot_weights /= np.sum(spot_weights)

        base_payoff = np.zeros_like(spot_arr)
        for p in self.positions:
            if id(p) in roll_position_ids:
                continue
            bs_value = self.bs_value_for_position(spot_arr, p, option_smile=option_smile)
            if np.isnan(bs_value.sum()):
                continue
            base_payoff += bs_value

        raw_residual = target_interp - base_payoff
        cash_shift = float(np.sum(spot_weights * raw_residual) / np.sum(spot_weights))
        adjusted_base_payoff = base_payoff + cash_shift
        residual = target_interp - adjusted_base_payoff

        c_payoffs = [self._candidate_curve(c=c, spot_arr=spot_arr, option_smile=option_smile) for c in candidates]

        # Gross collateral cap: sum(|final_qty| × price) per counterparty ≤ current × (1 + budget).
        # budget=0.0 → no increase allowed; budget=-0.1 → must shrink by 10%.
        max_gross_exposure_by_counterparty: dict | None = None
        if collateral_budget_pct is not None:
            cand_gross: dict[str, float] = {}
            for c in candidates:
                cp = getattr(c, "counterparty", "")
                price = float(c.bs_price_usd or 0.0)
                eq = float(getattr(c, "existing_qty", 0.0) or 0.0)
                cand_gross[cp] = cand_gross.get(cp, 0.0) + abs(eq) * price
            max_gross_exposure_by_counterparty = {
                cp: gross * (1.0 + collateral_budget_pct)
                for cp, gross in cand_gross.items() if gross > 0
            }
            print(f"  gross collateral caps: { {cp: f'{v:,.0f}' for cp, v in max_gross_exposure_by_counterparty.items()} }")

        lp = CollateralOptimization(self.asset, counterparties)
        lp_result = lp.optimize(
            spot_arr, spot_weights, residual, candidates, c_payoffs,
            lam_factor=lam_factor,
            mu_factor=mu_factor,
            bid_ask_atm_pct=bid_ask_atm_pct,
            bid_ask_min_delta=bid_ask_min_delta,
            min_trade_delta=min_trade_delta,
            max_exposure_by_counterparty=max_exposure_by_counterparty,
            max_gross_exposure_by_counterparty=max_gross_exposure_by_counterparty,
            collateral_tier_free_pct=collateral_tier_free_pct,
            collateral_tier_mu=collateral_tier_mu,
        )

        if lp_result is None:
            return {"status": "lp_failed", "message": "LP solver did not find an optimal solution."}

        net_qty = lp_result["net_qty"]

        # "Before" = full portfolio from held_positions (all expiries, all counterparties).
        # Prices come from candidates where available, fall back to 0.
        price_by_key: dict[tuple, float] = {
            (c.expiry_code, c.strike, c.opt, c.counterparty): float(c.bs_price_usd or 0.0)
            for c in candidates
        }
        before_coll: dict[str, float] = {}
        for (exp_code, strike, opt, cp), qty in held_positions.items():
            price = price_by_key.get((exp_code, strike, opt, cp), 0.0)
            before_coll[cp] = before_coll.get(cp, 0.0) + abs(qty) * price

        # "After" = existing positions adjusted by LP net_qty, plus any new positions opened.
        existing_qty_arr = np.array([float(getattr(c, "existing_qty", 0.0) or 0.0) for c in candidates])
        after_coll: dict[str, float] = {}
        for c, eq, nq in zip(candidates, existing_qty_arr, net_qty):
            cp = getattr(c, "counterparty", "")
            price = float(c.bs_price_usd or 0.0)
            after_coll[cp] = after_coll.get(cp, 0.0) + abs(eq + nq) * price

        all_cps = sorted(set(before_coll) | set(after_coll))
        print("=== Collateral by counterparty ===")
        for cp in all_cps:
            b = before_coll.get(cp, 0.0)
            a = after_coll.get(cp, 0.0)
            print(f"  {cp:20s}  before={b:>12,.0f}  after={a:>12,.0f}  change={a - b:>+12,.0f}")

        roll_unwind_trades = self._build_roll_unwind_trades(self.asset, roll_positions)
        trades = list(roll_unwind_trades)
        fitted_payoff = adjusted_base_payoff.copy()

        for j, (qty, c) in enumerate(zip(net_qty, candidates)):
            rounded_qty = int(np.round(qty))
            if rounded_qty == 0:
                continue

            est_cost = self._estimate_candidate_trade_cost(
                c=c,
                qty=rounded_qty,
                held_positions=held_positions,
                unwind_discount=unwind_discount,
                new_position_penalty=new_position_penalty,
            )
            instrument_name = self._candidate_instrument_name(c)
            fitted_payoff += rounded_qty * np.array(c_payoffs[j])

            for leg, leg_qty, strategy in self._candidate_trade_legs(c, rounded_qty):
                leg_instrument_name = (
                    f"{self.asset}-PERPETUAL" if leg.opt == "F"
                    else f"{self.asset}-{leg.expiry_code}-{np.round(leg.strike, self.asset_precision)}-{leg.opt}"
                )
                trades.append({
                    "counterparty": leg.counterparty,
                    "instrument": leg_instrument_name,
                    "strategy": strategy,
                    "strategy_instrument": instrument_name,
                    "expiry": leg.expiry_date,
                    "dte": leg.dte,
                    "strike": leg.strike,
                    "opt": leg.opt,
                    "qty": leg_qty,
                    "side": "Buy" if leg_qty > 0 else "Sell",
                    "iv_pct": round(float(leg.iv_pct or 0.0), 1),
                    "bs_price_usd": round(float(leg.bs_price_usd or 0.0), 2),
                    "vega": round(float(leg.vega or 0.0), 4),
                    "notional": round(abs(float(leg_qty)) * float(leg.bs_price_usd or 0.0), 2),
                    "is_unwind": bool(rounded_qty * getattr(c, "existing_qty", 0.0) < 0),
                    "unwind_qty": abs(int(leg_qty)) if rounded_qty * getattr(c, "existing_qty", 0.0) < 0 else 0,
                    "new_qty": 0 if rounded_qty * getattr(c, "existing_qty", 0.0) < 0 else abs(int(leg_qty)),
                    "estimated_cost": round(float(est_cost), 2),
                    "normalized_benefit": 0.0,
                    "net_benefit": 0.0,
                    "delta_contribution": round(float(leg_qty * (leg.delta or 0.0)), 4),
                    "gamma_contribution": round(float(leg_qty * (leg.gamma or 0.0)), 6),
                    "vega_contribution": round(float(leg_qty * (leg.vega or 0.0)), 4),
                })

        trades = self._aggregate_trade_legs(trades)

        lp_trades = [t for t in trades if t.get("strategy") != "ROLL_UNWIND"]
        total_notional = sum(abs(t.get("qty", 0)) * float(t.get("bs_price_usd", 0) or 0) for t in lp_trades)
        total_est_cost = sum(float(t.get("estimated_cost", 0) or 0) for t in lp_trades)
        print(f"=== Trading cost estimate ===")
        print(f"  notional traded : {total_notional:>14,.0f}")
        print(f"  estimated cost  : {total_est_cost:>14,.0f}  ({100*total_est_cost/max(total_notional,1):.2f}% of notional)")

        premium_summary = self._trade_premium_summary(trades)
        roll_unwind_output = [t for t in trades if t.get("strategy") == "ROLL_UNWIND"]
        replacement_output = [t for t in trades if t.get("strategy") != "ROLL_UNWIND"]

        horizons = sorted(set(self.chart_horizons + [0, 90]))
        before_payoff_by_horizon, after_payoff_by_horizon = self.build_payoffs(horizons, spot_arr, trades)

        fitted_payoff_cash_shift = float(
            np.sum(spot_weights * (adjusted_base_payoff - fitted_payoff)) / np.sum(spot_weights)
        )
        fitted_payoff_comparable = fitted_payoff + fitted_payoff_cash_shift

        sum_weights = np.sum(spot_weights)
        weighted_fit_error_before = float(
            np.sum(spot_weights * (adjusted_base_payoff - target_interp) ** 2) / sum_weights
        )
        weighted_fit_error_after = float(
            np.sum(spot_weights * (fitted_payoff_comparable - target_interp) ** 2) / sum_weights
        )
        print(f"fit error ratio: {weighted_fit_error_after / max(weighted_fit_error_before, 1e-12):.3f}")

        if is_replay:
            spot = self.spot
            x = np.log(spot_arr / spot)

            spot_ticks = self.nice_spot_ticks(spot)
            spot_ticks = spot_ticks[(spot_ticks >= spot_arr.min()) & (spot_ticks <= spot_arr.max())]
            tick_positions = np.log(spot_ticks / spot)

            if spot >= 100:
                spot_tick_labels = [f"{s:,.0f}" for s in spot_ticks]
            elif spot >= 10:
                spot_tick_labels = [f"{s:,.1f}" for s in spot_ticks]
            elif spot >= 1:
                spot_tick_labels = [f"{s:,.2f}" for s in spot_ticks]
            else:
                spot_tick_labels = [f"{s:,.3f}" for s in spot_ticks]

            fig, axes = plt.subplots(3, 1, sharex=True)
            axes[0].plot(x, adjusted_base_payoff, label="Adjusted Base Payoff")
            axes[0].plot(x, target_interp, label="Target Payoff")
            axes[0].plot(x, fitted_payoff_comparable, label="Fitted Payoff, cash-adjusted")
            axes[0].axvline(0, color="gray", linestyle="--", linewidth=1)
            axes[0].legend()

            axes[1].plot(x, fitted_payoff_comparable - adjusted_base_payoff, label="Fitted - Adjusted Base")
            axes[1].axvline(0, color="gray", linestyle="--", linewidth=1)
            axes[1].legend()
            axes[1].set_xticks(tick_positions)
            axes[1].set_xticklabels(spot_tick_labels)

            axes[2].plot(x, spot_weights, label="Weights")
            axes[2].legend()
            plt.show()

        return {
            "status": "ok",
            "asset": self.asset,
            "target_expiry": target_expiry,
            "optimizer_converged": True,
            "spot": round(float(self.spot), 2),
            "cash_shift": round(float(cash_shift), 2),
            "fitted_payoff_cash_shift": round(float(fitted_payoff_cash_shift), 2),
            "premium_summary": premium_summary,
            "net_premium_generated": premium_summary["net_premium_generated"],
            "fit_error_before": round(weighted_fit_error_before, 2),
            "fit_error_after": round(weighted_fit_error_after, 2),
            "spot_ladder": spot_arr.tolist(),
            "chart_horizons": horizons,
            "target_payoff": np.round(target_interp, 2).tolist(),
            "before_payoff": np.round(adjusted_base_payoff, 2).tolist(),
            "after_payoff": np.round(fitted_payoff_comparable, 2).tolist(),
            "raw_after_payoff": np.round(fitted_payoff, 2).tolist(),
            "raw_before_payoff": np.round(base_payoff, 2).tolist(),
            "before": {"payoff_by_horizon": before_payoff_by_horizon},
            "after": {"payoff_by_horizon": after_payoff_by_horizon},
            "roll_unwind_trades": roll_unwind_output,
            "replacement_trades": replacement_output,
            "trades": trades,
            "candidates_evaluated": len(candidates),
        }


    def run(self,
                 lam_factor: float = 0.5,
                 target_expiry: str | None = None,
                 unwind_discount: float = 0.2,
                 new_position_penalty: float = 0.04,
                 is_replay: bool = False,
                 roll_dte_threshold: int | None = 7,
                 roll_itm_only: bool = False,
                 counterparties: list[str] | None = None,
                 asset: str | None = None,
            ):
        lam_factor *= self.spot/1000.0
        print(self.spot)
        if asset is not None:
            self.asset = asset.upper()
            if self.asset == "ETH":
                self.asset_precision = 0
            elif self.asset == "FIL":
                self.asset_precision = 2
            else:
                raise ValueError(f"Unsupported asset: {self.asset}")
        print(f"asset: {self.asset}")

        selected_counterparties = {
            c.strip()
            for c in (counterparties or [])
            if c and c.strip() and c.strip().upper() != "ALL"
        }
        if selected_counterparties:
            self.positions = [
                p for p in self.positions
                if getattr(p, "counterparty", "") in selected_counterparties
            ]

        print(lam_factor)
        print(target_expiry)
        print(unwind_discount)
        print(new_position_penalty)
        print(is_replay)
        print(f"roll_dte_threshold: {roll_dte_threshold}")
        print(self.spot)
        print(self.spot_ladder)
        # is_replay = (target_expiry is not None)#False
        # target_profile = shift_target_profile(load_target_profile(), self.spot)
        target_profile = build_parametric_target_profile(self.asset, spot_ladder=self.spot_ladder, current_spot=self.spot)

        held_positions = self.get_held_positions()
        roll_positions = self._get_roll_positions(roll_dte_threshold, roll_itm_only=roll_itm_only)
        roll_position_ids = {id(p) for p in roll_positions}
        is_roll_mode = len(roll_positions) > 0
        
        print(f"roll positions: {len(roll_positions)}")
        roll_unwind_trades = self._build_roll_unwind_trades(self.asset, roll_positions)
        print(f"roll unwind trades: {len(roll_unwind_trades)}")

        option_legs = self._build_candidates(target_expiry=target_expiry, include_itm=is_roll_mode)
        spread_candidates = self._build_spread_candidates(option_legs, target_expiry=target_expiry)
        straddle_candidates = self._build_straddle_candidates(option_legs, target_expiry=target_expiry)
        iron_condor_candidates = self._build_iron_condor_candidates(option_legs, target_expiry=target_expiry)
        candidates = option_legs + spread_candidates + straddle_candidates# + iron_condor_candidates

        '''
        roll_replacement_trades = self._build_roll_replacement_trades(
            roll_positions=roll_positions,
            option_legs=option_legs,
            target_expiry=target_expiry,
        )

        roll_summary = self._build_roll_summary(
            roll_positions=roll_positions,
            roll_unwind_trades=roll_unwind_trades,
            roll_replacement_trades=roll_replacement_trades,
        )
        print(f"roll summary: {roll_summary}")
        print(f"roll replacement trades: {len(roll_replacement_trades)}")
        '''

        target_strikes = np.asarray(target_profile.index, dtype=float)
        target_payoff = np.asarray(target_profile["Payoff($)"], dtype=float)  # - 2000000

        spot_arr = np.array(self.spot_ladder, dtype=float)
        target_interp = np.interp(spot_arr, target_strikes, target_payoff)

        option_smile = self._build_option_smile()
        if target_expiry is not None and option_smile is not None:
            spot_weights = self._risk_neutral_spot_weights(
                spot_arr=spot_arr,
                option_smile=option_smile,
                target_expiry=target_expiry,
            )
        else:
            spot_weights = np.ones_like(spot_arr, dtype=float)

        base_payoff = np.zeros_like(spot_arr)
        cash_roll = 0.
        for p in self.positions:
            if id(p) in roll_position_ids:
                cash_roll += p.current_mtm
                continue

            bs_value = self.bs_value_for_position(spot_arr, p, option_smile=option_smile)
            if np.isnan(bs_value.sum()):
                continue
            base_payoff += bs_value

        #for trade in roll_replacement_trades:
        #    base_payoff += self._trade_value_curve(trade, spot_arr)
        raw_residual = target_interp - base_payoff
        cash_shift = float(np.sum(spot_weights * raw_residual) / np.sum(spot_weights))
        adjusted_base_payoff = base_payoff + cash_shift
        residual = target_interp - adjusted_base_payoff

        # Normalize improvement to something comparable to dollars, keeps huge target curves from drowning the cost signal.
        payoff_scale = max(float(np.mean(np.abs(target_interp))), 1.0)

        c_vega = np.array([abs(self._candidate_vega(c)) for c in candidates], dtype=float)
        if np.all(c_vega == 0.0):
            vega_weight = np.ones_like(c_vega)
        else:
            vega_weight = c_vega / np.max(c_vega)

        min_weight = 0.2
        strike_weights = np.maximum(vega_weight, min_weight)
        lams = 0.01 * np.ones(len(candidates))
        base_lam = lam_factor
        print(f"base_lam: {base_lam}")

        A_cols = []
        meta = []

        max_vega = max(float(c_vega.max()), 1e-12)

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
                if True:  # smile["expiry_code"] in held_expiry_codes:
                    matching_smiles.append(smile)

        option_smile = OptionSmile(
                [
                    {
                        "expiry_code": smile["expiry_code"],
                        "expiry_date": smile["expiry_date"],
                        "strikes": smile["strikes"],
                        "ivs": [iv / 100.0 for iv in smile["ivs"]],
                    }
                    for smile in matching_smiles
                ],
                today=self.today,
            )

        curves = []
        for i, c in enumerate(candidates):
            if not self._is_structured_candidate(c) and c.opt not in ("C", "P", "F"):
                lams[i] = 1.E10
                continue

            candidate_vega = max(abs(self._candidate_vega(c)), 1e-12)
            lams[i] = base_lam * np.pow(max_vega / candidate_vega, 2)
            if self._is_spread_candidate(c):
                lams[i] *= 1.
            elif self._is_straddle_candidate(c):
                lams[i] *= 1.5
            elif self._is_iron_condor_candidate(c):
                lams[i] *= 2.0
            else:
                strike = float(getattr(c, "strike", 0.0) or 0.0)
                opt = str(getattr(c, "opt", "") or "")
                is_itm = (opt == "C" and strike < self.spot) or (opt == "P" and strike > self.spot)
                if is_itm:
                    lams[i] *= 2.0

            curve = self._candidate_curve(c=c, spot_arr=spot_arr, option_smile=option_smile)
            curves.append(curve)
            weighted_curve = strike_weights[i] * curve
            A_cols.append(curve)#weighted_curve)
            meta.append(c)

        if not A_cols:
            return {"status": "no_fit_candidates", "message": "No candidates available for payoff fitting."}

        A = np.column_stack(A_cols)
        lasso = GeneralizedLasso()
        lasso.fit(A, residual*1.E-6, lams, w=spot_weights)
        betas_lasso = lasso.betas * 1.E6
        err_fit_lasso = lasso.err_fit

        x = betas_lasso
        #fitted_payoff = adjusted_base_payoff + A @ x

        sum_weights = np.sum(spot_weights)
        base_rmse = float(np.sqrt(np.sum(spot_weights*np.pow(adjusted_base_payoff - target_interp, 2))/sum_weights))
        scored_trades = []

        i = -1
        for qty, c, w in zip(x, meta, strike_weights[: len(meta)]):
            i += 1
            rounded_qty = int(np.round(qty))
            if rounded_qty == 0:
                continue

            est_cost = self._estimate_candidate_trade_cost(
                c=c,
                qty=rounded_qty,
                held_positions=held_positions,
                unwind_discount=unwind_discount,
                new_position_penalty=new_position_penalty,
            )

            instrument_name = self._candidate_instrument_name(c)

            curve = rounded_qty * curves[i]
            new_payoff = adjusted_base_payoff + curve
            new_rmse = float(np.sqrt(np.sum(spot_weights*np.pow(new_payoff - target_interp, 2)/sum_weights)))

            rmse_improvement = base_rmse - new_rmse
            normalized_benefit = rmse_improvement * payoff_scale * abs(rounded_qty)

            net_benefit = normalized_benefit - est_cost
            base_rmse = new_rmse
            scored_trades.append((net_benefit, normalized_benefit, est_cost, rounded_qty, c, w, curve, instrument_name))

        scored_trades.sort(key=lambda t: t[0], reverse=True)

        trades = list(roll_unwind_trades) #+ list(roll_replacement_trades)
        roll_unwind_output = [t for t in trades if t.get("strategy") == "ROLL_UNWIND"]
        replacement_output = [t for t in trades if t.get("strategy") != "ROLL_UNWIND"]
        fitted_payoff = adjusted_base_payoff.copy()

        horizons = sorted(set(self.chart_horizons + [0, 90]))

        if not scored_trades:
            box_neutralizer_trades = self._build_box_premium_neutralizer_trades(
                token=self.asset,
                trades=trades,
                option_legs=option_legs,
                target_expiry=target_expiry,
            )
            trades.extend(box_neutralizer_trades)

            adjusted_after_payoff = adjusted_base_payoff.copy()
            for trade in box_neutralizer_trades:
                adjusted_after_payoff += self._trade_value_curve(trade, spot_arr)

            trades = self._aggregate_trade_legs(trades)
            premium_summary = self._trade_premium_summary(trades)
            before_payoff_by_horizon, after_payoff_by_horizon = self.build_payoffs(
                horizons,
                spot_arr,
                trades,
            )

            return {
                "status": "ok",
                "asset": self.asset,
                "target_expiry": target_expiry,
                "optimizer_converged": True,
                "spot": round(float(self.spot), 2),
                "cash_shift": round(float(cash_shift), 2),
                "premium_summary": premium_summary,
                "net_premium_generated": premium_summary["net_premium_generated"],
                "spot_ladder": spot_arr.tolist(),
                "chart_horizons": horizons,
                "target_payoff": np.round(target_interp, 2).tolist(),
                "before_payoff": np.round(adjusted_base_payoff, 2).tolist(),
                "after_payoff": np.round(adjusted_base_payoff, 2).tolist(),
                "raw_before_payoff": np.round(base_payoff, 2).tolist(),
                "before": {
                    "payoff_by_horizon": before_payoff_by_horizon,
                },
                "after": {
                    "payoff_by_horizon": after_payoff_by_horizon,
                },
                "roll_unwind_trades": roll_unwind_output,
                "replacement_trades": replacement_output,
                "trades": trades,
                "candidates_evaluated": len(meta),
            }

        total_cost = sum(est_cost for _, _, est_cost, _, _, _, _, _ in scored_trades)
        for net_benefit, normalized_benefit, est_cost, rounded_qty, c, w, curve, instrument_name in scored_trades:
            fitted_payoff += curve

            for leg, leg_qty, strategy in self._candidate_trade_legs(c, rounded_qty):
                leg_instrument_name = (
                    f"{self.asset}-PERPETUAL" if leg.opt == "F"
                    else f"{self.asset}-{leg.expiry_code}-{np.round(leg.strike, self.asset_precision)}-{leg.opt}"
                )

                trades.append({
                    "counterparty": leg.counterparty,
                    "instrument": leg_instrument_name,
                    "strategy": strategy,
                    "strategy_instrument": instrument_name,
                    "expiry": leg.expiry_date,
                    "dte": leg.dte,
                    "strike": leg.strike,
                    "opt": leg.opt,
                    "qty": leg_qty,
                    "side": "Buy" if leg_qty > 0 else "Sell",
                    "iv_pct": round(float(leg.iv_pct or 0.0), 1),
                    "bs_price_usd": round(float(leg.bs_price_usd or 0.0), 2),
                    "vega": round(float(leg.vega or 0.0), 4),
                    "notional": round(abs(float(leg_qty)) * float(leg.bs_price_usd or 0.0), 2),
                    "is_unwind": False,
                    "unwind_qty": 0,
                    "new_qty": abs(int(leg_qty)),
                    "strike_weight": round(float(w), 4),
                    "estimated_cost": round(float(est_cost), 2),
                    "normalized_benefit": round(float(normalized_benefit), 2),
                    "net_benefit": round(float(net_benefit), 2),
                    "delta_contribution": round(float(leg_qty * (leg.delta or 0.0)), 4),
                    "gamma_contribution": round(float(leg_qty * (leg.gamma or 0.0)), 6),
                    "vega_contribution": round(float(leg_qty * (leg.vega or 0.0)), 4),
                })

        box_neutralizer_trades = self._build_box_premium_neutralizer_trades(
            token=self.asset,
            trades=trades,
            option_legs=option_legs,
            target_expiry=target_expiry,
        )
        trades.extend(box_neutralizer_trades)
        for trade in box_neutralizer_trades:
            fitted_payoff += self._trade_value_curve(trade, spot_arr)

        fitted_payoff_cash_shift = float(
            np.sum(spot_weights * (adjusted_base_payoff - fitted_payoff)) / np.sum(spot_weights)
        )
        fitted_payoff_comparable = fitted_payoff + fitted_payoff_cash_shift

        print("is_replay:" + str(is_replay))
        if is_replay:
            spot = self.spot  # or your reference spot S0
            x = np.log(spot_arr / spot)

            spot_ticks = self.nice_spot_ticks(spot)
            spot_ticks = spot_ticks[(spot_ticks >= spot_arr.min()) & (spot_ticks <= spot_arr.max())]
            tick_positions = np.log(spot_ticks / spot)

            if spot >= 100:
                spot_tick_labels = [f"{s:,.0f}" for s in spot_ticks]
            elif spot >= 10:
                spot_tick_labels = [f"{s:,.1f}" for s in spot_ticks]
            elif spot >= 1:
                spot_tick_labels = [f"{s:,.2f}" for s in spot_ticks]
            else:
                spot_tick_labels = [f"{s:,.3f}" for s in spot_ticks]

            fig, axes = plt.subplots(3, 1, sharex=True)

            # axes[0].plot(x, base_payoff, label="
            axes[0].plot(x, adjusted_base_payoff, label="Adjusted Base Payoff")
            axes[0].plot(x, target_interp, label="Target Payoff")
            axes[0].plot(x, fitted_payoff_comparable, label="Fitted Payoff, cash-adjusted")
            axes[0].axvline(0, color="gray", linestyle="--", linewidth=1)
            axes[0].legend()

            axes[1].plot(
                x,
                fitted_payoff_comparable - adjusted_base_payoff,
                label="Fitted - Adjusted Base, cash-adjusted",
            )
            axes[1].axvline(0, color="gray", linestyle="--", linewidth=1)
            axes[1].set_xlabel("Spot")
            # axes[1].set_ylim(210000, 215000)
            axes[1].legend()
            axes[1].set_xticks(tick_positions)
            axes[1].set_xticklabels(spot_tick_labels)

            axes[2].plot(x, spot_weights, label="Weights")
            axes[2].legend()
            plt.show()

        trades = self._aggregate_trade_legs(trades)
        premium_summary = self._trade_premium_summary(trades)
        roll_unwind_output = [
            trade for trade in trades
            if trade.get("strategy") == "ROLL_UNWIND"
        ]
        replacement_output = [
            trade for trade in trades
            if trade.get("strategy") != "ROLL_UNWIND"
        ]

        before_payoff_by_horizon, after_payoff_by_horizon = self.build_payoffs(
            horizons,
            spot_arr,
            trades,
        )

        print(f"selected structures: {len(scored_trades)}")
        print(f"trade legs emitted: {len(trades)}")

        for trade in trades:
            print(
                trade.get("strategy", "NA"),
                trade.get("strategy_instrument", ""),
                trade["instrument"],
                trade["qty"],
            )

        weighted_fit_error_before = float(
            np.sum(spot_weights * (adjusted_base_payoff - target_interp) ** 2) / np.sum(spot_weights)
        )
        weighted_fit_error_after = float(
            np.sum(spot_weights * (fitted_payoff_comparable - target_interp) ** 2) / np.sum(spot_weights)
        )

        print("ratio: " + str(weighted_fit_error_after/weighted_fit_error_before))

        return {
            "status": "ok",
            "asset": self.asset,
            "target_expiry": target_expiry,
            "optimizer_converged": True,
            "spot": round(float(self.spot), 2),
            "cash_shift": round(float(cash_shift), 2),
            "fitted_payoff_cash_shift": round(float(fitted_payoff_cash_shift), 2),
            "premium_summary": premium_summary,
            "net_premium_generated": premium_summary["net_premium_generated"],
            "fit_error_after": round(weighted_fit_error_after, 2),
            "fit_error_before": round(float(np.mean((adjusted_base_payoff - target_interp) ** 2)), 2),
            "spot_ladder": spot_arr.tolist(),
            "chart_horizons": horizons,
            "target_payoff": np.round(target_interp, 2).tolist(),
            "before_payoff": np.round(adjusted_base_payoff, 2).tolist(),
            "after_payoff": np.round(fitted_payoff_comparable, 2).tolist(),
            "raw_after_payoff": np.round(fitted_payoff, 2).tolist(),
            "raw_before_payoff": np.round(base_payoff, 2).tolist(),
            "before": {
                "payoff_by_horizon": before_payoff_by_horizon,
            },
            "after": {
                "payoff_by_horizon": after_payoff_by_horizon,
            },
            "roll_unwind_trades": roll_unwind_output,
            "replacement_trades": replacement_output,
            "trades": trades,
            "candidates_evaluated": len(meta),
        }

    def run_previous(self,
                     risk_aversion: float = 1.0,
                     brokerage_txn_cost_pct: float = 5.0,
                     deribit_txn_cost_pct: float = 0.1,
                     max_collateral: float = 4_000_000.0,
                     target_expiry: str | None = None,
                     lambda_delta: float = 1.0,
                     lambda_gamma: float = 1.0,
                     lambda_vega: float = 1.0,
                     unwind_discount: float = 0.2,
                     new_position_penalty: float = 0.04,
                     vega_cross_expiry_corr: float = 0.0, ):

        # Liquidate all existing positions (outside of the target expiry range?)
        held_positions = self.get_held_positions()
        candidates = self._build_candidates(target_expiry=None)

        # Build a quick lookup for candidate quotes by (expiry_code, strike, opt, counterparty)
        candidate_by_key = {(c.expiry_code, c.strike, c.opt, c.counterparty): c for c in candidates}
        trades = []
        unwind_discount = 1.

        x = np.array([0.0] * len(candidates))
        i = -1
        for c in candidates:  # (exp_code, strike_i, opt_i, counterparty_i), held_qty in held_positions.items():
            i += 1
            held_qty = held_positions.get((c.expiry_code, c.strike, c.opt, c.counterparty), 0)
            if held_qty == 0:
                continue
            # candidate = candidate_by_key.get((exp_code, strike_i, opt_i, counterparty_i))  # matching candidate quote if exists
            # Fallbacks if the instrument is not in candidates
            price_i = float(c.bs_price_usd) if c and c.bs_price_usd is not None else 0.0
            dte_i = int(c.dte) if c and c.dte is not None else 0
            cost_rate = float(self.compute_costs(
                self.spot, [c] if c else [], perp_cost_bps=2.0, brokerage_txn_cost_pct=0.5,
                deribit_txn_cost_pct=0.1,
            )[0]) if c else 0.0

            if dte_i > 10:
                continue
            instrument_name = ("ETH-PERPETUAL" if c.opt == "F" else f"ETH-{c.expiry_code}-{int(c.strike)}-{c.opt}")

            # Close the full held quantity: long position  -> sell to unwind, short position -> buy to unwind
            unwind_signed = -int(round(held_qty))
            unwind_qty = abs(unwind_signed)
            unwind_notional = unwind_qty * price_i
            cost_unwind_part = cost_rate * unwind_discount * unwind_notional
            x[i] = unwind_signed

            trades.append({
                "counterparty": c.counterparty, "instrument": instrument_name, "expiry": c.expiry_date if c else "",
                "dte": c.dte if c else 0, "strike": c.strike if c else 0.0, "opt": c.opt, "qty": unwind_signed,
                "side": "Buy" if unwind_signed > 0 else "Sell", "iv_pct": round(c.iv_pct, 1),
                "bs_price_usd": round(c.bs_price_usd, 2), "notional": round(unwind_notional, 2),
                "cost_bps": round(cost_rate * 10_000, 1), "trade_cost": round(cost_unwind_part, 2),
                "delta_contribution": round(unwind_signed * float(c.delta), 4),
                "gamma_contribution": round(unwind_signed * float(c.gamma), 6),
                "vega_contribution": round(unwind_signed * c.vega, 4),
                "is_unwind": True, "unwind_qty": unwind_qty, "new_qty": 0,
            })

        port_delta, port_gamma, port_theta, port_vega = self._portfolio_greeks()
        port_vega_by_expiry = self._portfolio_vega_by_expiry()

        perp_candidate = candidates[-1]  # candidate_by_key[('PERP', 2210, 'F', 'Deribit')]
        print(lambda_delta)
        print(lambda_gamma)
        perp_trade = self.add_perp_hedge(perp_candidate, lambda_delta)
        perp_trade['notional'] = perp_trade['qty'] * perp_candidate.strike
        x[-1] += perp_trade['qty']
        trades.append(perp_trade)

        qty = 1000. * lambda_gamma
        call_to_put_ratio = lambda_vega
        condor_trades, x = self.solve_condor(qty, candidate_by_key, x, call_to_put_ratio)
        for trade in condor_trades:
            trades.append(trade)
            expiry_code = get_expiry_code(trade['expiry'])
            trade_key = (expiry_code, trade['strike'], trade['opt'], trade['counterparty'])
            i = list(candidate_by_key.keys()).index(trade_key)
            x[i] += trade['qty']
        # trades = []

        new_delta, new_gamma, new_theta, new_vega, new_vega_by_expiry = (
            self.compute_greeks(x, candidates, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry))

        print(port_delta)
        print(new_delta)
        print(port_gamma)
        print(new_gamma)

        spot = self.spot
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
            "spot": spot,
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

    @staticmethod
    def compute_greeks(x, candidates, port_delta, port_gamma, port_theta, port_vega, port_vega_by_expiry):
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
            # c_vega_by_expiry[exp_code] = np.sum(c_vega_by_expiry[exp_code], axis=0)
            diff = np.sum(np.dot(x, c_vega_by_expiry[exp_code]))
            k = 1

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
        # Populate per-position payoff curves first
        for p in self.positions:
            p.payoff_by_horizon = {}

            # You can infer these from your position object / instrument string
            # Adjust the field names here if your Position model differs.
            qty = float(getattr(p, "net_qty", 0.0) or 0.0)
            strike = float(getattr(p, "strike", 0.0) or 0.0)
            opt = str(getattr(p, "opt", "") or "")
            iv_pct = float(getattr(p, "iv_pct", 0.0) or 0.0)
            dte = int(getattr(p, "days_remaining", 0) or 0)

            # Use signed quantity so long/short is reflected in the curve
            signed_qty = qty if str(getattr(p, "side", "")).lower() == "long" else -qty

            for h in horizons:
                h_key = str(h)

                if opt == "F":
                    # Perpetual / future: linear mark-to-market
                    curve = signed_qty * (spot_arr - strike)
                else:
                    # Option curve at horizon h
                    dte_at_h = max(dte - h, 0)
                    T_h = dte_at_h / 365.25
                    sigma = iv_pct / 100.0
                    curve = signed_qty * bs_vec(spot_arr, strike, T_h, 0.0, sigma, opt)

                p.payoff_by_horizon[h_key] = np.round(curve, 2).tolist()

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

        pnl = 0.
        for trade in trades:
            pnl += trade["bs_price_usd"] * trade["qty"]

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
            trade_payoff_delta[h_key] = np.round(total - pnl, 2).tolist()

        # After payoff = before + trades
        after_payoff = {}
        for h_key in before_payoff:
            before = np.array(before_payoff[h_key])
            delta_arr = np.array(trade_payoff_delta[h_key])
            after_payoff[h_key] = np.round(before + delta_arr, 2).tolist()

        return before_payoff, after_payoff

    def add_perp_hedge(self, perp_candidate, qty):
        c = perp_candidate
        instrument_name = ("ETH-PERPETUAL" if c.opt == "F" else f"ETH-{c.expiry_code}-{int(c.strike)}-{c.opt}")
        cost_rate = 5. / 10000.

        # Close the full held quantity: long position  -> sell to unwind, short position -> buy to unwind
        unwind_signed = qty  # * condor_mults[k]  # -int(round(held_qty))
        unwind_qty = abs(unwind_signed)
        unwind_notional = unwind_qty * 0
        cost_unwind_part = cost_rate * 0 * unwind_notional

        trade = {
            "counterparty": c.counterparty, "instrument": instrument_name, "expiry": c.expiry_date if c else "",
            "dte": c.dte if c else 0, "strike": c.strike if c else 0.0, "opt": c.opt, "qty": unwind_signed,
            "side": "Buy" if unwind_signed > 0 else "Sell", "iv_pct": round(c.iv_pct, 1),
            "bs_price_usd": round(c.bs_price_usd, 2), "notional": round(unwind_notional, 2),
            "cost_bps": round(cost_rate * 10_000, 1), "trade_cost": round(cost_unwind_part, 2),
            "delta_contribution": round(unwind_signed * float(c.delta), 4),
            "gamma_contribution": round(unwind_signed * float(c.gamma), 6),
            "vega_contribution": round(unwind_signed * c.vega, 4),
            "is_unwind": True, "unwind_qty": unwind_qty, "new_qty": 0,
        }
        return trade

    def _is_spread_candidate(self, c) -> bool:
        return hasattr(c, "long_leg") and hasattr(c, "short_leg")

    def _is_straddle_candidate(self, c) -> bool:
        return hasattr(c, "call_leg") and hasattr(c, "put_leg")

    def _is_iron_condor_candidate(self, c) -> bool:
        return (
            hasattr(c, "put_low_leg")
            and hasattr(c, "put_high_leg")
            and hasattr(c, "call_low_leg")
            and hasattr(c, "call_high_leg")
        )

    def _is_structured_candidate(self, c) -> bool:
        return self._is_spread_candidate(c) or self._is_straddle_candidate(c) or self._is_iron_condor_candidate(c)

    def _candidate_vega(self, c) -> float:
        if self._is_spread_candidate(c):
            return float(c.vega or 0.0)
        return float(getattr(c, "vega", 0.0) or 0.0)

    def _candidate_delta(self, c) -> float:
        if self._is_spread_candidate(c):
            return float(c.delta or 0.0)
        return float(getattr(c, "delta", 0.0) or 0.0)

    def _candidate_gamma(self, c) -> float:
        if self._is_spread_candidate(c):
            return float(c.gamma or 0.0)
        return float(getattr(c, "gamma", 0.0) or 0.0)

    def _candidate_iv_pct(self, c) -> float:
        if self._is_spread_candidate(c):
            return float(c.iv_pct or 0.0)
        return float(getattr(c, "iv_pct", 0.0) or 0.0)

    def _candidate_price(self, c) -> float:
        if self._is_spread_candidate(c):
            return float(c.bs_price_usd or 0.0)
        return float(getattr(c, "bs_price_usd", 0.0) or 0.0)

    def _candidate_dte(self, c) -> int:
        if self._is_spread_candidate(c):
            return int(c.dte)
        return int(getattr(c, "dte", 0) or 0)

    def _candidate_curve(
            self,
            c,
            spot_arr: np.ndarray,
            option_smile: OptionSmile,
    ) -> np.ndarray:
        """
        Return one optimizer-unit curve.

        Naked option:
            option value across spot ladder minus entry price.

        Spread:
            long leg value minus short leg value minus net entry price.
        """
        matching_slice = next(
            (
                smile_slice
                for smile_slice in option_smile.slices
                if smile_slice.expiry_code == c.expiry_code
            ),
            None,
        )
        maturity = matching_slice.maturity if matching_slice is not None else option_smile.slices[0].maturity

        if self._is_spread_candidate(c):
            long_leg = c.long_leg
            short_leg = c.short_leg

            T = c.dte / 365.25
            r = 0.0

            long_strike = float(long_leg.strike or 0.0)
            short_strike = float(short_leg.strike or 0.0)

            long_entry = float(long_leg.bs_price_usd or 0.0)
            short_entry = float(short_leg.bs_price_usd or 0.0)
            spread_entry = long_entry - short_entry

            curve_list = []
            for spot in spot_arr:
                long_vol = option_smile.compute_vol(
                    maturity,
                    strike=long_strike,
                )
                short_vol = option_smile.compute_vol(
                    maturity,
                    strike=short_strike,
                )

                long_price = options.bs_price(
                    spot,
                    long_strike,
                    T,
                    r,
                    long_vol,
                    long_leg.opt,
                )
                short_price = options.bs_price(
                    spot,
                    short_strike,
                    T,
                    r,
                    short_vol,
                    short_leg.opt,
                )

                curve_list.append((long_price - short_price) - spread_entry)

            return np.array(curve_list, dtype=float)
        elif self._is_straddle_candidate(c):
            call_leg = c.call_leg
            put_leg = c.put_leg

            T = c.dte / 365.25
            r = 0.0
            strike = float(c.strike or 0.0)
            entry_price = float(c.bs_price_usd or 0.0)

            curve_list = []
            for spot in spot_arr:
                call_vol = option_smile.compute_vol(
                    maturity,
                    strike=float(call_leg.strike or 0.0),
                )
                put_vol = option_smile.compute_vol(
                    maturity,
                    strike=float(put_leg.strike or 0.0),
                )

                call_price = options.bs_price(
                    spot,
                    float(call_leg.strike or 0.0),
                    T,
                    r,
                    call_vol,
                    "C",
                )
                put_price = options.bs_price(
                    spot,
                    float(put_leg.strike or 0.0),
                    T,
                    r,
                    put_vol,
                    "P",
                )

                curve_list.append((call_price + put_price) - entry_price)

            return np.array(curve_list, dtype=float)
        elif self._is_iron_condor_candidate(c):
            T = c.dte / 365.25
            r = 0.0
            entry_price = float(c.bs_price_usd or 0.0)

            legs = [
                (c.put_low_leg, 1.0),
                (c.put_high_leg, -1.0),
                (c.call_low_leg, -1.0),
                (c.call_high_leg, 1.0),
            ]

            curve_list = []
            for spot in spot_arr:
                value = 0.0
                for leg, leg_sign in legs:
                    strike = float(leg.strike or 0.0)
                    vol = option_smile.compute_vol(
                        maturity,
                        strike=strike,
                    )
                    value += leg_sign * options.bs_price(
                        spot,
                        strike,
                        T,
                        r,
                        vol,
                        leg.opt,
                    )

                curve_list.append(value - entry_price)

            return np.array(curve_list, dtype=float)
        if c.opt not in ("C", "P", "F"):
            return np.zeros_like(spot_arr, dtype=float)

        strike = float(c.strike or 0.0)
        bs_price = float(c.bs_price_usd or 0.0)
        T = c.dte / 365.25
        r = 0.0

        curve_list = []
        for spot in spot_arr:
            vol = option_smile.compute_vol(
                maturity,
                strike=strike,
            )
            price = options.bs_price(spot, strike, T, r, vol, c.opt)
            curve_list.append(price - bs_price)

        return np.array(curve_list, dtype=float)

    def _candidate_trade_legs(self, c, qty: int) -> list[tuple[Candidate, int, str]]:
        """
            Expand optimizer quantity into executable option legs.
            Naked candidate:
                qty of that candidate.
            Spread candidate:
                qty of long leg and -qty of short leg.
            Straddle candidate:
                qty of call leg and qty of put leg.
            """
        if self._is_spread_candidate(c):
            return [(c.long_leg, qty, c.kind), (c.short_leg, -qty, c.kind),]
        elif self._is_straddle_candidate(c):
            return [(c.call_leg, qty, c.kind), (c.put_leg, qty, c.kind)]
        elif self._is_iron_condor_candidate(c):
            return [(c.put_low_leg, qty, c.kind), (c.put_high_leg, -qty, c.kind),
                    (c.call_low_leg, -qty, c.kind), (c.call_high_leg, qty, c.kind)]
        else:
            return [(c, qty, "NAKED")]

    def _aggregate_trade_legs(self, trades: list[dict]) -> list[dict]:
        aggregated: dict[tuple, dict] = {}

        for trade in trades:
            key = (
                trade.get("counterparty"),
                trade.get("instrument"),
                trade.get("expiry"),
                trade.get("strike"),
                trade.get("opt"),
            )

            qty = int(trade.get("qty", 0) or 0)
            if qty == 0:
                continue

            if key not in aggregated:
                aggregated[key] = trade.copy()
                aggregated[key]["qty"] = qty
                aggregated[key]["estimated_cost"] = float(trade.get("estimated_cost", 0.0) or 0.0)
                aggregated[key]["normalized_benefit"] = float(trade.get("normalized_benefit", 0.0) or 0.0)
                aggregated[key]["net_benefit"] = float(trade.get("net_benefit", 0.0) or 0.0)
                aggregated[key]["delta_contribution"] = float(trade.get("delta_contribution", 0.0) or 0.0)
                aggregated[key]["gamma_contribution"] = float(trade.get("gamma_contribution", 0.0) or 0.0)
                aggregated[key]["vega_contribution"] = float(trade.get("vega_contribution", 0.0) or 0.0)
                aggregated[key]["strategy"] = trade.get("strategy", "MIXED")
                aggregated[key]["strategy_instrument"] = trade.get("strategy_instrument", "")
                continue

            existing = aggregated[key]
            existing["qty"] += qty
            existing["estimated_cost"] += float(trade.get("estimated_cost", 0.0) or 0.0)
            existing["normalized_benefit"] += float(trade.get("normalized_benefit", 0.0) or 0.0)
            existing["net_benefit"] += float(trade.get("net_benefit", 0.0) or 0.0)
            existing["delta_contribution"] += float(trade.get("delta_contribution", 0.0) or 0.0)
            existing["gamma_contribution"] += float(trade.get("gamma_contribution", 0.0) or 0.0)
            existing["vega_contribution"] += float(trade.get("vega_contribution", 0.0) or 0.0)

            if existing.get("strategy_instrument") != trade.get("strategy_instrument"):
                existing["strategy"] = "MIXED"
                existing["strategy_instrument"] = "Aggregated"

        result = []
        for trade in aggregated.values():
            if trade["qty"] == 0:
                continue

            trade["side"] = "Buy" if trade["qty"] > 0 else "Sell"
            trade["estimated_cost"] = round(float(trade.get("estimated_cost", 0.0)), 2)
            trade["normalized_benefit"] = round(float(trade.get("normalized_benefit", 0.0)), 2)
            trade["net_benefit"] = round(float(trade.get("net_benefit", 0.0)), 2)
            trade["delta_contribution"] = round(float(trade.get("delta_contribution", 0.0)), 4)
            trade["gamma_contribution"] = round(float(trade.get("gamma_contribution", 0.0)), 6)
            trade["vega_contribution"] = round(float(trade.get("vega_contribution", 0.0)), 4)
            result.append(trade)

        result.sort(key=lambda t: (str(t.get("expiry")), float(t.get("strike") or 0.0), str(t.get("opt"))))
        return result

    def _candidate_instrument_name(self, c) -> str:
        if self._is_spread_candidate(c):
            return (
                f"{c.kind}: "
                f"{self.asset}-{c.expiry_code}-{int(c.long_leg.strike)}-{c.long_leg.opt} / "
                f"{self.asset}-{c.expiry_code}-{int(c.short_leg.strike)}-{c.short_leg.opt}"
            )
        if self._is_straddle_candidate(c):
            return (
                f"{c.kind}: "
                f"{self.asset}-{c.expiry_code}-{int(c.strike)}-C / "
                f"{self.asset}-{c.expiry_code}-{int(c.strike)}-P"
            )
        if self._is_iron_condor_candidate(c):
            return (
                f"{c.kind}: "
                f"{self.asset}-{c.expiry_code}-{int(c.put_low_leg.strike)}-P / "
                f"{self.asset}-{c.expiry_code}-{int(c.put_high_leg.strike)}-P / "
                f"{self.asset}-{c.expiry_code}-{int(c.call_low_leg.strike)}-C / "
                f"{self.asset}-{c.expiry_code}-{int(c.call_high_leg.strike)}-C"
            )
        return (
            "{self.asset}-PERPETUAL" if c.opt == "F"
            else f"{self.asset}-{c.expiry_code}-{int(c.strike)}-{c.opt}"
        )

    def _estimate_candidate_trade_cost(
            self,
            c,
            qty: int,
            held_positions: dict,
            unwind_discount: float,
            new_position_penalty: float,
    ) -> float:
        est_cost = 0.0

        for leg, leg_qty, _strategy in self._candidate_trade_legs(c, qty):
            held_qty = float(
                held_positions.get(
                    (leg.expiry_code, leg.strike, leg.opt, leg.counterparty),
                    0.0,
                )
            )

            est_cost += self._estimate_trade_cost(
                qty=leg_qty,
                price=float(leg.bs_price_usd or 0.0),
                held_qty=held_qty,
                unwind_discount=unwind_discount,
                new_position_penalty=new_position_penalty,
                is_held=abs(held_qty) > 0,
            )

        return est_cost

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

    def solve_condor(self, qty, candidate_by_key, x, call_to_put_ratio=1.):
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

        # front_condor = self._pick_iron_condor_legs(front_candidates, front_expiry)
        back_condor = self._pick_iron_condor_legs(back_candidates, back_expiry)

        selected_candidates = back_condor

        price_by_expiry = {
            # front_expiry: self._condor_price(front_condor),
            back_expiry: self._condor_price(back_condor),
        }
        # Now the LP works only on those 8-ish legs
        candidates = selected_candidates
        # Solve the LP: maximize x*front_ic_qty + (1-x)*back_ic_qty under collateral constraints
        collateral_by_counterparty = {"FlowDesk": 8750000, "KeyRock": 0}  # 7926168
        cost_by_counterparty = {"FlowDesk": 0.01, "KeyRock": 0.05}
        solver = PulpSolver()
        solution = solver.solve(price_by_expiry, cost_by_counterparty, collateral_by_counterparty)

        # Build trades
        condor_qty = qty
        condor_mults = [1., -1., -call_to_put_ratio, call_to_put_ratio]
        condor_trades = []
        for k in range(4):
            c = candidates[k] if k < len(candidates) else None
            instrument_name = (f"{self.asset}-PERPETUAL" if c.opt == "F" else f"{self.asset}-{c.expiry_code}-{int(c.strike)}-{c.opt}")

            cost_rate = float(self.compute_costs(
                self.spot, [c] if c else [], perp_cost_bps=2.0, brokerage_txn_cost_pct=0.5,
                deribit_txn_cost_pct=0.1,
            )[0]) if c else 0.0

            # Close the full held quantity: long position  -> sell to unwind, short position -> buy to unwind
            unwind_signed = condor_qty * condor_mults[k]  # -int(round(held_qty))
            unwind_qty = abs(unwind_signed)
            unwind_notional = unwind_qty * c.bs_price_usd
            cost_unwind_part = cost_rate * 0 * unwind_notional
            # x[i] = unwind_signed

            condor_trades.append({
                "counterparty": c.counterparty, "instrument": instrument_name, "expiry": c.expiry_date if c else "",
                "dte": c.dte if c else 0, "strike": c.strike if c else 0.0, "opt": c.opt, "qty": unwind_signed,
                "side": "Buy" if unwind_signed > 0 else "Sell", "iv_pct": round(c.iv_pct, 1),
                "bs_price_usd": round(c.bs_price_usd, 2), "notional": round(unwind_notional, 2),
                "cost_bps": round(cost_rate * 10_000, 1), "trade_cost": round(cost_unwind_part, 2),
                "delta_contribution": round(unwind_signed * float(c.delta), 4),
                "gamma_contribution": round(unwind_signed * float(c.gamma), 6),
                "vega_contribution": round(unwind_signed * c.vega, 4),
                "is_unwind": True, "unwind_qty": unwind_qty, "new_qty": 0,
            })

        return condor_trades, x

