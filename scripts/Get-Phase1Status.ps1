param(
    [switch]$WriteRuntimeHealth
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
$runtimeDir = Join-Path $projectRoot "runtime"
$pidFile = Join-Path $runtimeDir "gateway.pid"
$lockFile = Join-Path $runtimeDir "current-worker.json"
$adminRelayLock = Join-Path $runtimeDir "current-admin-relay.json"
$configPath = Resolve-Phase1Path -Path (Get-Phase1DefaultConfigPath -ProjectRoot $projectRoot) -ProjectRoot $projectRoot
$healthPath = Join-Path $runtimeDir "health.json"
$gatewayRecoveryPath = Join-Path $runtimeDir "gateway-recovery.json"
$codexPluginRoot = Get-Phase1CodexPluginRoot -ProjectRoot $projectRoot
$codexCompanion = Get-Phase1CodexCompanionPath -ProjectRoot $projectRoot
$adminRelayTaskName = "Phase1AdminRelay"
$adminHostTaskName = "CodexAdminHost"
$autoStartTaskName = "Phase1AutoStart"

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AliveProcess {
    param([int]$PidValue)
    if ($PidValue -le 0) {
        return $null
    }
    return Get-Process -Id $PidValue -ErrorAction SilentlyContinue
}

function Get-ProcessTreePids {
    param([int]$RootPid)

    if ($RootPid -le 0) {
        return @()
    }

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

function Get-GatewayConnectionInfo {
    param([int]$GatewayPid)

    $info = [ordered]@{
        state = "none"
        remote_endpoint = ""
        healthy = $false
    }

    foreach ($procId in (Get-ProcessTreePids -RootPid $GatewayPid)) {
        $connections = Get-NetTCPConnection -OwningProcess $procId -ErrorAction SilentlyContinue
        foreach ($connection in @($connections)) {
            $remoteAddress = [string]$connection.RemoteAddress
            if ([string]::IsNullOrWhiteSpace($remoteAddress)) {
                continue
            }
            if ($remoteAddress -in @("0.0.0.0", "127.0.0.1", "::", "::1")) {
                continue
            }

            $info.state = [string]$connection.State
            $info.remote_endpoint = ($remoteAddress + ":" + [string]$connection.RemotePort)
            if ([string]$connection.State -eq "Established") {
                $info.healthy = $true
                return [pscustomobject]$info
            }
        }
    }

    return [pscustomobject]$info
}

function Try-ParseDateValue {
    param($Value)

    if ($null -eq $Value) {
        return $null
    }

    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }

    $parsed = [datetime]::MinValue
    if ([datetime]::TryParse($text, [ref]$parsed)) {
        return $parsed
    }
    return $null
}

function Get-NewerTimestampText {
    param(
        [string]$Current,
        [string]$Candidate
    )

    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return $Current
    }
    if ([string]::IsNullOrWhiteSpace($Current)) {
        return $Candidate
    }

    $currentDate = Try-ParseDateValue -Value $Current
    $candidateDate = Try-ParseDateValue -Value $Candidate
    if ($null -eq $currentDate) {
        return $Candidate
    }
    if ($null -eq $candidateDate) {
        return $Current
    }
    if ($candidateDate -gt $currentDate) {
        return $Candidate
    }
    return $Current
}

function Add-UniqueText {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return
    }
    if (-not $List.Contains($Value)) {
        $List.Add($Value)
    }
}

function Add-HealthIssue {
    param(
        [System.Collections.Generic.List[object]]$Issues,
        [string]$Code,
        [string]$Severity,
        [string]$Message
    )

    $Issues.Add([pscustomobject]@{
        code = $Code
        severity = $Severity
        message = $Message
    })
}

function Read-JsonFile {
    param([string]$Path)

    $candidatePaths = @($Path)
    $backupPath = $Path + ".bak"
    if (Test-Path $backupPath) {
        $candidatePaths += $backupPath
    }

    foreach ($candidate in $candidatePaths) {
        if (-not (Test-Path $candidate)) {
            continue
        }
        try {
            return Get-Content -Raw -Encoding UTF8 $candidate | ConvertFrom-Json
        } catch {
            continue
        }
    }
    return $null
}

function Get-JsonPropValue {
    param(
        $Object,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) {
            return $Object[$Name]
        }
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function Get-ReceiptAwarePropValue {
    param(
        $Payload,
        [string]$Name,
        $Default = ""
    )

    $value = Get-JsonPropValue $Payload $Name $null
    if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
        return $value
    }

    $receipt = Get-JsonPropValue $Payload "receipt"
    $value = Get-JsonPropValue $receipt $Name $null
    if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
        return $value
    }

    if ($Name -eq "failure_category") {
        $errorPayload = Get-JsonPropValue $receipt "error"
        $value = Get-JsonPropValue $errorPayload "failure_category" $null
        if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
            return $value
        }
    } elseif ($Name -eq "error") {
        $errorPayload = Get-JsonPropValue $receipt "error"
        $value = Get-JsonPropValue $errorPayload "message" $null
        if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
            return $value
        }
    }

    return $Default
}

function New-BusinessOutcomeIssue {
    param(
        [string]$Code,
        [string]$Severity,
        [string]$Message,
        [string]$Recommendation
    )

    return [pscustomobject]@{
        code = $Code
        severity = $Severity
        message = $Message
        recommendation = $Recommendation
    }
}

