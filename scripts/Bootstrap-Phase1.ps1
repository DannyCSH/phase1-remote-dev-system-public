param(
    [string]$PythonCommand = "",
    [switch]$SkipGlobalCliInstall,
    [switch]$ForceConfigCopy
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "Phase1-ConfigHelpers.ps1")
Import-Phase1EnvPlaceholders

$projectRoot = Get-Phase1ProjectRoot -BaseDir $PSScriptRoot
Set-Phase1ProjectEnv -ProjectRoot $projectRoot

$bootstrapPython = if ($PythonCommand) {
    Resolve-Phase1CommandPath -Candidates @($PythonCommand)
} else {
    Resolve-Phase1CommandPath -CommandNames @("python.exe", "python", "py.exe", "py")
}

if (-not $bootstrapPython) {
    throw "Python 3.11+ not found. Install Python first, or rerun with -PythonCommand <path>."
}

$venvCapability = & $bootstrapPython -c "import importlib.util; print(int(importlib.util.find_spec('venv') is not None)); print(int(importlib.util.find_spec('virtualenv') is not None))"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect Python environment: $bootstrapPython"
}
$venvCapability = @($venvCapability | Where-Object { $_ -ne $null })
$supportsVenv = $venvCapability.Count -ge 1 -and [int]$venvCapability[0] -eq 1
$supportsVirtualenv = $venvCapability.Count -ge 2 -and [int]$venvCapability[1] -eq 1

$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$exampleConfig = Join-Path $projectRoot "config\nanobot.example.json"
$localConfig = Join-Path $projectRoot "config\nanobot.local.json"

if (-not (Test-Path $venvPython)) {
    if (-not $supportsVenv -and -not $supportsVirtualenv) {
        throw "The selected Python cannot create virtual environments. Install full CPython 3.11+ with the venv module, or rerun with -PythonCommand pointing to one."
    }

    Write-Host "[1/4] Creating project virtual environment..."
    if ($supportsVenv) {
        & $bootstrapPython -m venv $venvDir
    } else {
        & $bootstrapPython -m virtualenv $venvDir
    }
}

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment creation failed: $venvPython"
}

Write-Host "[2/4] Installing Python dependencies..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r $requirementsPath

if (-not $SkipGlobalCliInstall) {
    $npm = Resolve-Phase1CommandPath -CommandNames @("npm.cmd", "npm")
    if (-not $npm) {
        throw "npm not found. Install Node.js 18+ first, or rerun with -SkipGlobalCliInstall."
    }

    $claudeCli = Resolve-Phase1CommandPath -CommandNames @("claude.exe", "claude")
    if (-not $claudeCli) {
        Write-Host "[3/4] Installing Claude Code CLI..."
        & $npm install -g @anthropic-ai/claude-code
    } else {
        Write-Host "[3/4] Claude Code CLI already available: $claudeCli"
    }

    $codexCli = Resolve-Phase1CommandPath -CommandNames @("codex.cmd", "codex.ps1", "codex")
    if (-not $codexCli) {
        Write-Host "[3/4] Installing Codex CLI..."
        & $npm install -g @openai/codex
    } else {
        Write-Host "[3/4] Codex CLI already available: $codexCli"
    }
} else {
    Write-Host "[3/4] Skipped global Claude/Codex CLI installation."
}

if ((-not (Test-Path $localConfig)) -or $ForceConfigCopy) {
    Copy-Item -Path $exampleConfig -Destination $localConfig -Force
    Write-Host "[4/4] Wrote local config: $localConfig"
} else {
    Write-Host "[4/4] Local config already exists: $localConfig"
}

Write-Host
Write-Host "Bootstrap complete."
Write-Host
Write-Host "Next steps:"
Write-Host "1. Open config\\nanobot.local.json and fill these placeholders:"
Write-Host "   - QQ_APP_ID"
Write-Host "   - QQ_APP_SECRET"
Write-Host "   - QQ_OPENID"
Write-Host "   - MINIMAX_API_KEY"
Write-Host "2. Run: claude login"
Write-Host "3. Run: codex login"
Write-Host "4. Start the stack from repo root:"
Write-Host "   powershell -ExecutionPolicy Bypass -File .\\scripts\\Start-Phase1Stack.ps1"
Write-Host "5. Check status if needed:"
Write-Host "   powershell -ExecutionPolicy Bypass -File .\\scripts\\Get-Phase1Status.ps1"
