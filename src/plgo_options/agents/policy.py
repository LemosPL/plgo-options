"""Monday's settings: the one place the agents read "what we decided".

Everything here is set by people (Lucas and Chris, Monday) and only read by the
agents. Defaults are the strategy document's EXAMPLE numbers so the system runs
in shadow mode from day one; ``is_example`` stays True until someone saves real
values, and every brief says so at the top.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OptimizerPreset:
    """The locked Optimizer v4 settings the agents run, plus the sweep grid.

    Mirrors Lucas's manual routine on the v4 page: pick the maturity and the
    counterparty, tune the parametric target, then try a few λ / κ / T+90 /
    max-trades combinations and keep the one that improves the profile with the
    fewest and smallest trades.
    """
    target_expiry: str = "25DEC26"
    counterparties: list[str] = field(default_factory=lambda: ["Flowdesk"])

    # Parametric V target (see misc_utils._scaled_linear_v_target_profile):
    # max loss at spot, breakevens at spot*(1-down) and spot*(1+up).
    # Chosen from the 28 Sep 2026 sweep: the only shape tested that improved
    # both wings without giving back value around +35%.
    target_trough_payoff: float = -17_500_000.0
    target_down_ratio: float = 0.85
    target_up_ratio: float = 0.75
    target_profile_file: str | None = None      # a saved CSV wins over the V

    # Sweep grid (Lucas: λ 0.3-0.5, κ 1-1.1, T+90 0.2 or 0.5, 5-8 trades).
    # 28 Sep 2026 sweep (ETH, 25DEC26, Flowdesk, V -17.5M/85%/75%): λ 0.3 barely
    # moved the book (+3.8% fit); λ 0.5 κ 1.1 T+90 0.5 with max 5 trades won
    # (+59% fit, $108k cost, 5 lines, better at every key spot). Allowing 7
    # trades added 4.5 points of fit for $45k more - not worth it.
    lam_grid: list[float] = field(default_factory=lambda: [0.4, 0.5])
    downside_grid: list[float] = field(default_factory=lambda: [1.0, 1.1])
    t90_grid: list[float] = field(default_factory=lambda: [0.2, 0.5])
    max_trades_grid: list[int] = field(default_factory=lambda: [5, 7])
    max_qty_grid: list[float] = field(default_factory=lambda: [5000.0])

    # Fixed engine settings (the v4 page's current values, 28 Sep 2026).
    mu_factor: float = 2.3
    cash_neutrality_factor: float = 0.05
    unwind_discount: float = 0.2
    new_position_penalty: float = 0.04
    collateral_budget_pct: float | None = 0.05
    roll_dte_threshold: int | None = 7
    atm_concentration: float = 0.0
    enable_box_neutralizer: bool = True
    enable_composite_unwind: bool = True
    enable_delta_rehedge: bool = False
    delta_band_usd: float = 150_000.0

    # Ranking: fit gain (%) minus penalties. Tuned so one extra option line has
    # to buy ~1.5 points of fit and $100k of dealing cost ~10 points.
    score_cost_per_100k: float = 10.0
    score_per_option_line: float = 1.5
    # A run may not give back more than this at any key spot vs today's book.
    max_giveback_usd: float = 250_000.0
    max_runs: int = 16


@dataclass
class AssetPolicy:
    asset: str
    view: str = "big_move"            # big_move | up | range   (A1)
    view_note: str = ""
    view_range_low: float | None = None
    view_range_high: float | None = None
    view_check_date: str | None = None
    reference_price: float | None = None   # Monday's reference (B1)
    reference_set_on: str | None = None
    reference_delta: float | None = None   # book delta at the reference = "target exposure"
    row_steps_pct: list[float] = field(default_factory=list)  # e.g. [10,20,30]
    stop_price: float | None = None
    stop_loss_usd: float | None = None     # further loss vs Monday MTM
    floor_price: float | None = None       # only ever goes up (A2)
    book_notional_usd: float = 0.0
    max_single_trade_usd: float = 0.0      # Lucas alone (A3)
    max_cost_per_trade_usd: float = 0.0
    max_cost_per_month_usd: float = 0.0
    max_perp_notional_usd: float = 0.0
    perp_funding_budget_month_usd: float = 0.0
    cost_tolerance_usd: float = 50_000.0   # B1a ±US$50k
    allowed_counterparties: list[str] = field(default_factory=list)
    optimizer: OptimizerPreset = field(default_factory=OptimizerPreset)
    is_example: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssetPolicy":
        d = dict(d or {})
        opt = OptimizerPreset(**{k: v for k, v in (d.pop("optimizer", None) or {}).items()
                                 if k in OptimizerPreset.__dataclass_fields__})
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(optimizer=opt, **known)


def default_policy(asset: str) -> AssetPolicy:
    """The strategy document's example numbers (A3, B1). Replace before live."""
    a = asset.upper()
    if a == "FIL":
        return AssetPolicy(
            asset="FIL", row_steps_pct=[15, 30, 45],
            stop_price=1.10, stop_loss_usd=3_000_000,
            book_notional_usd=40_000_000, max_single_trade_usd=2_000_000,
            max_cost_per_trade_usd=100_000, max_cost_per_month_usd=250_000,
            max_perp_notional_usd=10_000_000, perp_funding_budget_month_usd=40_000,
            allowed_counterparties=["FalconX", "Flowdesk", "G20", "Galaxy", "KeyRock",
                                    "Binance Futures"],
            optimizer=OptimizerPreset(
                target_trough_payoff=-15_750_000.0, target_down_ratio=1.0,
                target_up_ratio=1.75, counterparties=[]),
        )
    return AssetPolicy(
        asset="ETH", row_steps_pct=[10, 20, 30],
        stop_price=2_100, stop_loss_usd=5_000_000,
        book_notional_usd=60_000_000, max_single_trade_usd=3_000_000,
        max_cost_per_trade_usd=150_000, max_cost_per_month_usd=400_000,
        max_perp_notional_usd=15_000_000, perp_funding_budget_month_usd=60_000,
        allowed_counterparties=["Flowdesk", "KeyRock", "Binance Futures"],
    )
