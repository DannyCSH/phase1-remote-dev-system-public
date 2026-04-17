param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$sourceScripts = @(
    "Phase1-ConfigHelpers.ps1",
    "Get-Phase1Status.ps1",
    "Check-Phase1Env.ps1",
    "New-Phase1DiagnosticBundle.ps1",
    "Start-Phase1StabilityRun.ps1",
    "Test-Phase1Pipeline.ps1"
)

$tempRoot = Join-Path "D:\AI_TEMP" ("phase1-powershell-tests-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

$failures = New-Object 'System.Collections.Generic.List[string]'
$passes = 0
$demoOpenId = "11111111111111111111111111111111"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Contains {
    param(
        [string]$Text,
        [string]$Needle,
        [string]$Message
    )

    if ($Text -notlike ("*" + $Needle + "*")) {
        throw $Message
    }
}

function Assert-NotContains {
    param(
        [string]$Text,
        [string]$Needle,
        [string]$Message
    )

    if ($Text -like ("*" + $Needle + "*")) {
        throw $Message
    }
}

function New-FixtureProject {
    param([string]$Name)

    $root = Join-Path $tempRoot $Name
    $scriptsDir = Join-Path $root "scripts"
    $configDir = Join-Path $root "config"
    $runtimeDir = Join-Path $root "runtime"
    foreach ($path in @($root, $scriptsDir, $configDir, $runtimeDir, (Join-Path $runtimeDir "tasks"), (Join-Path $runtimeDir "sessions"), (Join-Path $runtimeDir "projects"), (Join-Path $runtimeDir "support"))) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }

    foreach ($scriptName in $sourceScripts) {
        Copy-Item -Path (Join-Path $repoRoot ("scripts\" + $scriptName)) -Destination (Join-Path $scriptsDir $scriptName) -Force
    }

    $config = @"
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "demo-app",
      "secret": "demo-secret",
      "allowFrom": ["demo-openid"],
      "mediaDir": "$($runtimeDir.Replace('\', '\\'))\\media\\qq"
    }
  },
  "phase1": {
    "project": {
      "defaultId": "fixture-project",
      "defaultRoot": "$($root.Replace('\', '\\'))",
      "allowedRoots": ["$($root.Replace('\', '\\'))"]
    },
    "artifacts": {
      "maxFiles": 0,
      "maxTotalBytes": 83886080,
      "maxSingleFileBytes": 26214400,
      "qqMaxUploadBytes": 26214400,
      "zipThreshold": 0,
      "preferDirectSend": true,
      "interFileDelayMs": 0,
      "directSendNoticeThreshold": 8,
      "allowedRoots": []
    }
  }
}
"@
    Set-Content -Path (Join-Path $configDir "nanobot.local.json") -Value $config -Encoding UTF8
    return $root
}

function Invoke-ExternalPowerShellFile {
    param(
        [string]$Path,
        [string[]]$Arguments = @()
    )

    $psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $output = & $psExe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments 2>&1 | Out-String
    return [pscustomobject]@{
        Output = $output
        ExitCode = $LASTEXITCODE
    }
}

function Invoke-TestCase {
    param(
        [string]$Name,
        [scriptblock]$Body
    )

    try {
        & $Body
        Write-Host ("[PASS] " + $Name)
        return $true
    } catch {
        Write-Host ("[FAIL] " + $Name + " -> " + $_.Exception.Message)
        $failures.Add($Name + ": " + $_.Exception.Message)
        return $false
    }
}

$originalNanobotExe = $env:PHASE1_NANOBOT_EXE
$env:PHASE1_NANOBOT_EXE = (Join-Path $PSHOME "powershell.exe")

