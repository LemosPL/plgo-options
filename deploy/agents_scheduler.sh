#!/usr/bin/env bash
# Create (or update) the Cloud Scheduler jobs that run the desk agents on the
# B2 timetable. Times are Europe/London, as the manual says: "anchor everything
# to UK time and let the ET column shift".
#
# Usage:
#   SERVICE_URL=https://plgo-options-180233067711.us-central1.run.app \
#   SIGNALS_TOKEN=... PROJECT=... REGION=us-central1 ./deploy/agents_scheduler.sh
#
# Cloud Run: give the service a request timeout of at least 900s so the
# optimizer sweep (16 runs of ~15s) completes:
#   gcloud run services update plgo-options --timeout=900 --region=$REGION
set -euo pipefail

: "${SERVICE_URL:?set SERVICE_URL}"
: "${SIGNALS_TOKEN:?set SIGNALS_TOKEN}"
: "${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
TZ_UK="Europe/London"

job() {  # name  cron  agent  [deadline]  [body]
  local name="$1" cron="$2" agent="$3" deadline="${4:-180s}" body="${5:-{\"deliver\":true}}"
  local args=(--project="$PROJECT" --location="$REGION" --schedule="$cron" --time-zone="$TZ_UK"
              --uri="$SERVICE_URL/api/agents/run/$agent" --http-method=POST
              --headers="Content-Type=application/json,X-Signals-Token=$SIGNALS_TOKEN"
              --message-body="$body" --attempt-deadline="$deadline")
  if gcloud scheduler jobs describe "$name" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$name" "${args[@]}"
  else
    gcloud scheduler jobs create http "$name" "${args[@]}"
  fi
}

job agents-row-watcher     "*/5 * * * *"  row-watcher
job agents-morning-open    "0 9 * * 1-5"  morning-open
job agents-optimizer-am    "30 9 * * 1-5" optimizer 1800s '{"deliver":true,"ctx":{"label":"am","assets":["ETH"]}}'
job agents-handover        "30 15 * * 1-5" handover
job agents-optimizer-pm    "0 16 * * 1-5" optimizer 1800s '{"deliver":true,"ctx":{"label":"pm","assets":["ETH"]}}'
job agents-night-desk      "0 23 * * 1-5" night-desk
job agents-close-check     "0 2 * * 2-6"  close-check
job agents-monday-pack     "30 7 * * 1"   monday-pack
job agents-monthly-review  "0 8 1 * *"    monthly-review

echo "Scheduler jobs in place. Pause everything with the Kill switch on the Agents page,"
echo "or: gcloud scheduler jobs pause agents-row-watcher --location=$REGION"
