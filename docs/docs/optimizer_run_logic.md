## Purpose

The optimizer is designed to recommend option trades that move the current ETH options portfolio closer to a desired target payoff profile.

In practical terms, it answers the question:

> Given the current portfolio, market data, and target payoff shape, what trades should be added, reduced, or unwound to improve the portfolio outcome across a range of ETH spot prices?

The optimizer compares three things:

- the current portfolio payoff
- the desired target payoff
- the available trade candidates

It then proposes trades that aim to reduce the gap between the current portfolio and the target profile, while considering estimated trade costs and the efficiency of each proposed adjustment.

The output should be treated as a decision-support result, not as an automatic execution instruction. It helps identify portfolio reshaping opportunities, roll actions, and trade ideas under the assumptions of the model.

## Portfolio Payoff

For each existing position, the optimizer computes value across the spot ladder.

For a call:

The target profile is a dollar payoff curve:

| Input | Meaning |
|---|---|
| ETH spot | Current ETH reference price |
| Spot ladder | ETH spot scenarios used for payoff curves |
| Vol surface | Option implied vols by expiry and strike |
| Positions | Current portfolio positions |
| Target profile | Desired payoff across spot scenarios |
| Run parameters | Expiry filter, regularization, roll threshold, cost assumptions |

Typical spot ladder:

The optimizer is designed to recommend option trades that move the current ETH options portfolio closer to a desired target payoff profile.

In practical terms, it answers the question:

> Given the current portfolio, market data, and target payoff shape, what trades should be added, reduced, or unwound to improve the portfolio outcome across a range of ETH spot prices?

The optimizer compares three things:

- the current portfolio payoff
- the desired target payoff
- the available trade candidates

It then proposes trades that aim to reduce the gap between the current portfolio and the target profile, while considering estimated trade costs and the efficiency of each proposed adjustment.

The output should be treated as a decision-support result, not as an automatic execution instruction. It helps identify portfolio reshaping opportunities, roll actions, and trade ideas under the assumptions of the model.

| ETH spot | Target payoff |
|---:|---:|
| 0 | -5.0m |
| 1,000 | -9.0m |
| 2,000 | -19.0m |
| 3,000 | -10.0m |
| 4,000 | -10.0m |
| 5,000 | -10.0m |

## Output

The optimizer returns:

| Field | Meaning |
|---|---|
| `target_payoff` | Desired payoff curve |
| `before_payoff` | Cash-adjusted payoff before trades |
| `after_payoff` | Payoff after proposed trades |
| `raw_before_payoff` | Portfolio payoff before cash adjustment |
| `trades` | Final proposed trade list |
| `roll_unwind_trades` | Near-expiry unwind trades |
| `replacement_trades` | Non-roll optimizer trades |
| `premium_summary` | Premium sold, bought, and net |
| `fit_error_before` | Pre-trade mismatch |
| `fit_error_after` | Post-trade mismatch |
| `cash_shift` | Constant vertical adjustment |
| `candidates_evaluated` | Number of candidates considered |

---

## Summary

The optimizer is a payoff-shape fitter.

It:

1. Builds the current portfolio payoff.
2. Builds the target payoff.
3. Applies a cash adjustment.
4. Computes the residual mismatch.
5. Builds candidate trade curves.
6. Solves a sparse weighted fit.
7. Rounds and scores trades.
8. Adds roll unwinds and optional premium neutralization.
9. Returns before/after curves and proposed trades.

The key objective is simple:
