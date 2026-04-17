param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

$helperPath = Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1"
. $helperPath
$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
$tempDir = Join-Path $projectRoot "runtime\tmp"
$claudeCli = Resolve-Phase1ClaudePath
if (-not $claudeCli) {
    throw "Claude Code CLI not found. Set PHASE1_CLAUDE_PATH or install claude."
}

New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:TMPDIR = $tempDir

Set-Location $projectRoot
& $claudeCli --setting-sources user,project,local @ClaudeArgs
exit $LASTEXITCODE
