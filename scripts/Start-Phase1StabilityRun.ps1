param(
    [double]$DurationHours = 3,
    [int]$StatusIntervalSeconds = 60,
    [int]$ProbeIntervalMinutes = 30,
    [int]$ProbeTimeoutMinutes = 15,
    [string]$Request = "Reply in one short Chinese sentence: Phase 1 stability probe successful.",
    [string]$Config = "",
    [string]$RunId = "",
    [switch]$SkipPipelineProbe
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Config) {
    $Config = Join-Path $projectRoot "config\nanobot.local.json"
}

$runtimeDir = Join-Path $projectRoot "runtime"
$stabilityRoot = Join-Path $runtimeDir "stability"
$statusScript = Join-Path $projectRoot "scripts\Get-Phase1Status.ps1"
$pipelineScript = Join-Path $projectRoot "scripts\Test-Phase1Pipeline.ps1"
$healthPath = Join-Path $runtimeDir "health.json"
$tasksDir = Join-Path $runtimeDir "tasks"

New-Item -ItemType Directory -Force -Path $stabilityRoot | Out-Null

if (-not $RunId) {
    $RunId = "stability-" + (Get-Date -Format "yyyyMMdd-HHmmss")
}

$runDir = Join-Path $stabilityRoot $RunId
$samplesDir = Join-Path $runDir "samples"
$probesDir = Join-Path $runDir "probes"
$statePath = Join-Path $runDir "state.json"
$statusCsv = Join-Path $runDir "status.csv"
$probesJsonl = Join-Path $runDir "probes.jsonl"
$summaryPath = Join-Path $runDir "summary.md"
$latestPointer = Join-Path $stabilityRoot "latest-run.txt"

New-Item -ItemType Directory -Force -Path $runDir | Out-Null
New-Item -ItemType Directory -Force -Path $samplesDir | Out-Null
New-Item -ItemType Directory -Force -Path $probesDir | Out-Null
Set-Content -Path $latestPointer -Value $runDir -Encoding UTF8

function Write-State {
    param([hashtable]$Payload)
    $Payload | ConvertTo-Json -Depth 8 | Set-Content -Path $statePath -Encoding UTF8
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Append-Line {
    param(
        [string]$Path,
        [string]$Line
    )

    Add-Content -Path $Path -Value $Line -Encoding UTF8
}

function Get-FirstMatchValue {
    param(
        [string]$Text,
        [string]$Pattern
    )

    $match = [regex]::Match($Text, $Pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($match.Success) {
        return $match.Groups[1].Value.Trim()
    }
    return ""
}

function Get-OptionalPropertyString {
    param(
        $InputObject,
        [string]$PropertyName
    )

    if ($null -eq $InputObject) {
        return ""
    }

    $property = $InputObject.PSObject.Properties[$PropertyName]
    if ($null -eq $property) {
        return ""
    }

    return [string]$property.Value
}

function Get-NestedPropertyValue {
    param(
        $InputObject,
        [string[]]$PropertyPath
    )

    $current = $InputObject
    foreach ($segment in $PropertyPath) {
        if ($null -eq $current) {
            return $null
        }

        $property = $current.PSObject.Properties[$segment]
        if ($null -eq $property) {
            return $null
        }
        $current = $property.Value
    }

    return $current
}

function Get-TaskStatusString {
    param(
        $InputObject,
        [string]$PropertyName
    )

    $value = Get-OptionalPropertyString -InputObject $InputObject -PropertyName $PropertyName
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
    }

    $receipt = Get-NestedPropertyValue -InputObject $InputObject -PropertyPath @("receipt")
    $value = Get-OptionalPropertyString -InputObject $receipt -PropertyName $PropertyName
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
    }

    if ($PropertyName -eq "failure_category") {
        $receiptError = Get-NestedPropertyValue -InputObject $InputObject -PropertyPath @("receipt", "error")
        $value = Get-OptionalPropertyString -InputObject $receiptError -PropertyName "failure_category"
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    } elseif ($PropertyName -eq "error") {
        $receiptError = Get-NestedPropertyValue -InputObject $InputObject -PropertyPath @("receipt", "error")
        $value = Get-OptionalPropertyString -InputObject $receiptError -PropertyName "message"
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }

    return ""
}

