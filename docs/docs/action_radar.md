# Action Radar — trigger map, 09:00 brief, proximity alerts

Tells you *when* to act on a deal and *exactly what* the paired trade is, based
on price movement. Lives under **Deals / Risk**.

## Why it's shaped this way

Rather than waiting for spot to move and then computing advice, the engine scans
a spot ladder now and finds the levels at which each deal's recommended action
switches on. That gives a **conditional playbook** — when price moves intraday
the decision is already made — and turns into alerts for free (spot crosses a
precomputed level → fire).

Two hard rules from the desk are baked in:

* **A close is never suggested alone.** Every recommendation is a paired
  close+open package. Protection is replaced, never dropped.
* **Everything passes an economic gate.** Priced after bid/ask using a
  delta-scaled half-spread, so the tool can't recommend churn that loses money
  on the spread. This is the single most important guard: without it these tools
  degenerate into churn generators.

Rules compute, Claude narrates. Every price, cash figure, trigger level and
threshold comes from the engine; the model ranks the actions, picks the one
worth doing today, and writes the market read. **If the model is unavailable the
brief still goes out** with deterministic ordering.

## The four actions

| Kind | Fires when | Package |
|---|---|---|
| `recycle` | Short leg decayed to ≤10% of premium collected AND \|delta\| ≤ 0.05 | Buy it back for pennies, sell a fresh one at 0.20Δ |
| `tighten` | Both strikes of a vertical ≥15% ITM — payoff pinned, width is dead weight | Pull the far leg in, halving width |
| `increase` | Deal working: unrealised ≥40% of premium collected | Add 25% of qty at strikes re-centred on the trigger spot |
| `reduce` | Exit cost ≥150% of premium collected | Buy back 30% of the worst short, re-strike at 0.10Δ |

Rolls are deliberately **not** here — Trade Management and the optimizer own those.

Every threshold is tunable from the **Thresholds** panel and persists in
localStorage. Defaults are strict on purpose; loosen them and use
**Show rejected** to see near-misses with the reason each was refused.

### Reading the numbers

* **Net cash** — after bid/ask. `+` is money in.
* **Margin freed** — change in Σ\|negative MTM\|, matching the desk's collateral
  liability definition. Positive = frees collateral.
* **Δ max loss** — worst-case payoff change, *including* the trade's cash.
* **`structural_risk_cut`** — the same thing with the cash backed out, so it
  isolates the risk change (for a width-reducing trade it equals exactly
  qty × width removed). The gate ratios use this; `d_max_loss` would charge for
  the cost twice.

Note that a `recycle` almost always **adds** tail risk — selling a 0.20Δ option
instead of a 0.02Δ one is the business model. The gate checks that it pays;
whether the added tail risk is acceptable is a judgement call, which is why both
figures are shown side by side.

## Cadence

* **09:00 Europe/London** — the daily brief.
* **~every 5 minutes** — the proximity poller.
* **Once daily** — the ATM IV snapshot (see below).

### Proximity bands and latching

Each signal is a state machine that only ever escalates:

```
FAR ──10%──▶ APPROACHING ──5%──▶ IMMINENT ──cross──▶ LIVE
```

A band fires at most once. Re-arming requires spot to retreat past the band edge
**plus 1.5pp of hysteresis** (a Schmitt trigger), and `ALERT_COOLDOWN_HOURS`
applies on top. Without the hysteresis a price sitting on a band edge would
re-fire all day. Retreats are silent.

Latching keys on `deal|kind|subject` — *not* the engine's signal id, which
embeds the trigger direction and changes when a signal goes from "approaching
from below" to live. Keying on the id would treat an escalation as a brand-new
signal and double-alert.

One Slack message per poll, listing all escalations. Three separate pings read
as noise; one message listing three reads as information.

During quiet hours (`QUIET_HOURS`, default `22:00-07:00` local) the 10%
heads-ups are held and folded into the morning brief; 5% and crossings always
go out. Set `QUIET_HOURS=off` for 24/7.

The poller **re-scans** rather than reading a cached ladder: a scan is
sub-second, so a cache would only add staleness. Levels therefore drift slightly
intraday as theta bleeds, which is arguably more correct than a fixed morning
snapshot.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(empty)* | Incoming webhook. Empty = brief still builds, just isn't delivered. |
| `SIGNALS_TOKEN` | *(empty)* | Shared secret for the delivery endpoints. Empty = no check. |
| `ANTHROPIC_API_KEY` | *(empty)* | Empty = deterministic brief, no analyst read. |
| `ANTHROPIC_BRIEF_MODEL` | `claude-opus-5` | Separate from `ANTHROPIC_MODEL` so tuning the brief can't disturb the optimizer chat's cached prefixes. |
| `BRIEF_TIMEZONE` | `Europe/London` | Timestamp on the brief. |
| `QUIET_HOURS` | `22:00-07:00` | Defers 10% pings. `off` disables. |
| `ALERT_COOLDOWN_HOURS` | `6` | Minimum gap before the same (signal, band) can alert again. |

