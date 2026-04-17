param(
    [Parameter(Mandatory = $true)]
    [string]$TaskFile,
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
$python = Resolve-Phase1PythonPath -ProjectRoot $projectRoot
$runtimeDir = Join-Path $projectRoot "runtime"
$tempDir = Join-Path $runtimeDir "tmp"
$lockFile = Join-Path $runtimeDir "current-worker.json"
$workerStdout = Join-Path $runtimeDir "worker.out.log"
$workerStderr = Join-Path $runtimeDir "worker.err.log"
$pendingQueueDir = Join-Path $runtimeDir "queue\pending"
$routerScript = Join-Path $projectRoot "app\phase1_router_queue.py"
$workerScript = Join-Path $projectRoot "app\phase1_worker.py"
$watchdogScriptPath = Join-Path $projectRoot "scripts\Watch-Phase1Worker.ps1"
$watchdogStdout = Join-Path $runtimeDir "watchdog.out.log"
$watchdogStderr = Join-Path $runtimeDir "watchdog.err.log"
$repairCodexConfig = Join-Path $projectRoot "scripts\Repair-CodexConfig.ps1"

function Get-WorkerLockState {
    param([string]$LockPath)

    $state = [pscustomobject]@{
        Running = $false
        Pid = ""
    }

    if (-not (Test-Path $LockPath)) {
        return $state
    }

    try {
        $lock = Get-Content -Raw -Encoding UTF8 $LockPath | ConvertFrom-Json
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

    Remove-Item -Force $LockPath -ErrorAction SilentlyContinue
    return $state
}

function Get-ProjectScopedWatchdogMutexName {
    param([string]$RootPath)

    $normalized = if ([string]::IsNullOrWhiteSpace($RootPath)) { "phase1-default" } else { $RootPath.ToLowerInvariant() }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
        $hash = ($sha256.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
        return ("Local\Phase1Watchdog-" + $hash)
    } finally {
        $sha256.Dispose()
    }
}

function Invoke-Router {
    param(
        [string]$PythonPath,
        [string]$ScriptPath,
        [string]$TaskPath,
        [string]$ConfigPath,
        [string]$TempPath,
        [string]$WorkingDirectory,
        [int]$TimeoutSeconds = 60
    )

    $taskStem = [IO.Path]::GetFileNameWithoutExtension($TaskPath)
    $stdoutPath = Join-Path $TempPath ($taskStem + ".router.stdout.log")
    $stderrPath = Join-Path $TempPath ($taskStem + ".router.stderr.log")
    Remove-Item -Force $stdoutPath, $stderrPath -ErrorAction SilentlyContinue

    $proc = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @($ScriptPath, "--task-file", $TaskPath, "--config", $ConfigPath) `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

    if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
        & taskkill /PID $proc.Id /T /F | Out-Null
        $stderr = if (Test-Path $stderrPath) { Get-Content -Raw -Encoding UTF8 $stderrPath } else { "" }
        throw ("Router timed out after {0}s. {1}" -f $TimeoutSeconds, $stderr.Trim())
    }

    $stdout = if (Test-Path $stdoutPath) { Get-Content -Raw -Encoding UTF8 $stdoutPath } else { "" }
    $stderr = if (Test-Path $stderrPath) { Get-Content -Raw -Encoding UTF8 $stderrPath } else { "" }
    if ($proc.ExitCode -ne 0 -and [string]::IsNullOrWhiteSpace($stdout)) {
        throw ("Router failed with exit code {0}. {1}" -f $proc.ExitCode, $stderr.Trim())
    }

    return $stdout
}

function Start-Phase1Watchdog {
    param([string]$MutexName)

    if (-not (Test-Path $watchdogScriptPath)) {
        throw "Watchdog script not found: $watchdogScriptPath"
    }

    $psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $proc = Start-Process -FilePath $psExe -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $watchdogScriptPath,
        "-LockFile", $lockFile,
        "-PendingQueueDir", $pendingQueueDir,
        "-TempDir", $tempDir,
        "-PythonPath", $python,
        "-WorkerScript", $workerScript,
        "-ConfigPath", $Config,
        "-ProjectRoot", $projectRoot,
        "-WorkerStdout", $workerStdout,
        "-WorkerStderr", $workerStderr,
        "-MutexName", $MutexName
    ) -RedirectStandardOutput $watchdogStdout -RedirectStandardError $watchdogStderr -WindowStyle Hidden -PassThru

    Start-Sleep -Seconds 2
    if ($proc.HasExited) {
        $stderr = if (Test-Path $watchdogStderr) { Get-Content -Raw -Encoding UTF8 $watchdogStderr } else { "" }
        if ($proc.ExitCode -eq 0 -and [string]::IsNullOrWhiteSpace($stderr)) {
            return $null
        }
        throw ("Watchdog failed to stay alive. {0}" -f $stderr.Trim())
    }
    return $proc
}

if (-not (Test-Path $python)) {
    throw "Python runtime not found. Set PHASE1_PYTHON_PATH or create a repo-local .venv."
}

if (-not (Test-Path $TaskFile)) {
    throw "Task file not found: $TaskFile"
}

$TaskFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TaskFile)

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
foreach ($dir in @("queue", "queue\pending", "queue\processing", "control", "control\locks", "control\message-ids")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir $dir) | Out-Null
}
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:TMPDIR = $tempDir
$env:PYTHONIOENCODING = "utf-8"

if (Test-Path $repairCodexConfig) {
    & $repairCodexConfig | Out-Null
}

$routeJson = Invoke-Router `
    -PythonPath $python `
    -ScriptPath $routerScript `
    -TaskPath $TaskFile `
    -ConfigPath $Config `
    -TempPath $tempDir `
    -WorkingDirectory $projectRoot `
    -TimeoutSeconds 60
if (-not $routeJson) {
    throw "Router returned empty output."
}

$route = $routeJson | ConvertFrom-Json

Write-Output ("ACTION=" + [string]$route.action)
if ($route.task_id) { Write-Output ("TASK_ID=" + [string]$route.task_id) }
if ($route.project_id) { Write-Output ("PROJECT_ID=" + [string]$route.project_id) }
if ($route.session_key) { Write-Output ("SESSION_KEY=" + [string]$route.session_key) }
if ($route.session_id) { Write-Output ("SESSION_ID=" + [string]$route.session_id) }
if ($route.reply_text) {
    $replyText = [string]$route.reply_text
    $replyPreview = $replyText -replace "(\r\n|\n|\r).*", ""
    $replyBytes = [System.Text.Encoding]::UTF8.GetBytes($replyText)
    Write-Output ("REPLY=" + $replyPreview)
    Write-Output ("REPLY_B64=" + [Convert]::ToBase64String($replyBytes))
    if ($replyPreview -ne $replyText) {
        Write-Output "REPLY_MULTILINE=1"
    }
}
if ($route.queue_depth -ne $null) { Write-Output ("QUEUE_DEPTH=" + [string]$route.queue_depth) }

if ($route.action -eq "error") {
    $errorMessage = if ($route.reply_text) { [string]$route.reply_text } else { "Router returned action=error." }
    Write-Error $errorMessage
    exit 1
}

if ($route.action -ne "enqueued") {
    exit 0
}

$watchdogMutexName = Get-ProjectScopedWatchdogMutexName -RootPath $projectRoot
$mutex = New-Object System.Threading.Mutex($false, $watchdogMutexName)
$hasMutex = $false
$workerState = $null
$workerProc = $null

try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }

    if (-not $hasMutex) {
        Write-Output "LAUNCH_RESULT=QUEUED"
        exit 0
    }

    $workerState = Get-WorkerLockState -LockPath $lockFile
    if ($workerState.Running) {
        Write-Output "WORKER_STATE=running"
        Write-Output ("WORKER_PID=" + [string]$workerState.Pid)
    } else {
        $workerProc = Start-Process `
            -FilePath $python `
            -ArgumentList @($workerScript, "--config", $Config) `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput $workerStdout `
            -RedirectStandardError $workerStderr `
            -WindowStyle Hidden `
            -PassThru

        $lockPayload = @{
            pid = $workerProc.Id
            phase = "booting"
            active_task_id = $route.task_id
            project_id = $route.project_id
            session_key = $route.session_key
            session_id = $route.session_id
            started_at = (Get-Date).ToString("s")
            worker_stdout_log = $workerStdout
            worker_stderr_log = $workerStderr
        } | ConvertTo-Json -Depth 4
        Set-Content -Path $lockFile -Value $lockPayload -Encoding UTF8

        Write-Output "WORKER_STATE=started"
        Write-Output ("WORKER_PID=" + [string]$workerProc.Id)
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

$watchdogProc = Start-Phase1Watchdog -MutexName $watchdogMutexName
if ($watchdogProc) {
    Write-Output ("WATCHDOG_PID=" + [string]$watchdogProc.Id)
}
Write-Output ("LAUNCH_RESULT=" + $(if ($workerProc) { "STARTED" } else { "QUEUED" }))