function Get-BusinessOutcomeIssue {
    param(
        [string]$SourceName,
        [string]$TaskId = "",
        [string]$Phase = "",
        [string]$ReplyCode = "",
        [string]$FailureCategory = "",
        [string]$Detail = ""
    )

    $phaseText = [string]$Phase
    $replyText = [string]$ReplyCode
    $failureText = [string]$FailureCategory
    $detailText = ([string]$Detail).Trim()
    $sourceLabel = if ($TaskId) { "$SourceName ($TaskId)" } else { $SourceName }
    $detailSuffix = if ($detailText) { " Detail: $detailText" } else { "" }
    $taskHint = if ($TaskId) { "runtime\\tasks\\$TaskId\\status.json" } else { "the latest task status under runtime\\tasks" }

    if ($failureText -eq "qq_api_error" -or $replyText -eq "completed_with_delivery_warning") {
        return New-BusinessOutcomeIssue `
            -Code "qq_delivery_warning" `
            -Severity "high" `
            -Message "$sourceLabel completed, but QQ delivery returned an API or delivery warning.$detailSuffix" `
            -Recommendation "Inspect $taskHint and verify QQ upload limits, delivery permissions, and artifact size before customer release."
    }

    if ($failureText -eq "artifact_send_failed") {
        return New-BusinessOutcomeIssue `
            -Code "artifact_send_failed" `
            -Severity "high" `
            -Message "$sourceLabel completed, but artifact delivery back to QQ failed.$detailSuffix" `
            -Recommendation "Inspect $taskHint and reduce/split artifacts before retrying delivery."
    }

    if (
        $phaseText -eq "failed" -or
        $failureText -in @("gateway_unavailable", "router_failed", "worker_failed", "admin_relay_failed", "environment_invalid", "timeout")
    ) {
        $issueCode = if ($failureText) { $failureText } else { "latest_task_failed" }
        return New-BusinessOutcomeIssue `
            -Code $issueCode `
            -Severity "critical" `
            -Message "$sourceLabel ended in a failed business outcome.$detailSuffix" `
            -Recommendation "Inspect $taskHint plus gateway/worker/admin logs before treating this environment as customer-ready."
    }

    if ($failureText -eq "unauthorized_sender") {
        return New-BusinessOutcomeIssue `
            -Code "unauthorized_sender" `
            -Severity "high" `
            -Message "$sourceLabel was blocked because the sender is not in allowFrom.$detailSuffix" `
            -Recommendation "Update channels.qq.allowFrom or retry from an authorized QQ account."
    }

    if ($failureText -eq "duplicate") {
        return New-BusinessOutcomeIssue `
            -Code "duplicate_message" `
            -Severity "medium" `
            -Message "$sourceLabel was treated as a duplicate inbound message.$detailSuffix" `
            -Recommendation "Review runtime\\control\\message-ids if duplicate suppression is firing unexpectedly."
    }

    if ($failureText -eq "stopped") {
        return New-BusinessOutcomeIssue `
            -Code "task_stopped" `
            -Severity "medium" `
            -Message "$sourceLabel was stopped before a normal completion.$detailSuffix" `
            -Recommendation "Review runtime\\control\\stop and recent session history if this stop was not intentional."
    }

    if ($replyText -and $replyText -notin @("completed", "running", "queued", "sending_local_file")) {
        return New-BusinessOutcomeIssue `
            -Code "unexpected_reply_code" `
            -Severity "high" `
            -Message "$sourceLabel ended with unexpected reply_code '$replyText'.$detailSuffix" `
            -Recommendation "Inspect $taskHint to confirm the task produced a full receipt and a customer-visible result."
    }

    return $null
}

function Get-SafeBucketName {
    param([string]$Text, [string]$Fallback = "bucket")
    $cleaned = [regex]::Replace(([string]$Text).Trim(), "[^\w\-\.]+", "_")
    $cleaned = $cleaned.Trim("._")
    if ([string]::IsNullOrWhiteSpace($cleaned)) {
        return $Fallback
    }
    return $cleaned
}

function Get-JsonStateFiles {
    param(
        [string]$RootPath,
        [string]$PrimaryName
    )
    if (-not (Test-Path $RootPath)) {
        return @()
    }

    $files = @()
    $files += Get-ChildItem -Path $RootPath -Recurse -Filter $PrimaryName -File -ErrorAction SilentlyContinue
    $files += Get-ChildItem -Path $RootPath -Recurse -Filter ($PrimaryName + ".bak") -File -ErrorAction SilentlyContinue
    return $files | Sort-Object FullName -Unique
}

function Get-TaskStatusById {
    param(
        [string]$TaskId,
        [string[]]$PreferredScopes = @()
    )
    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        return $null
    }

    $scopes = @()
    foreach ($preferredScope in $PreferredScopes) {
        if ($preferredScope -and $preferredScope -notin $scopes) {
            $scopes += $preferredScope
        }
    }
    foreach ($scope in @("tasks", "admin-tasks")) {
        if ($scope -notin $scopes) {
            $scopes += $scope
        }
    }

    foreach ($scope in $scopes) {
        $statusPath = Join-Path $runtimeDir (Join-Path $scope (Join-Path $TaskId "status.json"))
        $payload = Read-JsonFile -Path $statusPath
        if (-not $payload) {
            continue
        }
        $updatedAt = Get-JsonPropValue $payload "finished_at"
        if ([string]::IsNullOrWhiteSpace([string]$updatedAt)) {
            $timestampPath = if (Test-Path $statusPath) { $statusPath } elseif (Test-Path ($statusPath + ".bak")) { ($statusPath + ".bak") } else { $null }
            if ($timestampPath) {
                $updatedAt = (Get-Item $timestampPath).LastWriteTime.ToString("s")
            }
        }
        return [pscustomobject]@{
            path = $statusPath
            task_scope = if ($scope -eq "admin-tasks") { "admin-relay" } else { "task" }
            task_id = Get-JsonPropValue $payload "task_id"
            phase = Get-JsonPropValue $payload "phase"
            task_name = Get-JsonPropValue $payload "task_name"
            project_id = Get-JsonPropValue $payload "project_id"
            session_id = Get-JsonPropValue $payload "session_id"
            result = Get-JsonPropValue $payload "result"
            error = Get-ReceiptAwarePropValue $payload "error"
            reply_code = Get-ReceiptAwarePropValue $payload "reply_code"
            failure_category = Get-ReceiptAwarePropValue $payload "failure_category"
            user_visible_status = Get-ReceiptAwarePropValue $payload "user_visible_status"
            updated_at = $updatedAt
        }
    }
    return $null
}

