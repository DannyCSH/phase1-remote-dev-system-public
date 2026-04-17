param(
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$Phase1Root = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
if (-not $Phase1Root) {
    $Phase1Root = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
}
$Phase1Root = Resolve-Phase1Path -Path $Phase1Root -ProjectRoot $Phase1Root

if (-not (Test-Path $ConfigPath)) {
    Write-Output "Codex config not found: $ConfigPath"
    exit 0
}

$lines = Get-Content -Encoding UTF8 $ConfigPath
$prelude = New-Object System.Collections.Generic.List[string]
$blocks = New-Object System.Collections.Generic.List[object]
$currentHeader = $null
$currentLines = New-Object System.Collections.Generic.List[string]

foreach ($line in $lines) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^\[.+\]$') {
        if ($null -eq $currentHeader) {
            foreach ($entry in $currentLines) {
                [void]$prelude.Add($entry)
            }
        } else {
            $blocks.Add([pscustomobject]@{
                Header = $currentHeader
                Lines = @($currentLines)
            }) | Out-Null
        }
        $currentHeader = $trimmed
        $currentLines = New-Object System.Collections.Generic.List[string]
        [void]$currentLines.Add($line)
        continue
    }
    [void]$currentLines.Add($line)
}

if ($null -ne $currentHeader) {
    $blocks.Add([pscustomobject]@{
        Header = $currentHeader
        Lines = @($currentLines)
    }) | Out-Null
} elseif ($prelude.Count -eq 0) {
    Write-Output "Codex config is empty: $ConfigPath"
    exit 0
}

$phase1RootNorm = $Phase1Root.Replace('/', '\').ToLowerInvariant()
$staleMarkers = @(
    ($phase1RootNorm + '\runtime\tmp\review-snapshots\snapshot-'),
    ($phase1RootNorm + '\runtime\tmp\mini-review-')
)
$updated = New-Object System.Collections.Generic.List[string]
foreach ($entry in $prelude) {
    [void]$updated.Add($entry)
}

$removedHeaders = New-Object System.Collections.Generic.List[string]

foreach ($block in $blocks) {
    $header = [string]$block.Header
    $blockLines = @($block.Lines)
    $blockText = ($blockLines -join "`n").ToLowerInvariant()
    $drop = $false

    if ($header -eq "[windows]" -and $blockText.Contains('sandbox = "elevated"')) {
        $drop = $true
    } elseif ($header -match "^\[projects\.'(.+)'\]$") {
        $projectPath = $Matches[1].Replace('/', '\').ToLowerInvariant()
        foreach ($marker in $staleMarkers) {
            if ($projectPath.Contains($marker)) {
                $drop = $true
                break
            }
        }
    }

    if ($drop) {
        [void]$removedHeaders.Add($header)
        continue
    }

    foreach ($line in $blockLines) {
        [void]$updated.Add([string]$line)
    }
}

$content = ($updated -join "`r`n").TrimEnd() + "`r`n"
Set-Content -Path $ConfigPath -Value $content -Encoding UTF8

if ($removedHeaders.Count -eq 0) {
    Write-Output "Codex config already clean."
} else {
    Write-Output "Codex config repaired."
    foreach ($header in $removedHeaders) {
        Write-Output ("Removed: " + $header)
    }
}
