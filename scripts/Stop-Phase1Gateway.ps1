param()

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $projectRoot "runtime\gateway.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "Gateway pid file not found."
    exit 0
}

$gatewayPid = (Get-Content -Raw $pidFile).Trim()
if ($gatewayPid) {
    Stop-Process -Id ([int]$gatewayPid) -ErrorAction SilentlyContinue
}

Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
Write-Host "Phase 1 gateway stopped."