function Get-LatestTaskStatus {
    $statusFiles = @()
    foreach ($candidateDir in @(
        (Join-Path $runtimeDir "tasks"),
        (Join-Path $runtimeDir "admin-tasks")
    )) {
        $statusFiles += Get-JsonStateFiles -RootPath $candidateDir -PrimaryName "status.json"
    }
    if (-not $statusFiles) {
        return $null
    }
    $statusFile = $statusFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $statusFile) {
        return $null
    }
    $payload = Read-JsonFile -Path $statusFile.FullName
    if (-not $payload) {
        return $null
    }
    return [pscustomobject]@{
        path = $statusFile.FullName
        task_scope = if ($statusFile.FullName -like "*\admin-tasks\*") { "admin-relay" } else { "task" }
        task_id = Get-JsonPropValue $payload "task_id"
        phase = Get-JsonPropValue $payload "phase"
        task_name = Get-JsonPropValue $payload "task_name"
        project_id = Get-JsonPropValue $payload "project_id"
        session_id = Get-JsonPropValue $payload "session_id"
        result = Get-JsonPropValue $payload "result"
        error = Get-ReceiptAwarePropValue $payload "error"
        reply_code = Get-ReceiptAwarePropValue $payload "reply_code"
        failure_category = Get-ReceiptAwarePropValue $payload "failure_category"
        user_visible_status = Get-ReceiptAwarePropValue $payload "user_visible_status"
        updated_at = $statusFile.LastWriteTime.ToString("s")
    }
}

function Get-SessionStateByKey {
    param([string]$SessionKey)
    if ([string]::IsNullOrWhiteSpace($SessionKey)) {
        return $null
    }
    $path = Join-Path (Join-Path $runtimeDir "sessions") (Join-Path (Get-SafeBucketName -Text $SessionKey -Fallback "session") "state.json")
    $payload = Read-JsonFile -Path $path
    if (-not $payload) {
        return $null
    }
    return [pscustomobject]@{
        path = $path
        session_key = Get-JsonPropValue $payload "session_key"
        current_project_id = Get-JsonPropValue $payload "current_project_id"
        current_project_root = Get-JsonPropValue $payload "current_project_root"
        current_session_id = Get-JsonPropValue $payload "current_session_id"
        active_task_id = Get-JsonPropValue $payload "active_task_id"
        last_task_id = Get-JsonPropValue $payload "last_task_id"
        last_result = Get-JsonPropValue $payload "last_result"
        last_reply_code = Get-JsonPropValue $payload "last_reply_code"
        last_failure_category = Get-JsonPropValue $payload "last_failure_category"
        last_task_finished_at = Get-JsonPropValue $payload "last_task_finished_at"
        last_inbound_at = Get-JsonPropValue $payload "last_inbound_at"
        updated_at = Get-JsonPropValue $payload "updated_at"
    }
}

function Get-SessionStateById {
    param(
        [string]$SessionId,
        [string]$SessionKey = ""
    )
    if ([string]::IsNullOrWhiteSpace($SessionId)) {
        return $null
    }

    $sessionStates = Get-ChildItem -Path (Join-Path $runtimeDir "sessions") -Recurse -Filter "state.json" -File -ErrorAction SilentlyContinue
    if (-not $sessionStates) {
        return $null
    }

    $candidates = foreach ($sessionState in $sessionStates) {
        $payload = Read-JsonFile -Path $sessionState.FullName
        if (-not $payload) {
            continue
        }
        if ([string](Get-JsonPropValue $payload "current_session_id") -ne $SessionId) {
            continue
        }
        if ($SessionKey -and [string](Get-JsonPropValue $payload "session_key") -ne $SessionKey) {
            continue
        }
        [pscustomobject]@{
            path = $sessionState.FullName
            session_key = Get-JsonPropValue $payload "session_key"
            current_project_id = Get-JsonPropValue $payload "current_project_id"
            current_project_root = Get-JsonPropValue $payload "current_project_root"
            current_session_id = Get-JsonPropValue $payload "current_session_id"
            active_task_id = Get-JsonPropValue $payload "active_task_id"
            last_task_id = Get-JsonPropValue $payload "last_task_id"
            last_result = Get-JsonPropValue $payload "last_result"
            last_reply_code = Get-JsonPropValue $payload "last_reply_code"
            last_failure_category = Get-JsonPropValue $payload "last_failure_category"
            last_task_finished_at = Get-JsonPropValue $payload "last_task_finished_at"
            last_inbound_at = Get-JsonPropValue $payload "last_inbound_at"
            updated_at = Get-JsonPropValue $payload "updated_at"
            sort_time = $sessionState.LastWriteTime
        }
    }

    return ($candidates | Sort-Object sort_time -Descending | Select-Object -First 1)
}

function Get-LatestSessionState {
    $sessionState = Get-JsonStateFiles -RootPath (Join-Path $runtimeDir "sessions") -PrimaryName "state.json" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $sessionState) {
        return $null
    }
    $payload = Read-JsonFile -Path $sessionState.FullName
    if (-not $payload) {
        return $null
    }
    return [pscustomobject]@{
        path = $sessionState.FullName
        session_key = Get-JsonPropValue $payload "session_key"
        current_project_id = Get-JsonPropValue $payload "current_project_id"
        current_project_root = Get-JsonPropValue $payload "current_project_root"
        current_session_id = Get-JsonPropValue $payload "current_session_id"
        active_task_id = Get-JsonPropValue $payload "active_task_id"
        last_task_id = Get-JsonPropValue $payload "last_task_id"
        last_result = Get-JsonPropValue $payload "last_result"
        last_reply_code = Get-JsonPropValue $payload "last_reply_code"
        last_failure_category = Get-JsonPropValue $payload "last_failure_category"
        last_task_finished_at = Get-JsonPropValue $payload "last_task_finished_at"
        last_inbound_at = Get-JsonPropValue $payload "last_inbound_at"
        updated_at = Get-JsonPropValue $payload "updated_at"
    }
}

