param(
    [string]$TaskName = "Phase1AdminRelay",
    [string]$PythonPath = "",
    [string]$RelayScript = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
if (-not $PythonPath) {
    $PythonPath = Resolve-Phase1PythonPath -ProjectRoot $projectRoot
}
if (-not $RelayScript) {
    $RelayScript = Join-Path $projectRoot "app\phase1_admin_relay.py"
}
$PythonPath = Resolve-Phase1Path -Path $PythonPath -ProjectRoot $projectRoot
$RelayScript = Resolve-Phase1Path -Path $RelayScript -ProjectRoot $projectRoot

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsElevated)) {
    throw "Run this script in an elevated PowerShell window."
}

if (-not (Test-Path $PythonPath)) {
    throw "Python runtime not found. Set PHASE1_PYTHON_PATH or create a repo-local .venv."
}

if (-not (Test-Path $RelayScript)) {
    throw "Admin relay script not found: $RelayScript"
}

$quotedRelayScript = '"' + $RelayScript + '"'
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $quotedRelayScript
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Scheduled task registered:"
Write-Host "  Name: $TaskName"
Write-Host "  Action: $PythonPath $quotedRelayScript"
