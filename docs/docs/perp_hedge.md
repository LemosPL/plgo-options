# Perp Hedge — the delta hedge, and what it costs to carry

Records the perp leg the optimizer proposes, so the book's delta, P&L and next
optimizer run all reflect the position actually being run. Lives under
**Perp Hedge**.

## The gap this closes

The optimizer has always ended a run with a delta-rehedge step
(`optimization/delta_hedger.py`): if net option delta, offset by the perp
position, sits outside its band, propose one perp trade on Binance Futures to
flatten it. `check_rehedge` reads that existing perp position off positions with
`opt == "F"` — and no such position could exist. The `trades` table holds OTC
option legs reconciled against counterparty spreadsheets, and every reader
normalizes `option_type` to Call/Put (`database.py` rewrites it on startup).

So three things were true at once:

* **The rehedger was amnesiac.** `existing_perp_qty` was always 0, so every run
  proposed flattening the *entire* option delta from scratch, however much of
  it was already hedged. On the FIL book that meant re-proposing ~14m tokens
  instead of the few thousand actually needed.
* **Portfolio greeks were the unhedged book.** `portfolio_mtm_history` carried
  an options-only delta series.
* **Funding was invisible.** A $12m perp leg at ~6% annualised funding is
  ~$700k/yr of P&L that appeared in no total anywhere.

## Model

`perp_trades` is a **ledger of fills**, never a net position. The net is
Σ signed qty, and the funding accrual has to know what was held at each past
funding timestamp — a question only a ledger can answer.

Average entry and realized P&L use signed weighted-average cost: adding moves
the average, reducing realizes against it, and flipping through zero closes the
old side out and re-opens the new one at the fill price.

Perps are kept **out of** `trades` rather than squeezed into it. That leaves
reconciliation, deal grouping and the OTC counterparty views untouched — a
perp's exchange margin is not OTC collateral, and a "Binance Futures" line on
the Collateral page would be wrong.

## Where the leg surfaces

| Consumer | Sees the hedge? | How |
|---|---|---|
| Perp Hedge page | yes | `/api/perps/*` |
| Portfolio P&L | yes, as `perp` + `totals.net_delta` | **not** appended to `positions` — see below |
| Daily MTM snapshot | yes | `perp_qty`, `perp_mtm_usd`, `perp_funding_usd`, `net_delta` |
| Optimizer / rehedger | yes | injected as one `opt == "F"` position in `routes/optimization.py` |
| Collateral, Deals, Reconciliation | **no** | by design |

`portfolio_pnl` deliberately keeps the leg out of its `positions` list: that
list feeds the Collateral page's per-counterparty liability and the Deals
grouping, neither of which should grow a venue line. The optimizer is the one
consumer that must see it, so it gets it injected explicitly.

`totals.portfolio_delta` stays **options-only** so the stored series keeps
meaning what it always meant; `totals.net_delta` is that plus the hedge.

### The injected position

Three fields carry real meaning downstream (`_build_perp_position`):

* **`strike` = average entry.** OptimizerV3 values an `"F"` leg as
  `qty * (spot - strike)`, so entry is exactly the basis that makes the hedge's
  payoff curve correct.
* **`expiry` = 2099-12-31.** A perp never expires, and `_get_roll_positions`
  sweeps in anything under the roll threshold — a zero DTE would put the hedge
  up for rolling.
* **`counterparty` = `Binance Futures`**, matching `base_optimizer.PERP_COUNTERPARTY`.

It is deliberately **not** keyed to match the perp *candidate* in
`get_held_positions()`. The position's strike is average entry while the
candidate is built at spot, so they would only ever match by accident — and
forcing a match would let `unwind_discount` cheapen a reducing perp trade, which
is wrong (closing a perp costs the same bps as opening one). The LP can still
trade the perp freely in either direction; `check_rehedge` reads the held
quantity straight off the position list.

## Funding

Longs pay shorts when the rate is positive, so the payment **to the holder** is
`-qty × mark × rate`: short into positive funding is money in. This is the
opposite sign to the rate itself, which is where it gets mis-booked, so it is
computed in exactly one place (`_payment_usd`).

Each settled event is stored with the position the ledger says was held **at
that moment**, not today's — so the row is a permanent record of what was
actually paid, not a figure that silently revises itself when the position
changes.

The funding interval is **inferred** from the spacing of recent events, not
hardcoded: Binance has moved some symbols off the 8h default to 4h, and a stale
constant would misstate every annualized figure by a factor of two.

### Invalidation

Adding a back-dated fill, or deleting one, changes what was held from that
moment on — making every accrued row at or after it wrong. Because the accrual
walks forward from the newest stored row, those rows would never be revisited.
So both mutations clear `perp_funding` from the affected timestamp onward
(`invalidate_funding_from`) and re-accrue; rows strictly before it are untouched,
since the position over that period genuinely did not change.

Accrual is idempotent — the `perp_funding` primary key absorbs a re-run over an
overlapping window — so it is safe to schedule on any cadence.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/perps/position` | Net qty, avg entry, live mark, P&L, funding. |
| `GET` | `/api/perps/trades` | The ledger, oldest first. |
| `POST` | `/api/perps/trades` | Record a fill. Re-accrues automatically. |
| `DELETE` | `/api/perps/trades/{id}` | Remove a fill. Re-accrues automatically. |
| `GET` | `/api/perps/funding` | Settled payments + totals. |
| `POST` | `/api/perps/accrue-funding` | Book everything settled since the last run. Token required. |

Only the accrual is token-guarded (`X-Signals-Token`, the same secret the Action
Radar scheduler endpoints use) — it writes, and it runs on a public Cloud Run
URL.

## Cloud Scheduler

Funding settles every 8h on Binance's default; running every 4h leaves room for
a missed invocation without a gap, since the overlap is absorbed.

```bash
PROJECT=fildeploymentws
REGION=us-central1
URL=$(gcloud run services describe plgo-options --region $REGION \
        --format='value(status.url)')
TOKEN='<the SIGNALS_TOKEN value>'

gcloud scheduler jobs create http plgo-perp-funding \
  --location $REGION --schedule "15 */4 * * *" --time-zone "Europe/London" \
  --uri "$URL/api/perps/accrue-funding" --http-method POST \
  --headers "Content-Type=application/json,X-Signals-Token=$TOKEN" \
  --message-body '{"assets":["ETH","FIL"]}' \
  --attempt-deadline 120s
```

Accrual is not required for the position, mark or net delta to be right — only
for the carry. A missed window is picked up by the next run.

## Recording a fill

The **traded at** timestamp matters: funding is accrued against the position
held at each past settlement, so back-dating a fill re-prices the carry
correctly rather than approximating it. A naive timestamp is read as UTC.