try {
    if (Invoke-TestCase -Name "Get-Phase1Status writes health snapshot" -Body {
        $fixture = New-FixtureProject -Name "status"
        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Get-Phase1Status.ps1") -Arguments @("-WriteRuntimeHealth")
        $healthPath = Join-Path $fixture "runtime\health.json"
        Assert-True -Condition (Test-Path $healthPath) -Message "health.json was not created."
        $health = Get-Content -Raw -Encoding UTF8 $healthPath | ConvertFrom-Json
        Assert-True -Condition ($null -ne $health.overall) -Message "health.json missing overall."
        Assert-True -Condition ($null -ne $health.gateway.state) -Message "health.json missing gateway state."
        Assert-Contains -Text $result.Output -Needle "== Phase 1 Status ==" -Message "status output header missing."
        Assert-Contains -Text $result.Output -Needle "[Overall]" -Message "status output overall block missing."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "Get-Phase1Status surfaces receipt-only QQ delivery warnings" -Body {
        $fixture = New-FixtureProject -Name "status-receipt-warning"
        $taskDir = Join-Path $fixture "runtime\tasks\fixture-task"
        New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
        Set-Content -Path (Join-Path $taskDir "status.json") -Encoding UTF8 -Value @"
{
  "task_id": "fixture-task",
  "phase": "finished",
  "task_name": "demo request",
  "project_id": "fixture-project",
  "project_root": "$($fixture.Replace('\', '\\'))",
  "session_key": "qq:demo-openid",
  "session_id": "session-demo",
  "result": "QQ upload API returned 850012 while sending the final archive.",
  "receipt": {
    "protocol": "p1-receipt.v1",
    "stage": "worker",
    "ack": "finished",
    "reply_code": "completed_with_delivery_warning",
    "user_visible_status": "completed",
    "failure_category": "qq_api_error",
    "message": "QQ upload API returned 850012 while sending the final archive.",
    "phase": "finished",
    "ts": "2026-04-16T18:00:00",
    "error": {
      "code": "qq-api-error",
      "message": "QQ upload API returned 850012 while sending the final archive.",
      "failure_category": "qq_api_error"
    }
  }
}
"@

        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Get-Phase1Status.ps1") -Arguments @("-WriteRuntimeHealth")
        Assert-True -Condition ($result.ExitCode -eq 0) -Message "Get-Phase1Status failed on a receipt-only warning fixture."
        $health = Get-Content -Raw -Encoding UTF8 (Join-Path $fixture "runtime\health.json") | ConvertFrom-Json
        $issuesJson = $health.issues | ConvertTo-Json -Depth 6
        Assert-Contains -Text $issuesJson -Needle "qq_delivery_warning" -Message "health.json did not surface the QQ delivery warning."
        Assert-Contains -Text $issuesJson -Needle "fixture-task" -Message "health.json warning did not point to the latest task."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "Check-Phase1Env produces readable output" -Body {
        $fixture = New-FixtureProject -Name "env-check"
        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Check-Phase1Env.ps1")
        Assert-True -Condition ($result.ExitCode -eq 1) -Message "Check-Phase1Env should fail on a cold fixture."
        Assert-Contains -Text $result.Output -Needle "== Phase 1 Environment Check ==" -Message "env check header missing."
        Assert-Contains -Text $result.Output -Needle "[Issues]" -Message "env check issues block missing."
        Assert-Contains -Text $result.Output -Needle "Runtime overall status is" -Message "env check runtime issue missing."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "New-Phase1DiagnosticBundle redacts sensitive values" -Body {
        $fixture = New-FixtureProject -Name "diagnostic"
        $runtimeDir = Join-Path $fixture "runtime"
        $sessionDir = Join-Path $runtimeDir ("sessions\qq_" + $demoOpenId)
        $projectDir = Join-Path $runtimeDir "projects\fixture-project"
        $taskDir = Join-Path $runtimeDir "tasks\fixture-task"
        $userMessage = "still-no-reply-yet"
        foreach ($path in @($sessionDir, $projectDir, $taskDir)) {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
        }

        Set-Content -Path (Join-Path $sessionDir "state.json") -Encoding UTF8 -Value @"
{
  "session_key": "qq:$demoOpenId",
  "current_project_id": "fixture-project",
  "current_project_root": "$($fixture.Replace('\', '\\'))",
  "current_session_id": "session-demo",
  "last_task_id": "fixture-task",
  "last_result": "ok",
  "last_inbound_at": "2026-04-16T18:00:00",
  "updated_at": "2026-04-16T18:00:00"
}
"@
        Set-Content -Path (Join-Path $projectDir "state.json") -Encoding UTF8 -Value @"
{
  "project_id": "fixture-project",
  "project_root": "$($fixture.Replace('\', '\\'))",
  "current_session_key": "qq:$demoOpenId",
  "current_session_id": "session-demo",
  "last_task_id": "fixture-task",
  "updated_at": "2026-04-16T18:00:00"
}
"@
        Set-Content -Path (Join-Path $taskDir "status.json") -Encoding UTF8 -Value @"
{
  "task_id": "fixture-task",
  "phase": "failed",
  "task_name": "$userMessage",
  "project_id": "fixture-project",
  "project_root": "$($fixture.Replace('\', '\\'))",
  "session_key": "qq:$demoOpenId",
  "session_id": "session-demo",
  "error": "upload failed"
}
"@
        Set-Content -Path (Join-Path $runtimeDir "gateway.err.log") -Encoding UTF8 -Value ("Processing message from qq:${demoOpenId}: " + $userMessage)

        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\New-Phase1DiagnosticBundle.ps1")
        Assert-True -Condition ($result.ExitCode -eq 0) -Message "diagnostic bundle script failed."
        $bundleDirLine = ($result.Output -split "`r?`n" | Where-Object { $_ -like "BUNDLE_DIR=*" } | Select-Object -First 1)
        Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($bundleDirLine)) -Message "diagnostic bundle path missing."
        $bundleDir = $bundleDirLine.Substring("BUNDLE_DIR=".Length)
        $healthText = Get-Content -Raw -Encoding UTF8 (Join-Path $bundleDir "runtime\health.redacted.json")
        $logText = Get-Content -Raw -Encoding UTF8 (Join-Path $bundleDir "logs\gateway.err.redacted.log")
        $taskText = Get-Content -Raw -Encoding UTF8 (Join-Path $bundleDir "runtime\tasks-fixture-task\status.redacted.json")
        Assert-Contains -Text $healthText -Needle "***REDACTED***" -Message "health bundle did not redact session data."
        Assert-NotContains -Text $healthText -Needle $demoOpenId -Message "health bundle leaked session key."
        Assert-NotContains -Text $logText -Needle $userMessage -Message "sanitized log leaked message text."
        Assert-Contains -Text $taskText -Needle '"task_name": "***REDACTED***"' -Message "task status did not redact task_name."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "Test-Phase1Pipeline marks health probes for fast-path routing" -Body {
        $fixture = New-FixtureProject -Name "pipeline-health-probe"
        Set-Content -Path (Join-Path $fixture "scripts\Launch-Phase1Task.ps1") -Encoding UTF8 -Value @'
param(
    [string]$TaskFile,
    [string]$Config
)

$task = Get-Content -Raw -Encoding UTF8 $TaskFile | ConvertFrom-Json
$phase1 = $task.metadata.phase1
Write-Output ("ACTION=enqueued")
Write-Output ("TASK_ID=" + [string]$task.task_id)
Write-Output ("HEALTH_PROBE=" + [string][bool]$phase1.health_probe)
Write-Output ("PROBE_SOURCE=" + [string]$phase1.health_probe_source)
'@

        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Test-Phase1Pipeline.ps1") -Arguments @("-Config", (Join-Path $fixture "config\nanobot.local.json"), "-HealthProbe")
        Assert-True -Condition ($result.ExitCode -eq 0) -Message "Test-Phase1Pipeline failed."
        Assert-Contains -Text $result.Output -Needle "HEALTH_PROBE=True" -Message "health probe marker missing."
        Assert-Contains -Text $result.Output -Needle "PROBE_SOURCE=Test-Phase1Pipeline.ps1" -Message "health probe source missing."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "Start-Phase1StabilityRun creates report in health-only mode" -Body {
        $fixture = New-FixtureProject -Name "stability"
        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Start-Phase1StabilityRun.ps1") -Arguments @("-DurationHours", "0", "-StatusIntervalSeconds", "1", "-SkipPipelineProbe")
        Assert-True -Condition ($result.ExitCode -eq 0) -Message "stability run script failed."
        $runDirLine = ($result.Output -split "`r?`n" | Where-Object { $_ -like "RUN_DIR=*" } | Select-Object -First 1)
        Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($runDirLine)) -Message "stability run dir missing."
        $runDir = $runDirLine.Substring("RUN_DIR=".Length)
        Assert-True -Condition (Test-Path (Join-Path $runDir "state.json")) -Message "stability state.json missing."
        Assert-True -Condition (Test-Path (Join-Path $runDir "summary.md")) -Message "stability summary.md missing."
        $state = Get-Content -Raw -Encoding UTF8 (Join-Path $runDir "state.json") | ConvertFrom-Json
        Assert-True -Condition ($state.status -eq "completed") -Message "stability state did not complete."
    }) { $passes += 1 }

    if (Invoke-TestCase -Name "Start-Phase1StabilityRun marks warning probe outcomes from final receipts" -Body {
        $fixture = New-FixtureProject -Name "stability-warning"
        $taskDir = Join-Path $fixture "runtime\tasks\fixture-probe-task"
        New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
        Set-Content -Path (Join-Path $taskDir "status.json") -Encoding UTF8 -Value @"
{
  "task_id": "fixture-probe-task",
  "phase": "finished",
  "task_name": "probe request",
  "project_id": "fixture-project",
  "project_root": "$($fixture.Replace('\', '\\'))",
  "session_key": "qq:demo-openid",
  "session_id": "session-demo",
  "receipt": {
    "protocol": "p1-receipt.v1",
    "stage": "worker",
    "ack": "finished",
    "reply_code": "completed_with_delivery_warning",
    "user_visible_status": "completed",
    "failure_category": "qq_api_error",
    "message": "QQ upload API returned 850012 while sending the final archive.",
    "phase": "finished",
    "ts": "2026-04-16T18:00:00",
    "meta": {
      "delivery_errors": ["files: QQ upload API returned 850012 while sending archive.zip"]
    },
    "error": {
      "code": "qq-api-error",
      "message": "QQ upload API returned 850012 while sending the final archive.",
      "failure_category": "qq_api_error"
    }
  }
}
"@
        Set-Content -Path (Join-Path $fixture "scripts\Test-Phase1Pipeline.ps1") -Encoding UTF8 -Value @'
param(
    [string]$ChatId = "",
    [string]$Request = "",
    [string]$Config = ""
)

Write-Output "ACTION=enqueued"
Write-Output "TASK_ID=fixture-probe-task"
'@

        $result = Invoke-ExternalPowerShellFile -Path (Join-Path $fixture "scripts\Start-Phase1StabilityRun.ps1") -Arguments @("-DurationHours", "0", "-StatusIntervalSeconds", "1", "-ProbeIntervalMinutes", "1", "-ProbeTimeoutMinutes", "1")
        Assert-True -Condition ($result.ExitCode -eq 0) -Message "stability run with probe failed."
        $runDirLine = ($result.Output -split "`r?`n" | Where-Object { $_ -like "RUN_DIR=*" } | Select-Object -First 1)
        Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($runDirLine)) -Message "stability run dir missing for warning probe."
        $runDir = $runDirLine.Substring("RUN_DIR=".Length)
        $state = Get-Content -Raw -Encoding UTF8 (Join-Path $runDir "state.json") | ConvertFrom-Json
        Assert-True -Condition ($state.probe_counts.warning -eq 1) -Message "warning probe count was not incremented."
        Assert-True -Condition ($state.latest_probe.probe_outcome -eq "warning") -Message "latest probe outcome should be warning."
        Assert-True -Condition ($state.latest_probe.delivery_error_count -eq 1) -Message "delivery error count should come from the final receipt."
        Assert-True -Condition ($state.latest_probe.failure_category -eq "qq_api_error") -Message "failure category should be recovered from the final receipt."
    }) { $passes += 1 }
} finally {
    if ($null -eq $originalNanobotExe) {
        Remove-Item Env:PHASE1_NANOBOT_EXE -ErrorAction SilentlyContinue
    } else {
        $env:PHASE1_NANOBOT_EXE = $originalNanobotExe
    }
}

Write-Host
Write-Host ("Passes: " + $passes)
Write-Host ("Failures: " + $failures.Count)

if ($failures.Count -gt 0) {
    exit 1
}

exit 0
