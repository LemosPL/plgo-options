"""Pricing endpoints."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, field_validator

import numpy as np

from plgo_options.pricing.options import bs_price, strategy_payoff
from plgo_options.pricing.vol_surface import VolSmile
from plgo_options.market_data.deribit_client import DeribitClient
from plgo_options.market_data.schemas import OptionTicker

router = APIRouter()
client = DeribitClient()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BSRequest(BaseModel):
    spot: float
    strike: float
    time_to_expiry: float  # in years
    risk_free_rate: float = 0.0
    volatility: float
    option_type: str = "C"


class LegInput(BaseModel):
    strike: float
    type: str  # "C" or "P"
    premium: float = 0.0
    quantity: float = 1.0
    is_long: bool = True


class PayoffRequest(BaseModel):
    spot_min: float
    spot_max: float
    legs: list[LegInput]
    num_points: int = 200


class ReplicationRequest(BaseModel):
    """Price a strategy by replicating from Deribit's live vol surface."""
    expiry: str  # Deribit expiry string, e.g. "27JUN25"
    legs: list[LegInput]  # premium field is ignored — will be computed
    spot_min: float
    spot_max: float
    risk_free_rate: float = 0.0
    num_points: int = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_deribit_expiry(expiry_str: str) -> datetime:
    """
    Parse a Deribit expiry like '27JUN25' into a timezone-aware datetime.
    Deribit options expire at 08:00 UTC on the expiry date.
    """
    dt = datetime.strptime(expiry_str, "%d%b%y")
    return dt.replace(hour=8, minute=0, second=0, tzinfo=timezone.utc)


def _time_to_expiry_years(expiry_str: str) -> float:
    """Return T in years from now to the Deribit expiry."""
    expiry_dt = _parse_deribit_expiry(expiry_str)
    now = datetime.now(timezone.utc)
    delta = expiry_dt - now
    return max(delta.total_seconds() / (365.25 * 86400), 1e-6)


async def _build_smile(expiry: str) -> tuple[VolSmile, list[OptionTicker], float]:
    """
    Fetch all option tickers for a given expiry from Deribit,
    build a VolSmile, and return (smile, tickers, eth_spot).
    """
    tickers = await client.get_all_option_tickers(expiry_dates={expiry})
    eth_spot = await client.get_eth_spot_price()

    # Collect (strike, IV) from calls and puts with valid mark_iv
    strike_iv: dict[float, list[float]] = {}
    for tk in tickers:
        if tk.mark_iv is not None and tk.mark_iv > 0:
            k = tk.strike
            strike_iv.setdefault(k, []).append(tk.mark_iv)

    # Average call/put IVs at each strike
    strikes = sorted(strike_iv.keys())
    ivs = [float(np.mean(strike_iv[k])) for k in strikes]

    if len(strikes) < 2:
        raise ValueError(
            f"Not enough IV data for expiry {expiry} "
            f"(got {len(strikes)} strikes with valid IV)"
        )

    smile = VolSmile(strikes, ivs)
    return smile, tickers, eth_spot


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/bs")
async def price_bs(req: BSRequest) -> dict:
    price = bs_price(
        S=req.spot,
        K=req.strike,
        T=req.time_to_expiry,
        r=req.risk_free_rate,
        sigma=req.volatility,
        option_type=req.option_type,
    )
    return {"price": round(price, 6)}


@router.post("/payoff")
async def compute_payoff(req: PayoffRequest) -> dict:
    spots = np.linspace(req.spot_min, req.spot_max, req.num_points)
    legs = [leg.model_dump() for leg in req.legs]
    pnl = strategy_payoff(spots, legs)
    return {
        "spots": spots.tolist(),
        "pnl": pnl.tolist(),
    }


@router.post("/replicate")
async def replicate_strategy(req: ReplicationRequest) -> dict:
    """
    Price each leg of a strategy using Deribit's live implied vol surface.

    1. Fetch all option IVs for the given expiry
    2. Build a cubic-spline vol smile
    3. For each leg, interpolate IV at the leg's strike
    4. Compute BS premium with that IV
    5. Return premiums, payoff curves, and the smile for charting
    """
    smile, tickers, eth_spot = await _build_smile(req.expiry)
    T = _time_to_expiry_years(req.expiry)
    r = req.risk_free_rate

    spots = np.linspace(req.spot_min, req.spot_max, req.num_points)
    pnl_expiry = np.zeros_like(spots, dtype=float)
    pnl_now = np.zeros_like(spots, dtype=float)

    leg_details = []

    for leg in req.legs:
        # Interpolate IV at this strike
        iv_pct = smile.iv_at(leg.strike)  # e.g. 80.0
        sigma = iv_pct / 100.0  # e.g. 0.80

        # BS premium at current spot
        prem = bs_price(S=eth_spot, K=leg.strike, T=T, r=r, sigma=sigma,
                        option_type=leg.type)

        direction = 1.0 if leg.is_long else -1.0

        # Expiry payoff
        if leg.type == "C":
            intrinsic = np.maximum(spots - leg.strike, 0.0)
        else:
            intrinsic = np.maximum(leg.strike - spots, 0.0)
        pnl_expiry += direction * leg.quantity * (intrinsic - prem)

        # Current BS value across spot range (using same IV for this leg)
        bs_vals = np.array([
            bs_price(S=s, K=leg.strike, T=T, r=r, sigma=sigma, option_type=leg.type)
            for s in spots
        ])
        pnl_now += direction * leg.quantity * (bs_vals - prem)

        leg_details.append({
            "strike": leg.strike,
            "type": leg.type,
            "side": "buy" if leg.is_long else "sell",
            "quantity": leg.quantity,
            "iv_pct": round(iv_pct, 2),
            "sigma": round(sigma, 4),
            "bs_premium_usd": round(prem, 4),
            "bs_premium_eth": round(prem / eth_spot, 6),
        })

    # Total strategy cost (in USD, since bs_price returns USD)
    total_cost_usd = sum(
        (1 if d["side"] == "buy" else -1) * d["quantity"] * d["bs_premium_usd"]
        for d in leg_details
    )

    return {
        "eth_spot": eth_spot,
        "expiry": req.expiry,
        "time_to_expiry": round(T, 6),
        "spots": spots.tolist(),
        "pnl_expiry": pnl_expiry.tolist(),
        "pnl_now": pnl_now.tolist(),
        "legs": leg_details,
        "total_cost_usd": round(total_cost_usd, 2),
        "total_cost_eth": round(total_cost_usd / eth_spot, 6),
        "smile": smile.to_dict(),
    }