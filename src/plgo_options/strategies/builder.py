"""Pre-built option strategy templates."""

from __future__ import annotations


def long_call(strike: float, premium: float, qty: float = 1.0) -> list[dict]:
    return [{"strike": strike, "type": "C", "premium": premium, "quantity": qty, "is_long": True}]


def long_put(strike: float, premium: float, qty: float = 1.0) -> list[dict]:
    return [{"strike": strike, "type": "P", "premium": premium, "quantity": qty, "is_long": True}]


def bull_call_spread(
    low_strike: float, high_strike: float,
    low_premium: float, high_premium: float,
    qty: float = 1.0,
) -> list[dict]:
    return [
        {"strike": low_strike, "type": "C", "premium": low_premium, "quantity": qty, "is_long": True},
        {"strike": high_strike, "type": "C", "premium": high_premium, "quantity": qty, "is_long": False},
    ]


def straddle(
    strike: float, call_premium: float, put_premium: float,
    qty: float = 1.0, is_long: bool = True,
) -> list[dict]:
    return [
        {"strike": strike, "type": "C", "premium": call_premium, "quantity": qty, "is_long": is_long},
        {"strike": strike, "type": "P", "premium": put_premium, "quantity": qty, "is_long": is_long},
    ]


def strangle(
    call_strike: float, put_strike: float,
    call_premium: float, put_premium: float,
    qty: float = 1.0, is_long: bool = True,
) -> list[dict]:
    return [
        {"strike": call_strike, "type": "C", "premium": call_premium, "quantity": qty, "is_long": is_long},
        {"strike": put_strike, "type": "P", "premium": put_premium, "quantity": qty, "is_long": is_long},
    ]


def iron_condor(
    put_low: float, put_high: float,
    call_low: float, call_high: float,
    premiums: dict[str, float],
    qty: float = 1.0,
) -> list[dict]:
    """Short iron condor (collect premium)."""
    return [
        {"strike": put_low, "type": "P", "premium": premiums["put_low"], "quantity": qty, "is_long": True},
        {"strike": put_high, "type": "P", "premium": premiums["put_high"], "quantity": qty, "is_long": False},
        {"strike": call_low, "type": "C", "premium": premiums["call_low"], "quantity": qty, "is_long": False},
        {"strike": call_high, "type": "C", "premium": premiums["call_high"], "quantity": qty, "is_long": True},
    ]