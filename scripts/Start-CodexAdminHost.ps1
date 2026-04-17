param(
    [string]$TaskName = "CodexAdminHost",
    [int]$DelaySeconds = 3
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Scheduled task not found: $TaskName. Run Register-CodexAdminHostTask.ps1 first."
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds $DelaySeconds

Write-Host "Elevated Codex host launch requested."
Write-Host "Task: $TaskName"
Write-Host "If VS Code does not appear, open Task Scheduler and inspect the task history."
