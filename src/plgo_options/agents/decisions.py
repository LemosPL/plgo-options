"""Decision sheets agreed by Lucas and Chris, applied to the stored policy once.

The live app sits behind Google Sign-In, so a decision can't be pushed to it
from a script. Each sheet here is applied once at startup (a flag records it)
through ``store.save_policy``, so it shows in the policy history under its own
name. A sheet only touches the fields it names; anything else, such as the
optimizer's tuned ratios or the floor, keeps its stored value.
"""

from __future__ import annotations

from typing import Any

from plgo_options.agents import store
from plgo_options.agents.policy import AssetPolicy

# 5 Oct 2026 decision sheet (Chris's feedback, section 4). Sheet line in brackets.
# Interpretation agreed with Lucas the same day:
#   [6]  all counterparties, 25DEC26, switch to 26MAR27 once Dec is a month out
#   [7]  Lucas rolls alone inside his limits
#   [14] options alone up to $1M ETH / $2.5M FIL; perps up to full delta
#   [15] $250k (the sheet said "250K 300K")
#   [16] 1% of the $25M book = $250k
#   [17] perp cap $500k, or the options book's delta if larger
#   [12] floor "-" and [18] funding "-": no floor written, no funding budget
_COMMON: dict[str, Any] = {
    "view": "up",                                  # [1]
    "view_check_date": "2026-10-31",               # [3]
    "reference_set_on": "2026-10-05",              # [8] stops monday-pack restriking this week
    "book_notional_usd": 25_000_000,               # [13]
    "max_cost_per_trade_usd": 250_000,             # [15]
    "max_cost_per_month_usd": 250_000,             # [16]
    "max_perp_notional_usd": 500_000,              # [17]
    "perp_cap_full_delta": True,                   # [17]
    "perp_funding_budget_month_usd": None,         # [18]
    "perp_venue": "Flowdesk",                      # [19]
    "rolls_need_chris": False,                     # [7]
    "is_example": False,
}
_OPT_COMMON: dict[str, Any] = {
    "target_trough_payoff": -20_000_000.0,         # [5]
    "target_expiry": "25DEC26",                    # [6]
    "next_expiry": "26MAR27",
    "switch_days_before": 30,
    "counterparties": [],                          # [6] all
}

SHEET_2026_10_05: dict[str, dict[str, Any]] = {
    "ETH": {**_COMMON,
            "view_range_low": 3_000, "view_range_high": 3_500,          # [2]
            "reference_price": 2_700,                                     # [8]
            "row_steps_pct": [5, 10, 20],                                 # [9]
            "stop_price": 2_300,                                          # [10]
            "stop_loss_usd": 2_500_000,                                   # [11]
            "max_single_trade_usd": 1_000_000,                            # [14]
            "optimizer": dict(_OPT_COMMON)},
    "FIL": {**_COMMON,
            "view_range_low": 2.00, "view_range_high": 2.50,             # [2]
            "view_note": "Big move up; if it comes, $4 is the target.",
            "reference_price": 1.00,                                      # [8]
            "row_steps_pct": [10, 20, 30],                                # [9]
            "stop_price": 0.89,                                           # [10]
            "stop_loss_usd": 5_000_000,                                   # [11]
            "max_single_trade_usd": 2_500_000,                            # [14]
            "optimizer": dict(_OPT_COMMON)},
}

# 6 Oct 2026, Lucas: the sweep must test target profiles (the agreed V plus the
# saved ones), λ coarse 0.1-3.5 then 0.1 steps around the best, and FIL in FIL-sized quantities (it was capped
# at 5,000 FIL, v4's ETH default; the v4 page uses 5,000,000 for FIL).
_SWEEP_GRID: dict[str, Any] = {
    "target_grid": [], "max_targets": 3,
    "lam_grid": [0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5], "lam_refine_step": 0.1,
    "downside_grid": [1.1], "t90_grid": [0.5], "max_trades_grid": [5, 7], "max_runs": 40,
}
SWEEP_2026_10_06: dict[str, dict[str, Any]] = {
    "ETH": {"optimizer": {**_SWEEP_GRID, "max_qty_grid": [5_000.0]}},
    "FIL": {"optimizer": {**_SWEEP_GRID, "max_qty_grid": [5_000_000.0]}},
}

# 6 Oct 2026, Lucas: judged at the expiry curve, every ETH run gave back more
# than $250k at some key spot (closest: -$700k at spot for +$3.2M at -45%).
# Allow up to $1M versus today's book at any key spot.
GIVEBACK_2026_10_06: dict[str, dict[str, Any]] = {
    "ETH": {"optimizer": {"max_giveback_usd": 1_000_000.0}},
}

SHEETS: dict[str, dict[str, dict[str, Any]]] = {
    "2026-10-05": SHEET_2026_10_05,
    "2026-10-06-sweep": SWEEP_2026_10_06,
    "2026-10-06-giveback": GIVEBACK_2026_10_06,
}


def apply_sheet(pol: AssetPolicy, patch: dict[str, Any]) -> AssetPolicy:
    """The stored policy with the sheet's fields written over it."""
    d = pol.to_dict()
    patch = dict(patch)
    opt = patch.pop("optimizer", None) or {}
    d.update(patch)
    d["optimizer"] = {**d["optimizer"], **opt}
    out = AssetPolicy.from_dict(d)
    if patch.get("perp_venue") and out.allowed_counterparties \
            and patch["perp_venue"] not in out.allowed_counterparties:
        out.allowed_counterparties.append(patch["perp_venue"])
    return out


async def apply_pending() -> list[str]:
    """Apply every sheet not yet applied. Returns what was applied."""
    done = []
    for day, sheet in SHEETS.items():
        flag = f"decision_sheet_{day}"
        if await store.get_flag(flag, "") == "applied":
            continue
        for asset, patch in sheet.items():
            pol = await store.get_policy(asset)
            await store.save_policy(apply_sheet(pol, patch), by=f"decision-sheet {day}")
        await store.set_flag(flag, "applied", "startup")
        done.append(day)
    return done
