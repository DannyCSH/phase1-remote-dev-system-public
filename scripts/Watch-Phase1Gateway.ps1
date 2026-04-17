param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PidFile = "",
    [string]$WatchdogPidFile = "",
    [string]$MutexName = "Local\Phase1GatewayWatchdog",
    [int]$CheckIntervalSeconds = 20,
    [int]$StartupGraceSeconds = 90,
    [int]$RestartAfterUnhealthySeconds = 75,
    [int]$RestartCooldownSeconds = 45,
    [int]$MaxRestartCount = 6,
    [string]$RecoveryStateFile = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $PidFile) {
    $PidFile = Join-Path $ProjectRoot "runtime\gateway.pid"
}
if (-not $WatchdogPidFile) {
    $WatchdogPidFile = Join-Path $ProjectRoot "runtime\gateway-watchdog.pid"
}
if (-not $RecoveryStateFile) {
    $RecoveryStateFile = Join-Path $ProjectRoot "runtime\gateway-recovery.json"
}

$startScript = Join-Path $ProjectRoot "scripts\Start-Phase1Gateway.ps1"
$stopScript = Join-Path $ProjectRoot "scripts\Stop-Phase1Gateway.ps1"

function Get-ProcessTreePids {
    param([int]$RootPid)

    $collected = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootPid)

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($collected.Contains($current)) {
            continue
        }
        $collected.Add($current)

        $children = Get-CimInstance Win32_Process -Filter ("ParentProcessId = " + $current) -ErrorAction SilentlyContinue
        foreach ($child in @($children)) {
            if ($child.ProcessId) {
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
    }

    return @($collected)
}

function Get-GatewayHealth {
    param([string]$GatewayPidFile)

    $result = [pscustomobject]@{
        State = "stopped"
        Pid = 0
        StartedAt = $null
        RemoteState = "none"
        RemoteEndpoint = ""
        Healthy = $false
        DegradedReason = ""
    }

    if (-not (Test-Path $GatewayPidFile)) {
        $result.DegradedReason = "gateway_not_running"
        return $result
    }

    $rawPid = (Get-Content -Raw -Encoding UTF8 $GatewayPidFile -ErrorAction SilentlyContinue).Trim()
    if ($rawPid -notmatch "^\d+$") {
        $result.DegradedReason = "invalid_pid_file"
        return $result
    }

    $gatewayPid = [int]$rawPid
    $gatewayProc = Get-Process -Id $gatewayPid -ErrorAction SilentlyContinue
    if (-not $gatewayProc) {
        Remove-Item -Force $GatewayPidFile -ErrorAction SilentlyContinue
        $result.State = "stale-pid"
        $result.DegradedReason = "stale_pid"
        return $result
    }

    $result.State = "running"
    $result.Pid = $gatewayPid
    $result.StartedAt = $gatewayProc.StartTime

    $processIds = Get-ProcessTreePids -RootPid $gatewayPid
    foreach ($procId in $processIds) {
        $connections = Get-NetTCPConnection -OwningProcess $procId -ErrorAction SilentlyContinue
        foreach ($connection in @($connections)) {
            $remoteAddress = [string]$connection.RemoteAddress
            if ([string]::IsNullOrWhiteSpace($remoteAddress)) {
                continue
            }
            if ($remoteAddress -in @("0.0.0.0", "127.0.0.1", "::", "::1")) {
                continue
            }

            $result.RemoteState = [string]$connection.State
            $result.RemoteEndpoint = ($remoteAddress + ":" + [string]$connection.RemotePort)
            if ([string]$connection.State -eq "Established") {
                $result.Healthy = $true
                $result.DegradedReason = ""
                return $result
            }
        }
    }

    if ($result.RemoteState -eq "CloseWait") {
        $result.DegradedReason = "qq_connection_closewait"
    } elseif ($result.RemoteEndpoint) {
        $result.DegradedReason = "pid_exists_but_child_connection_missing"
    } else {
        $result.DegradedReason = "qq_connection_missing"
    }
    return $result
}

function Read-RecoveryState {
    param([string]$Path)

    $defaultState = [ordered]@{
        last_check_at = ""
        last_healthy_at = ""
        last_restart_at = ""
        last_restart_reason = ""
        last_gateway_state = "unknown"
        last_remote_state = "none"
        last_remote_endpoint = ""
        consecutive_restart_count = 0
        hard_failed = $false
    }

    if (-not (Test-Path $Path)) {
        return $defaultState
    }

    try {
        $payload = Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
        if (-not $payload) {
            return $defaultState
        }
        foreach ($key in @($defaultState.Keys)) {
            if ($payload.PSObject.Properties[$key]) {
                $defaultState[$key] = $payload.$key
            }
        }
        return $defaultState
    } catch {
        return $defaultState
    }
}

function Write-RecoveryState {
    param(
        [string]$Path,
        [hashtable]$State
    )

    $json = $State | ConvertTo-Json -Depth 6
    Set-Content -Path $Path -Value $json -Encoding UTF8
}

function Start-Gateway {
    & $startScript -Config $ConfigPath | Out-Null
}

function Restart-Gateway {
    & $stopScript | Out-Null
    Start-Sleep -Seconds 2
    Start-Gateway
}

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasMutex = $false
$recoveryState = Read-RecoveryState -Path $RecoveryStateFile
$lastHealthyAt = Try {
    if ($recoveryState.last_healthy_at) { [datetime]$recoveryState.last_healthy_at } else { $null }
} Catch {
    $null
}

try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }

    if (-not $hasMutex) {
        exit 0
    }

    Set-Content -Path $WatchdogPidFile -Value $PID -Encoding UTF8

    while ($true) {
        $now = Get-Date
        $health = Get-GatewayHealth -GatewayPidFile $PidFile
        $recoveryState.last_check_at = $now.ToString("s")
        $recoveryState.last_gateway_state = $health.State
        $recoveryState.last_remote_state = $health.RemoteState
        $recoveryState.last_remote_endpoint = $health.RemoteEndpoint
        $recoveryState.last_degraded_reason = $health.DegradedReason

        if ($health.Healthy) {
            $lastHealthyAt = $now
            $recoveryState.last_healthy_at = $now.ToString("s")
            $recoveryState.consecutive_restart_count = 0
            $recoveryState.hard_failed = $false
        } elseif ($health.State -eq "stopped" -or $health.State -eq "stale-pid") {
            $lastRestartAt = Try {
                if ($recoveryState.last_restart_at) { [datetime]$recoveryState.last_restart_at } else { $null }
            } Catch {
                $null
            }
            $cooldownExpired = (-not $lastRestartAt) -or (($now - $lastRestartAt).TotalSeconds -ge $RestartCooldownSeconds)
            if (-not $recoveryState.hard_failed -and $cooldownExpired) {
                Start-Gateway
                $recoveryState.last_restart_at = $now.ToString("s")
                $recoveryState.last_restart_reason = if ($health.State -eq "stale-pid") { "stale_pid" } else { "gateway_not_running" }
                $recoveryState.consecutive_restart_count = [int]$recoveryState.consecutive_restart_count + 1
                if ([int]$recoveryState.consecutive_restart_count -ge $MaxRestartCount) {
                    $recoveryState.hard_failed = $true
                }
            }
            Write-RecoveryState -Path $RecoveryStateFile -State $recoveryState
            Start-Sleep -Seconds ([Math]::Max(8, $CheckIntervalSeconds))
            continue
        } elseif ($health.State -eq "running") {
            $startedAt = if ($health.StartedAt) { [datetime]$health.StartedAt } else { $null }
            $secondsSinceStart = if ($startedAt) { ($now - $startedAt).TotalSeconds } else { 0 }
            $secondsSinceHealthy = if ($lastHealthyAt) { ($now - $lastHealthyAt).TotalSeconds } else { [double]::PositiveInfinity }

            $startupWindowExpired = $secondsSinceStart -ge $StartupGraceSeconds
            $healthyWindowExpired = $secondsSinceHealthy -ge $RestartAfterUnhealthySeconds

            if ($startupWindowExpired -and $healthyWindowExpired) {
                $lastRestartAt = Try {
                    if ($recoveryState.last_restart_at) { [datetime]$recoveryState.last_restart_at } else { $null }
                } Catch {
                    $null
                }
                $cooldownExpired = (-not $lastRestartAt) -or (($now - $lastRestartAt).TotalSeconds -ge $RestartCooldownSeconds)
                if (-not $recoveryState.hard_failed -and $cooldownExpired) {
                    Restart-Gateway
                    $recoveryState.last_restart_at = $now.ToString("s")
                    $recoveryState.last_restart_reason = $health.DegradedReason
                    $recoveryState.consecutive_restart_count = [int]$recoveryState.consecutive_restart_count + 1
                    if ([int]$recoveryState.consecutive_restart_count -ge $MaxRestartCount) {
                        $recoveryState.hard_failed = $true
                    }
                    $lastHealthyAt = $null
                }
                Write-RecoveryState -Path $RecoveryStateFile -State $recoveryState
                Start-Sleep -Seconds ([Math]::Max(8, $CheckIntervalSeconds))
                continue
            }
        }

        Write-RecoveryState -Path $RecoveryStateFile -State $recoveryState
        Start-Sleep -Seconds ([Math]::Max(5, $CheckIntervalSeconds))
    }
} finally {
    Remove-Item -Force $WatchdogPidFile -ErrorAction SilentlyContinue
    if ($hasMutex) {
        try {
            $mutex.ReleaseMutex()
        } catch {
        }
    }
    $mutex.Dispose()
}
