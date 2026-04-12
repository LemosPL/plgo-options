from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left
from datetime import date, datetime
from math import log, sqrt
from typing import Any

import numpy as np
from scipy.interpolate import CubicSpline


@dataclass(frozen=True)
class SmileSlice:
    expiry_code: str
    maturity: datetime
    strikes: list[float]
    ivs: list[float]   # decimal vols, e.g. 0.80 for 80%


class OptionSmile:
    """
    Smile surface built from Deribit-style smile slices.

    Interpolation:
      - in strike: cubic spline on log(strike) vs total variance
      - flat tails outside strike range
      - in time: linear interpolation on total variance between expiries

    Inputs:
      - slices can be dicts like:
        {
          "expiry_code": "27JUN25",
          "expiry_date": "2025-06-27",
          "strikes": [...],
          "ivs": [...]
        }
      - or SmileSlice objects
    """

    def __init__(self, slices: list[SmileSlice | dict[str, Any]]):
        self.slices = [self._coerce_slice(s) for s in slices]
        self.slices.sort(key=lambda s: s.maturity)

        if not self.slices:
            raise ValueError("OptionSmile requires at least one smile slice.")

        self._maturities = [s.maturity for s in self.slices]
        self._maturity_to_spline: dict[datetime, CubicSpline] = {
            s.maturity: self._build_spline(s) for s in self.slices
        }

    def _coerce_slice(self, s: SmileSlice | dict[str, Any]) -> SmileSlice:
        if isinstance(s, SmileSlice):
            return s

        expiry_code = str(s.get("expiry_code") or "")
        expiry_date = s.get("expiry_date") or s.get("expiry") or s.get("maturity")
        if expiry_date is None:
            raise ValueError(f"Slice {expiry_code or '<unknown>'}: missing expiry_date/maturity")

        maturity = self._parse_datetime(expiry_date)
        strikes = list(map(float, s.get("strikes") or []))
        ivs = list(map(float, s.get("ivs") or s.get("vols") or []))

        return SmileSlice(
            expiry_code=expiry_code,
            maturity=maturity,
            strikes=strikes,
            ivs=ivs,
        )

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        if isinstance(value, str):
            # Accept "YYYY-MM-DD" or ISO datetime strings
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return datetime.strptime(value, "%Y-%m-%d")
        raise TypeError(f"Unsupported maturity type: {type(value).__name__}")

    def _year_fraction(self, maturity: datetime) -> float:
        today = datetime.utcnow()
        return max((maturity - today).total_seconds(), 0.0) / (365.25 * 24 * 3600)

    def _build_spline(self, s: SmileSlice) -> CubicSpline:
        if len(s.strikes) != len(s.ivs):
            raise ValueError(f"{s.expiry_code}: strikes and ivs must have same length")
        if len(s.strikes) < 2:
            raise ValueError(f"{s.expiry_code}: need at least 2 strike points")

        strikes = np.asarray(s.strikes, dtype=float)
        ivs = np.asarray(s.ivs, dtype=float)

        if np.any(strikes <= 0):
            raise ValueError(f"{s.expiry_code}: strikes must be positive")
        if np.any(ivs < 0):
            raise ValueError(f"{s.expiry_code}: ivs must be non-negative")

        order = np.argsort(strikes)
        strikes = strikes[order]
        ivs = ivs[order]

        T = self._year_fraction(s.maturity)
        w = (ivs ** 2) * T if T > 0 else np.zeros_like(ivs)
        x = np.log(strikes)

        # natural spline; tails are handled explicitly by clamping in compute_vol()
        return CubicSpline(x, w, bc_type="natural", extrapolate=True)

    def _variance_at_maturity(self, maturity: datetime, strike: float) -> float:
        if strike <= 0:
            raise ValueError("Strike must be positive")

        log_strike = log(float(strike))
        mats = self._maturities

        def _eval_clamped(spline, x: float) -> float:
            x_min = float(spline.x[0])
            x_max = float(spline.x[-1])
            x_clamped = min(max(x, x_min), x_max)
            return float(spline(x_clamped))

        if maturity in self._maturity_to_spline:
            return max(_eval_clamped(self._maturity_to_spline[maturity], log_strike), 0.0)

        if maturity <= mats[0]:
            return max(_eval_clamped(self._maturity_to_spline[mats[0]], log_strike), 0.0)
        if maturity >= mats[-1]:
            return max(_eval_clamped(self._maturity_to_spline[mats[-1]], log_strike), 0.0)

        i = bisect_left(mats, maturity)
        m0, m1 = mats[i - 1], mats[i]

        w0 = _eval_clamped(self._maturity_to_spline[m0], log_strike)
        w1 = _eval_clamped(self._maturity_to_spline[m1], log_strike)

        T = self._year_fraction(maturity)
        T0 = self._year_fraction(m0)
        T1 = self._year_fraction(m1)

        alpha = (T - T0) / (T1 - T0) if T1 > T0 else 0.0
        return max((1.0 - alpha) * w0 + alpha * w1, 0.0)

    def compute_vol(self, maturity: datetime, strike: float) -> float:
        """
        Return implied vol in decimal form.
        """
        T = self._year_fraction(maturity)
        if T <= 0:
            return 0.0

        w = self._variance_at_maturity(maturity, strike)
        return sqrt(w / T) if w > 0 else 0.0