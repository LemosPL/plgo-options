"""
Fetch ETH perpetual and options market data from Deribit public API.

Uses only public (unauthenticated) endpoints — no API key required.
Filters options to maturities that match the trades in the portfolio.

References:
  - https://docs.deribit.com/api-reference/market-data/public-get_instruments
  - https://docs.deribit.com/api-reference/market-data/public-ticker
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# -- Add project root so we can import the trade reader --
sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_eth_trades import read_eth_trades

BASE_URL = "https://www.deribit.com/api/v2"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _get(method: str, params: dict | None = None) -> dict:
    """Call a Deribit public JSON-RPC endpoint via HTTP GET."""
    url = f"{BASE_URL}/public/{method}"
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Deribit API error: {data['error']}")
    return data["result"]


# ---------------------------------------------------------------------------
# Instrument discovery
# ---------------------------------------------------------------------------

def get_eth_perpetual() -> dict:
    """Return the ETH-PERPETUAL instrument metadata."""
    instruments = _get("get_instruments", {"currency": "ETH", "kind": "future"})
    for inst in instruments:
        if inst["instrument_name"] == "ETH-PERPETUAL":
            return inst
    raise ValueError("ETH-PERPETUAL not found")


def get_eth_options(expiry_dates: set[str] | None = None) -> list[dict]:
    """
    Return all active ETH options.
    If *expiry_dates* is given (e.g. {"28MAR25", "27JUN25"}), only options
    whose instrument name contains one of those date strings are returned.
    """
    instruments = _get("get_instruments", {"currency": "ETH", "kind": "option"})
    if expiry_dates is None:
        return instruments
    return [
        inst for inst in instruments
        if any(d in inst["instrument_name"] for d in expiry_dates)
    ]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_ticker(instrument_name: str) -> dict:
    """Get current ticker (mark price, bid/ask, greeks, IV, …)."""
    return _get("ticker", {"instrument_name": instrument_name})


def get_order_book(instrument_name: str, depth: int = 5) -> dict:
    """Get top-of-book order book for an instrument."""
    return _get("get_order_book", {"instrument_name": instrument_name, "depth": depth})


# ---------------------------------------------------------------------------
# Portfolio-aware helpers
# ---------------------------------------------------------------------------

def extract_maturities_from_trades(trades: list[dict]) -> set[str]:
    """
    Scan trades for expiry / maturity fields and return a set of
    Deribit-style date strings (e.g. {"28MAR25", "27JUN25"}).
    """
    maturities: set[str] = set()
    for trade in trades:
        # Try common column names for expiry/maturity
        key = 'Option Expiry Date'
        raw = trade[key]
        if raw is None:
            continue
        if isinstance(raw, datetime):
            maturities.add(raw.strftime("%d%b%y").upper())
        elif isinstance(raw, str) and raw.strip():
            # Try to parse various date formats
            for fmt in ("%d%b%y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d-%b-%y"):
                try:
                    dt = datetime.strptime(raw.strip(), fmt)
                    maturities.add(dt.strftime("%d%b%y").upper())
                    break
                except ValueError:
                    continue
    return maturities


def fetch_portfolio_market_data(trades: list[dict]) -> dict:
    """
    Given the portfolio trades, fetch:
      1. ETH-PERPETUAL ticker
      2. Tickers for all options on maturities of interest
    Returns a dict with keys 'perpetual' and 'options'.
    """
    # --- Perpetual ---
    print("Fetching ETH-PERPETUAL ticker …")
    perp_ticker = get_ticker("ETH-PERPETUAL")
    eth_price = perp_ticker["last_price"]
    print(f"  ETH-PERPETUAL  last={eth_price}  mark={perp_ticker['mark_price']}")

    # --- Options on portfolio maturities ---
    maturities = extract_maturities_from_trades(trades)
    if not maturities:
        print("\n⚠  Could not extract maturities from trades. Fetching ALL active ETH options.")
        options = get_eth_options()
    else:
        print(f"\nPortfolio maturities detected: {sorted(maturities)}")
        options = get_eth_options(expiry_dates=maturities)
        if not options:
            print("  No active Deribit options match those maturities (they may have expired).")
            print("  Falling back to all active ETH options.")
            options = get_eth_options()

    print(f"Found {len(options)} option instruments to price.\n")

    option_tickers: list[dict] = []
    for i, opt in enumerate(options):
        name = opt["instrument_name"]
        try:
            tk = get_ticker(name)
            option_tickers.append(tk)
            if i < 10 or (i + 1) % 50 == 0:
                greeks = tk.get("greeks", {})
                print(
                    f"  [{i+1}/{len(options)}] {name:40s}"
                    f"  mark={tk.get('mark_price', 'N/A'):>10}"
                    f"  iv={tk.get('mark_iv', 'N/A'):>8}"
                    f"  delta={greeks.get('delta', 'N/A'):>8}"
                )
        except Exception as exc:
            print(f"  [{i+1}/{len(options)}] {name:40s}  ERROR: {exc}")

    if len(options) > 10:
        print(f"  … ({len(options) - 10} more fetched, showing first 10 above)")

    return {
        "perpetual": perp_ticker,
        "options": option_tickers,
        "eth_spot": eth_price,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    trades = read_eth_trades()
    print(f"Loaded {len(trades)} trades from Excel.\n")

    mkt = fetch_portfolio_market_data(trades)

    print(f"\n{'='*60}")
    print(f"Summary")
    print(f"{'='*60}")
    print(f"  ETH spot (perp last): ${mkt['eth_spot']:,.2f}")
    print(f"  Options priced:       {len(mkt['options'])}")