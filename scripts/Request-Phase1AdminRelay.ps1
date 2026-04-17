param(
    [string]$TaskName = "Phase1AdminRelay"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Scheduled task not found: $TaskName. Run Register-Phase1AdminRelayTask.ps1 first."
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Admin relay launch requested."
Write-Host "Task: $TaskName"
