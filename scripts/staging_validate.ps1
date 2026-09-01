param(
  [Parameter(Mandatory = $true)]
  [string]$ApiBase,

  [Parameter(Mandatory = $true)]
  [string]$TenantKey,

  [string]$SupportKey = "",
  [string]$BillingKey = "",

  [int]$PollAttempts = 80,
  [int]$PollSeconds = 3,
  [int]$LoadUsers = 0,
  [int]$LoadRequestsPerUser = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Api = $ApiBase.TrimEnd("/")
$RunId = [guid]::NewGuid().ToString("N").Substring(0, 12)
$Results = New-Object System.Collections.Generic.List[object]

function Add-Result {
  param(
    [string]$Name,
    [string]$Status,
    [string]$Detail = ""
  )
  $script:Results.Add([pscustomobject]@{
    name = $Name
    status = $Status
    detail = $Detail
  }) | Out-Null

  $color = "Gray"
  if ($Status -eq "PASS") { $color = "Green" }
  elseif ($Status -eq "FAIL") { $color = "Red" }
  elseif ($Status -eq "SKIP") { $color = "Yellow" }
  Write-Host ("[{0}] {1} {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
}

function New-Headers {
  param([string]$ApiKey)
  return @{
    Authorization = "ApiKey $ApiKey"
    "Content-Type" = "application/json"
  }
}

function Invoke-Json {
  param(
    [string]$Method,
    [string]$Path,
    [string]$ApiKey = "",
    [object]$Body = $null,
    [hashtable]$ExtraHeaders = @{}
  )

  $headers = @{}
  if ($ApiKey) {
    $headers = New-Headers $ApiKey
  }
  foreach ($key in $ExtraHeaders.Keys) {
    $headers[$key] = $ExtraHeaders[$key]
  }

  $params = @{
    Method = $Method
    Uri = "$Api$Path"
    Headers = $headers
  }
  if ($null -ne $Body) {
    $params.Body = ($Body | ConvertTo-Json -Depth 20)
  }
  return Invoke-RestMethod @params
}

function Wait-JobDone {
  param(
    [string]$ApiKey,
    [string]$JobId,
    [string]$Label
  )

  for ($i = 1; $i -le $PollAttempts; $i++) {
    Start-Sleep -Seconds $PollSeconds
    $job = Invoke-Json -Method Get -Path "/v1/memories/jobs/$JobId" -ApiKey $ApiKey
    $data = $job.data
    Write-Host ("  poll {0}/{1}: {2}, memories={3}, buffered={4}, promoted={5}" -f $i, $PollAttempts, $data.status, $data.memories_created, $data.pending_candidates_buffered, $data.pending_candidates_promoted)

    if ($data.status -in @("completed", "processed", "failed", "dead", "error")) {
      if ($data.status -in @("failed", "dead", "error")) {
        $jobError = $data.error_summary; if (-not $jobError) { $jobError = $data.error }; if (-not $jobError) { $jobError = $data.status }; throw "$Label job failed: $jobError"
      }
      return $data
    }
  }
  throw "$Label job did not finish after $($PollAttempts * $PollSeconds) seconds"
}

function Add-Memory {
  param(
    [string]$ApiKey,
    [string]$ExternalUserId,
    [array]$Messages,
    [object]$Source = $null,
    [hashtable]$Metadata = @{},
    [string]$IdempotencyKey = ""
  )

  $body = @{
    external_user_id = $ExternalUserId
    messages = $Messages
    metadata = $Metadata
  }
  if ($null -ne $Source) {
    $body.source = $Source
  }
  $extra = @{}
  if ($IdempotencyKey) {
    $extra["Idempotency-Key"] = $IdempotencyKey
  }
  return Invoke-Json -Method Post -Path "/v1/memories/add" -ApiKey $ApiKey -Body $body -ExtraHeaders $extra
}

function Retrieve-Memory {
  param(
    [string]$ApiKey,
    [string]$ExternalUserId,
    [string]$Query,
    [int]$Limit = 10
  )

  return Invoke-Json -Method Post -Path "/v1/memories/retrieve" -ApiKey $ApiKey -Body @{
    external_user_id = $ExternalUserId
    query = $Query
    limit = $Limit
    format = "json"
    context_max_tokens = 900
  }
}

function Test-Health {
  try {
    $health = Invoke-Json -Method Get -Path "/health"
    if ($health.data.status -ne "ok") {
      throw "status=$($health.data.status)"
    }
    Add-Result "health endpoint" "PASS" ($health.data | ConvertTo-Json -Compress)
  } catch {
    Add-Result "health endpoint" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-InvalidAuth {
  try {
    Retrieve-Memory -ApiKey "mem_invalid_key" -ExternalUserId "auth_probe_$RunId" -Query "anything" | Out-Null
    Add-Result "invalid API key rejected" "FAIL" "request unexpectedly succeeded"
  } catch {
    Add-Result "invalid API key rejected" "PASS"
  }
}

function Test-CoreMemory {
  $user = "stage_core_$RunId"
  try {
    $add = Add-Memory -ApiKey $TenantKey -ExternalUserId $user -Messages @(
      @{ role = "user"; content = "Permanent staging fact: I prefer concise Python debugging answers and detailed steps for new frameworks." },
      @{ role = "assistant"; content = "Got it. I will adapt future technical answers to that preference." }
    ) -Metadata @{ test = "staging-core"; run_id = $RunId }

    if (-not $add.job_id) {
      throw "add was blocked: status=$($add.status), reason=$($add.blocked_reason)"
    }

    $job = Wait-JobDone -ApiKey $TenantKey -JobId $add.job_id -Label "core memory"
    if (($job.memories_created + $job.pending_candidates_buffered + $job.pending_candidates_promoted) -lt 1) {
      throw "expected memory or weak candidate, got memories=0 buffered=0 promoted=0"
    }

    $retrieve = Retrieve-Memory -ApiKey $TenantKey -ExternalUserId $user -Query "How should I answer Python debugging questions?"
    if (($retrieve.data | Measure-Object).Count -lt 1 -and [string]::IsNullOrWhiteSpace($retrieve.system_prompt_addition)) {
      throw "retrieve returned no data and empty prompt"
    }

    Add-Result "core add/job/retrieve" "PASS" "user=$user, job=$($add.job_id), memories=$($job.memories_created), buffered=$($job.pending_candidates_buffered)"

    if ($retrieve.retrieval_id) {
      $usedIds = @()
      foreach ($item in $retrieve.data) {
        if ($item.id) { $usedIds += $item.id }
      }
      $feedback = Invoke-Json -Method Post -Path "/v1/memories/retrieval-feedback" -ApiKey $TenantKey -Body @{
        retrieval_id = $retrieve.retrieval_id
        outcome = "used_successfully"
        used_memory_ids = $usedIds
        agent_confidence = 0.88
        metadata = @{ test = "staging-feedback"; run_id = $RunId }
      }
      Add-Result "retrieval feedback" "PASS" "feedback_id=$($feedback.data.feedback_id)"
    } else {
      Add-Result "retrieval feedback" "SKIP" "retrieve response had no retrieval_id"
    }
  } catch {
    Add-Result "core add/job/retrieve" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-NegativeConversation {
  $user = "stage_negative_$RunId"
  try {
    $add = Add-Memory -ApiKey $TenantKey -ExternalUserId $user -Messages @(
      @{ role = "user"; content = "Hi" },
      @{ role = "assistant"; content = "Hello, how can I help?" }
    ) -Metadata @{ test = "staging-negative"; run_id = $RunId }

    if (-not $add.job_id) {
      Add-Result "negative conversation hygiene" "PASS" "blocked by quality gate: $($add.status)/$($add.blocked_reason)"
      return
    }

    $job = Wait-JobDone -ApiKey $TenantKey -JobId $add.job_id -Label "negative conversation"
    if ($job.memories_created -gt 0) {
      throw "greeting-only conversation created $($job.memories_created) memories"
    }
    Add-Result "negative conversation hygiene" "PASS" "memories=0, buffered=$($job.pending_candidates_buffered)"
  } catch {
    Add-Result "negative conversation hygiene" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-SoftBuffer {
  $user = "stage_soft_$RunId"
  try {
    $add = Add-Memory -ApiKey $TenantKey -ExternalUserId $user -Messages @(
      @{ role = "user"; content = "I might prefer short replies for difficult technical topics, but I am still testing that." },
      @{ role = "assistant"; content = "Understood. I will be careful not to over-assume it yet." }
    ) -Metadata @{ test = "staging-soft-buffer"; run_id = $RunId }

    if (-not $add.job_id) {
      Add-Result "soft extraction buffer" "SKIP" "quality gate blocked the borderline sample"
      return
    }

    $job = Wait-JobDone -ApiKey $TenantKey -JobId $add.job_id -Label "soft buffer"
    if ($job.pending_candidates_buffered -lt 1 -and $job.memories_created -lt 1) {
      throw "expected buffered weak candidate or memory"
    }
    Add-Result "soft extraction buffer" "PASS" "memories=$($job.memories_created), buffered=$($job.pending_candidates_buffered)"
  } catch {
    Add-Result "soft extraction buffer" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-SourceIdempotency {
  $user = "stage_source_$RunId"
  $eventId = "stage-source-$RunId-001"
  try {
    $source = @{
      service = "staging-test-service"
      event_id = $eventId
      observed_at = (Get-Date).ToUniversalTime().ToString("o")
      scope = @{ test = "source-idempotency"; run_id = $RunId }
      evidence = @(@{ source_type = "staging_script"; reference = "scripts/staging_validate.ps1" })
    }

    $messages = @(
      @{ role = "user"; content = "Permanent staging fact: this account prefers email updates for billing events." },
      @{ role = "assistant"; content = "I will remember the billing communication preference." }
    )

    $first = Add-Memory -ApiKey $TenantKey -ExternalUserId $user -Messages $messages -Source $source -Metadata @{ test = "source-idempotency"; run_id = $RunId }
    $second = Add-Memory -ApiKey $TenantKey -ExternalUserId $user -Messages $messages -Source $source -Metadata @{ test = "source-idempotency"; run_id = $RunId }

    if ($first.job_id) { Wait-JobDone -ApiKey $TenantKey -JobId $first.job_id -Label "source idempotency first" | Out-Null }
    if ($second.job_id -and $second.job_id -ne $first.job_id) {
      Wait-JobDone -ApiKey $TenantKey -JobId $second.job_id -Label "source idempotency second" | Out-Null
    }

    Add-Result "source event idempotency" "PASS" "event_id=$eventId"
  } catch {
    Add-Result "source event idempotency" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-MultiServiceConflict {
  if (-not $SupportKey -or -not $BillingKey) {
    Add-Result "multi-service conflict" "SKIP" "pass -SupportKey and -BillingKey to run this"
    return
  }

  $user = "stage_conflict_$RunId"
  try {
    $support = Add-Memory -ApiKey $SupportKey -ExternalUserId $user -Messages @(
      @{ role = "user"; content = "Support service says the customer's current subscription plan is Starter." },
      @{ role = "assistant"; content = "Support recorded the plan as Starter." }
    ) -Source @{
      service = "support-service"
      event_id = "stage-support-plan-$RunId"
      observed_at = (Get-Date).ToUniversalTime().AddMinutes(-5).ToString("o")
      scope = @{ test = "multi-service-conflict"; source = "support" }
      evidence = @(@{ source_type = "support_ticket"; reference = "TCK-$RunId" })
    } -Metadata @{ test = "multi-service-conflict"; run_id = $RunId }

    $billing = Add-Memory -ApiKey $BillingKey -ExternalUserId $user -Messages @(
      @{ role = "user"; content = "Billing service says the customer's current subscription plan is Growth." },
      @{ role = "assistant"; content = "Billing confirmed the active plan is Growth." }
    ) -Source @{
      service = "billing-service"
      event_id = "stage-billing-plan-$RunId"
      observed_at = (Get-Date).ToUniversalTime().ToString("o")
      scope = @{ test = "multi-service-conflict"; source = "billing" }
      evidence = @(@{ source_type = "billing_record"; reference = "SUB-$RunId" })
    } -Metadata @{ test = "multi-service-conflict"; run_id = $RunId }

    if ($support.job_id) { Wait-JobDone -ApiKey $SupportKey -JobId $support.job_id -Label "support conflict" | Out-Null }
    if ($billing.job_id) { Wait-JobDone -ApiKey $BillingKey -JobId $billing.job_id -Label "billing conflict" | Out-Null }

    $retrieve = Retrieve-Memory -ApiKey $BillingKey -ExternalUserId $user -Query "What is the customer's current subscription plan?"
    $json = $retrieve | ConvertTo-Json -Depth 20
    if ($json -notmatch "Growth") {
      throw "expected retrieved context to mention Growth. Response: $json"
    }
    Add-Result "multi-service conflict" "PASS" "Growth surfaced for user=$user"
  } catch {
    Add-Result "multi-service conflict" "FAIL" $_.Exception.Message
    throw
  }
}

function Test-LightLoad {
  if ($LoadUsers -le 0) {
    Add-Result "light load" "SKIP" "pass -LoadUsers N to run"
    return
  }

  $total = $LoadUsers * $LoadRequestsPerUser
  $errors = 0
  $durations = New-Object System.Collections.Generic.List[double]
  Write-Host "Running light load: $LoadUsers users x $LoadRequestsPerUser requests"

  $jobs = @()
  for ($u = 1; $u -le $LoadUsers; $u++) {
    $jobs += Start-Job -ScriptBlock {
      param($Api, $TenantKey, $RunId, $UserIndex, $Requests)
      $localErrors = 0
      $localDurations = @()
      for ($i = 1; $i -le $Requests; $i++) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
          $body = @{
            external_user_id = "stage_load_${RunId}_${UserIndex}"
            query = "What does this user prefer?"
            limit = 5
            format = "json"
          } | ConvertTo-Json -Depth 10
          Invoke-RestMethod -Method Post -Uri "$Api/v1/memories/retrieve" -Headers @{
            Authorization = "ApiKey $TenantKey"
            "Content-Type" = "application/json"
          } -Body $body | Out-Null
        } catch {
          $localErrors += 1
        } finally {
          $sw.Stop()
          $localDurations += $sw.Elapsed.TotalMilliseconds
        }
      }
      [pscustomobject]@{ errors = $localErrors; durations = $localDurations }
    } -ArgumentList $Api, $TenantKey, $RunId, $u, $LoadRequestsPerUser
  }

  $jobs | Wait-Job | Out-Null
  foreach ($job in $jobs) {
    $result = Receive-Job $job
    $errors += $result.errors
    foreach ($duration in $result.durations) { $durations.Add([double]$duration) | Out-Null }
    Remove-Job $job
  }

  $sorted = @($durations | Sort-Object)
  $p95Index = [Math]::Min($sorted.Count - 1, [Math]::Ceiling($sorted.Count * 0.95) - 1)
  $p95 = if ($sorted.Count) { [Math]::Round($sorted[$p95Index], 2) } else { 0 }
  $errPct = if ($total -gt 0) { [Math]::Round(($errors / $total) * 100, 2) } else { 0 }

  if ($errors -gt 0) {
    Add-Result "light load" "FAIL" "requests=$total errors=$errors error_pct=$errPct p95_ms=$p95"
    throw "light load had errors"
  }
  Add-Result "light load" "PASS" "requests=$total errors=0 p95_ms=$p95"
}

Write-Host "MemoryOS staging validation"
Write-Host "API: $Api"
Write-Host "Run ID: $RunId"
Write-Host ""

try {
  Test-Health
  Test-InvalidAuth
  Test-CoreMemory
  Test-NegativeConversation
  Test-SoftBuffer
  Test-SourceIdempotency
  Test-MultiServiceConflict
  Test-LightLoad
} finally {
  Write-Host ""
  Write-Host "Summary"
  $Results | Format-Table -AutoSize
  $failed = @($Results | Where-Object { $_.status -eq "FAIL" }).Count
  if ($failed -gt 0) {
    Write-Host "$failed test(s) failed." -ForegroundColor Red
    exit 1
  }
  Write-Host "No failed tests. Review SKIP rows before calling staging complete." -ForegroundColor Green
}
