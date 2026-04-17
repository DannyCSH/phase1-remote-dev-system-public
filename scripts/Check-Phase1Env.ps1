param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
$statusScript = Join-Path $projectRoot "scripts\Get-Phase1Status.ps1"
$healthPath = Join-Path $projectRoot "runtime\health.json"
$configLocalPath = Join-Path $projectRoot "config\nanobot.local.json"
$configExamplePath = Join-Path $projectRoot "config\nanobot.example.json"

& $statusScript -WriteRuntimeHealth *> $null

$health = $null
if (Test-Path $healthPath) {
    try {
        $health = Get-Content -Raw -Encoding UTF8 $healthPath | ConvertFrom-Json
    } catch {
        $health = $null
    }
}

$configSource = if (Test-Path $configLocalPath) { $configLocalPath } else { $configExamplePath }
$config = Read-Phase1JsonResolved -Path $configSource

$nanobotPath = Resolve-Phase1NanobotPath -ProjectRoot $projectRoot
$nanobotCmd = $null
$nanobotExists = -not [string]::IsNullOrWhiteSpace($nanobotPath)
$claudeCmd = Get-Command claude.exe -ErrorAction SilentlyContinue
if (-not $claudeCmd) {
    $claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
}
$codexCmd = Get-Command codex -ErrorAction SilentlyContinue
$nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
}
$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
}
$pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
}

$issues = New-Object 'System.Collections.Generic.List[string]'
$actions = New-Object 'System.Collections.Generic.List[string]'

function Add-CheckIssue {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Text
    )

    if (-not [string]::IsNullOrWhiteSpace($Text) -and -not $List.Contains($Text)) {
        $List.Add($Text)
    }
}

function Write-ToolStatus {
    param(
        [string]$Name,
        $CommandInfo,
        [string]$ResolvedPath = ""
    )

    if ($CommandInfo -and $CommandInfo.Source) {
        Write-Host ($Name + ": True (" + $CommandInfo.Source + ")")
    } elseif (-not [string]::IsNullOrWhiteSpace($ResolvedPath)) {
        Write-Host ($Name + ": True (" + $ResolvedPath + ")")
    } else {
        Write-Host ($Name + ": False")
    }
}

if (-not (Test-Path $configSource)) {
    Add-CheckIssue -List $issues -Text "No config file was found."
    Add-CheckIssue -List $actions -Text "Create nanobot.local.json or keep nanobot.example.json available."
}

if (-not $nanobotExists) {
    Add-CheckIssue -List $issues -Text "NanoBot CLI was not found."
    Add-CheckIssue -List $actions -Text "Install NanoBot, or set PHASE1_NANOBOT_EXE to the local nanobot.exe path."
}

if (-not $claudeCmd) {
    Add-CheckIssue -List $issues -Text "Claude CLI was not found."
    Add-CheckIssue -List $actions -Text "Install Claude Code CLI and make sure it is logged in."
}

if (-not $codexCmd) {
    Add-CheckIssue -List $issues -Text "Codex CLI was not found."
    Add-CheckIssue -List $actions -Text "Install Codex CLI and make sure it is logged in."
}

if (-not $nodeCmd) {
    Add-CheckIssue -List $issues -Text "Node.js was not found."
    Add-CheckIssue -List $actions -Text "Install Node.js LTS."
}

if (-not $npmCmd) {
    Add-CheckIssue -List $issues -Text "npm was not found."
    Add-CheckIssue -List $actions -Text "Repair the Node.js installation so npm is available."
}

if (-not $pythonCmd) {
    Add-CheckIssue -List $issues -Text "Python was not found."
    Add-CheckIssue -List $actions -Text "Install Python 3 and make sure python.exe is on PATH."
}

$qqEnabled = $false
$allowFromCount = 0
if ($config) {
    $qqEnabled = [bool]($config.channels.qq.enabled)
    $allowFromCount = @($config.channels.qq.allowFrom).Count
}

if (-not $qqEnabled) {
    Add-CheckIssue -List $issues -Text "QQ channel is currently disabled."
    Add-CheckIssue -List $actions -Text "Enable channels.qq.enabled before testing from the phone."
}

if ($qqEnabled -and $allowFromCount -eq 0) {
    Add-CheckIssue -List $issues -Text "QQ channel is enabled but allowFrom is empty."
    Add-CheckIssue -List $actions -Text "Add the authorized QQ OpenID, otherwise phone messages will still be rejected."
}

if ($health -and $health.overall -and [string]($health.overall) -ne "healthy") {
    Add-CheckIssue -List $issues -Text ("Runtime overall status is " + [string]($health.overall) + ".")
}

if ($health -and $health.recommended_actions) {
    foreach ($item in @($health.recommended_actions)) {
        Add-CheckIssue -List $actions -Text ([string]$item)
    }
}

Write-Host "== Phase 1 Environment Check =="
Write-Host
Write-Host "[Config]"
Write-Host ("Project root: " + $projectRoot)
Write-Host ("Config source: " + $configSource)
Write-Host ("QQ enabled: " + $qqEnabled)
Write-Host ("QQ allowFrom count: " + $allowFromCount)
Write-Host
Write-Host "[Dependencies]"
Write-ToolStatus -Name "NanoBot" -CommandInfo $nanobotCmd -ResolvedPath $nanobotPath
Write-ToolStatus -Name "Claude CLI" -CommandInfo $claudeCmd
Write-ToolStatus -Name "Codex CLI" -CommandInfo $codexCmd
Write-ToolStatus -Name "Node.js" -CommandInfo $nodeCmd
Write-ToolStatus -Name "npm" -CommandInfo $npmCmd
Write-ToolStatus -Name "Python" -CommandInfo $pythonCmd
Write-Host
Write-Host "[Runtime]"
if ($health) {
    Write-Host ("Overall: " + [string]($health.overall))
    Write-Host ("Gateway: " + [string]($health.gateway.state))
    Write-Host ("Worker: " + [string]($health.worker.state))
    Write-Host ("Admin relay: " + [string]($health.admin_relay.state))
    Write-Host ("Last inbound: " + [string]($health.last_inbound_at))
    Write-Host ("Last task finished: " + [string]($health.last_task_finished_at))
} else {
    Write-Host "Health snapshot: unavailable"
}
Write-Host

if ($issues.Count -eq 0) {
    Write-Host "[Result]"
    Write-Host "Environment check passed."
    exit 0
}

Write-Host "[Issues]"
foreach ($item in $issues) {
    Write-Host ("- " + $item)
}
Write-Host
Write-Host "[Recommended Actions]"
foreach ($item in $actions) {
    Write-Host ("- " + $item)
}
exit 1