function Get-TaskDeliveryErrors {
    param($InputObject)

    $values = @()
    foreach ($propertyPath in @(@("delivery_errors"), @("receipt", "meta", "delivery_errors"))) {
        $candidate = Get-NestedPropertyValue -InputObject $InputObject -PropertyPath $propertyPath
        foreach ($item in @($candidate)) {
            $text = [string]$item
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $values += $text
            }
        }
        if ($values.Count -gt 0) {
            break
        }
    }
    return @($values)
}

function Get-ProbeOutcomeInfo {
    param($ProbeResult)

    if ($ProbeResult.error) {
        return [pscustomobject]@{
            outcome = "error"
            detail = "The probe launcher threw an exception."
        }
    }

    if (-not $ProbeResult.task_id) {
        return [pscustomobject]@{
            outcome = "other"
            detail = "The router did not return a task id."
        }
    }

    if (-not $ProbeResult.task_phase) {
        return [pscustomobject]@{
            outcome = "timeout"
            detail = "The task never reached a terminal phase before the probe timeout."
        }
    }

    if ($ProbeResult.task_phase -eq "failed") {
        return [pscustomobject]@{
            outcome = "failed"
            detail = "The task reached phase=failed."
        }
    }

    if ($ProbeResult.task_phase -ne "finished") {
        return [pscustomobject]@{
            outcome = "other"
            detail = "The task reached an unexpected terminal phase."
        }
    }

    if ($ProbeResult.failure_category -or $ProbeResult.reply_code -eq "completed_with_delivery_warning" -or $ProbeResult.delivery_error_count -gt 0 -or $ProbeResult.task_error) {
        return [pscustomobject]@{
            outcome = "warning"
            detail = "The task finished, but delivery/business warnings were recorded."
        }
    }

    if (-not $ProbeResult.receipt_present -or [string]::IsNullOrWhiteSpace([string]$ProbeResult.reply_code)) {
        return [pscustomobject]@{
            outcome = "incomplete"
            detail = "The task finished, but the final receipt fields were not persisted."
        }
    }

    if ($ProbeResult.reply_code -ne "completed") {
        return [pscustomobject]@{
            outcome = "other"
            detail = "The task finished with an unexpected reply code."
        }
    }

    return [pscustomobject]@{
        outcome = "finished"
        detail = "The task finished with a completed receipt."
    }
}

function Write-StatusRow {
    param(
        [string]$Timestamp,
        $Health
    )

    if (-not (Test-Path $statusCsv)) {
        Set-Content -Path $statusCsv -Value "timestamp,overall,gateway,worker,admin_relay,pending,last_inbound,last_task_finished" -Encoding UTF8
    }

    $pending = ""
    if ($Health -and $Health.queue -and $Health.queue.processing -ne $null) {
        $pending = [string]$Health.queue.pending
    }
    $row = @(
        $Timestamp,
        [string]($Health.overall),
        [string]($Health.gateway.state),
        [string]($Health.worker.state),
        [string]($Health.admin_relay.state),
        $pending,
        [string]($Health.last_inbound_at),
        [string]($Health.last_task_finished_at)
    ) -join ","
    Add-Content -Path $statusCsv -Value $row -Encoding UTF8
}

function Wait-ForTaskStatus {
    param(
        [string]$TaskId,
        [int]$TimeoutSeconds
    )

    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        return $null
    }

    $statusPath = Join-Path $tasksDir $TaskId
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $status = Read-JsonFile -Path (Join-Path $statusPath "status.json")
        if (-not $status) {
            $status = Read-JsonFile -Path (Join-Path $statusPath "status.json.bak")
        }
        if ($status) {
            $phase = [string]($status.phase)
            if ($phase -in @("finished", "failed")) {
                return $status
            }
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    return $null
}

function Invoke-HealthSample {
    param([int]$Index)

    & $statusScript -WriteRuntimeHealth *> $null
    $health = Read-JsonFile -Path $healthPath
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $samplePath = Join-Path $samplesDir ("health-" + $Index.ToString("000") + "-" + $stamp + ".json")
    if ($health) {
        $health | ConvertTo-Json -Depth 8 | Set-Content -Path $samplePath -Encoding UTF8
    } else {
        Set-Content -Path $samplePath -Value "{}" -Encoding UTF8
    }
    return $health
}

