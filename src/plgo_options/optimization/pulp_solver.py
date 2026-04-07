import numpy as np
import pulp


class PulpSolver:
    def __init__(self):
        pass

    def solve(self, price_by_expiry, cost_by_counterparty, collateral_by_counterparty):
        prob = pulp.LpProblem("IronCondorOptimization", pulp.LpMaximize)

        # --- Decision variables: buy_j, sell_j for each leg j ---
        n = len(price_by_expiry)
        m = len(cost_by_counterparty)
        x_vars = [None] * (2 * m)
        buy_bound = 10000000
        k = 0
        for expiry, price in price_by_expiry.items():
            for j in range(m):
                x_vars[k] = pulp.LpVariable(f"buy_fixed_{k}", lowBound=0, upBound=buy_bound, cat='Continuous')
                k += 1

        # --- Per counterparty collateral constraint ---
        j = 0
        for counterparty, collateral in collateral_by_counterparty.items():
            collateral_bound = 0.1*list(collateral_by_counterparty.values())[j]
            prob += (pulp.lpSum(x_vars[j + i*m] for i in range(n)) <= collateral_bound, f'collateral {counterparty}')
            j += 1

        # --- Objective: maximize iron condor exposure ---
        objective_terms = []
        for k in range(len(x_vars)):
            objective_terms.append(x_vars[k])

        prob += pulp.lpSum(objective_terms)

        prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=1))
        print(f"Status: {pulp.LpStatus[prob.status]}")

        solution = np.zeros(2 * m)
        for j in range(m):
            solution[2 * j] = x_vars[2 * j].varValue if x_vars[2 * j].varValue is not None else 0
            solution[2 * j + 1] = x_vars[2 * j + 1].varValue if x_vars[2 * j + 1].varValue is not None else 0

        return solution
