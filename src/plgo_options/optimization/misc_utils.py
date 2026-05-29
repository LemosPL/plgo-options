import os
import numpy as np
import pandas as pd
from scipy.interpolate import UnivariateSpline


def shift_target_profile(
    target_profile: pd.DataFrame,
    current_spot: float,
    payoff_col: str = "Payoff($)",
) -> pd.DataFrame:
    """
    Homothetically scale the target profile's strike axis so that the minimum
    payoff occurs at current_spot.

    Example:
        If the CSV minimum is at 2000 and current_spot is 2400,
        all strikes are multiplied by 2400 / 2000 = 1.2.
    """
    shifted = target_profile.copy()
    shifted.index = shifted.index.astype(float)

    min_strike = float(shifted[payoff_col].idxmin())
    if min_strike <= 0:
        raise ValueError("Cannot homothetically shift target profile with non-positive minimum strike.")

    scale = float(current_spot) / min_strike
    shifted.index = shifted.index * scale
    shifted.index.name = target_profile.index.name

    return shifted


def smooth_target_profile(target_profile, payoff_col="Payoff($)", smooth_factor=1e13):
    strikes = target_profile.index.astype(float).to_numpy()
    payoffs = target_profile[payoff_col].astype(float).to_numpy()

    spline = UnivariateSpline(strikes, payoffs, s=smooth_factor)

    smoothed = target_profile.copy()
    smoothed[payoff_col] = spline(strikes)
    return smoothed


def load_target_profile():
    base_filename = "data/ETH - target shifted v2.csv"
    filename = base_filename
    if os.path.exists("../" + base_filename):
        filename = "../" + base_filename
    elif os.path.exists("../../" + base_filename):
        filename = "../../" + base_filename
    elif os.path.exists("../../../" + base_filename):
        filename = "../../../" + base_filename
    target_profile = pd.read_csv(filename, index_col=0)  # "Payoff ($)")
    smoothed_profile = smooth_target_profile(target_profile)
    return smoothed_profile

def build_parametric_target_profile(
    spot_ladder: list[float] | np.ndarray,
    current_spot: float,
    payoff_col: str = "Payoff($)",
    low_floor_ratio: float = 0.5,
    trough_ratio: float = 1.0,
    high_plateau_ratio: float = 1.7,
    low_floor_payoff: float = -5_000_000.0,
    trough_payoff: float = -19_000_000.0,
    high_plateau_payoff: float = -5_000_000.0,
) -> pd.DataFrame:
    strikes = np.asarray(spot_ladder, dtype=float)
    ratios = strikes / float(current_spot)

    payoffs = np.interp(
        ratios,
        [low_floor_ratio, trough_ratio, high_plateau_ratio],
        [low_floor_payoff, trough_payoff, high_plateau_payoff],
    )

    target_profile = pd.DataFrame(
        {payoff_col: payoffs},
        index=pd.Index(strikes, name="Strike($)"),
    )

    smoothed_profile = smooth_target_profile(target_profile)
    return smoothed_profile
