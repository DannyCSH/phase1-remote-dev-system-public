param(
    [switch]$EnableNow
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$vendorDir = Join-Path $projectRoot "vendor\wsl"
$planFile = Join-Path $vendorDir "NEXT_STEPS.txt"
$projectRootForWsl = $projectRoot -replace "\\", "/"
if ($projectRootForWsl -match "^([A-Za-z]):/(.+)$") {
    $driveLetter = $Matches[1].ToLowerInvariant()
    $rest = $Matches[2]
    $wslProjectRoot = "/mnt/$driveLetter/$rest"
} else {
    $wslProjectRoot = "<PROJECT_ROOT>"
}

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$buildNumber = [int]$cv.CurrentBuild
$ubr = [int]$cv.UBR
$fullBuild = "{0}.{1}" -f $buildNumber, $ubr
$wsl2Ready = ($buildNumber -gt 19041) -or (($buildNumber -eq 18363 -or $buildNumber -eq 18362) -and $ubr -ge 1049)
$isElevated = Test-IsElevated

New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

$lines = @()
$lines += "Phase 1 WSL installation notes"
$lines += ""
$lines += "Current machine:"
$lines += "- Windows build: $fullBuild"
$lines += "- Elevated shell: $isElevated"
$lines += "- WSL2 ready by official minimum build check: $wsl2Ready"
$lines += ""
$lines += "Recommended path:"
$lines += "1. Open Windows PowerShell as Administrator."
$lines += "2. Run: dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart"
if ($wsl2Ready) {
    $lines += "3. Run: dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart"
    $lines += "4. Reboot Windows."
    $lines += "5. Install an Ubuntu distro, then inside Ubuntu run: bash $wslProjectRoot/scripts/Setup-WslTmux.sh"
} else {
    $lines += "3. Reboot Windows."
    $lines += "4. Install an Ubuntu distro for WSL1, then inside Ubuntu run: bash $wslProjectRoot/scripts/Setup-WslTmux.sh"
    $lines += "5. If you want WSL2 later, update Windows first to a newer build."
}
$lines += ""
$lines += "Important:"
$lines += "- This script is project-scoped documentation and preparation."
$lines += "- The current Codex shell is not elevated, so system features cannot be enabled from here unless you rerun in an Administrator shell."

Set-Content -Path $planFile -Value ($lines -join [Environment]::NewLine) -Encoding UTF8

if ($EnableNow) {
    if (-not $isElevated) {
        throw "EnableNow requires an elevated PowerShell window."
    }

    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Host
    if ($wsl2Ready) {
        dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Host
    }
}

Write-Host "WSL preparation notes written to:"
Write-Host $planFile
Write-Host
Get-Content -Path $planFile -Encoding UTF8
