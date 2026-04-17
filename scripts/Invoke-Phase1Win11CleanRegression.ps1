param(
    [string]$OutputDir = "",
    [switch]$BuildDesktopConfigurator
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDir = Join-Path $projectRoot "runtime"
$statusScript = Join-Path $projectRoot "scripts\Get-Phase1Status.ps1"
$envScript = Join-Path $projectRoot "scripts\Check-Phase1Env.ps1"
$publicRoot = $projectRoot
$desktopConfiguratorRoot = Join-Path $publicRoot "desktop-configurator"

if (-not $OutputDir) {
    $OutputDir = Join-Path $runtimeDir "regression"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$runDir = Join-Path $OutputDir ("win11-regression-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

function Get-WebView2Version {
    $candidates = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    )
    foreach ($candidate in $candidates) {
        try {
            $item = Get-ItemProperty -Path $candidate -ErrorAction Stop
            if ($item.pv) {
                return [string]$item.pv
            }
        } catch {
        }
    }
    return ""
}

function Resolve-ToolStatus {
    param([string[]]$CommandNames, [string[]]$CandidatePaths = @())
    $path = Resolve-Phase1CommandPath -CommandNames $CommandNames -CandidatePaths $CandidatePaths
    return [ordered]@{
        exists = -not [string]::IsNullOrWhiteSpace($path)
        path = $path
    }
}

function Invoke-ExternalPowerShellFile {
    param(
        [string]$Path,
        [string[]]$Arguments = @()
    )

    $psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $output = & $psExe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments 2>&1 | Out-String
    return [pscustomobject]@{
        Output = $output
        ExitCode = $LASTEXITCODE
    }
}

$osInfo = Get-CimInstance Win32_OperatingSystem
$buildNumber = 0
[void][int]::TryParse([string]$osInfo.BuildNumber, [ref]$buildNumber)
$isWin11 = $buildNumber -ge 22000
$webView2Version = Get-WebView2Version

$statusResult = Invoke-ExternalPowerShellFile -Path $statusScript -Arguments @("-WriteRuntimeHealth")
$statusOutput = $statusResult.Output
$statusExit = $statusResult.ExitCode
$envResult = Invoke-ExternalPowerShellFile -Path $envScript
$envOutput = $envResult.Output
$envExit = $envResult.ExitCode

$desktopBuild = [ordered]@{
    attempted = [bool]$BuildDesktopConfigurator
    success = $false
    skipped = -not [bool]$BuildDesktopConfigurator
    output = ""
}

if ($BuildDesktopConfigurator -and (Test-Path $desktopConfiguratorRoot)) {
    try {
        $npm = Resolve-Phase1CommandPath -CommandNames @("npm.cmd", "npm")
        $cargo = Resolve-Phase1CommandPath -CommandNames @("cargo.exe", "cargo")
        if ($npm -and $cargo) {
            Push-Location $desktopConfiguratorRoot
            try {
                $desktopBuild.output = (& $npm run build 2>&1 | Out-String)
                $desktopBuild.success = $LASTEXITCODE -eq 0
                $desktopBuild.skipped = $false
            } finally {
                Pop-Location
            }
        } else {
            $desktopBuild.output = "npm or cargo not found."
            $desktopBuild.skipped = $true
        }
    } catch {
        $desktopBuild.output = $_.Exception.Message
        $desktopBuild.success = $false
        $desktopBuild.skipped = $false
    }
}

$report = [ordered]@{
    generated_at = (Get-Date).ToString("s")
    project_root = $projectRoot
    os = [ordered]@{
        caption = [string]$osInfo.Caption
        version = [string]$osInfo.Version
        build = [string]$osInfo.BuildNumber
        is_win11 = $isWin11
    }
    webview2 = [ordered]@{
        installed = -not [string]::IsNullOrWhiteSpace($webView2Version)
        version = $webView2Version
    }
    tools = [ordered]@{
        nanobot = Resolve-ToolStatus -CommandNames @("nanobot.exe", "nanobot") -CandidatePaths @((Resolve-Phase1NanobotPath -ProjectRoot $projectRoot))
        claude = Resolve-ToolStatus -CommandNames @("claude.exe", "claude")
        codex = Resolve-ToolStatus -CommandNames @("codex.cmd", "codex.ps1", "codex")
        python = Resolve-ToolStatus -CommandNames @("python.exe", "python", "py.exe", "py")
        node = Resolve-ToolStatus -CommandNames @("node.exe", "node")
        npm = Resolve-ToolStatus -CommandNames @("npm.cmd", "npm")
        cargo = Resolve-ToolStatus -CommandNames @("cargo.exe", "cargo")
    }
    checks = [ordered]@{
        status_exit_code = $statusExit
        env_exit_code = $envExit
        desktop_build = $desktopBuild
    }
}

$reportPath = Join-Path $runDir "report.json"
$statusOutputPath = Join-Path $runDir "status.txt"
$envOutputPath = Join-Path $runDir "env-check.txt"
$summaryPath = Join-Path $runDir "summary.md"

$report | ConvertTo-Json -Depth 8 | Set-Content -Path $reportPath -Encoding UTF8
Set-Content -Path $statusOutputPath -Value $statusOutput -Encoding UTF8
Set-Content -Path $envOutputPath -Value $envOutput -Encoding UTF8

$summaryLines = @(
    "# Phase 1 Win11 Clean Regression",
    "",
    "- Generated At: $($report.generated_at)",
    "- OS: $($report.os.caption)",
    "- Version: $($report.os.version)",
    "- Build: $($report.os.build)",
    "- Is Win11: $($report.os.is_win11)",
    "- WebView2 Installed: $($report.webview2.installed)",
    "- WebView2 Version: $($report.webview2.version)",
    "- Status Script Exit: $($report.checks.status_exit_code)",
    "- Env Check Exit: $($report.checks.env_exit_code)",
    "- Desktop Build Attempted: $($report.checks.desktop_build.attempted)",
    "- Desktop Build Success: $($report.checks.desktop_build.success)",
    ""
)

if (-not $isWin11) {
    $summaryLines += "- Note: This machine is not running Windows 11, so this run only validated the regression harness itself."
}

$summaryLines += @(
    "",
    "## Tool Snapshot",
    "",
    "- NanoBot: $($report.tools.nanobot.exists) $($report.tools.nanobot.path)",
    "- Claude: $($report.tools.claude.exists) $($report.tools.claude.path)",
    "- Codex: $($report.tools.codex.exists) $($report.tools.codex.path)",
    "- Python: $($report.tools.python.exists) $($report.tools.python.path)",
    "- Node: $($report.tools.node.exists) $($report.tools.node.path)",
    "- npm: $($report.tools.npm.exists) $($report.tools.npm.path)",
    "- Cargo: $($report.tools.cargo.exists) $($report.tools.cargo.path)"
)

$summaryLines | Set-Content -Path $summaryPath -Encoding UTF8

Write-Output ("RUN_DIR=" + $runDir)
Write-Output ("REPORT_PATH=" + $reportPath)
Write-Output ("SUMMARY_PATH=" + $summaryPath)
