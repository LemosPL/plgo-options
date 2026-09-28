"""The mandate gate: sections A2-A5 and B1-B3 of the manual, as plain code.

Every proposal - from Optimizer v4, a fired row, or a person - goes through
``evaluate``. It returns a route and the reasons:

    lucas     inside Lucas's mandate (perp/forward, inside size and cost)
    chris     changes the shape of the book, or is above a limit: handover
    rejected  breaks a rule nobody may break (never reaches the market)

No LLM is involved. A rejection always names the rule.

Proposal shape (dict):
    asset, source ("optimizer" | "row" | "manual"), purpose ("direction" | "shape")
    legs: [{kind: "option"|"perp", side: "Buy"|"Sell", opt: "C"|"P", strike,
            expiry (YYYY-MM-DD), qty, counterparty, is_unwind, strategy}]
    net_cost_usd   (+ = we pay, - = we receive)
    notional_usd   (gross, for the size limit)
    reason         (written reason; required for a debit - A5 test 2)
    row_key        (for row proposals)
    spot, before_payoff, after_payoff, spot_ladder (optional; for the floor test)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plgo_options.agents.policy import AssetPolicy

LUCAS, CHRIS, REJECTED = "lucas", "chris", "rejected"


@dataclass
class GateResult:
    route: str
    reasons: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    lucas_notional_usd: float = 0.0     # when a trade is split (A3 example)
    chris_notional_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route, "reasons": self.reasons, "checks": self.checks,
                "lucas_notional_usd": self.lucas_notional_usd,
                "chris_notional_usd": self.chris_notional_usd}


@dataclass
class GateContext:
    policy: AssetPolicy
    spot: float
    kill_switch: bool = False
    curves_frozen: bool = False
    fired_this_week: set[str] = field(default_factory=set)
    month_cost_usd: float = 0.0
    perp_notional_usd: float = 0.0       # current signed perp notional
    perp_funding_month_usd: float = 0.0
    monday_mtm_usd: float | None = None
    current_mtm_usd: float | None = None


def _is_box(leg: dict) -> bool:
    return "BOX" in str(leg.get("strategy") or "").upper()


def classify_rolls(legs: list[dict]) -> list[dict]:
    """Pair unwinds with new legs of the same counterparty and option type (A5).

    Returns one entry per pair with type: out_same_strike | out_and_up |
    out_and_down | same_expiry_strike_move | in | diagonal.
    """
    opts = [l for l in legs if l.get("kind", "option") == "option" and not _is_box(l)]
    unwinds = [l for l in opts if l.get("is_unwind")]
    news = [l for l in opts if not l.get("is_unwind")]
    pairs = []
    for u in unwinds:
        cands = [n for n in news if n.get("counterparty") == u.get("counterparty")
                 and str(n.get("opt")) == str(u.get("opt"))]
        if not cands:
            continue
        n = min(cands, key=lambda c: abs(float(c["strike"]) - float(u["strike"])))
        ue, ne = str(u.get("expiry"))[:10], str(n.get("expiry"))[:10]
        uk, nk = float(u["strike"]), float(n["strike"])
        if ne == ue:
            t = "same_expiry_strike_move" if nk != uk else "none"
        elif ne < ue:
            t = "in"
        elif nk == uk:
            t = "out_same_strike"
        elif nk > uk:
            t = "out_and_up"
        else:
            t = "out_and_down"
        pairs.append({"type": t, "from": f"{u.get('opt')} {uk:g} {ue}",
                      "to": f"{n.get('opt')} {nk:g} {ne}", "counterparty": u.get("counterparty")})
    return pairs


def _floor_ok(p: dict, policy: AssetPolicy, tol: float) -> tuple[bool, str]:
    """A2 / A5 test 1: after the trade, the book is no worse below the floor."""
    ladder, before, after = p.get("spot_ladder"), p.get("before_payoff"), p.get("after_payoff")
    if not (ladder and before and after):
        return True, "no payoff curves supplied; floor not testable"
    floor = policy.floor_price or (p.get("spot") or 0) * (1 - (policy.row_steps_pct or [30])[-1] / 100)
    worst = 0.0
    for s, b, a in zip(ladder, before, after):
        if s <= floor:
            worst = min(worst, float(a) - float(b))
    if worst < -tol:
        return False, f"lowers the book by ${-worst:,.0f} below the floor {floor:,.2f}"
    return True, f"floor {floor:,.2f} held (worst change below it ${worst:,.0f})"


def evaluate(p: dict, ctx: GateContext) -> GateResult:
    pol = ctx.policy
    res = GateResult(route=LUCAS)
    to_chris: list[str] = []
    reject: list[str] = []

    def check(name: str, ok: bool, note: str, rule: str):
        res.checks.append({"check": name, "ok": ok, "note": note, "rule": rule})

    legs = p.get("legs") or []
    option_legs = [l for l in legs if l.get("kind", "option") == "option" and not _is_box(l)]
    perp_legs = [l for l in legs if l.get("kind") == "perp"]
    notional = abs(float(p.get("notional_usd") or 0))
    net_cost = float(p.get("net_cost_usd") or 0)

    # 0. Kill switch and frozen curves stop everything.
    if ctx.kill_switch:
        reject.append("Kill switch is on.")
    check("kill switch", not ctx.kill_switch, "on" if ctx.kill_switch else "off", "B2")
    if ctx.curves_frozen:
        reject.append("Two-curve check disagreed; nobody trades until the inputs are checked (B2a).")
    check("curves", not ctx.curves_frozen, "frozen" if ctx.curves_frozen else "agree", "B2a")

    # 1. Instrument (A4): direction through perps only; options change shape -> Chris.
    if p.get("purpose") == "direction" and option_legs:
        reject.append("Never trade an option to change direction; use a perp or forward (A4).")
        check("instrument", False, "option legs on a direction trade", "A4")
    elif option_legs:
        to_chris.append("Option trade changes the shape of the book (B1, B2b).")
        check("instrument", True, f"{len(option_legs)} option legs -> Chris", "A4")
    else:
        check("instrument", True, "perp/forward only", "A4")

    # 2. Row state (B1): one fire per row per week.
    rk = p.get("row_key")
    if rk:
        fired = rk in ctx.fired_this_week
        if fired:
            reject.append(f"Row {rk} already fired this week (B1).")
        check("row state", not fired, f"row {rk}", "B1")
        if rk.lstrip("+-") == str(int((pol.row_steps_pct or [0, 0, 0])[-1])):
            to_chris.append(f"Row {rk} is a stop-and-call row (B1).")

    # 3. Roll type (A5).
    for pair in classify_rolls(legs):
        t = pair["type"]
        desc = f"{pair['from']} -> {pair['to']} ({pair['counterparty']})"
        if t in ("same_expiry_strike_move",):
            reject.append(f"Roll moves the strike at the same expiry: {desc}. Never roll down or chase up (A5).")
            check("roll", False, desc, "A5")
        elif t == "in":
            reject.append(f"Roll shortens expiry: {desc} (A5).")
            check("roll", False, desc, "A5")
        elif t in ("out_same_strike", "out_and_up", "out_and_down", "diagonal"):
            to_chris.append(f"Roll {t.replace('_', ' ')}: {desc}. Nobody rolls alone (A5).")
            check("roll", True, desc + " -> Chris", "A5")

    # 4. Floor (A2, A5 test 1).
    ok, note = _floor_ok(p, pol, pol.cost_tolerance_usd)
    if not ok:
        reject.append(f"Floor test failed: {note} (A2).")
    check("floor", ok, note, "A2 / A5-1")

    # 5. Cost (A3, A5 test 2). Box legs count: they are real fills.
    if net_cost > pol.cost_tolerance_usd:
        if net_cost > pol.max_cost_per_trade_usd:
            to_chris.append(f"Net debit ${net_cost:,.0f} is over the per-trade limit ${pol.max_cost_per_trade_usd:,.0f} (A3).")
        if not (p.get("reason") or "").strip():
            to_chris.append("A debit needs a written reason (A2, A5 test 2).")
        if ctx.month_cost_usd + net_cost > pol.max_cost_per_month_usd:
            to_chris.append(
                f"Month-to-date ${ctx.month_cost_usd:,.0f} + ${net_cost:,.0f} exceeds the monthly budget "
                f"${pol.max_cost_per_month_usd:,.0f}; waits for the 1st or goes to Chris (A3).")
    check("cost", net_cost <= pol.max_cost_per_trade_usd,
          f"net {'debit' if net_cost > 0 else 'credit'} ${abs(net_cost):,.0f}", "A3 / A5-2")

    # 6. Size (A3): above Lucas's limit, he does the limit, Chris the rest.
    if perp_legs and not option_legs and notional > pol.max_single_trade_usd:
        res.lucas_notional_usd = pol.max_single_trade_usd
        res.chris_notional_usd = notional - pol.max_single_trade_usd
        to_chris.append(f"Size ${notional:,.0f} over Lucas's ${pol.max_single_trade_usd:,.0f}: "
                        f"Lucas does ${res.lucas_notional_usd:,.0f}, Chris decides the rest (A3).")
    elif option_legs and notional > pol.max_single_trade_usd:
        to_chris.append(f"Option notional ${notional:,.0f} over the single-trade limit (A3).")
    check("size", notional <= pol.max_single_trade_usd, f"${notional:,.0f}", "A3")

    # 7. Perp caps (A3).
    if perp_legs:
        signed = sum((1 if l.get("side") == "Buy" else -1) * abs(float(l.get("qty") or 0)) * ctx.spot
                     for l in perp_legs)
        after = abs(ctx.perp_notional_usd + signed)
        if after > pol.max_perp_notional_usd:
            to_chris.append(f"Perp position would be ${after:,.0f}, over the ${pol.max_perp_notional_usd:,.0f} cap (A3).")
        if ctx.perp_funding_month_usd > pol.perp_funding_budget_month_usd:
            to_chris.append("Perp funding this month is over budget (A3).")
        check("perp caps", after <= pol.max_perp_notional_usd, f"after ${after:,.0f}", "A3")

    # 8. Price (B3): the whole package, our model, two quotes. Recorded, not blocking here.
    check("price", True, "needs our model price + 2 package quotes before execution", "B3 / A5-3")

    # 9. Stop (A3): at the stop, close only.
    at_stop = False
    if pol.stop_price and ctx.spot and ctx.spot <= pol.stop_price:
        at_stop = True
    if (pol.stop_loss_usd and ctx.monday_mtm_usd is not None and ctx.current_mtm_usd is not None
            and ctx.monday_mtm_usd - ctx.current_mtm_usd >= pol.stop_loss_usd):
        at_stop = True
    if at_stop:
        news = [l for l in option_legs if not l.get("is_unwind")]
        if news:
            reject.append("At the stop we close; we do not open or roll (A3, A5).")
        to_chris.append("Book is at the stop: de-risk decision is Chris's (A3).")
    check("stop", not at_stop, f"spot {ctx.spot:,.4g} vs stop {pol.stop_price}", "A3")

    # 10. View (B2b): anything contradicting Monday's view goes on the handover.
    if pol.view == "range":
        adds_long = [l for l in option_legs if not l.get("is_unwind") and l.get("side") == "Buy"
                     and abs(float(l["strike"]) - ctx.spot) / ctx.spot > 0.3]
        if adds_long:
            to_chris.append("Buys far-out strikes while the view is range-bound (A1).")
    check("view", True, f"view={pol.view}", "A1 / B2b")

    # 11. Universe (A3): known counterparties and products only.
    allowed = {c.lower() for c in (pol.allowed_counterparties or [])}
    unknown = sorted({str(l.get("counterparty")) for l in legs
                      if allowed and str(l.get("counterparty") or "").lower() not in allowed
                      and l.get("kind", "option") == "option"})
    if unknown:
        reject.append(f"Unknown counterparty: {', '.join(unknown)} (A3).")
    check("universe", not unknown, "ok" if not unknown else ", ".join(unknown), "A3")

    if reject:
        res.route, res.reasons = REJECTED, reject + to_chris
    elif to_chris:
        res.route, res.reasons = CHRIS, to_chris
    else:
        res.route, res.reasons = LUCAS, ["Inside Lucas's mandate."]
    return res
