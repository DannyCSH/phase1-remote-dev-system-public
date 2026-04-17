param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

$helperPath = Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1"
. $helperPath
$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot
$promptPath = Join-Path $projectRoot "prompts\nanobot-session.txt"
$tempDir = Join-Path $projectRoot "runtime\tmp"
$claudeCli = Resolve-Phase1ClaudePath
if (-not $claudeCli) {
    throw "Claude Code CLI not found. Set PHASE1_CLAUDE_PATH or install claude."
}

if (-not (Test-Path $promptPath)) {
    throw "Remote session prompt not found: $promptPath"
}

$extraPrompt = Get-Content -Path $promptPath -Raw
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:TMPDIR = $tempDir

Set-Location $projectRoot
& $claudeCli --dangerously-skip-permissions --setting-sources user,project,local --append-system-prompt $extraPrompt @ClaudeArgs
exit $LASTEXITCODE
