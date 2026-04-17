param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDir = Join-Path $projectRoot "runtime"
$statusScript = Join-Path $projectRoot "scripts\Get-Phase1Status.ps1"
$healthPath = Join-Path $runtimeDir "health.json"
$gatewayRecoveryPath = Join-Path $runtimeDir "gateway-recovery.json"
$configLocalPath = Join-Path $projectRoot "config\nanobot.local.json"
$configExamplePath = Join-Path $projectRoot "config\nanobot.example.json"

if (-not $OutputDir) {
    $OutputDir = Join-Path $runtimeDir "support"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
& $statusScript -WriteRuntimeHealth *> $null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bundleRoot = Join-Path $OutputDir ("diagnostic-" + $timestamp)
New-Item -ItemType Directory -Force -Path $bundleRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $bundleRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $bundleRoot "runtime") | Out-Null

function Get-RedactedText {
    param(
        [AllowNull()][string]$Text
    )

    if ($null -eq $Text) {
        return $null
    }

    $value = [string]$Text
    if ([string]::IsNullOrEmpty($value)) {
        return $value
    }

    foreach ($entry in @(
        @{ From = $projectRoot; To = "%PROJECT_ROOT%" },
        @{ From = $runtimeDir; To = "%PROJECT_ROOT%\\runtime" },
        @{ From = $env:USERPROFILE; To = "%USERPROFILE%" },
        @{ From = $env:WINDIR; To = "%WINDIR%" }
    )) {
        $from = [string]$entry.From
        $to = [string]$entry.To
        if (-not [string]::IsNullOrWhiteSpace($from)) {
            $value = $value -replace [regex]::Escape($from), $to
            $value = $value -replace [regex]::Escape($from.Replace('\', '\\')), $to.Replace('\', '\\')
        }
    }

    $value = $value -replace '(?m)(Processing message from qq:)[^\r\n]+', '$1***REDACTED***: ***REDACTED***'
    $value = $value -replace 'qq:[A-Za-z0-9-]{16,}', 'qq:***REDACTED***'
    $value = $value -replace '(?i)(message id=)[^\s]+', '$1***REDACTED***'
    $value = $value -replace '(?i)(chat_id=)[^\s]+', '$1***REDACTED***'
    $value = $value -replace '(?i)(sender_id=)[^\s]+', '$1***REDACTED***'
    $value = $value -replace '(?i)(https://api\.sgroup\.qq\.com/v2/users/)[^/\s]+(/files)', '$1***REDACTED***$2'
    $value = $value -replace '(?<![A-Fa-f0-9])[A-Fa-f0-9]{32}(?![A-Fa-f0-9])', '***REDACTED***'
    return $value
}

function Convert-ToRedactedObject {
    param(
        $InputObject,
        [string]$KeyName = ""
    )

    $sensitiveKeys = @(
        "session_key",
        "current_session_key",
        "chat_id",
        "sender_id",
        "allow_from",
        "allowFrom",
        "appId",
        "secret",
        "apiKey",
        "openid",
        "open_id"
    )

    if ($null -eq $InputObject) {
        return $null
    }

    if ($InputObject -is [string]) {
        if ($sensitiveKeys -contains $KeyName) {
            return "***REDACTED***"
        }
        return (Get-RedactedText -Text $InputObject)
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        foreach ($key in $InputObject.Keys) {
            $name = [string]$key
            if ($sensitiveKeys -contains $name) {
                $result[$name] = "***REDACTED***"
            } else {
                $result[$name] = Convert-ToRedactedObject -InputObject $InputObject[$key] -KeyName $name
            }
        }
        return $result
    }

    if ($InputObject -is [System.Collections.IEnumerable] -and -not ($InputObject -is [string])) {
        if ($sensitiveKeys -contains $KeyName) {
            return @("***REDACTED***")
        }

        $items = @()
        foreach ($item in $InputObject) {
            $items += ,(Convert-ToRedactedObject -InputObject $item -KeyName $KeyName)
        }
        return $items
    }

    if ($InputObject.PSObject -and $InputObject.PSObject.Properties.Count -gt 0 -and -not ($InputObject -is [ValueType])) {
        $result = [ordered]@{}
        foreach ($property in $InputObject.PSObject.Properties) {
            $name = [string]$property.Name
            if ($sensitiveKeys -contains $name) {
                $result[$name] = "***REDACTED***"
            } else {
                $result[$name] = Convert-ToRedactedObject -InputObject $property.Value -KeyName $name
            }
        }
        return $result
    }

    return $InputObject
}

function Export-RedactedJson {
    param(
        [string]$SourcePath,
        [string]$TargetPath,
        [string]$FailureText
    )

    if (-not (Test-Path $SourcePath)) {
        return
    }

    try {
        $content = Get-Content -Raw -Encoding UTF8 $SourcePath
        $content = Get-RedactedText -Text $content
        $content = $content -replace '("session_key"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("current_session_key"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("chat_id"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("sender_id"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("appId"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("secret"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("apiKey"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("task_name"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("user_request"\s*:\s*")[^"]*(")', '$1***REDACTED***$2'
        $content = $content -replace '("allow_from"\s*:\s*\[)[^\]]*(\])', '$1"***REDACTED***"$2'
        $content = $content -replace '("allowFrom"\s*:\s*\[)[^\]]*(\])', '$1"***REDACTED***"$2'
        Set-Content -Path $TargetPath -Value $content -Encoding UTF8
    } catch {
        Set-Content -Path $TargetPath -Value $FailureText -Encoding UTF8
    }
}

function Export-SanitizedLog {
    param(
        [string]$SourcePath,
        [string]$TargetPath,
        [int]$TailLines = 200
    )

    if (-not (Test-Path $SourcePath)) {
        return
    }

    try {
        $lines = Get-Content -Encoding UTF8 $SourcePath -Tail $TailLines
        $sanitized = @(
            "# Sanitized tail from: $(Split-Path -Leaf $SourcePath)"
            "# Included lines: $TailLines"
            ""
        )
        foreach ($line in $lines) {
            $sanitized += (Get-RedactedText -Text ([string]$line))
        }
        $sanitized | Set-Content -Path $TargetPath -Encoding UTF8
    } catch {
        Set-Content -Path $TargetPath -Value "Failed to sanitize log." -Encoding UTF8
    }
}

Export-RedactedJson -SourcePath $healthPath -TargetPath (Join-Path $bundleRoot "runtime\health.redacted.json") -FailureText "Failed to parse health.json for redaction."
Export-RedactedJson -SourcePath $gatewayRecoveryPath -TargetPath (Join-Path $bundleRoot "runtime\gateway-recovery.redacted.json") -FailureText "Failed to parse gateway-recovery.json for redaction."
Export-RedactedJson -SourcePath (Join-Path $runtimeDir "current-worker.json") -TargetPath (Join-Path $bundleRoot "runtime\current-worker.redacted.json") -FailureText "Failed to parse current-worker.json for redaction."
Export-RedactedJson -SourcePath (Join-Path $runtimeDir "current-admin-relay.json") -TargetPath (Join-Path $bundleRoot "runtime\current-admin-relay.redacted.json") -FailureText "Failed to parse current-admin-relay.json for redaction."
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "gateway.out.log") -TargetPath (Join-Path $bundleRoot "logs\gateway.out.redacted.log")
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "gateway.err.log") -TargetPath (Join-Path $bundleRoot "logs\gateway.err.redacted.log")
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "worker.out.log") -TargetPath (Join-Path $bundleRoot "logs\worker.out.redacted.log")
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "worker.err.log") -TargetPath (Join-Path $bundleRoot "logs\worker.err.redacted.log")
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "gateway-watchdog.err.log") -TargetPath (Join-Path $bundleRoot "logs\gateway-watchdog.err.redacted.log")
Export-SanitizedLog -SourcePath (Join-Path $runtimeDir "gateway-watchdog.out.log") -TargetPath (Join-Path $bundleRoot "logs\gateway-watchdog.out.redacted.log")

