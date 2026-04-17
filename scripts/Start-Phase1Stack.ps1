param(
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
if (-not $Config) {
    $Config = Get-Phase1DefaultConfigPath -ProjectRoot $projectRoot
}
$Config = Resolve-Phase1Path -Path $Config -ProjectRoot $projectRoot
$runtimeDir = Join-Path $projectRoot "runtime"
$pidFile = Join-Path $runtimeDir "gateway.pid"
$gatewayWatchdogPidFile = Join-Path $runtimeDir "gateway-watchdog.pid"
$startGateway = Join-Path $projectRoot "scripts\Start-Phase1Gateway.ps1"
$statusScript = Join-Path $projectRoot "scripts\Get-Phase1Status.ps1"
$repairCodexConfig = Join-Path $projectRoot "scripts\Repair-CodexConfig.ps1"
$gatewayWatchdogScript = Join-Path $projectRoot "scripts\Watch-Phase1Gateway.ps1"
$gatewayWatchdogStdout = Join-Path $runtimeDir "gateway-watchdog.out.log"
$gatewayWatchdogStderr = Join-Path $runtimeDir "gateway-watchdog.err.log"

foreach ($dir in @(
    "inbox",
    "tasks",
    "queue",
    "queue\pending",
    "queue\processing",
    "admin-inbox",
    "admin-tasks",
    "logs",
    "logs\router",
    "logs\tasks",
    "media",
    "projects",
    "sessions",
    "control",
    "control\stop",
    "control\locks",
    "control\message-ids",
    "tmp"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir $dir) | Out-Null
}

if (Test-Path $repairCodexConfig) {
    & $repairCodexConfig | Out-Null
}

$shouldStart = $true
if (Test-Path $pidFile) {
    $rawPid = (Get-Content -Raw -Encoding UTF8 $pidFile -ErrorAction SilentlyContinue).Trim()
    if ($rawPid -match "^\d+$") {
        $existing = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
        if ($existing) {
            $shouldStart = $false
            Write-Host "Gateway already running with PID $rawPid."
        } else {
            Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
        }
    }
}

if ($shouldStart) {
    & $startGateway -Config $Config
}

if (Test-Path $gatewayWatchdogScript) {
    $watchdogRunning = $false
    if (Test-Path $gatewayWatchdogPidFile) {
        $watchdogPid = (Get-Content -Raw -Encoding UTF8 $gatewayWatchdogPidFile -ErrorAction SilentlyContinue).Trim()
        if ($watchdogPid -match "^\d+$") {
            $watchdogProc = Get-Process -Id ([int]$watchdogPid) -ErrorAction SilentlyContinue
            if ($watchdogProc) {
                $watchdogRunning = $true
            } else {
                Remove-Item -Force $gatewayWatchdogPidFile -ErrorAction SilentlyContinue
            }
        }
    }

    if (-not $watchdogRunning) {
        $psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        Start-Process -FilePath $psExe -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $gatewayWatchdogScript,
            "-ProjectRoot", $projectRoot,
            "-ConfigPath", $Config,
            "-PidFile", $pidFile,
            "-WatchdogPidFile", $gatewayWatchdogPidFile
        ) -RedirectStandardOutput $gatewayWatchdogStdout -RedirectStandardError $gatewayWatchdogStderr -WindowStyle Hidden | Out-Null
    }
}

& $statusScript -WriteRuntimeHealth
