# Desk agents

Eight agents run the no-judgement part of *Options Portfolio — Strategy and Execution* (Part B) inside this app. They read the book, compute, write briefs and **propose** trades. People trade.

## How it works

```
Cloud Scheduler (Europe/London)
   │  POST /api/agents/run/<agent>   (X-Signals-Token)
   ▼
agents/desk.py ── reads ──► the app's own functions (portfolio_pnl, spot, perps, collateral, Optimizer v4 engine)
   │           ── reads ──► Book State (agent_policy: view, reference, rows, stop, limits, v4 preset)
   │
   ├─ numbers computed in code
   ├─ every proposal ──► agents/gate.py (mandate gate) ──► route: LUCAS | CHRIS | REJECTED (+ the rule)
   ├─ Claude (ANTHROPIC_BRIEF_MODEL) rewrites the facts into a desk note — may not add numbers
   └─ stored in agent_runs / agent_proposals, posted to Slack (SLACK_WEBHOOK_URL), shown on the Agents page
```

Nothing in `agents/` places an order, writes a trade or edits a position.

| Agent | UK time | Does |
|---|---|---|
| row-watcher | every 5 min | Spot vs Monday's B1 rows. Fires a row once per week, builds the B1a trade, runs the gate. Wakes people only for the B2b cases (±30% ETH / ±45% FIL, stop, margin shortfall). Silent in quiet hours otherwise. |
| morning-open | 09:00 | Spot, MTM, Greeks, distance to the next rows, rows fired, stop distance, perp and funding, open proposals, resting orders, collateral. |
| optimizer | 09:30, 16:00 | Optimizer v4 sweep (below). Top 3 through the gate. Compares with the earlier run the same day (B2a); a disagreement freezes trading for that asset. |
| handover | 15:30 | The six handover lines. |
| night-desk | 23:00 | Mark, today's dealing cost, tomorrow's brief, proposed night orders for Chris. |
| close-check | 02:00 | Resting orders live, no margin issue. Silent unless something is wrong. |
| monday-pack | Mon 07:30 | Last week's fires and proposals, decay, realised vs implied vol, view scorecard, draft rows. Ends with the three decisions only people make. |
| monthly-review | 1st, 08:00 | Dealing cost and time decay month on month (A3's test). |

## The Optimizer agent (Optimizer v4, automated)

It follows the manual routine on the v4 page: load the book, set the parametric target, pick maturity and counterparty, try λ / κ / T+90, then squeeze max trades and max qty for fewer, smaller trades. It uses the same engine (`OptimizerUseCase`) with the same parameters the page posts (`tests/test_agents_v4_parity.py` enforces this). The book is fetched once and each run goes to a worker thread.

Score = fit gain % − 10 × (cost ÷ $100k) − 1.5 × option lines. Disqualified: not converged, or gives back more than $250k versus today's book at −45%, −20%, spot, +35% or +85%.

**Target profiles and λ (6 Oct 2026).** Each sweep tests three target profiles, picked by hand before 09:00 in Agents > Policy > "Optimizer · target profiles for the sweep" (stored as `optimizer.target_grid`; the 09:00 morning brief lists them and flags any missing file). On Auto it uses the agreed parametric V, then the saved CSVs in name order. Each target is ranked on its own, because a fit gain against one target says nothing about another; its best run becomes a proposal labelled with the target. Each target is searched one axis at a time (κ 1.1, T+90 0.5): coarse λ 0.1, 0.5, 1, 1.5, 2, 2.5, 3, 3.5 at max 5 trades and the largest max qty; then every 0.1 between the best coarse λ and its better neighbour; then max trades 5, 7, 9, 11, 13, 15 at the best λ; then max qty 1k–5k ETH / 1M–5M FIL (steps of 1k / 1M) at the best λ and trade count (decision sheet `2026-10-06-size`). That is ~22 runs per target and ~20–25 min per asset, so ETH runs at 09:30/16:00, FIL at 10:00/16:30, and Cloud Run needs a 3600s request timeout. The two-curve check compares same-target runs only.

Defaults come from the 28 Sep 2026 sweep (ETH, 25DEC26, Flowdesk):

| Target V (max loss / down / up) | Best settings | Fit gain | Cost | Option lines | Change vs book at −45% / +85% |
|---|---|---|---|---|---|
| −$17.5M / 85% / 75% | λ 0.5, κ 1.1, T+90 0.5, max 5 trades, max qty 5,000 | +59.0% | $107.7k | 5 | +$2.7M / +$5.5M |
| same | same, max 7 trades | +63.5% | $153.5k | 7 | +$3.9M / +$5.0M |
| same | λ 0.3 (any κ, T+90) | +3.8% | $21k | 3–5 | +$1.2M / −$1.0M |
| −$20M / 91% / 91% (old default) | λ 0.4, κ 1, T+90 0.2 | +39.9% | $82.3k | 4 | +$2.1M / +$1.6M |

Change everything in the `optimizer` block of the policy (Agents page). Run a sweep on demand with `POST /api/agents/optimizer/sweep`.

## The mandate gate

`agents/gate.py`, plain code, no model. Checks: kill switch, frozen curves, instrument (A4), row once a week (B1), roll type (A5: same-expiry strike moves and shortening rejected, roll-outs to Chris), floor (A2), cost and monthly budget (A3), size and split above Lucas's limit (A3), perp caps (A3), stop (A3), view (A1), counterparty universe (A3). Try any trade with `POST /api/agents/gate`.

## Setup

1. Deploy as usual. Tables are created on startup.
2. Secrets: `SIGNALS_TOKEN` (already used by the brief), `SLACK_WEBHOOK_URL`, `ANTHROPIC_API_KEY`.
3. Cloud Run request timeout ≥ 900s (the sweep takes ~4–5 min): `gcloud run services update plgo-options --timeout=900`.
4. `SERVICE_URL=... SIGNALS_TOKEN=... PROJECT=... ./deploy/agents_scheduler.sh`
5. Monday: on the Agents page set the view, reference prices, stop and real limits for ETH and FIL, then save (this clears the "example limits" warning).

Pause everything with the **Kill switch** on the Agents page.

## Tests

`PYTHONPATH=src python -m pytest tests/` covers rows, every gate rule, the ranking, v4 parameter parity, and the manual's example week end to end.
