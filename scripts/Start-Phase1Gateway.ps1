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
$nanobot = Resolve-Phase1NanobotPath -ProjectRoot $projectRoot
$runtimeDir = Join-Path $projectRoot "runtime"
$tempDir = Join-Path $runtimeDir "tmp"
$pidFile = Join-Path $runtimeDir "gateway.pid"
$stdoutLog = Join-Path $runtimeDir "gateway.out.log"
$stderrLog = Join-Path $runtimeDir "gateway.err.log"
$resolvedConfig = Join-Path $tempDir "nanobot.resolved.json"

if (-not (Test-Path $nanobot)) {
    throw "NanoBot executable not found. Set PHASE1_NANOBOT_PATH or install nanobot into the repo .venv."
}

if (-not (Test-Path $Config)) {
    throw "Config not found: $Config"
}

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
foreach ($dir in @(
    "queue",
    "queue\pending",
    "queue\processing",
    "projects",
    "sessions",
    "control",
    "control\stop",
    "control\locks",
    "control\message-ids"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir $dir) | Out-Null
}

$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:TMPDIR = $tempDir
$env:PYTHONIOENCODING = "utf-8"

Write-ResolvedPhase1Config -SourcePath $Config -TargetPath $resolvedConfig -StripPhase1Section | Out-Null

if (Test-Path $pidFile) {
    $oldPid = (Get-Content -Raw -Encoding UTF8 $pidFile -ErrorAction SilentlyContinue).Trim()
    if ($oldPid -match "^\d+$") {
        $existing = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($existing) {
            throw "Gateway already appears to be running with PID $oldPid."
        }
    }
    Remove-Item -Force $pidFile -ErrorAction SilentlyContinue
}

$proc = Start-Process `
    -FilePath $nanobot `
    -ArgumentList @("gateway", "--config", $resolvedConfig, "--workspace", $projectRoot) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -Path $pidFile -Value $proc.Id -Encoding UTF8

Write-Host "Phase 1 gateway started."
Write-Host "PID: $($proc.Id)"
Write-Host "Config: $resolvedConfig"
Write-Host "NanoBot: $nanobot"
Write-Host "STDOUT: $stdoutLog"
Write-Host "STDERR: $stderrLog"