function Invoke-PipelineProbe {
    param([int]$Index)

    $probeStartedAt = Get-Date
    $requestText = "{0} [probe-{1} {2}]" -f $Request, $Index, $probeStartedAt.ToString("HH:mm:ss")
    $output = ""
    $taskStatus = $null
    $taskId = ""
    $action = ""
    $errorText = ""
    $deliveryErrors = @()
    $receiptPresent = $false
    $replyCode = ""
    $failureCategory = ""
    $userVisibleStatus = ""
    $taskPhase = ""
    $taskError = ""

    try {
        $output = (& $pipelineScript -Config $Config -Request $requestText -HealthProbe 2>&1 | Out-String)
        $taskId = Get-FirstMatchValue -Text $output -Pattern "TASK_ID=(.+)"
        $action = Get-FirstMatchValue -Text $output -Pattern "ACTION=(.+)"
        if ($taskId -and $action -eq "enqueued") {
            $taskStatus = Wait-ForTaskStatus -TaskId $taskId -TimeoutSeconds ($ProbeTimeoutMinutes * 60)
            $deliveryErrors = Get-TaskDeliveryErrors -InputObject $taskStatus
            $receiptPresent = $null -ne (Get-NestedPropertyValue -InputObject $taskStatus -PropertyPath @("receipt"))
            $replyCode = Get-TaskStatusString -InputObject $taskStatus -PropertyName "reply_code"
            $failureCategory = Get-TaskStatusString -InputObject $taskStatus -PropertyName "failure_category"
            $userVisibleStatus = Get-TaskStatusString -InputObject $taskStatus -PropertyName "user_visible_status"
            $taskPhase = Get-TaskStatusString -InputObject $taskStatus -PropertyName "phase"
            $taskError = Get-TaskStatusString -InputObject $taskStatus -PropertyName "error"
        }
    } catch {
        $errorText = $_.Exception.Message
    }

    $probeResult = [ordered]@{
        probe_index = $Index
        started_at = $probeStartedAt.ToString("s")
        finished_at = (Get-Date).ToString("s")
        request = $requestText
        action = $action
        task_id = $taskId
        output = $output.Trim()
        error = $errorText
        task_phase = $taskPhase
        reply_code = $replyCode
        failure_category = $failureCategory
        user_visible_status = $userVisibleStatus
        task_error = $taskError
        receipt_present = $receiptPresent
        delivery_errors = @($deliveryErrors)
        delivery_error_count = @($deliveryErrors).Count
    }
    $probeOutcome = Get-ProbeOutcomeInfo -ProbeResult ([pscustomobject]$probeResult)
    $probeResult["probe_outcome"] = $probeOutcome.outcome
    $probeResult["probe_detail"] = $probeOutcome.detail

    $probePath = Join-Path $probesDir ("probe-" + $Index.ToString("000") + ".json")
    $probeResult | ConvertTo-Json -Depth 8 | Set-Content -Path $probePath -Encoding UTF8
    Append-Line -Path $probesJsonl -Line ($probeResult | ConvertTo-Json -Depth 8 -Compress)

    return [pscustomobject]$probeResult
}

$startedAt = Get-Date
$endAt = $startedAt.AddHours($DurationHours)
$nextProbeAt = $startedAt
$sampleIndex = 0
$probeIndex = 0
$latestProbe = $null
$overallCounts = @{
    healthy = 0
    degraded = 0
    failed = 0
    unknown = 0
}
$probeCounts = @{
    finished = 0
    warning = 0
    incomplete = 0
    failed = 0
    timeout = 0
    error = 0
    other = 0
}

$state = [ordered]@{
    run_id = $RunId
    run_dir = $runDir
    pid = $PID
    started_at = $startedAt.ToString("s")
    ends_at = $endAt.ToString("s")
    duration_hours = $DurationHours
    status_interval_seconds = $StatusIntervalSeconds
    probe_interval_minutes = $ProbeIntervalMinutes
    probe_timeout_minutes = $ProbeTimeoutMinutes
    skip_pipeline_probe = [bool]$SkipPipelineProbe
    status = "running"
    samples = 0
    probes = 0
    last_sample_at = ""
    last_probe_at = ""
    latest_overall = ""
    latest_probe = $null
}
Write-State -Payload $state

