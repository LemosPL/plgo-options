
import pulp
import numpy as np


class CollateralOptimization:
    def __init__(self, asset, counterparties):
        self.asset = asset
        self.counterparties = counterparties
        self.positions = []

    def optimize(
        self,
        spot_ladder,
        spot_weights,
        residual_payoff,
        candidates,
        c_payoffs,
        lam_factor=1.0,
        mu_factor=0.0,
        max_exposure_by_counterparty=None,
    ):
        n_spots = len(spot_ladder)
        n_candidates = len(candidates)

        existing_qty = np.array([float(getattr(c, "existing_qty", 0.0) or 0.0) for c in candidates])
        unwind_only = np.array([bool(getattr(c, "unwind_only", False)) for c in candidates])

        prob = pulp.LpProblem("CollateralOptimization", pulp.LpMinimize)

        buy_vars = [
            pulp.LpVariable(
                f"buy_qty_{j}",
                lowBound=0,
                upBound=(
                    0.0 if (unwind_only[j] and existing_qty[j] >= 0)   # long unwind-only: no new buying
                    else float(-existing_qty[j]) if existing_qty[j] < 0  # short: cover only
                    else None
                ),
                cat="Continuous",
            )
            for j in range(n_candidates)
        ]
        sell_vars = [
            pulp.LpVariable(
                f"sell_qty_{j}",
                lowBound=0,
                upBound=(
                    float(existing_qty[j]) if existing_qty[j] > 0      # long: unwind only
                    else 0.0 if (unwind_only[j] and existing_qty[j] <= 0)  # short unwind-only: no new shorting
                    else None
                ),
                cat="Continuous",
            )
            for j in range(n_candidates)
        ]
        abs_error_vars = [pulp.LpVariable(f"abs_error_{i}", lowBound=0, cat="Continuous") for i in range(n_spots)]
        abs_net_pos_vars = [pulp.LpVariable(f"abs_net_pos_{j}", lowBound=0, cat="Continuous") for j in range(n_candidates)]

        for i in range(n_spots):
            trade_payoff_i = pulp.lpSum(
                (buy_vars[j] - sell_vars[j]) * float(c_payoffs[j][i]) for j in range(n_candidates)
            )
            error_i = float(residual_payoff[i]) - trade_payoff_i
            prob += abs_error_vars[i] >= error_i, f"abs_error_pos_{i}"
            prob += abs_error_vars[i] >= -error_i, f"abs_error_neg_{i}"

        for j in range(n_candidates):
            net_pos_j = float(existing_qty[j]) + buy_vars[j] - sell_vars[j]
            prob += abs_net_pos_vars[j] >= net_pos_j, f"abs_net_pos_pos_{j}"
            prob += abs_net_pos_vars[j] >= -net_pos_j, f"abs_net_pos_neg_{j}"

        c_vegas = np.array([abs(float(getattr(c, "vega", 0.0) or 0.0)) for c in candidates])
        max_vega = float(np.max(c_vegas)) if np.any(c_vegas > 0) else 1.0
        c_prices = np.array([max(float(getattr(c, "bs_price_usd", 0.0) or 0.0), 1e-8) for c in candidates])
        vega_penalty = np.where(c_vegas > 0, (max_vega / np.maximum(c_vegas, 1e-12)) ** 2, 1.0)
        # Unwind-only candidates are already held: skip vega_penalty (it can be huge for near-expiry
        # or far OTM options) so the LP isn't artificially deterred from closing them.
        c_costs = np.where(unwind_only, c_prices, vega_penalty * c_prices)

        # Saturate mu_factor so holding cost ≤ 1 × price × qty at any mu_factor.
        # effective_mu → 1 as mu_factor → ∞, so the LP never unwinds purely for collateral
        # (holding cost < trading cost always); unwinding happens only when profile improvement
        # covers the residual net cost (1 − effective_mu) × price × qty.
        effective_mu = mu_factor / (1.0 + mu_factor)

        profile_error = pulp.lpSum(float(spot_weights[i]) * abs_error_vars[i] for i in range(n_spots))
        trading_cost = pulp.lpSum(float(c_costs[j]) * (buy_vars[j] + sell_vars[j]) for j in range(n_candidates))
        collateral_cost = pulp.lpSum(
            effective_mu * float(c_prices[j]) * abs_net_pos_vars[j] for j in range(n_candidates)
        )
        prob += lam_factor * profile_error + trading_cost + collateral_cost

        # Per-counterparty net notional exposure constraints (existing + net new <= max)
        if max_exposure_by_counterparty:
            cp_indices = {}
            for j, c in enumerate(candidates):
                cp = getattr(c, "counterparty", "")
                cp_indices.setdefault(cp, []).append(j)

            for cp, max_exp in max_exposure_by_counterparty.items():
                indices = cp_indices.get(cp, [])
                if not indices:
                    continue
                existing_exp_cp = float(sum(existing_qty[j] * float(candidates[j].bs_price_usd or 0.0) for j in indices))
                prob += (
                    existing_exp_cp + pulp.lpSum(
                        (buy_vars[j] - sell_vars[j]) * float(candidates[j].bs_price_usd or 0.0)
                        for j in indices
                    ) <= float(max_exp),
                    f"collateral_{cp}",
                )

        self._solve_problem(prob, "PULP_CBC_CMD")

        if prob.status != pulp.LpStatusOptimal:
            print(f"CollateralOptimization failed: {pulp.LpStatus[prob.status]}")
            return None

        buy_qty = np.array([pulp.value(v) or 0.0 for v in buy_vars])
        sell_qty = np.array([pulp.value(v) or 0.0 for v in sell_vars])
        net_qty = buy_qty - sell_qty

        c_payoffs_arr = np.array(c_payoffs)  # (n_candidates, n_spots)
        trade_payoff = net_qty @ c_payoffs_arr
        residual_after = np.array(residual_payoff) - trade_payoff
        profile_error_val = float(np.sum(np.array(spot_weights) * np.abs(residual_after)))

        # Objective breakdown
        _profile_err_before = float(np.sum(np.array(spot_weights) * np.abs(residual_payoff)))
        _trading_cost_val = float(np.sum(c_costs * (buy_qty + sell_qty)))
        _net_pos = existing_qty + net_qty
        _coll_cost_val = float(np.sum(effective_mu * c_prices * np.abs(_net_pos)))
        print(f"  LP objective  profile_err={_profile_err_before:,.0f}→{profile_error_val:,.0f}"
              f"  trading_cost={_trading_cost_val:,.0f}  collateral_cost={_coll_cost_val:,.0f}"
              f"  (effective_mu={effective_mu:.3f})")
        _n_unwind = int(np.sum((net_qty * existing_qty < 0) & (np.abs(net_qty) > 0.5)))
        _n_new = int(np.sum((existing_qty == 0) & (np.abs(net_qty) > 0.5)))
        print(f"  LP trades  unwind={_n_unwind}  new={_n_new}")

        return {
            "net_qty": net_qty,
            "trade_payoff": trade_payoff,
            "profile_error": profile_error_val,
        }

    def _solve_problem(self, prob, algo):
        if algo == "PULP_CBC_CMD":
            prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=1))
        elif algo == "HiGHS":
            prob.solve(pulp.HiGHS(msg=False, timeLimit=1))
        else:
            raise ValueError(f"Invalid algo: {algo}")
        return prob.status
