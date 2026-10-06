#!/usr/bin/env bash
# Create (or update) the Cloud Scheduler jobs that run the desk agents on the
# B2 timetable. Times are Europe/London, as the manual says: "anchor everything
# to UK time and let the ET column shift".
#
# Usage:
#   SERVICE_URL=https://plgo-options-180233067711.us-central1.run.app \
#   SIGNALS_TOKEN=... PROJECT=... REGION=us-central1 ./deploy/agents_scheduler.sh
#
# Deadlines: the once-a-day agents pay a container cold start plus slow
# external feeds, and 09:00 morning-open measured 222s against the old 180s
# default - Scheduler recorded DEADLINE_EXCEEDED on a request the app had
# actually completed (HTTP 200), and would have retried it. 600s covers it.
#
# Cloud Run: give the service a request timeout of 3600s so the optimizer
# sweep (two-pass λ search over up to 4 target profiles, ~20 min) completes:
#   gcloud run services update plgo-options --timeout=3600 --region=$REGION
set -euo pipefail

: "${SERVICE_URL:?set SERVICE_URL}"
: "${SIGNALS_TOKEN:?set SIGNALS_TOKEN}"
: "${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
TZ_UK="Europe/London"

job() {  # name  cron  agent  [deadline]  [body]
  local default_body='{"deliver":true}'
  local name="$1" cron="$2" agent="$3" deadline="${4:-600s}" body="${5:-$default_body}"
  local hdrs="Content-Type=application/json,X-Signals-Token=$SIGNALS_TOKEN"
  local args=(--project="$PROJECT" --location="$REGION" --schedule="$cron" --time-zone="$TZ_UK"
              --uri="$SERVICE_URL/api/agents/run/$agent" --http-method=POST
              --message-body="$body" --attempt-deadline="$deadline")
  # `update http` takes --update-headers; only `create http` accepts --headers.
  if gcloud scheduler jobs describe "$name" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$name" "${args[@]}" --update-headers="$hdrs"
  else
    gcloud scheduler jobs create http "$name" "${args[@]}" --headers="$hdrs"
  fi
}

job agents-row-watcher     "*/5 * * * *"  row-watcher   180s
job agents-morning-open    "0 9 * * 1-5"  morning-open
# One optimizer job per asset, 30 minutes apart: each sweep is up to 4 target
# profiles x (8 coarse + ~10 fine λ runs), ~20 min on one instance, so the two
# must not share the CPU. Needs the Cloud Run request timeout at 3600s.
job agents-optimizer-am     "30 9 * * 1-5"  optimizer 1800s '{"deliver":true,"ctx":{"label":"am","assets":["ETH"]}}'
job agents-optimizer-am-fil "0 10 * * 1-5"  optimizer 1800s '{"deliver":true,"ctx":{"label":"am","assets":["FIL"]}}'
job agents-handover         "30 15 * * 1-5" handover
job agents-optimizer-pm     "0 16 * * 1-5"  optimizer 1800s '{"deliver":true,"ctx":{"label":"pm","assets":["ETH"]}}'
job agents-optimizer-pm-fil "30 16 * * 1-5" optimizer 1800s '{"deliver":true,"ctx":{"label":"pm","assets":["FIL"]}}'
job agents-night-desk      "0 23 * * 1-5" night-desk
job agents-close-check     "0 2 * * 2-6"  close-check
job agents-monday-pack     "30 7 * * 1"   monday-pack
job agents-monthly-review  "0 8 1 * *"    monthly-review

echo "Scheduler jobs in place. Pause everything with the Kill switch on the Agents page,"
echo "or: gcloud scheduler jobs pause agents-row-watcher --location=$REGION"
