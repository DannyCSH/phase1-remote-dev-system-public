param(
    [string]$TaskName = "Phase1AutoStart",
    [string]$ProjectRoot = "",
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
if (-not $ProjectRoot) {
    $ProjectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
}
Set-Phase1ProjectEnv -ProjectRoot $ProjectRoot
$ProjectRoot = Resolve-Phase1Path -Path $ProjectRoot -ProjectRoot $ProjectRoot
if (-not $ConfigPath) {
    $ConfigPath = Get-Phase1DefaultConfigPath -ProjectRoot $ProjectRoot
}
$ConfigPath = Resolve-Phase1Path -Path $ConfigPath -ProjectRoot $ProjectRoot

$startScript = Join-Path $ProjectRoot "scripts\Start-Phase1Stack.ps1"
if (-not (Test-Path $startScript)) {
    throw "Start script not found: $startScript"
}

if (-not (Test-Path $ConfigPath)) {
    throw "Config not found: $ConfigPath"
}

$psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$argument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $psExe -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Scheduled task registered:"
Write-Host "  Name: $TaskName"
Write-Host "  Action: $psExe $argument"