function Get-ProjectState {
    param(
        [string]$ProjectId,
        [string]$SessionKey = "",
        [string]$SessionId = ""
    )

    $projectStates = Get-JsonStateFiles -RootPath (Join-Path $runtimeDir "projects") -PrimaryName "state.json"
    if (-not $projectStates) {
        return $null
    }

    $candidates = foreach ($projectState in $projectStates) {
        $payload = Read-JsonFile -Path $projectState.FullName
        if (-not $payload) {
            continue
        }
        if ($ProjectId -and [string](Get-JsonPropValue $payload "project_id") -ne $ProjectId) {
            continue
        }
        [pscustomobject]@{
            path = $projectState.FullName
            project_id = Get-JsonPropValue $payload "project_id"
            project_root = Get-JsonPropValue $payload "project_root"
            current_session_key = Get-JsonPropValue $payload "current_session_key"
            current_session_id = Get-JsonPropValue $payload "current_session_id"
            last_task_id = Get-JsonPropValue $payload "last_task_id"
            last_result = Get-JsonPropValue $payload "last_result"
            last_reply_code = Get-JsonPropValue $payload "last_reply_code"
            last_failure_category = Get-JsonPropValue $payload "last_failure_category"
            last_task_finished_at = Get-JsonPropValue $payload "last_task_finished_at"
            updated_at = Get-JsonPropValue $payload "updated_at"
            sort_time = $projectState.LastWriteTime
        }
    }

    if (-not $candidates) {
        return $null
    }

    if ($SessionKey -or $SessionId) {
        $matched = $candidates | Where-Object {
            ((-not $SessionKey) -or [string]$_.current_session_key -eq $SessionKey) -and
            ((-not $SessionId) -or [string]$_.current_session_id -eq $SessionId)
        } | Sort-Object sort_time -Descending | Select-Object -First 1
        if ($matched) {
            return $matched
        }
    }

    return ($candidates | Sort-Object sort_time -Descending | Select-Object -First 1)
}

function Get-RuntimeActivitySummary {
    $summary = [ordered]@{
        last_inbound_at = ""
        last_task_finished_at = ""
    }

    $sessionStates = Get-JsonStateFiles -RootPath (Join-Path $runtimeDir "sessions") -PrimaryName "state.json"
    foreach ($sessionState in @($sessionStates)) {
        $payload = Read-JsonFile -Path $sessionState.FullName
        if (-not $payload) {
            continue
        }
        $summary.last_inbound_at = Get-NewerTimestampText -Current $summary.last_inbound_at -Candidate ([string](Get-JsonPropValue $payload "last_inbound_at"))
        $summary.last_task_finished_at = Get-NewerTimestampText -Current $summary.last_task_finished_at -Candidate ([string](Get-JsonPropValue $payload "last_task_finished_at"))
    }

    $projectStates = Get-JsonStateFiles -RootPath (Join-Path $runtimeDir "projects") -PrimaryName "state.json"
    foreach ($projectState in @($projectStates)) {
        $payload = Read-JsonFile -Path $projectState.FullName
        if (-not $payload) {
            continue
        }
        $summary.last_task_finished_at = Get-NewerTimestampText -Current $summary.last_task_finished_at -Candidate ([string](Get-JsonPropValue $payload "last_task_finished_at"))
    }

    $statusFiles = @()
    foreach ($candidateDir in @((Join-Path $runtimeDir "tasks"), (Join-Path $runtimeDir "admin-tasks"))) {
        $statusFiles += Get-JsonStateFiles -RootPath $candidateDir -PrimaryName "status.json"
    }
    foreach ($statusFile in @($statusFiles)) {
        $payload = Read-JsonFile -Path $statusFile.FullName
        if (-not $payload) {
            continue
        }
        $summary.last_task_finished_at = Get-NewerTimestampText -Current $summary.last_task_finished_at -Candidate ([string](Get-JsonPropValue $payload "finished_at"))
    }

    return [pscustomobject]$summary
}

function Get-OptionalScheduledTask {
    param([string]$TaskName)

    try {
        return Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        return $null
    }
}

$cfg = Read-Phase1JsonResolved -Path $configPath
$cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$buildNumber = [int]$cv.CurrentBuild
$ubr = [int]$cv.UBR
$fullBuild = "{0}.{1}" -f $buildNumber, $ubr
$releaseId = $cv.ReleaseId
$wslExePath = Join-Path $env:WINDIR "System32\wsl.exe"
$wslExeExists = Test-Path $wslExePath
$isElevated = Test-IsElevated
$wsl2Ready = ($buildNumber -gt 19041) -or (($buildNumber -eq 18363 -or $buildNumber -eq 18362) -and $ubr -ge 1049)

$qqEnabled = $false
$qqAllowFrom = @()
$qqMediaDir = ""
$qqModel = ""
$qqWorkspace = ""
if ($cfg) {
    $channelsCfg = Get-JsonPropValue $cfg "channels"
    $qqCfg = Get-JsonPropValue $channelsCfg "qq"
    $agentsCfg = Get-JsonPropValue $cfg "agents"
    $defaultsCfg = Get-JsonPropValue $agentsCfg "defaults"
    $phase1Cfg = Get-JsonPropValue $cfg "phase1"
    $autostartCfg = Get-JsonPropValue $phase1Cfg "autostart"

    $qqEnabled = [bool](Get-JsonPropValue $qqCfg "enabled" $false)
    $qqAllowFrom = @((Get-JsonPropValue $qqCfg "allowFrom" @()))
    $qqMediaDir = [string](Get-JsonPropValue $qqCfg "mediaDir" "")
    $qqModel = [string](Get-JsonPropValue $defaultsCfg "model" "")
    $qqWorkspace = [string](Get-JsonPropValue $defaultsCfg "workspace" "")
    $configuredTaskName = [string](Get-JsonPropValue $autostartCfg "taskName" "")
    if ($configuredTaskName) {
        $autoStartTaskName = $configuredTaskName
    }
}

