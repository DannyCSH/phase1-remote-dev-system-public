param(
    [string]$RunDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stabilityRoot = Join-Path $projectRoot "runtime\stability"

if (-not $RunDir) {
    $latest = Get-ChildItem -Path $stabilityRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "No stability run found under $stabilityRoot"
    }
    $RunDir = $latest.FullName
}

$statePath = Join-Path $RunDir "state.json"
$summaryPath = Join-Path $RunDir "summary.md"

if (-not (Test-Path $statePath)) {
    throw "State file not found: $statePath"
}

$state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json

Write-Host "== Phase 1 Stability Report =="
Write-Host
Write-Host "Run Dir: $RunDir"
Write-Host "Status: $($state.status)"
Write-Host "Started: $($state.started_at)"
Write-Host "Completed: $($state.completed_at)"
Write-Host "Samples: $($state.samples)"
Write-Host "Probes: $($state.probes)"
Write-Host "Latest Overall: $($state.latest_overall)"
Write-Host

if ($state.overall_counts) {
    Write-Host "[Overall Counts]"
    Write-Host "healthy: $($state.overall_counts.healthy)"
    Write-Host "degraded: $($state.overall_counts.degraded)"
    Write-Host "failed: $($state.overall_counts.failed)"
    Write-Host "unknown: $($state.overall_counts.unknown)"
    Write-Host
}

if ($state.probe_counts) {
    Write-Host "[Probe Counts]"
    Write-Host "finished: $($state.probe_counts.finished)"
    Write-Host "warning: $($state.probe_counts.warning)"
    Write-Host "incomplete: $($state.probe_counts.incomplete)"
    Write-Host "failed: $($state.probe_counts.failed)"
    Write-Host "timeout: $($state.probe_counts.timeout)"
    Write-Host "error: $($state.probe_counts.error)"
    Write-Host "other: $($state.probe_counts.other)"
    Write-Host
}

if ($state.latest_probe) {
    Write-Host "[Latest Probe]"
    Write-Host "Probe Index: $($state.latest_probe.probe_index)"
    Write-Host "Task ID: $($state.latest_probe.task_id)"
    Write-Host "Action: $($state.latest_probe.action)"
    Write-Host "Task Phase: $($state.latest_probe.task_phase)"
    Write-Host "Reply Code: $($state.latest_probe.reply_code)"
    Write-Host "Failure Category: $($state.latest_probe.failure_category)"
    Write-Host "Delivery Errors: $($state.latest_probe.delivery_error_count)"
    Write-Host "Probe Outcome: $($state.latest_probe.probe_outcome)"
    Write-Host "Probe Detail: $($state.latest_probe.probe_detail)"
    Write-Host "Error: $($state.latest_probe.error)"
    Write-Host
}

if (Test-Path $summaryPath) {
    Write-Host "[Summary Path]"
    Write-Host $summaryPath
}