**Set `SIGNALS_TOKEN` in prod.** A Cloud Run URL is public by default, and
without it anyone who finds the URL can make the desk's Slack channel say
whatever a scan happens to return.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/signals/deals` | The scan. `include_rejected: true` shows gate failures. |
| `GET` | `/api/signals/config` | Default thresholds. |
| `POST` | `/api/signals/brief` | `deliver: false` = preview (no token needed); `true` = post to Slack (token required). |
| `POST` | `/api/signals/proximity-check` | One poll. Token required when `deliver: true`. |
| `POST` | `/api/signals/snapshot-iv` | Daily ATM IV snapshot. Idempotent. Token required. |
| `GET` | `/api/signals/iv-history` | Read back the vol log. |
| `GET` | `/api/signals/market-trend` | What the MARKET section is built from. |
| `GET`/`DELETE` | `/api/signals/alert-state` | Inspect / hand-re-arm latches. Only ever touches `signal_alert_state`. |
| `GET` | `/api/signals/journal` | Delivered signals + outcome totals. |
| `POST` | `/api/signals/journal/{id}/outcome` | `executed` \| `dismissed` \| `expired` \| `open`. |

## Cloud Scheduler setup

`--time-zone` must be `Europe/London`, **not** UTC — a UTC cron silently shifts
an hour across the BST/GMT boundary.

```bash
PROJECT=fildeploymentws
REGION=us-central1
URL=$(gcloud run services describe plgo-options --region $REGION \
        --format='value(status.url)')
TOKEN='<the SIGNALS_TOKEN value>'

# 1. Daily brief — 09:00 London
gcloud scheduler jobs create http plgo-radar-brief \
  --location $REGION --schedule "0 9 * * *" --time-zone "Europe/London" \
  --uri "$URL/api/signals/brief" --http-method POST \
  --headers "Content-Type=application/json,X-Signals-Token=$TOKEN" \
  --message-body '{"assets":["ETH","FIL"],"use_ai":true,"deliver":true}' \
  --attempt-deadline 300s

# 2. Proximity poller — every 5 minutes
gcloud scheduler jobs create http plgo-radar-proximity \
  --location $REGION --schedule "*/5 * * * *" --time-zone "Europe/London" \
  --uri "$URL/api/signals/proximity-check" --http-method POST \
  --headers "Content-Type=application/json,X-Signals-Token=$TOKEN" \
  --message-body '{"assets":["ETH","FIL"],"deliver":true}' \
  --attempt-deadline 120s

# 3. IV snapshot — 23:30 London, after the day has settled
gcloud scheduler jobs create http plgo-radar-iv-snapshot \
  --location $REGION --schedule "30 23 * * *" --time-zone "Europe/London" \
  --uri "$URL/api/signals/snapshot-iv" --http-method POST \
  --headers "Content-Type=application/json,X-Signals-Token=$TOKEN" \
  --message-body '{"assets":["ETH","FIL"]}' \
  --attempt-deadline 120s
```

Store the webhook and token in Secret Manager and expose them to the service:

```bash
printf '%s' 'https://hooks.slack.com/services/...' | \
  gcloud secrets create plgo-slack-webhook --data-file=-
printf '%s' "$TOKEN" | gcloud secrets create plgo-signals-token --data-file=-

gcloud run services update plgo-options --region $REGION \
  --set-secrets 'SLACK_WEBHOOK_URL=plgo-slack-webhook:latest,SIGNALS_TOKEN=plgo-signals-token:latest'
```

## Start the IV snapshot early

`portfolio_mtm_history` has carried a daily spot + greeks series for a while, so
price trend and **realised** vol work immediately. Nothing ever recorded
**implied** vol, so `iv_surface_history` starts empty: the brief shows the IV
level from day one but reports `N obs — no percentile yet` until it has ≥20
observations. Deploy job 3 as early as possible — the log is nearly free and it
is what later unlocks regime awareness ("front vol is rich, hold the recycle")
and backtesting.

## The journal

Every delivered signal is written to `signal_journal` with the market state at
delivery. Marking one **executed** stamps the spot, which becomes that deal's
last-action anchor — this is what stops already-acted-on advice from repeating.

Two payoffs: it's the evidence for whether the thresholds are any good, and it's
the seed of the backtesting capability. Review it with the **Journal** button.

## Diagnostics

Both run without a live market feed or a populated DB — they stub the deal
payload with synthetic structures, so they work anywhere.

```bash
.venv/Scripts/python.exe scripts/diag_signals.py   # engine math, gates, triggers
.venv/Scripts/python.exe scripts/diag_brief.py     # brief render, latch, re-arm
```

`diag_signals.py` asserts the structural identity that catches the
"cost counted twice" class of bug: structural risk change must equal exactly
qty × strikes moved. `diag_brief.py` walks one signal through
approaching → imminent → live and asserts that a wobble across a band edge fires
nothing. It writes only to `signal_journal` / `signal_alert_state` under the
throwaway asset `TSTETH` and deletes those rows afterwards; the trades table is
never touched.
