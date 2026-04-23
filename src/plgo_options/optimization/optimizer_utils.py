from __future__ import annotations

from datetime import datetime


def _safe_int(v, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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

def get_expiry_code(expiry_str) -> str:
    expiry_code = datetime.strptime(expiry_str, "%Y-%m-%d").strftime("%d%b%y").upper()
    return expiry_code
