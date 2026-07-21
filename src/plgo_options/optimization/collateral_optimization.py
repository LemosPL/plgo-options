
import pulp
import numpy as np


class CollateralOptimization:
    def __init__(self, asset, counterparties):
        self.asset = asset
        self.counterparties = counterparties
        self.positions = []

    @staticmethod
    def _resolve(param, cp, default=0.0):
        """Return per-CP value from a dict, or the scalar itself, or default."""
        if isinstance(param, dict):
            return param.get(cp, default)
        if param is None:
            return default
        return param

    def optimize(
        self,
        spot_ladder,
        spot_weights,
        residual_payoff,
        candidates,
        c_payoffs,
        lam_factor=1.0,
        mu_factor=0.0,
        bid_ask_atm_pct=0.03,
        bid_ask_min_delta=0.05,
        min_trade_delta=0.10,
        max_exposure_by_counterparty=None,
        max_gross_exposure_by_counterparty=None,
        collateral_tier_free_pct=0.0,
        collateral_tier_mu=None,
        cash_neutrality_factor=0.0,
        forced_cash_by_counterparty=None,
        max_qty=None,
        leg_groups=None,
        downside_factor=1.0,
        residual_payoff_90=None,
        c_payoffs_90=None,
        t90_weight=0.0,
    ):
        n_spots = len(spot_ladder)
        n_candidates = len(candidates)

        existing_qty = np.array([float(getattr(c, "existing_qty", 0.0) or 0.0) for c in candidates])
        unwind_only = np.array([bool(getattr(c, "unwind_only", False)) for c in candidates])
        c_deltas = np.array([abs(float(getattr(c, "delta", 0.5) or 0.5)) for c in candidates])
        # Candidates below min_trade_delta are too illiquid to trade: freeze their bounds at 0.
        tradeable = c_deltas >= min_trade_delta

        prob = pulp.LpProblem("CollateralOptimization", pulp.LpMinimize)

        buy_vars = [
            pulp.LpVariable(
                f"buy_qty_{j}",
                lowBound=0,
                upBound=(
                    0.0 if not tradeable[j]                              # below liquidity threshold
                    else 0.0 if (unwind_only[j] and existing_qty[j] >= 0)   # long unwind-only: no new buying
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
                    0.0 if not tradeable[j]                              # below liquidity threshold
                    else float(existing_qty[j]) if existing_qty[j] > 0  # long: unwind only
                    else 0.0 if (unwind_only[j] and existing_qty[j] <= 0)  # short unwind-only: no new shorting
                    else None
                ),
                cat="Continuous",
            )
            for j in range(n_candidates)
        ]
        # Cap the *aggregated* per-leg-instrument quantity (matching what
        # _aggregate_trade_legs merges into the trades table), not each raw
        # candidate — a naked leg and several spreads can share the same strike
        # as one leg, and capping each candidate individually still lets their
        # sum exceed max_qty once merged. leg_groups maps a leg's own identity
        # (expiry_code, strike, opt, counterparty) to the (candidate_index, sign)
        # pairs that contribute to it, mirroring _candidate_trade_legs.
        if max_qty is not None and leg_groups:
            for gi, members in enumerate(leg_groups.values()):
                group_net = pulp.lpSum(sign * (buy_vars[j] - sell_vars[j]) for j, sign in members)
                prob += group_net <= float(max_qty), f"max_qty_pos_{gi}"
                prob += group_net >= -float(max_qty), f"max_qty_neg_{gi}"
        # Split the symmetric |error| penalty into two one-sided terms so a
        # shortfall (book below target) and a surplus (book above target) can
        # be weighted differently — downside_factor > 1 makes the LP treat the
        # target as a floor to clear rather than a bullseye to hit exactly,
        # since overshooting it (more cushion, less risk) isn't actually as
        # bad as falling short of it. downside_factor=1 is exactly the old
        # symmetric behavior (shortfall and surplus equally weighted).
        shortfall_vars = [pulp.LpVariable(f"shortfall_{i}", lowBound=0, cat="Continuous") for i in range(n_spots)]
        surplus_vars = [pulp.LpVariable(f"surplus_{i}", lowBound=0, cat="Continuous") for i in range(n_spots)]
        abs_net_pos_vars = [pulp.LpVariable(f"abs_net_pos_{j}", lowBound=0, cat="Continuous") for j in range(n_candidates)]

        for i in range(n_spots):
            trade_payoff_i = pulp.lpSum(
                (buy_vars[j] - sell_vars[j]) * float(c_payoffs[j][i]) for j in range(n_candidates)
            )
            # error_i > 0 => target > fitted (shortfall); < 0 => fitted > target (surplus).
            error_i = float(residual_payoff[i]) - trade_payoff_i
            prob += shortfall_vars[i] >= error_i, f"shortfall_{i}"
            prob += surplus_vars[i] >= -error_i, f"surplus_{i}"

        # T+90 mirror of the block above: same shortfall/surplus split and same
        # downside_factor, but against the book repriced 90 days forward, so
        # the fit can care about "still roughly on-target in 90 days" and not
        # just "on-target today" — blended in via t90_weight below.
        use_t90 = t90_weight and residual_payoff_90 is not None and c_payoffs_90 is not None
        if use_t90:
            shortfall_vars_90 = [pulp.LpVariable(f"shortfall90_{i}", lowBound=0, cat="Continuous") for i in range(n_spots)]
            surplus_vars_90 = [pulp.LpVariable(f"surplus90_{i}", lowBound=0, cat="Continuous") for i in range(n_spots)]
            for i in range(n_spots):
                trade_payoff_90_i = pulp.lpSum(
                    (buy_vars[j] - sell_vars[j]) * float(c_payoffs_90[j][i]) for j in range(n_candidates)
                )
                error_90_i = float(residual_payoff_90[i]) - trade_payoff_90_i
                prob += shortfall_vars_90[i] >= error_90_i, f"shortfall90_{i}"
                prob += surplus_vars_90[i] >= -error_90_i, f"surplus90_{i}"

        for j in range(n_candidates):
            net_pos_j = float(existing_qty[j]) + buy_vars[j] - sell_vars[j]
            prob += abs_net_pos_vars[j] >= net_pos_j, f"abs_net_pos_pos_{j}"
            prob += abs_net_pos_vars[j] >= -net_pos_j, f"abs_net_pos_neg_{j}"

        c_prices = np.array([max(float(getattr(c, "bs_price_usd", 0.0) or 0.0), 1e-8) for c in candidates])
        # Delta-based bid-ask spread: wider for lower-delta options, same formula for all candidates.
        # bid_ask_pct(δ) = bid_ask_atm_pct / (2 × |δ|), floored at bid_ask_min_delta to cap the spread.
        # Deep ITM options (|δ| → 1) naturally get tighter spreads (they behave like forwards).
        # bid_ask_atm_pct can be a per-counterparty dict — different counterparties
        # can be quoted with genuinely different pricing/cost (client request:
        # e.g. Flowdesk trades wider/costlier than KeyRock on ETH) — resolved the
        # same way as collateral_tier_free_pct/collateral_tier_mu elsewhere.
        c_bid_ask_atm = np.array([
            self._resolve(bid_ask_atm_pct, getattr(c, "counterparty", ""), default=0.03)
            for c in candidates
        ])
        c_deltas_floored = np.maximum(c_deltas, bid_ask_min_delta)
        bid_ask_pct = c_bid_ask_atm / (2.0 * c_deltas_floored)
        c_costs = bid_ask_pct * c_prices

        # Saturate mu_factor so holding cost ≤ 1 × price × qty at any mu_factor.
        # effective_mu → 1 as mu_factor → ∞, so the LP never unwinds purely for collateral
        # (holding cost < trading cost always); unwinding happens only when profile improvement
        # covers the residual net cost (1 − effective_mu) × price × qty.
        effective_mu = mu_factor / (1.0 + mu_factor)

        # Build cp → candidate index map (used by tiered collateral and exposure constraints).
        cp_indices: dict[str, list[int]] = {}
        for j, c in enumerate(candidates):
            cp = getattr(c, "counterparty", "")
            cp_indices.setdefault(cp, []).append(j)

        profile_error_t0 = pulp.lpSum(
            float(spot_weights[i]) * (float(downside_factor) * shortfall_vars[i] + surplus_vars[i])
            for i in range(n_spots)
        )
        if use_t90:
            profile_error_t90 = pulp.lpSum(
                float(spot_weights[i]) * (float(downside_factor) * shortfall_vars_90[i] + surplus_vars_90[i])
                for i in range(n_spots)
            )
            profile_error = (1.0 - float(t90_weight)) * profile_error_t0 + float(t90_weight) * profile_error_t90
        else:
            profile_error = profile_error_t0
        trading_cost = pulp.lpSum(float(c_costs[j]) * (buy_vars[j] + sell_vars[j]) for j in range(n_candidates))

        tiered_mode = collateral_tier_mu is not None
        tier_vars: dict = {}  # cp → (t_base, t_steep, mu_base, mu_steep) — populated when tiered_mode
        if tiered_mode:
            # Piecewise-linear collateral cost per counterparty.
            # Tier 0 (cheap): gross notional up to G0_cp × (1 + free_pct) — penalised at effective_mu.
            # Tier 1 (steep): gross notional above that ceiling — penalised at effective_mu_steep.
            # Negative free_pct pushes the LP to reduce exposure (ceiling < current gross).
            # free_pct and mu_steep can be per-CP (dict) or a scalar applied to all CPs.
            for cp, indices in cp_indices.items():
                g0 = float(sum(abs(existing_qty[j]) * float(c_prices[j]) for j in indices))
                free_pct = self._resolve(collateral_tier_free_pct, cp, default=0.0)
                g_free = g0 * (1.0 + free_pct)
                mu_steep_cp = mu_factor + self._resolve(collateral_tier_mu, cp, default=0.07)
                effective_mu_steep = mu_steep_cp / (1.0 + mu_steep_cp)

                if g0 == 0.0:
                    t_base = pulp.LpVariable(f"tier_base_{cp}", lowBound=0, cat="Continuous")
                else:
                    t_base = pulp.LpVariable(f"tier_base_{cp}", lowBound=0, upBound=max(g_free, 0.0), cat="Continuous")
                t_steep = pulp.LpVariable(f"tier_steep_{cp}", lowBound=0, cat="Continuous")
                tier_vars[cp] = (t_base, t_steep, 0.0, effective_mu_steep)

                gross_notional_cp = pulp.lpSum(abs_net_pos_vars[j] * float(c_prices[j]) for j in indices)
                prob += t_base + t_steep == gross_notional_cp, f"tier_link_{cp}"

            collateral_cost = pulp.lpSum(
                mu_b * t_base + mu_s * t_steep
                for (t_base, t_steep, mu_b, mu_s) in tier_vars.values()
            )
        else:
            # Flat collateral cost: uniform penalty on absolute net position across all candidates.
            collateral_cost = pulp.lpSum(
                effective_mu * float(c_costs[j]) * abs_net_pos_vars[j] for j in range(n_candidates)
            )

        # Per-counterparty cash-neutrality penalty: premium paid (buys) vs.
        # premium collected (sells) — across the LP's own trades AND any
        # forced/DTE roll unwinds already locked in for that counterparty
        # (fixed before the LP runs, passed in as a constant offset so the LP
        # can actually counterbalance them, not just its own candidates) —
        # should roughly net to zero, so the desk isn't wiring cash to fund
        # trades it could have self-funded. Soft cost, not a hard constraint —
        # an equality would often be infeasible or force away a better risk
        # fit just to balance cash exactly. 0 (default) disables it.
        # Union with cp_indices: a forced roll can target a counterparty with
        # no tradeable candidates in this run (manual-selection mode bypasses
        # the counterparty filter) — still worth surfacing in the cost/report
        # even though the LP has nothing of its own to counterbalance it with.
        cash_cps = set(cp_indices) | set((forced_cash_by_counterparty or {}).keys())
        cash_imbalance_vars: dict[str, "pulp.LpVariable"] = {}
        for cp in cash_cps:
            indices = cp_indices.get(cp, [])
            forced_cash_cp = float((forced_cash_by_counterparty or {}).get(cp, 0.0))
            net_cash_cp = forced_cash_cp + pulp.lpSum(
                (buy_vars[j] - sell_vars[j]) * float(c_prices[j]) for j in indices
            )
            imbalance = pulp.LpVariable(f"cash_imbalance_{cp}", lowBound=0, cat="Continuous")
            prob += imbalance >= net_cash_cp, f"cash_imbalance_pos_{cp}"
            prob += imbalance >= -net_cash_cp, f"cash_imbalance_neg_{cp}"
            cash_imbalance_vars[cp] = imbalance

        cash_neutrality_cost = pulp.lpSum(
            self._resolve(cash_neutrality_factor, cp) * imbalance
            for cp, imbalance in cash_imbalance_vars.items()
        )

        prob += lam_factor * profile_error + trading_cost + collateral_cost + cash_neutrality_cost

        # Per-counterparty signed net notional constraint (existing + net new <= max).
        if max_exposure_by_counterparty:
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

        # Per-counterparty gross notional constraint: sum(|final_qty| × price) <= cap.
        # Uses abs_net_pos_vars (already linearised above), so no extra binary variables needed.
        if max_gross_exposure_by_counterparty:
            for cp, max_gross in max_gross_exposure_by_counterparty.items():
                indices = cp_indices.get(cp, [])
                if not indices:
                    continue
                prob += (
                    pulp.lpSum(abs_net_pos_vars[j] * float(c_prices[j]) for j in indices) <= float(max_gross),
                    f"gross_collateral_{cp}",
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
        _notional_traded = float(np.sum(c_prices * np.abs(net_qty)))
        _trading_cost_val = float(np.sum(c_costs * (buy_qty + sell_qty)))
        _net_pos = existing_qty + net_qty
        if tiered_mode:
            _tier_base_val = float(sum(pulp.value(t_base) or 0.0 for (t_base, _, _, _) in tier_vars.values()))
            _tier_steep_val = float(sum(pulp.value(t_steep) or 0.0 for (_, t_steep, _, _) in tier_vars.values()))
            _coll_cost_val = float(sum(
                (pulp.value(t_base) or 0.0) * mu_b + (pulp.value(t_steep) or 0.0) * mu_s
                for (t_base, t_steep, mu_b, mu_s) in tier_vars.values()
            ))
            print(f"  LP  profile_err={_profile_err_before:,.0f}→{profile_error_val:,.0f}"
                  f"  notional_traded={_notional_traded:,.0f}"
                  f"  trading_cost={_trading_cost_val:,.0f} ({100*_trading_cost_val/max(_notional_traded,1):.1f}% of notional)"
                  f"  collateral_cost={_coll_cost_val:,.0f}"
                  f"  [tiered: base={_tier_base_val:,.0f}  steep={_tier_steep_val:,.0f}]"
                  f"  (effective_mu={effective_mu:.3f})")
        else:
            _coll_cost_val = float(np.sum(effective_mu * c_costs * np.abs(_net_pos)))
            print(f"  LP  profile_err={_profile_err_before:,.0f}→{profile_error_val:,.0f}"
                  f"  notional_traded={_notional_traded:,.0f}"
                  f"  trading_cost={_trading_cost_val:,.0f} ({100*_trading_cost_val/max(_notional_traded,1):.1f}% of notional)"
                  f"  collateral_cost={_coll_cost_val:,.0f}  (effective_mu={effective_mu:.3f})")
        _n_unwind = int(np.sum((net_qty * existing_qty < 0) & (np.abs(net_qty) > 0.5)))
        _n_new = int(np.sum((existing_qty == 0) & (np.abs(net_qty) > 0.5)))
        _n_frozen = int(np.sum(~tradeable))
        print(f"  LP trades  unwind={_n_unwind}  new={_n_new}  frozen_illiquid={_n_frozen} (|delta|<{min_trade_delta})")

        cash_by_counterparty = {
            cp: (
                float((forced_cash_by_counterparty or {}).get(cp, 0.0))
                + float(np.sum(buy_qty[indices] * c_prices[indices]) - np.sum(sell_qty[indices] * c_prices[indices]))
            )
            for cp, indices in ((cp, cp_indices.get(cp, [])) for cp in cash_cps)
        }
        if any(self._resolve(cash_neutrality_factor, cp) for cp in cash_cps):
            _cash_cost_val = float(sum(
                self._resolve(cash_neutrality_factor, cp) * abs(v) for cp, v in cash_by_counterparty.items()
            ))
            print(f"  cash_neutrality_cost={_cash_cost_val:,.0f}  " + "  ".join(
                f"{cp}={v:+,.0f}" for cp, v in cash_by_counterparty.items()
            ))

        return {
            "net_qty": net_qty,
            "trade_payoff": trade_payoff,
            "profile_error": profile_error_val,
            "cash_by_counterparty": cash_by_counterparty,
        }

    def _solve_problem(self, prob, algo):
        if algo == "PULP_CBC_CMD":
            prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=1))
        elif algo == "HiGHS":
            prob.solve(pulp.HiGHS(msg=False, timeLimit=1))
        else:
            raise ValueError(f"Invalid algo: {algo}")
        return prob.status