$wslNote = "System prerequisites are sufficient for the next WSL installation step."
if (-not $isElevated) {
    $wslNote = "Current shell is not elevated, so WSL Windows features cannot be enabled from this session."
} elseif (-not $wsl2Ready) {
    $wslNote = "Current Windows build is below the official WSL2 minimum. WSL1 is the safe interim path."
}

$gatewayState = "stopped"
$gatewayPid = $null
$gatewayConnectionInfo = [pscustomobject]@{
    state = "none"
    remote_endpoint = ""
    healthy = $false
}
if (Test-Path $pidFile) {
    $rawPid = (Get-Content -Raw -Encoding UTF8 $pidFile -ErrorAction SilentlyContinue).Trim()
    if ($rawPid -match "^\d+$") {
        $gatewayPid = [int]$rawPid
        $gatewayProc = Get-AliveProcess -PidValue $gatewayPid
        if ($gatewayProc) {
            $gatewayState = "running"
            $gatewayConnectionInfo = Get-GatewayConnectionInfo -GatewayPid $gatewayPid
            if (-not $gatewayConnectionInfo.healthy) {
                $gatewayState = "degraded"
            }
        } else {
            $gatewayState = "stale-pid"
        }
    }
}

$workerState = "idle"
$workerInfo = $null
if (Test-Path $lockFile) {
    $lock = Read-JsonFile -Path $lockFile
    $workerPid = [int](Get-JsonPropValue $lock "pid" 0)
    if ($lock -and $workerPid -gt 0) {
        $workerProc = Get-AliveProcess -PidValue $workerPid
        $workerState = if ($workerProc) { "running" } else { "stale-lock" }
        $workerInfo = [pscustomobject]@{
            pid = $workerPid
            phase = Get-JsonPropValue $lock "phase"
            active_task_id = Get-JsonPropValue $lock "active_task_id"
            project_id = Get-JsonPropValue $lock "project_id"
            session_key = Get-JsonPropValue $lock "session_key"
            session_id = Get-JsonPropValue $lock "session_id"
            started_at = Get-JsonPropValue $lock "started_at"
            stdout_log = Get-JsonPropValue $lock "worker_stdout_log"
            stderr_log = Get-JsonPropValue $lock "worker_stderr_log"
        }
    }
}
$workerIsLive = $workerState -eq "running"

$adminRelayState = "idle"
$adminRelayInfo = $null
if (Test-Path $adminRelayLock) {
    $relay = Read-JsonFile -Path $adminRelayLock
    $relayPid = [int](Get-JsonPropValue $relay "pid" 0)
    if ($relay -and $relayPid -gt 0) {
        $adminRelayProc = Get-AliveProcess -PidValue $relayPid
        $adminRelayState = if ($adminRelayProc) { "running" } else { "stale-lock" }
        $adminRelayInfo = [pscustomobject]@{
            task_id = Get-JsonPropValue $relay "task_id"
            pid = $relayPid
            started_at = Get-JsonPropValue $relay "started_at"
        }
    }
}
$adminRelayIsLive = $adminRelayState -eq "running"

$queuePendingCount = (Get-ChildItem -Path (Join-Path $runtimeDir "queue\pending") -Filter "*.json" -File -ErrorAction SilentlyContinue | Measure-Object).Count
$queueProcessingCount = (Get-ChildItem -Path (Join-Path $runtimeDir "queue\processing") -Filter "*.json" -File -ErrorAction SilentlyContinue | Measure-Object).Count
$stopRequestCount = (Get-ChildItem -Path (Join-Path $runtimeDir "control\stop") -Filter "*.json" -File -ErrorAction SilentlyContinue | Measure-Object).Count

$activeTaskId = ""
$activeTaskScope = @()
if ($workerIsLive -and $workerInfo -and $workerInfo.active_task_id) {
    $activeTaskId = [string]$workerInfo.active_task_id
    $activeTaskScope = @("tasks")
} elseif ($adminRelayIsLive -and $adminRelayInfo -and $adminRelayInfo.task_id) {
    $activeTaskId = [string]$adminRelayInfo.task_id
    $activeTaskScope = @("admin-tasks", "tasks")
}

$latestTask = if ($activeTaskId) { Get-TaskStatusById -TaskId $activeTaskId -PreferredScopes $activeTaskScope } else { $null }
if (-not $latestTask) {
    $latestTask = Get-LatestTaskStatus
}

$latestSession = $null
if ($workerIsLive -and $workerInfo -and $workerInfo.session_key) {
    $latestSession = Get-SessionStateByKey -SessionKey $workerInfo.session_key
} elseif ($latestTask -and $latestTask.session_id) {
    $latestSession = Get-SessionStateById -SessionId ([string]$latestTask.session_id)
}
if ((-not $latestSession) -and (-not ($latestTask -and $latestTask.session_id))) {
    $latestSession = Get-LatestSessionState
}

$projectSelectorId = if ($workerIsLive -and $workerInfo -and $workerInfo.project_id) { [string]$workerInfo.project_id } elseif ($latestTask) { [string]$latestTask.project_id } else { "" }
$projectSelectorSessionKey = if ($latestSession) { [string]$latestSession.session_key } else { "" }
$projectSelectorSessionId = if ($workerIsLive -and $workerInfo -and $workerInfo.session_id) { [string]$workerInfo.session_id } elseif ($latestSession) { [string]$latestSession.current_session_id } else { "" }
$projectSelectorHasSpecificTarget = [bool]($projectSelectorId -or $projectSelectorSessionKey -or $projectSelectorSessionId)
$latestProject = Get-ProjectState -ProjectId $projectSelectorId -SessionKey $projectSelectorSessionKey -SessionId $projectSelectorSessionId
if ((-not $latestProject) -and (-not $projectSelectorHasSpecificTarget)) {
    $latestProject = Get-ProjectState -ProjectId ""
}

