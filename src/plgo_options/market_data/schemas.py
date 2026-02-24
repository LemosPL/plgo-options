"""Pydantic models for Deribit market data."""

from __future__ import annotations

from pydantic import BaseModel


class PerpetualTicker(BaseModel):
    instrument_name: str
    last_price: float
    mark_price: float
    best_bid: float | None = None
    best_ask: float | None = None


class OptionTicker(BaseModel):
    instrument_name: str
    mark_price: float | None = None
    mark_iv: float | None = None
    underlying_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None

    @property
    def strike(self) -> float:
        """Parse strike from Deribit instrument name like ETH-28MAR25-2000-C."""
        parts = self.instrument_name.split("-")
        return float(parts[2])

    @property
    def option_type(self) -> str:
        """'C' or 'P'."""
        return self.instrument_name.split("-")[-1]

    @property
    def expiry_str(self) -> str:
        """E.g. '28MAR25'."""
        return self.instrument_name.split("-")[1]