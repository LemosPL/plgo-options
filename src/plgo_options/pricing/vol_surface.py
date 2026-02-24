"""
Volatility surface construction from Deribit market IVs.

Given a set of (strike, IV) points for a single expiry, builds an interpolator
that can return σ(K) for any strike — including extrapolation into the wings.

Interpolation: cubic spline on the interior.
Extrapolation: flat (sticky-strike) beyond observed range.
"""

from __future__ import annotations

import numpy as np
from scipy.interpolate import CubicSpline


class VolSmile:
    """
    A single-expiry volatility smile built from market data.

    Parameters
    ----------
    strikes : array-like of observed strikes
    ivs     : array-like of corresponding implied vols (in %, e.g. 80.0 = 80%)
    """

    def __init__(self, strikes: list[float], ivs: list[float]) -> None:
        if len(strikes) < 2:
            raise ValueError("Need at least 2 strike/IV pairs to build a smile")

        # Sort by strike
        order = np.argsort(strikes)
        self.strikes = np.array(strikes, dtype=float)[order]
        self.ivs = np.array(ivs, dtype=float)[order]  # in % (e.g. 80.0)

        # Cubic spline — natural boundary (second derivative = 0 at edges)
        self._spline = CubicSpline(
            self.strikes,
            self.ivs,
            bc_type="natural",
            extrapolate=False,  # we handle extrapolation manually
        )

        self.k_min = float(self.strikes[0])
        self.k_max = float(self.strikes[-1])

    def iv_at(self, strike: float) -> float:
        """
        Return interpolated/extrapolated IV (in %) for a given strike.

        - Interior: cubic spline
        - Left wing (K < k_min): flat at the lowest observed IV
        - Right wing (K > k_max): flat at the highest observed IV
        """
        if strike <= self.k_min:
            return float(self.ivs[0])
        if strike >= self.k_max:
            return float(self.ivs[-1])
        return float(self._spline(strike))

    def iv_at_array(self, strikes: np.ndarray) -> np.ndarray:
        """Vectorised version of iv_at."""
        result = np.empty_like(strikes, dtype=float)
        interior = (strikes > self.k_min) & (strikes < self.k_max)
        result[strikes <= self.k_min] = self.ivs[0]
        result[strikes >= self.k_max] = self.ivs[-1]
        if interior.any():
            result[interior] = self._spline(strikes[interior])
        return result

    def to_dict(self) -> dict:
        """Serialisable representation for the frontend."""
        # Sample the smile densely for charting
        k_lo = self.k_min * 0.8
        k_hi = self.k_max * 1.2
        k_range = np.linspace(k_lo, k_hi, 200)
        iv_range = self.iv_at_array(k_range)
        return {
            "observed_strikes": self.strikes.tolist(),
            "observed_ivs": self.ivs.tolist(),
            "smile_strikes": k_range.tolist(),
            "smile_ivs": iv_range.tolist(),
            "k_min": self.k_min,
            "k_max": self.k_max,
        }