$runtimeActivity = Get-RuntimeActivitySummary
$gatewayRecovery = Read-JsonFile -Path $gatewayRecoveryPath

$nanobotExe = Resolve-Phase1NanobotPath -ProjectRoot $projectRoot
$nanobotExists = -not [string]::IsNullOrWhiteSpace($nanobotExe)
$nanobotDisplayPath = if ($nanobotExists) { $nanobotExe } else { "nanobot.exe" }
$claudePath = Resolve-Phase1ClaudePath
$claudeCmd = if ($claudePath) { [pscustomobject]@{ Source = $claudePath } } else { $null }
$codexCmd = Get-Command codex -ErrorAction SilentlyContinue
$adminRelayTask = Get-OptionalScheduledTask -TaskName $adminRelayTaskName
$adminHostTask = Get-OptionalScheduledTask -TaskName $adminHostTaskName
$autoStartTask = Get-OptionalScheduledTask -TaskName $autoStartTaskName

$gatewayDegradedReason = ""
if ($gatewayState -eq "stale-pid") {
    $gatewayDegradedReason = "stale_pid"
} elseif ($gatewayState -eq "degraded") {
    if ($gatewayConnectionInfo.state -eq "CloseWait") {
        $gatewayDegradedReason = "qq_connection_closewait"
    } elseif ($gatewayConnectionInfo.remote_endpoint) {
        $gatewayDegradedReason = "pid_exists_but_child_connection_missing"
    } else {
        $gatewayDegradedReason = "qq_connection_missing"
    }
}

$lastInboundAt = Get-NewerTimestampText -Current ([string]$runtimeActivity.last_inbound_at) -Candidate ([string](Get-JsonPropValue $latestSession "last_inbound_at"))
$lastTaskFinishedAt = Get-NewerTimestampText -Current ([string]$runtimeActivity.last_task_finished_at) -Candidate ([string](Get-JsonPropValue $latestSession "last_task_finished_at"))
$lastTaskFinishedAt = Get-NewerTimestampText -Current $lastTaskFinishedAt -Candidate ([string](Get-JsonPropValue $latestProject "last_task_finished_at"))
$lastTaskFinishedAt = Get-NewerTimestampText -Current $lastTaskFinishedAt -Candidate ([string](Get-JsonPropValue $latestTask "updated_at"))

$issues = New-Object 'System.Collections.Generic.List[object]'
$recommendedActions = New-Object 'System.Collections.Generic.List[string]'
$overall = "healthy"

if ($gatewayState -eq "stopped" -or $gatewayState -eq "stale-pid") {
    Add-HealthIssue -Issues $issues -Code "gateway_not_running" -Severity "critical" -Message "Gateway is not currently in a healthy running state."
    Add-UniqueText -List $recommendedActions -Value "Run Start-Phase1Stack.ps1 and confirm both NanoBot and the Gateway are running."
} elseif ($gatewayState -eq "degraded") {
    Add-HealthIssue -Issues $issues -Code ($gatewayDegradedReason -or "gateway_degraded") -Severity "high" -Message "Gateway is still running, but the QQ connection is unhealthy."
    Add-UniqueText -List $recommendedActions -Value "Check runtime\\gateway.err.log and gateway-recovery.json for fake-online or broken-connection symptoms."
}

if (-not $qqEnabled) {
    Add-HealthIssue -Issues $issues -Code "qq_disabled" -Severity "high" -Message "The QQ channel is currently disabled."
    Add-UniqueText -List $recommendedActions -Value "Enable channels.qq.enabled and confirm QQ AppId / Secret are filled in."
}

if ($qqEnabled -and (-not $qqAllowFrom -or @($qqAllowFrom).Count -eq 0)) {
    Add-HealthIssue -Issues $issues -Code "qq_allow_from_missing" -Severity "high" -Message "The QQ channel is enabled, but allowFrom is empty."
    Add-UniqueText -List $recommendedActions -Value "Add the authorized QQ OpenID, otherwise phone messages will not enter the execution path."
}

if ($queuePendingCount -gt 0 -and -not $workerIsLive) {
    Add-HealthIssue -Issues $issues -Code "worker_not_running" -Severity "high" -Message "Tasks are still queued, but the worker is not running."
    Add-UniqueText -List $recommendedActions -Value "Check runtime\\worker.err.log and restart the stack if needed."
}

if ($adminRelayState -eq "stale-lock") {
    Add-HealthIssue -Issues $issues -Code "admin_relay_stale_lock" -Severity "medium" -Message "The admin relay lock is stale, which suggests an earlier admin task exited unexpectedly."
    Add-UniqueText -List $recommendedActions -Value "Inspect runtime\\current-admin-relay.json and the latest status file under admin-tasks."
}

if (-not $nanobotExists) {
    Add-HealthIssue -Issues $issues -Code "nanobot_missing" -Severity "critical" -Message "NanoBot was not found on this machine."
    Add-UniqueText -List $recommendedActions -Value "Install NanoBot, or set PHASE1_NANOBOT_EXE to the local nanobot.exe path."
}

if (-not [bool]$claudeCmd) {
    Add-HealthIssue -Issues $issues -Code "claude_missing" -Severity "critical" -Message "Claude CLI was not found on this machine."
    Add-UniqueText -List $recommendedActions -Value "Install Claude Code CLI and confirm it is logged in."
}

if (-not [bool]$codexCmd) {
    Add-HealthIssue -Issues $issues -Code "codex_missing" -Severity "critical" -Message "Codex CLI was not found on this machine."
    Add-UniqueText -List $recommendedActions -Value "Install Codex CLI and confirm it is logged in."
}

$lastInboundDate = Try-ParseDateValue -Value $lastInboundAt
if ($qqEnabled -and $lastInboundDate -and $gatewayState -eq "degraded") {
    $idleSeconds = ((Get-Date) - $lastInboundDate).TotalSeconds
    if ($idleSeconds -ge 900) {
        Add-HealthIssue -Issues $issues -Code "no_inbound_for_too_long" -Severity "high" -Message "No recent inbound messages were seen while the Gateway stayed degraded."
        Add-UniqueText -List $recommendedActions -Value "If the phone has sent messages without acknowledgements, check last_restart_reason and last_healthy_at in gateway-recovery.json."
    }
}

