from __future__ import annotations

from datetime import datetime


def safe_num(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def expiry_sort_key(expiry_code: str) -> tuple[int, str]:
    if expiry_code == "PERP":
        return (-1, expiry_code)

    try:
        expiry_date = datetime.strptime(expiry_code.upper(), "%d%b%y").date()
        return (expiry_date.toordinal(), expiry_code)
    except ValueError:
        return (10**9, expiry_code)