$tasksRoot = Join-Path $runtimeDir "tasks"
if (Test-Path $tasksRoot) {
    Get-ChildItem -Path $tasksRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 2 |
        ForEach-Object {
            $targetDir = Join-Path $bundleRoot ("runtime\tasks-" + $_.Name)
            New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
            Export-RedactedJson -SourcePath (Join-Path $_.FullName "status.json") -TargetPath (Join-Path $targetDir "status.redacted.json") -FailureText "Failed to parse task status for redaction."
        }
}

if (Test-Path $configLocalPath) {
    Export-RedactedJson -SourcePath $configLocalPath -TargetPath (Join-Path $bundleRoot "config.local.redacted.json") -FailureText "Failed to parse config for redaction."
} elseif (Test-Path $configExamplePath) {
    Export-RedactedJson -SourcePath $configExamplePath -TargetPath (Join-Path $bundleRoot "config.example.redacted.json") -FailureText "Failed to parse config for redaction."
}

$manifest = [ordered]@{
    created_at = (Get-Date).ToString("s")
    project_root = $projectRoot
    bundle_root = $bundleRoot
    included = @(
        "runtime/health.redacted.json",
        "runtime/gateway-recovery.redacted.json",
        "runtime/current-worker.redacted.json",
        "runtime/current-admin-relay.redacted.json",
        "logs/*.redacted.log (tail only)",
        "latest task status excerpts (redacted)",
        "redacted config"
    )
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $bundleRoot "manifest.json") -Encoding UTF8

$zipPath = Join-Path $OutputDir ("phase1-diagnostic-" + $timestamp + ".zip")
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $zipPath -Force

Write-Output ("BUNDLE_PATH=" + $zipPath)
Write-Output ("BUNDLE_DIR=" + $bundleRoot)
