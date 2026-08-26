"""Counterparty-specific pricing methodologies, calibrated from real trades.

Mirrors web/static/app.js's CPTY_PRICING/applyCptyPricing exactly — same models,
same calibrated numbers — so the optimizer's own candidate prices match what the
Pricing tab shows for the same counterparty, instead of both silently agreeing
only when Pricing is set to "mid vol surface" (which is all the optimizer ever
used before this). Keep the two in sync if either changes.

Two shapes, tagged by "model":

"otm_flat_vol_itm_intrinsic" (Flowdesk FIL) — split by MONEYNESS: OTM legs get
a flat elevated wing vol (ignoring your skew); ITM legs get intrinsic value
only (no time premium), with the forward shaded a few % against the option
they trade.

"two_sided_vol" (Keyrock FIL) — split by WHICH SIDE we trade, not by strike: a
flat bid vol when we sell (they buy from us), a flat ask vol when we buy (they
sell to us), widening further once the strike is far from spot.

A counterparty/asset pair absent from CPTY_PRICING (Wave, G20, and ETH for
both Flowdesk and Keyrock as of writing — none calibrated yet) returns None
from resolve_price, and callers fall back to the mid vol surface, same as the
Pricing tab's own uncalibrated-counterparty behavior.
"""
from __future__ import annotations

import math

from ..optimization.math_utils import bs_price

CPTY_PRICING: dict[str, dict[str, dict]] = {
    "Flowdesk": {
        "FIL": {
            "model": "otm_flat_vol_itm_intrinsic",
            "otm_flat_vol": 95.0,          # flat wing vol (%) applied to every OTM leg
            "itm_forward_lean_pct": 4.5,   # forward shaded against the traded option on ITM legs
        },
        # ETH: not calibrated yet — fill in as ETH trades with Flowdesk.
    },
    "KeyRock": {
        "FIL": {
            "model": "two_sided_vol",
            "bid_vol": 116.0,              # vol they BUY from us at (legs we sell)
            "ask_vol": 163.0,              # vol they SELL to us at (legs we buy)
            "far_ask_vol": 185.0,          # ask vol once the strike is far from spot
            "far_wing_threshold": 0.5,     # |ln(K/spot)| beyond which far_ask_vol applies
        },
        # ETH: not calibrated yet — fill in as ETH trades with Keyrock.
    },
    # Wave, G20: not calibrated yet — resolve_price returns None, callers use mid vol.
}


def get_method(counterparty: str, asset: str) -> dict | None:
    """The calibrated methodology for (counterparty, asset), or None if uncalibrated."""
    return CPTY_PRICING.get(counterparty, {}).get(asset)


def resolve_price(
    counterparty: str, asset: str, spot: float, strike: float, T: float, opt: str, side: str,
) -> tuple[float, float] | None:
    """(price, iv_pct actually used) per the counterparty's real quoting behavior
    for a leg WE trade on `side` ("buy" or "sell"), or None if uncalibrated (fall
    back to the mid vol surface). r=0 throughout, matching the rest of this
    codebase's pricing (see math_utils.bs_price / bs_greeks)."""
    method = get_method(counterparty, asset)
    if method is None:
        return None

    if method["model"] == "two_sided_vol":
        if side == "sell":
            vol_pct = method["bid_vol"]
        else:
            logm = abs(math.log(strike / spot))
            far = method.get("far_ask_vol")
            vol_pct = far if (far is not None and logm > method["far_wing_threshold"]) else method["ask_vol"]
        price = bs_price(spot, strike, T, 0.0, vol_pct / 100.0, opt)
        return price, vol_pct

    if method["model"] == "otm_flat_vol_itm_intrinsic":
        intrinsic = max(spot - strike, 0.0) if opt == "C" else max(strike - spot, 0.0)
        if intrinsic <= 0:
            vol_pct = method["otm_flat_vol"]
            price = bs_price(spot, strike, T, 0.0, vol_pct / 100.0, opt)
            return price, vol_pct
        # ITM: intrinsic only, forward leaned in the counterparty's favor — the
        # forward moves against whatever WE'RE trading (buy call/sell put -> fwd
        # up is bad for the taker; sell call/buy put -> fwd down is bad for us).
        lean = method["itm_forward_lean_pct"] / 100.0
        sign = (+1 if side == "buy" else -1) if opt == "C" else (-1 if side == "buy" else +1)
        f_lean = spot * (1 + sign * lean)
        price = max(f_lean - strike, 0.0) if opt == "C" else max(strike - f_lean, 0.0)
        return price, 0.0

    return None
