param(
    [Parameter(Mandatory = $true)]
    [string]$LockFile,
    [Parameter(Mandatory = $true)]
    [string]$PendingQueueDir,
    [Parameter(Mandatory = $true)]
    [string]$TempDir,
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$WorkerScript,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$WorkerStdout,
    [Parameter(Mandatory = $true)]
    [string]$WorkerStderr,
    [string]$MutexName = "Local\Phase1RemoteDevWatchdog",
    [int]$TimeoutMinutes = 2
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

function Get-WorkerLockState {
    param([string]$Path)

    $state = [pscustomobject]@{
        Running = $false
        Pid = ""
    }

    if (-not (Test-Path $Path)) {
        return $state
    }

    try {
        $lock = Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
        if ($lock.pid) {
            $existing = Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue
            if ($existing) {
                $state.Running = $true
                $state.Pid = [string]$lock.pid
                return $state
            }
        }
    } catch {
    }

    Remove-Item -Force $Path -ErrorAction SilentlyContinue
    return $state
}

function Get-QueueState {
    param([string]$PendingPath)

    $processingPath = Join-Path (Split-Path $PendingPath -Parent) "processing"
    $pendingCount = (Get-ChildItem -Path $PendingPath -Filter '*.json' -File -ErrorAction SilentlyContinue | Measure-Object).Count
    $processingCount = 0
    if (Test-Path $processingPath) {
        $processingCount = (Get-ChildItem -Path $processingPath -Filter '*.json' -File -ErrorAction SilentlyContinue | Measure-Object).Count
    }

    return [pscustomobject]@{
        Pending = $pendingCount
        Processing = $processingCount
        Total = ($pendingCount + $processingCount)
    }
}

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasMutex = $false

try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        exit 0
    }

    $idleDeadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $idleDeadline) {
        Start-Sleep -Seconds 3

        $workerState = Get-WorkerLockState -Path $LockFile
        $queueState = Get-QueueState -PendingPath $PendingQueueDir
        if ($queueState.Total -le 0) {
            break
        }
        $idleDeadline = (Get-Date).AddMinutes($TimeoutMinutes)
        if ($workerState.Running) {
            continue
        }

        $env:TEMP = $TempDir
        $env:TMP = $TempDir
        $env:TMPDIR = $TempDir
        $env:PYTHONIOENCODING = "utf-8"

        $proc = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList @($WorkerScript, "--config", $ConfigPath) `
            -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $WorkerStdout `
            -RedirectStandardError $WorkerStderr `
            -WindowStyle Hidden `
            -PassThru

        $lockPayload = @{
            pid = $proc.Id
            phase = "booting"
            active_task_id = ""
            project_id = ""
            session_key = ""
            session_id = ""
            started_at = (Get-Date).ToString("s")
            worker_stdout_log = $WorkerStdout
            worker_stderr_log = $WorkerStderr
        } | ConvertTo-Json -Depth 4
        Set-Content -Path $LockFile -Value $lockPayload -Encoding UTF8

        $restartDeadline = (Get-Date).AddSeconds(8)
        $restartAlive = $false
        while ((Get-Date) -lt $restartDeadline) {
            Start-Sleep -Milliseconds 750
            $workerState = Get-WorkerLockState -Path $LockFile
            if ($workerState.Running) {
                $restartAlive = $true
                break
            }
        }

        if ($restartAlive) {
            continue
        }

        Remove-Item -Force $LockFile -ErrorAction SilentlyContinue
    }
} finally {
    if ($hasMutex) {
        try {
            $mutex.ReleaseMutex()
        } catch {
        }
    }
    $mutex.Dispose()
}
