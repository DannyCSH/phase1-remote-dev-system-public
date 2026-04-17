param(
    [switch]$Build,
    [switch]$SkipInstall,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$configuratorRoot = Join-Path $projectRoot "desktop-configurator"
$nodeModules = Join-Path $configuratorRoot "node_modules"
$npmCache = Join-Path $projectRoot ".cache\npm"
$cargoManifest = Join-Path $configuratorRoot "src-tauri\Cargo.toml"
$releaseExe = Join-Path $configuratorRoot "src-tauri\target\release\phase1-desktop-configurator.exe"

if (-not (Test-Path $configuratorRoot)) {
    throw "desktop-configurator directory not found: $configuratorRoot"
}

$nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
}
if (-not $nodeCmd) {
    throw "Node.js was not found. Install Node.js 18+ and try again."
}

$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
}
if (-not $npmCmd) {
    throw "npm was not found. Verify that Node.js was installed correctly."
}

$cargoCmd = Get-Command cargo.exe -ErrorAction SilentlyContinue
if (-not $cargoCmd) {
    $cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
}
if (-not $cargoCmd) {
    throw "Rust / cargo was not found. Install the Rust toolchain and try again."
}

New-Item -ItemType Directory -Force -Path $npmCache | Out-Null
$env:npm_config_cache = $npmCache
$env:PHASE1_PROJECT_ROOT = $projectRoot

function Ensure-ConfiguratorDependencies {
    if ((-not $SkipInstall) -or (-not (Test-Path $nodeModules))) {
        Write-Host "[Configurator] Installing desktop configurator dependencies..."
        & $npmCmd.Source install
        if ($LASTEXITCODE -ne 0) {
            throw "npm install failed with exit code: $LASTEXITCODE"
        }
    }
}

function Build-ConfiguratorRelease {
    Write-Host "[Configurator] Building frontend bundle..."
    & $npmCmd.Source run build
    if ($LASTEXITCODE -ne 0) {
        throw "npm run build failed with exit code: $LASTEXITCODE"
    }

    Write-Host "[Configurator] Building desktop executable with the Tauri production protocol..."
    & $cargoCmd.Source build --release --features custom-protocol --manifest-path $cargoManifest
    if ($LASTEXITCODE -ne 0) {
        throw "cargo build --release --features custom-protocol failed with exit code: $LASTEXITCODE"
    }

    if (-not (Test-Path $releaseExe)) {
        throw "Release executable was not produced: $releaseExe"
    }
}

function Start-ConfiguratorExecutable {
    if (-not (Test-Path $releaseExe)) {
        throw "Release executable not found: $releaseExe"
    }

    Write-Host "[Configurator] Launching desktop configurator executable..."
    Start-Process -FilePath $releaseExe -WorkingDirectory $configuratorRoot | Out-Null
}

Push-Location $configuratorRoot
try {
    if ($Build) {
        Ensure-ConfiguratorDependencies
        Build-ConfiguratorRelease
    } elseif ($Dev) {
        Ensure-ConfiguratorDependencies
        Write-Host "[Configurator] Launching desktop configurator in dev mode..."
        & $npmCmd.Source run tauri dev
        if ($LASTEXITCODE -ne 0) {
            throw "npm run tauri dev failed with exit code: $LASTEXITCODE"
        }
    } elseif (Test-Path $releaseExe) {
        Start-ConfiguratorExecutable
    } else {
        Ensure-ConfiguratorDependencies
        Write-Host "[Configurator] First launch detected. Building a local desktop executable..."
        Build-ConfiguratorRelease
        Start-ConfiguratorExecutable
    }
} finally {
    Pop-Location
}