$businessIssue = $null
if ($latestTask) {
    $latestTaskDetail = [string](Get-JsonPropValue $latestTask "error")
    if ([string]::IsNullOrWhiteSpace($latestTaskDetail)) {
        $latestTaskDetail = [string](Get-JsonPropValue $latestTask "result")
    }
    $businessIssue = Get-BusinessOutcomeIssue `
        -SourceName "latest task" `
        -TaskId ([string](Get-JsonPropValue $latestTask "task_id")) `
        -Phase ([string](Get-JsonPropValue $latestTask "phase")) `
        -ReplyCode ([string](Get-JsonPropValue $latestTask "reply_code")) `
        -FailureCategory ([string](Get-JsonPropValue $latestTask "failure_category")) `
        -Detail $latestTaskDetail
}
if (-not $businessIssue -and $latestSession) {
    $businessIssue = Get-BusinessOutcomeIssue `
        -SourceName "latest session state" `
        -TaskId ([string](Get-JsonPropValue $latestSession "last_task_id")) `
        -Phase "finished" `
        -ReplyCode ([string](Get-JsonPropValue $latestSession "last_reply_code")) `
        -FailureCategory ([string](Get-JsonPropValue $latestSession "last_failure_category")) `
        -Detail ([string](Get-JsonPropValue $latestSession "last_result"))
}
if (-not $businessIssue -and $latestProject) {
    $businessIssue = Get-BusinessOutcomeIssue `
        -SourceName "latest project state" `
        -TaskId ([string](Get-JsonPropValue $latestProject "last_task_id")) `
        -Phase "finished" `
        -ReplyCode ([string](Get-JsonPropValue $latestProject "last_reply_code")) `
        -FailureCategory ([string](Get-JsonPropValue $latestProject "last_failure_category")) `
        -Detail ([string](Get-JsonPropValue $latestProject "last_result"))
}
if ($businessIssue) {
    Add-HealthIssue -Issues $issues -Code $businessIssue.code -Severity $businessIssue.severity -Message $businessIssue.message
    Add-UniqueText -List $recommendedActions -Value $businessIssue.recommendation
}

if ($issues.Count -gt 0) {
    $criticalIssues = @($issues | Where-Object { $_.severity -eq "critical" })
    $overall = if ($criticalIssues.Count -gt 0) { "failed" } else { "degraded" }
}

$gatewayStatus = @{
    state = $gatewayState
    pid = $gatewayPid
    pid_file = $pidFile
    stdout_log = (Join-Path $runtimeDir "gateway.out.log")
    stderr_log = (Join-Path $runtimeDir "gateway.err.log")
    connection_state = $gatewayConnectionInfo.state
    remote_endpoint = $gatewayConnectionInfo.remote_endpoint
    degraded_reason = $gatewayDegradedReason
    recovery = $gatewayRecovery
}
$workerStatus = @{
    state = $workerState
    details = $workerInfo
}
$adminRelayStatus = @{
    state = $adminRelayState
    details = $adminRelayInfo
    task_registered = [bool]$adminRelayTask
    scheduled_task_state = $(if ($adminRelayTask) { $adminRelayTask.State.ToString() } else { "missing" })
}
$queueStatus = @{
    pending = $queuePendingCount
    processing = $queueProcessingCount
    stop_requests = $stopRequestCount
}
$toolingStatus = @{
    nanobot = @{
        exists = $nanobotExists
        path = $nanobotDisplayPath
    }
    claude = @{
        exists = [bool]$claudeCmd
        path = $(if ($claudeCmd -and $claudeCmd.Source) {
            $claudeCmd.Source
        } else {
            "claude"
        })
    }
    codex = @{
        exists = [bool]$codexCmd
        path = $(if ($codexCmd) { $codexCmd.Source } else { "" })
    }
    codex_plugin_cc = @{
        exists = (-not [string]::IsNullOrWhiteSpace($codexCompanion)) -and (Test-Path $codexCompanion)
        root = if ($codexPluginRoot) { $codexPluginRoot } else { "" }
        companion = if ($codexCompanion) { $codexCompanion } else { "" }
    }
}
$qqStatus = @{
    enabled = $qqEnabled
    allow_from = @($qqAllowFrom)
    media_dir = $qqMediaDir
    model = $qqModel
    workspace = $qqWorkspace
}
$schedulerStatus = @{
    codex_admin_host = @{
        registered = [bool]$adminHostTask
        state = $(if ($adminHostTask) { $adminHostTask.State.ToString() } else { "missing" })
    }
    phase1_admin_relay = @{
        registered = [bool]$adminRelayTask
        state = $(if ($adminRelayTask) { $adminRelayTask.State.ToString() } else { "missing" })
    }
    phase1_autostart = @{
        task_name = $autoStartTaskName
        registered = [bool]$autoStartTask
        state = $(if ($autoStartTask) { $autoStartTask.State.ToString() } else { "missing" })
    }
}
$wslStatus = @{
    current_build = $fullBuild
    release_id = $releaseId
    elevated_shell = $isElevated
    wsl_exe_exists = $wslExeExists
    wsl_exe_path = $wslExePath
    wsl2_ready = $wsl2Ready
    note = $wslNote
}
$status = @{
    overall = $overall
    issues = @($issues.ToArray())
    recommended_actions = @($recommendedActions.ToArray())
    generated_at = (Get-Date).ToString("s")
    project_root = $projectRoot
    last_inbound_at = $lastInboundAt
    last_task_finished_at = $lastTaskFinishedAt
    gateway = $gatewayStatus
    worker = $workerStatus
    admin_relay = $adminRelayStatus
    queue = $queueStatus
    current_session = $latestSession
    current_project = $latestProject
    latest_task = $latestTask
    tooling = $toolingStatus
    qq = $qqStatus
    scheduler = $schedulerStatus
    wsl = $wslStatus
}

