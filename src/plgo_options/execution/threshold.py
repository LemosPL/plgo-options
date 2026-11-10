"""Threshold execution: work a large option order as a taker, crossing only when
the book is cheap against the smoothing spline.

The cost of taking one book level, USD per contract, is

    buy:  level price - fair + taker fee        sell:  fair - level price + taker fee

with fair from smile_fit.spline_fair: the arbitrage-free smoothing spline through
the expiry's out-of-the-money vols, every strike included (the traded one too), each at
its mid moved toward the thinner side by 0.15 x the size imbalance x the half-spread
(smile_fit.MICRO_KAPPA). The fee is Deribit's (pricing.exchange_quotes.deribit_option_fee_usd).

The threshold is in vol points (x the option's vega = USD), so one setting carries
across strikes and expiries. It ramps linearly from ``start_vp`` when the order is
created to ``end_vp`` one hour before the deadline, then sits at ``hard_vp`` for the
final hour. It never goes above ``hard_vp``: whatever is still open at the deadline
stays unfilled and is reported, rather than crossed at any price.

Each pass (every 5 s) takes the top of the book only, when it costs <= the threshold:
up to ``cap`` contracts, the remaining size and the size shown there, as one IOC limit
order at that price; after a fill the option rests 2 minutes (execution/bot.py).
scripts/execution_threshold_study.py replays the same rule on recorded books; with
5-minute snapshots (3-6 Nov 2026), 25DEC26 2500C, 10,000 over 12 h, the top of the book
only, a ramp of 0.5 -> 0.7 vol points cost ~$1.80 per contract, filled in ~8 h, against
~$5 for taking it all at once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from plgo_options.pricing.exchange_quotes import deribit_option_fee_usd

from .models import BookDepth, Side

FINAL_HOUR = 3600.0
QTY_STEP = 1.0  # Deribit's minimum ETH option amount


@dataclass(frozen=True)
class ThresholdOrder:
    """A parent order as the bot works it (one row of the exec_orders table)."""

    id: int
    instrument: str  # e.g. ETH-25DEC26-2500-C
    side: Side
    qty: float
    filled: float
    created_ts: float
    deadline_ts: float
    start_vp: float
    end_vp: float
    hard_vp: float
    cap: float  # contracts per take
    mode: str = "paper"
    status: str = "working"

    @property
    def expiry(self) -> str:
        return self.instrument.split("-")[1]

    @property
    def strike(self) -> float:
        return float(self.instrument.split("-")[2])

    @property
    def cp(self) -> str:
        return self.instrument.split("-")[3]

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled, 0.0)


def threshold_vp(o: ThresholdOrder, now: float) -> float:
    """start_vp at creation, linear to end_vp one hour before the deadline, hard_vp after."""
    ramp_end = o.deadline_ts - FINAL_HOUR
    if now >= ramp_end:
        return o.hard_vp
    span = max(ramp_end - o.created_ts, 1.0)
    return o.start_vp + (o.end_vp - o.start_vp) * min(max((now - o.created_ts) / span, 0.0), 1.0)


def level_cost(side: Side, price_eth: float, S: float, fair_usd: float) -> float:
    """USD per contract of taking one level, fee included; S converts ETH prices to USD."""
    usd = price_eth * S
    edge = usd - fair_usd if side == "buy" else fair_usd - usd
    return edge + deribit_option_fee_usd(S, usd)


@dataclass(frozen=True)
class Decision:
    action: str  # take | wait | no_book | outside
    theta_vp: float
    theta_usd: float
    best_cost: float | None  # USD per contract at the touch
    qty: float = 0.0
    limit_price: float | None = None  # ETH, the top of the book
    est_cost: float | None = None  # USD per contract
    detail: str = ""


def decide(o: ThresholdOrder, now: float, fair: dict, S: float, book: BookDepth,
           qty_step: float = QTY_STEP) -> Decision:
    """What to take on this pass. ``fair`` is smile_fit.spline_fair's dict."""
    th_vp = threshold_vp(o, now)
    th = th_vp * fair["vega"]
    levels = book.asks if o.side == "buy" else book.bids
    levels = [(p, a) for p, a in levels if p and a and a > 0]
    if not levels:
        return Decision("no_book", th_vp, th, None, detail=f"no {'offer' if o.side == 'buy' else 'bid'}")
    costs = [(p, a, level_cost(o.side, p, S, fair["fair"])) for p, a in levels]
    best = costs[0][2]
    if not fair["inside"]:  # the spline extrapolates past its last strike: no fair value to trust
        return Decision("outside", th_vp, th, best, detail="strike outside the spline's quoted range")
    top, size, _ = costs[0]  # the top of the book only: never walk down the levels
    take = math.floor(min(o.cap, o.remaining, size) / qty_step + 1e-9) * qty_step
    if best > th:
        return Decision("wait", th_vp, th, best,
                        detail=f"best ${best:.2f} > threshold ${th:.2f}")
    if take <= 0:
        return Decision("wait", th_vp, th, best, detail=f"top of book shows {size:g}, under one contract")
    return Decision("take", th_vp, th, best, take, top, best, detail=f"{take:g} at {top} ETH")
