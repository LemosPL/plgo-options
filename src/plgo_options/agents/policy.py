"""Monday's settings: the one place the agents read "what we decided".

Everything here is set by people (Lucas and Chris, Monday) and only read by the
agents. Defaults are the strategy document's EXAMPLE numbers so the system runs
in shadow mode from day one; ``is_example`` stays True until someone saves real
values, and every brief says so at the top.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


TRADES_GRID = (5, 7, 9, 11, 13, 15)
ETH_QTY_GRID = (1_000.0, 2_000.0, 3_000.0, 4_000.0, 5_000.0)
FIL_QTY_GRID = (1_000_000.0, 2_000_000.0, 3_000_000.0, 4_000_000.0, 5_000_000.0)


@dataclass
class OptimizerPreset:
    """The locked Optimizer v4 settings the agents run, plus the sweep grid.

    Mirrors Lucas's manual routine on the v4 page: pick the maturity and the
    counterparty, tune the parametric target, then try a few λ / κ / T+90 /
    max-trades combinations and keep the one that improves the profile with the
    fewest and smallest trades.
    """
    target_expiry: str = "25DEC26"
    # Move the target out once it gets close (decision sheet 5 Oct 2026: "all
    # DEC26, move once 1-month out to MAR27"). None = never switch.
    next_expiry: str | None = None
    switch_days_before: int = 30
    counterparties: list[str] = field(default_factory=lambda: ["Flowdesk"])

    # Parametric V target (see misc_utils._scaled_linear_v_target_profile):
    # max loss at spot, breakevens at spot*(1-down) and spot*(1+up).
    # Chosen from the 28 Sep 2026 sweep: the only shape tested that improved
    # both wings without giving back value around +35%.
    target_trough_payoff: float = -17_500_000.0
    target_down_ratio: float = 0.85
    target_up_ratio: float = 0.75
    target_profile_file: str | None = None      # a saved CSV wins over the V

    # Target profiles the sweep tests, each ranked on its own (a fit gain
    # against one target says nothing about another); the best run for each
    # becomes a proposal. "parametric" is the V above; anything else is a saved
    # target-profile CSV, as in v4's Target dropdown. Empty = the V first, then
    # every saved profile for the asset, up to max_targets.
    target_grid: list[str] = field(default_factory=list)
    max_targets: int = 3

    # Sweep grid (Lucas: λ 0.3-0.5, κ 1-1.1, T+90 0.2 or 0.5, 5-8 trades).
    # 28 Sep 2026 sweep (ETH, 25DEC26, Flowdesk, V -17.5M/85%/75%): λ 0.3 barely
    # moved the book (+3.8% fit); λ 0.5 κ 1.1 T+90 0.5 with max 5 trades won
    # (+59% fit, $108k cost, 5 lines, better at every key spot). Allowing 7
    # trades added 4.5 points of fit for $45k more - not worth it.
    # 6 Oct 2026 (Lucas): λ is searched in two passes per target. Coarse: every
    # lam_grid value at the first max_trades. Fine: every lam_refine_step between
    # the best coarse λ and its better neighbour, at every max_trades - e.g. best
    # between 2.5 and 3 -> 2.5, 2.6 ... 3.0. κ and T+90 held at the 28 Sep winner.
    lam_grid: list[float] = field(default_factory=lambda: [0.1, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    lam_refine_step: float | None = 0.1          # None = coarse grid only
    downside_grid: list[float] = field(default_factory=lambda: [1.1])
    t90_grid: list[float] = field(default_factory=lambda: [0.5])
    # 6 Oct 2026 (Lucas): a proposal can be 5-15 trades and ETH 1k-5k / FIL
    # 1M-5M per line. Searched one axis at a time after λ (see sweep()).
    max_trades_grid: list[int] = field(default_factory=lambda: list(TRADES_GRID))
    max_qty_grid: list[float] = field(default_factory=lambda: list(ETH_QTY_GRID))

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

    # Ranking: target tracking gain (%) minus penalties. Tuned so one extra
    # option line has to buy ~1.5 points and $100k of dealing cost ~10 points.
    score_cost_per_100k: float = 10.0
    score_per_option_line: float = 1.5
    # A run may not give back more than this at any key spot vs today's book.
    max_giveback_usd: float = 250_000.0
    # 7 Oct 2026 (Lucas): the result must follow the target. A run is ranked on
    # how much closer its target-expiry curve gets to the target's shape, and is
    # out if at any key spot it ends up further from the target than today's
    # book by more than max(track_tolerance_usd, track_tolerance_pct x today's
    # distance there).
    track_tolerance_usd: float = 250_000.0
    track_tolerance_pct: float = 0.10
    # How far down each target's ranking the desk goes for a run the validation
    # and the gate accept, before it reports the target as empty.
    gate_retries: int = 4
    max_runs: int = 40              # per target, both passes

    def targets(self, asset: str) -> list[dict[str, Any]]:
        """[{"name", "file"}] in test order; file None = the parametric V."""
        names = list(self.target_grid)
        if not names:
            if self.target_profile_file:
                names = [self.target_profile_file]
            else:
                from plgo_options.optimization.misc_utils import list_target_profiles
                try:
                    saved = [p["file"] for p in list_target_profiles(asset.upper())]
                except Exception:
                    saved = []
                names = ["parametric"] + saved
        out = []
        for n in names[: max(1, self.max_targets)]:
            if n == "parametric":
                out.append({"name": f"V {self.target_trough_payoff / 1e6:+.1f}M "
                                    f"{self.target_down_ratio:.0%}/{self.target_up_ratio:.0%}",
                            "file": None})
            else:
                out.append({"name": n.removesuffix(".csv"), "file": n})
        return out

    def effective_expiry(self, today: date | None = None) -> str:
        """The maturity the sweep targets today: next_expiry once target_expiry
        is within switch_days_before days."""
        if not self.next_expiry:
            return self.target_expiry
        try:
            exp = datetime.strptime(self.target_expiry, "%d%b%y").date()
        except ValueError:
            return self.target_expiry
        if (exp - (today or date.today())).days <= self.switch_days_before:
            return self.next_expiry
        return self.target_expiry


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
    # Where the "up" view says the price goes (A1). Under view "up" no proposal
    # may make the book worse at +35%, +85% or at this price.
    upside_target_price: float | None = None
    book_notional_usd: float = 0.0
    max_single_trade_usd: float = 0.0      # Lucas alone (A3)
    max_cost_per_trade_usd: float = 0.0
    max_cost_per_month_usd: float = 0.0
    max_perp_notional_usd: float = 0.0
    # "500k - full directional exposure": the perp cap and Lucas's single perp
    # trade grow to the options book's delta ($) when that is larger.
    perp_cap_full_delta: bool = False
    perp_funding_budget_month_usd: float | None = 0.0   # None = no budget set
    perp_venue: str = "Binance Futures"    # where row perps trade
    rolls_need_chris: bool = True          # A5; False = Lucas rolls inside his limits
    cost_tolerance_usd: float = 50_000.0   # B1a ±US$50k
    allowed_counterparties: list[str] = field(default_factory=list)
    optimizer: OptimizerPreset = field(default_factory=OptimizerPreset)
    is_example: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def perp_limits(self, book_delta_usd: float | None = None) -> tuple[float, float]:
        """(biggest perp trade Lucas does alone, biggest perp position)."""
        single, cap = self.max_single_trade_usd, self.max_perp_notional_usd
        if self.perp_cap_full_delta and book_delta_usd:
            full = abs(book_delta_usd)
            single, cap = max(single, full), max(cap, full)
        return single, cap

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
                target_up_ratio=1.75, counterparties=[], max_qty_grid=list(FIL_QTY_GRID)),
        )
    return AssetPolicy(
        asset="ETH", row_steps_pct=[10, 20, 30],
        stop_price=2_100, stop_loss_usd=5_000_000,
        book_notional_usd=60_000_000, max_single_trade_usd=3_000_000,
        max_cost_per_trade_usd=150_000, max_cost_per_month_usd=400_000,
        max_perp_notional_usd=15_000_000, perp_funding_budget_month_usd=60_000,
        allowed_counterparties=["Flowdesk", "KeyRock", "Binance Futures"],
    )
