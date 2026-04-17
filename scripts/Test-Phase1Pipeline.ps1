param(
    [string]$ChatId = "",
    [string]$Request = "Reply in one short Chinese sentence: Phase 1 pipeline test successful.",
    [string]$Config = "",
    [switch]$HealthProbe
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
$inboxDir = Join-Path $projectRoot "runtime\inbox"
New-Item -ItemType Directory -Force -Path $inboxDir | Out-Null

if (-not $ChatId) {
    if (-not (Test-Path $Config)) {
        throw "Config not found and -ChatId was not provided: $Config"
    }

    $cfg = Read-Phase1JsonResolved -Path $Config
    $allowFrom = @($cfg.channels.qq.allowFrom)
    if ($allowFrom.Count -gt 0) {
        $candidate = [string]$allowFrom[0]
        if ($candidate) {
            $ChatId = $candidate
        }
    }
}

if (-not $ChatId) {
    throw "No ChatId available. Provide -ChatId explicitly or set channels.qq.allowFrom in the resolved config."
}

$taskId = "manual-{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss")
$taskFile = Join-Path $inboxDir ($taskId + ".json")

$metadata = @{}
if ($HealthProbe) {
    $metadata = @{
        phase1 = @{
            health_probe = $true
            health_probe_source = "Test-Phase1Pipeline.ps1"
            health_probe_requested_at = (Get-Date).ToString("s")
        }
    }
}

$payload = @{
    task_id = $taskId
    channel = "qq"
    chat_id = $ChatId
    sender_id = $ChatId
    message_id = ""
    session_key = ("qq:" + $ChatId)
    project_id = "phase1-remote-dev"
    project_root = $projectRoot
    attachments = @()
    metadata = $metadata
    user_request = $Request
    received_at = (Get-Date).ToString("s")
} | ConvertTo-Json -Depth 4

Set-Content -Path $taskFile -Value $payload -Encoding UTF8

$launcher = Join-Path $projectRoot "scripts\Launch-Phase1Task.ps1"
& $launcher -TaskFile $taskFile -Config $Config
