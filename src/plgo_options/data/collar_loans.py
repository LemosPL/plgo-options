"""Collar-loan counterparties, excluded from the managed book by default.

The FalconX and Galaxy books are collar loans — borrowing structures, not
positions the desk is steering. Leaving them in meant Optimizer v4 fitted its
parametric target against a book that included legs nobody intends to adjust,
and the agents sized rows and gate checks off the same inflated exposure.

So they are excluded **by default**, everywhere, and come back only when a
caller explicitly asks for them (the Optimizer v4 screen has a toggle; it ships
checked). Default-excluded is the deliberate direction: a new collar loan with
either counterparty is caught automatically rather than silently entering the
analysis until somebody notices.

Matching is by counterparty name, case- and space-insensitive, because that is
the one field every path has — positions, trades, collateral rows — and it does
not depend on the structure classifier correctly recognising a collar.

The trade-off, accepted deliberately: any NON-collar business with either name
is hidden too. If that ever starts, this rule needs to become structure-aware.
"""

from __future__ import annotations

import os

# Overridable so a change does not need a code edit:
#   COLLAR_LOAN_COUNTERPARTIES="falconx,galaxy,someoneelse"
#   COLLAR_LOAN_COUNTERPARTIES=""        -> feature off, nothing excluded
_DEFAULT = "FalconX,Galaxy"
_raw = os.environ.get("COLLAR_LOAN_COUNTERPARTIES", _DEFAULT)


def _norm(name: object) -> str:
    """Fold a counterparty name for comparison: 'Falcon X ' -> 'falconx'."""
    return "".join(str(name or "").split()).lower()


COLLAR_LOAN_COUNTERPARTIES: frozenset[str] = frozenset(
    _norm(p) for p in _raw.split(",") if _norm(p)
)


def is_collar_loan(counterparty: object) -> bool:
    """True when this counterparty's book is collar loans, not managed risk."""
    return _norm(counterparty) in COLLAR_LOAN_COUNTERPARTIES


def _counterparty_of(row, key: str = "counterparty"):
    """Read ``key`` off a dict-like row or an object, whichever it is.

    The same helper runs over DB rows, /pnl positions and optimizer Positions,
    and those are not the same shape.
    """
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, None)


def split_collar_loans(rows: list, key: str = "counterparty") -> tuple[list, list]:
    """``(kept, excluded)`` in one pass, so callers can report what was removed."""
    if not COLLAR_LOAN_COUNTERPARTIES:
        return list(rows), []
    kept, excluded = [], []
    for r in rows:
        (excluded if is_collar_loan(_counterparty_of(r, key)) else kept).append(r)
    return kept, excluded


def drop_collar_loans(rows: list, key: str = "counterparty") -> list:
    """Rows minus any whose ``key`` is a collar-loan counterparty."""
    return split_collar_loans(rows, key)[0]
