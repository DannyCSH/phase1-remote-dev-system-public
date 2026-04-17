param(
    [string]$TaskName = "CodexAdminHost",
    [string]$VsCodePath = "",
    [string]$WorkspacePath = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
if (-not $WorkspacePath) {
    $WorkspacePath = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
}
Set-Phase1ProjectEnv -ProjectRoot $WorkspacePath
$WorkspacePath = Resolve-Phase1Path -Path $WorkspacePath -ProjectRoot $WorkspacePath
if (-not $VsCodePath) {
    $VsCodePath = Resolve-Phase1VsCodePath
}
$VsCodePath = Resolve-Phase1Path -Path $VsCodePath -ProjectRoot $WorkspacePath

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsElevated)) {
    throw "Run this script in an elevated PowerShell window."
}

if (-not (Test-Path $VsCodePath)) {
    throw "VS Code executable not found. Set PHASE1_VSCODE_PATH or install VS Code."
}

if (-not (Test-Path $WorkspacePath)) {
    throw "Workspace path not found: $WorkspacePath"
}

$quotedWorkspace = '"' + $WorkspacePath + '"'
$action = New-ScheduledTaskAction -Execute $VsCodePath -Argument $quotedWorkspace
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
Write-Host "  Action: $VsCodePath $quotedWorkspace"
Write-Host
Write-Host "You can now launch the elevated Codex host with:"
Write-Host ("  powershell -ExecutionPolicy Bypass -File {0}" -f (Join-Path $WorkspacePath "scripts\Start-CodexAdminHost.ps1"))