if ($WriteRuntimeHealth) {
    $json = $status | ConvertTo-Json -Depth 8
    Set-Content -Path $healthPath -Value $json -Encoding UTF8
}

Write-Host "== Phase 1 Status =="
Write-Host
Write-Host "[Overall]"
Write-Host "Overall: $($status.overall)"
Write-Host "Last inbound: $($status.last_inbound_at)"
Write-Host "Last task finished: $($status.last_task_finished_at)"
if ($status.issues -and @($status.issues).Count -gt 0) {
    Write-Host "Issues:"
    foreach ($issue in @($status.issues)) {
        Write-Host ("- [{0}] {1}: {2}" -f $issue.severity, $issue.code, $issue.message)
    }
}
if ($status.recommended_actions -and @($status.recommended_actions).Count -gt 0) {
    Write-Host "Recommended:"
    foreach ($item in @($status.recommended_actions)) {
        Write-Host ("- " + $item)
    }
}
Write-Host
Write-Host "[Project]"
Write-Host "Root: $projectRoot"
Write-Host
Write-Host "[Gateway]"
Write-Host "State: $($status.gateway.state)"
Write-Host "PID: $($status.gateway.pid)"
Write-Host "Connection: $($status.gateway.connection_state)"
if ($status.gateway.remote_endpoint) {
    Write-Host "Remote: $($status.gateway.remote_endpoint)"
}
if ($status.gateway.degraded_reason) {
    Write-Host "Degraded reason: $($status.gateway.degraded_reason)"
}
Write-Host "STDOUT: $($status.gateway.stdout_log)"
Write-Host "STDERR: $($status.gateway.stderr_log)"
Write-Host
Write-Host "[Worker]"
Write-Host "State: $($status.worker.state)"
if ($workerInfo) {
    Write-Host "Phase: $($workerInfo.phase)"
    Write-Host "Task: $($workerInfo.active_task_id)"
    Write-Host "Project: $($workerInfo.project_id)"
    Write-Host "Session: $($workerInfo.session_id)"
    Write-Host "PID: $($workerInfo.pid)"
}
Write-Host
Write-Host "[Admin Relay]"
Write-Host "State: $($status.admin_relay.state)"
Write-Host "Task registered: $($status.admin_relay.task_registered)"
Write-Host "Scheduled task state: $($status.admin_relay.scheduled_task_state)"
Write-Host
Write-Host "[Queue]"
Write-Host "Pending: $($status.queue.pending)"
Write-Host "Processing: $($status.queue.processing)"
Write-Host "Stop requests: $($status.queue.stop_requests)"
Write-Host
Write-Host "[Current Session]"
if ($latestSession) {
    Write-Host "Session key: $($latestSession.session_key)"
    Write-Host "Project: $($latestSession.current_project_id)"
    Write-Host "Project root: $($latestSession.current_project_root)"
    Write-Host "Session id: $($latestSession.current_session_id)"
    Write-Host "Active task: $($latestSession.active_task_id)"
    Write-Host "Last task: $($latestSession.last_task_id)"
    Write-Host "Last reply code: $($latestSession.last_reply_code)"
    Write-Host "Last failure category: $($latestSession.last_failure_category)"
    Write-Host "Last inbound: $($latestSession.last_inbound_at)"
    Write-Host "Last task finished: $($latestSession.last_task_finished_at)"
    Write-Host "Updated: $($latestSession.updated_at)"
} else {
    Write-Host "None"
}
Write-Host
Write-Host "[Current Project]"
if ($latestProject) {
    Write-Host "Project id: $($latestProject.project_id)"
    Write-Host "Project root: $($latestProject.project_root)"
    Write-Host "Session key: $($latestProject.current_session_key)"
    Write-Host "Session id: $($latestProject.current_session_id)"
    Write-Host "Last task: $($latestProject.last_task_id)"
    Write-Host "Last reply code: $($latestProject.last_reply_code)"
    Write-Host "Last failure category: $($latestProject.last_failure_category)"
    Write-Host "Last task finished: $($latestProject.last_task_finished_at)"
    Write-Host "Updated: $($latestProject.updated_at)"
} else {
    Write-Host "None"
}
Write-Host
Write-Host "[Latest Task]"
if ($latestTask) {
    Write-Host "Task id: $($latestTask.task_id)"
    Write-Host "Scope: $($latestTask.task_scope)"
    Write-Host "Phase: $($latestTask.phase)"
    Write-Host "Project: $($latestTask.project_id)"
    Write-Host "Session: $($latestTask.session_id)"
    Write-Host "Reply code: $($latestTask.reply_code)"
    Write-Host "Failure category: $($latestTask.failure_category)"
    Write-Host "User visible status: $($latestTask.user_visible_status)"
    Write-Host "Updated: $($latestTask.updated_at)"
} else {
    Write-Host "None"
}
Write-Host
Write-Host "[QQ]"
Write-Host "Enabled: $($status.qq.enabled)"
Write-Host "AllowFrom: $([string]::Join(', ', @($status.qq.allow_from)))"
Write-Host "MediaDir: $($status.qq.media_dir)"
Write-Host
Write-Host "[Tooling]"
Write-Host "NanoBot: $($status.tooling.nanobot.exists) ($($status.tooling.nanobot.path))"
Write-Host "Claude: $($status.tooling.claude.exists) ($($status.tooling.claude.path))"
Write-Host "Codex: $($status.tooling.codex.exists) ($($status.tooling.codex.path))"
Write-Host
Write-Host "[WSL]"
Write-Host "Build: $($status.wsl.current_build)"
Write-Host "Elevated: $($status.wsl.elevated_shell)"
Write-Host "WSL2 Ready: $($status.wsl.wsl2_ready)"
Write-Host "Note: $($status.wsl.note)"
