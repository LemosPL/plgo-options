<#
.SYNOPSIS
  One-shot setup for the Action Radar brief + proximity alerts on Cloud Run.

.DESCRIPTION
  Idempotent — safe to re-run. Does five things:
    1. Stores the Slack webhook and the signals token in Secret Manager.
    2. Grants the Cloud Run runtime service account access to them.
    3. Wires them onto the service (and, if you pass -AnthropicKey, moves the
       Anthropic key out of the plaintext env var into Secret Manager too).
    4. Creates the three Cloud Scheduler jobs, in Europe/London.
    5. Smoke-tests the token guard and fires the first IV snapshot.

  Deploy the new image FIRST — this script only wires config, it does not ship
  code. See -h output at the bottom for the deploy command.

.EXAMPLE
  ./scripts/setup_action_radar.ps1 `
      -WebhookUrl 'https://hooks.slack.com/services/T000/B000/xxxx' `
      -SignalsToken 'F0AooSxY58PpJJFK4069w84XyJkp159Oc2RPXr5Bqtk'
#>
param(
    [Parameter(Mandatory = $true)][string]$WebhookUrl,
    [Parameter(Mandatory = $true)][string]$SignalsToken,

    # Optional: pass a freshly-rotated Anthropic key to move it into Secret
    # Manager and delete the plaintext env var in the same revision.
    [string]$AnthropicKey = '',

    [string]$Service = 'plgo-options',
    [string]$Region  = 'us-central1',
    [string]$Project = 'fildeploymentws',
    [string]$Assets  = '["ETH","FIL"]',

    [switch]$SkipScheduler,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }

# Create the secret if absent, then always add a new version. Adding a version
# is what makes re-running safe: rotating a value is the same command.
function Set-Secret([string]$Name, [string]$Value) {
    $exists = $true
    try { gcloud secrets describe $Name --project $Project 2>$null | Out-Null }
    catch { $exists = $false }

    $tmp = New-TemporaryFile
    try {
        # -NoNewline matters: a trailing newline becomes part of the secret and
        # a webhook URL with "\n" on the end fails with a confusing 404.
        [System.IO.File]::WriteAllText($tmp, $Value)
        if (-not $exists) {
            gcloud secrets create $Name --data-file="$tmp" --replication-policy=automatic --project $Project | Out-Null
            Ok "created secret $Name"
        } else {
            gcloud secrets versions add $Name --data-file="$tmp" --project $Project | Out-Null
            Ok "added new version to $Name"
        }
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

function Ensure-SchedulerJob([string]$Name, [string]$Schedule, [string]$Uri,
                             [string]$Body, [string]$Deadline) {
    $headers = "Content-Type=application/json,X-Signals-Token=$SignalsToken"
    $exists = $true
    try { gcloud scheduler jobs describe $Name --location $Region --project $Project 2>$null | Out-Null }
    catch { $exists = $false }

    $verb = if ($exists) { 'update' } else { 'create' }
    gcloud scheduler jobs $verb http $Name `
        --location $Region --project $Project `
        --schedule "$Schedule" --time-zone "Europe/London" `
        --uri "$Uri" --http-method POST `
        --headers "$headers" `
        --message-body "$Body" `
        --attempt-deadline $Deadline | Out-Null
    Ok "$verb`d $Name  ($Schedule Europe/London)"
}

# ---------------------------------------------------------------------------

Step "Resolving service"
$Url = (gcloud run services describe $Service --region $Region --project $Project `
            --format='value(status.url)').Trim()
if (-not $Url) { throw "Could not resolve URL for $Service in $Region" }
$RuntimeSA = (gcloud run services describe $Service --region $Region --project $Project `
            --format='value(spec.template.spec.serviceAccountName)').Trim()
if (-not $RuntimeSA) {
    $num = (gcloud projects describe $Project --format='value(projectNumber)').Trim()
    $RuntimeSA = "$num-compute@developer.gserviceaccount.com"
}
Ok "service $Service -> $Url"
Ok "runtime SA  $RuntimeSA"

Step "Storing secrets"
Set-Secret 'plgo-slack-webhook' $WebhookUrl
Set-Secret 'plgo-signals-token' $SignalsToken
$secretMap = 'SLACK_WEBHOOK_URL=plgo-slack-webhook:latest,SIGNALS_TOKEN=plgo-signals-token:latest'
$secretNames = @('plgo-slack-webhook', 'plgo-signals-token')

if ($AnthropicKey) {
    Set-Secret 'plgo-anthropic-key' $AnthropicKey
    $secretMap += ',ANTHROPIC_API_KEY=plgo-anthropic-key:latest'
    $secretNames += 'plgo-anthropic-key'
}

Step "Granting the runtime SA read access"
foreach ($s in $secretNames) {
    gcloud secrets add-iam-policy-binding $s `
        --member "serviceAccount:$RuntimeSA" `
        --role roles/secretmanager.secretAccessor `
        --project $Project 2>$null | Out-Null
    Ok "secretAccessor on $s"
}

Step "Wiring secrets onto $Service (creates a new revision)"
$updateArgs = @(
    'run', 'services', 'update', $Service,
    '--region', $Region, '--project', $Project,
    '--set-secrets', $secretMap
)
if ($AnthropicKey) {
    # The plaintext env var would otherwise shadow the secret-backed one.
    $updateArgs += @('--remove-env-vars', 'ANTHROPIC_API_KEY')
}
gcloud @updateArgs | Out-Null
Ok "revision updated"
if ($AnthropicKey) { Ok "ANTHROPIC_API_KEY moved to Secret Manager, plaintext env var removed" }

if (-not $SkipScheduler) {
    Step "Creating Cloud Scheduler jobs"
    # Europe/London, NOT UTC: a UTC cron silently drifts an hour across BST/GMT.
    Ensure-SchedulerJob 'plgo-radar-brief' '0 9 * * *' `
        "$Url/api/signals/brief" `
        "{`"assets`":$Assets,`"use_ai`":true,`"deliver`":true}" '300s'

    Ensure-SchedulerJob 'plgo-radar-proximity' '*/5 * * * *' `
        "$Url/api/signals/proximity-check" `
        "{`"assets`":$Assets,`"deliver`":true}" '120s'

    Ensure-SchedulerJob 'plgo-radar-iv-snapshot' '30 23 * * *' `
        "$Url/api/signals/snapshot-iv" `
        "{`"assets`":$Assets}" '120s'
} else {
    Warn "scheduler jobs skipped (-SkipScheduler)"
}

if (-not $SkipSmokeTest) {
    Step "Smoke test"

    # 1. The guard must actually reject an unauthenticated call. The service is
    #    public (allUsers invoker), so this token is the only thing standing
    #    between a stranger and the desk's Slack channel.
    $guarded = $false
    try {
        Invoke-RestMethod -Method Post -Uri "$Url/api/signals/snapshot-iv" `
            -ContentType 'application/json' -Body '{"assets":["ETH"]}' `
            -TimeoutSec 90 | Out-Null
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -eq 401) { $guarded = $true }
    }
    if ($guarded) { Ok "unauthenticated call correctly rejected with 401" }
    else { Warn "endpoint did NOT reject an unauthenticated call - is the new image deployed?" }

    # 2. Same call with the token: proves the wiring AND starts the IV history,
    #    which needs ~20 daily observations before percentiles mean anything.
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Url/api/signals/snapshot-iv" `
            -Headers @{ 'X-Signals-Token' = $SignalsToken } `
            -ContentType 'application/json' -Body "{`"assets`":$Assets}" -TimeoutSec 120
        Ok "authenticated snapshot-iv succeeded: $($r.results | ConvertTo-Json -Compress)"
    } catch {
        Warn "authenticated snapshot-iv failed: $($_.Exception.Message)"
    }

    # 3. Build a brief WITHOUT delivering, so nothing hits Slack until you have
    #    read one yourself.
    try {
        $b = Invoke-RestMethod -Method Post -Uri "$Url/api/signals/brief" `
            -ContentType 'application/json' `
            -Body "{`"assets`":$Assets,`"use_ai`":false,`"deliver`":false}" -TimeoutSec 180
        Ok "brief built (slack_configured=$($b.slack_configured))"
        Write-Host "`n--- brief preview ---`n" -ForegroundColor DarkGray
        Write-Host $b.text
    } catch {
        Warn "brief preview failed: $($_.Exception.Message)"
    }
}

Write-Host @"

Done. Remaining manual steps:

  1. If you have not already, DEPLOY THE NEW IMAGE - this script only wires
     config, it ships no code:

       gcloud run deploy $Service --source . --region $Region --project $Project

  2. Read one brief in the UI (Deals / Risk -> Action Radar -> Preview brief)
     before letting the 09:00 job deliver. To pause delivery meanwhile:

       gcloud scheduler jobs pause plgo-radar-brief --location $Region
       gcloud scheduler jobs pause plgo-radar-proximity --location $Region

  3. Fire a job by hand to test end-to-end:

       gcloud scheduler jobs run plgo-radar-brief --location $Region

"@ -ForegroundColor Gray
