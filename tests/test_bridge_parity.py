from __future__ import annotations

import numpy as np

from plgo_options.optimization.math_utils import bs_vec_bridge


def test_box_keeps_its_value_past_expiry_with_a_smile():
    # A 0.66/1.65 box expiring on day 69, valued on day 90, with a different
    # vol at each strike: worth exactly K2 - K1 at every spot.
    S0, spots, dte, h = 1.16, np.array([0.5, 1.16, 2.5]), 69, 90
    leg = lambda k, vol, opt: bs_vec_bridge(S0, spots, k, dte, h, vol, opt)
    box = (leg(0.66, 1.91, "C") - leg(0.66, 1.91, "P")) - (leg(1.65, 1.44, "C") - leg(1.65, 1.44, "P"))
    np.testing.assert_allclose(box, 1.65 - 0.66, atol=1e-9)


def test_synthetic_forward_is_flat_at_unchanged_spot_past_expiry():
    S0 = 1.16
    fwd = bs_vec_bridge(S0, np.array([S0]), 1.0, 69, 90, 2.5, "C") - bs_vec_bridge(S0, np.array([S0]), 1.0, 69, 90, 2.5, "P")
    np.testing.assert_allclose(fwd, S0 - 1.0, atol=1e-9)
