"""
Plot the payoff at expiry of a portfolio of ETH options (20-Feb-2026 expiry).

Portfolio:
  #52  Buy  Put  K=2680    #53  Sell Put  K=4160
  #56  Buy  Call K=4100    #57  Sell Call K=5100
  #58  Buy  Put  K=2530    #59  Sell Put  K=4130
  #60  Buy  Call K=4100    #61  Sell Call K=5100
"""

import numpy as np
import matplotlib.pyplot as plt


def put_payoff(spot: np.ndarray, strike: float) -> np.ndarray:
    return np.maximum(strike - spot, 0.0)


def call_payoff(spot: np.ndarray, strike: float) -> np.ndarray:
    return np.maximum(spot - strike, 0.0)


def main():
    # Define the legs: (direction, option_type, strike)
    #   direction: +1 = Buy, -1 = Sell
    legs = [
        (+1, "put",  2680),   # #52
        (-1, "put",  4160),   # #53
        (+1, "call", 4100),   # #56
        (-1, "call", 5100),   # #57
        (+1, "put",  2530),   # #58
        (-1, "put",  4130),   # #59
        (+1, "call", 4100),   # #60
        (-1, "call", 5100),   # #61
    ]

    # Spot range at expiry
    strikes = [strike for _, _, strike in legs]
    lo = min(strikes) * 0.7
    hi = max(strikes) * 1.3
    spot = np.linspace(lo, hi, 2000)

    # Compute portfolio payoff
    portfolio_payoff = np.zeros_like(spot)
    for direction, opt_type, strike in legs:
        if opt_type == "put":
            portfolio_payoff += direction * put_payoff(spot, strike)
        else:
            portfolio_payoff += direction * call_payoff(spot, strike)

    # --- Plot ---
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(spot, portfolio_payoff, linewidth=2, color="steelblue", label="Portfolio payoff")
    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")

    # Mark the strikes
    for _, opt_type, strike in sorted(set(legs)):
        ax.axvline(strike, color="salmon", linewidth=0.6, linestyle=":")
        ax.annotate(
            f"{strike}\n({opt_type[0].upper()})",
            xy=(strike, 0), xytext=(0, -30),
            textcoords="offset points", ha="center", fontsize=8, color="grey",
        )

    ax.set_xlabel("ETH Spot Price at Expiry (USD)", fontsize=12)
    ax.set_ylabel("Portfolio Payoff (USD)", fontsize=12)
    ax.set_title("Portfolio Payoff at Expiry — 20 Feb 2026", fontsize=14)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()