do {
    $sampleIndex += 1
    $health = Invoke-HealthSample -Index $sampleIndex
    $sampleTs = (Get-Date).ToString("s")
    Write-StatusRow -Timestamp $sampleTs -Health $health

    $overall = if ($health) { [string]$health.overall } else { "unknown" }
    if (-not $overallCounts.Contains($overall)) {
        $overallCounts[$overall] = 0
    }
    $overallCounts[$overall] += 1

    $state.samples = $sampleIndex
    $state.last_sample_at = $sampleTs
    $state.latest_overall = $overall
    Write-State -Payload $state

    if (-not $SkipPipelineProbe -and (Get-Date) -ge $nextProbeAt) {
        $probeIndex += 1
        $latestProbe = Invoke-PipelineProbe -Index $probeIndex
        $state.probes = $probeIndex
        $state.last_probe_at = (Get-Date).ToString("s")
        $state.latest_probe = $latestProbe

        switch ([string]$latestProbe.probe_outcome) {
            "finished" { $probeCounts.finished += 1 }
            "warning" { $probeCounts.warning += 1 }
            "incomplete" { $probeCounts.incomplete += 1 }
            "failed" { $probeCounts.failed += 1 }
            "timeout" { $probeCounts.timeout += 1 }
            "error" { $probeCounts.error += 1 }
            default { $probeCounts.other += 1 }
        }

        $nextProbeAt = (Get-Date).AddMinutes($ProbeIntervalMinutes)
    }

    Write-State -Payload $state

    if ((Get-Date) -lt $endAt) {
        Start-Sleep -Seconds $StatusIntervalSeconds
    }
} while ((Get-Date) -lt $endAt)

$state.status = "completed"
$state.completed_at = (Get-Date).ToString("s")
$state.overall_counts = $overallCounts
$state.probe_counts = $probeCounts
Write-State -Payload $state

$summaryLines = @(
    "# Phase 1 Stability Run",
    "",
    "- Run ID: $RunId",
    "- Started: $($state.started_at)",
    "- Completed: $($state.completed_at)",
    "- Duration Hours: $DurationHours",
    "- Samples: $sampleIndex",
    "- Probes: $probeIndex",
    "- Latest Overall: $($state.latest_overall)",
    "",
    "## Overall Counts",
    "",
    "- healthy: $($overallCounts.healthy)",
    "- degraded: $($overallCounts.degraded)",
    "- failed: $($overallCounts.failed)",
    "- unknown: $($overallCounts.unknown)",
    "",
    "## Probe Counts",
    "",
    "- finished: $($probeCounts.finished)",
    "- warning: $($probeCounts.warning)",
    "- incomplete: $($probeCounts.incomplete)",
    "- failed: $($probeCounts.failed)",
    "- timeout: $($probeCounts.timeout)",
    "- error: $($probeCounts.error)",
    "- other: $($probeCounts.other)"
)

if ($latestProbe) {
    $summaryLines += @(
        "",
        "## Latest Probe",
        "",
        "- Probe Index: $($latestProbe.probe_index)",
        "- Task ID: $($latestProbe.task_id)",
        "- Action: $($latestProbe.action)",
        "- Task Phase: $($latestProbe.task_phase)",
        "- Reply Code: $($latestProbe.reply_code)",
        "- Failure Category: $($latestProbe.failure_category)",
        "- Delivery Errors: $($latestProbe.delivery_error_count)",
        "- Probe Outcome: $($latestProbe.probe_outcome)",
        "- Probe Detail: $($latestProbe.probe_detail)",
        "- Error: $($latestProbe.error)"
    )
}

$summaryLines | Set-Content -Path $summaryPath -Encoding UTF8

Write-Output ("RUN_DIR=" + $runDir)
Write-Output ("STATE_PATH=" + $statePath)
Write-Output ("SUMMARY_PATH=" + $summaryPath)
