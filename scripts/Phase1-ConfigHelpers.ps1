Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Web.Extensions

function New-Phase1JsonSerializer {
    $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $serializer.MaxJsonLength = [int]::MaxValue
    return $serializer
}

function Get-Phase1EnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue
    }

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if (-not [string]::IsNullOrWhiteSpace($userValue)) {
        return $userValue
    }

    $machineValue = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if (-not [string]::IsNullOrWhiteSpace($machineValue)) {
        return $machineValue
    }

    return $null
}

function Import-Phase1EnvPlaceholders {
    param(
        [string[]]$Names = @("QQ_APP_ID", "QQ_APP_SECRET", "QQ_OPENID", "MINIMAX_API_KEY")
    )

    foreach ($name in $Names) {
        $existing = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($existing)) {
            continue
        }
        $resolved = Get-Phase1EnvValue -Name $name
        if (-not [string]::IsNullOrWhiteSpace($resolved)) {
            Set-Item -Path ("Env:" + $name) -Value $resolved
        }
    }
}

function Resolve-Phase1ConfigValue {
    param(
        [Parameter(ValueFromPipeline = $true)]
        $Value
    )

    process {
        if ($null -eq $Value) {
            return $null
        }

        if ($Value -is [string]) {
            return ([regex]::Replace(
                $Value,
                '\$\{([A-Z0-9_]+)\}',
                {
                    param($match)
                    $resolved = Get-Phase1EnvValue -Name $match.Groups[1].Value
                    if ([string]::IsNullOrWhiteSpace($resolved)) {
                        return $match.Value
                    }
                    return $resolved
                }
            ))
        }

        if ($Value -is [System.Collections.IDictionary]) {
            $result = [ordered]@{}
            foreach ($key in $Value.Keys) {
                $result[$key] = Resolve-Phase1ConfigValue -Value $Value[$key]
            }
            return $result
        }

        if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
            $items = @()
            foreach ($item in $Value) {
                $items += ,(Resolve-Phase1ConfigValue -Value $item)
            }
            return ,$items
        }

        $properties = @()
        if ($Value.PSObject) {
            $properties = @($Value.PSObject.Properties)
        }
        if ($properties.Count -gt 0) {
            $result = [ordered]@{}
            foreach ($prop in $properties) {
                $result[$prop.Name] = Resolve-Phase1ConfigValue -Value $prop.Value
            }
            return $result
        }

        return $Value
    }
}

function Read-Phase1JsonResolved {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    $raw = Get-Content -Raw -Encoding UTF8 $Path
    $serializer = New-Phase1JsonSerializer
    $payload = $serializer.DeserializeObject($raw)
    return Resolve-Phase1ConfigValue -Value $payload
}

function Write-ResolvedPhase1Config {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$TargetPath,
        [switch]$StripPhase1Section
    )

    $resolved = Read-Phase1JsonResolved -Path $SourcePath
    if ($null -eq $resolved) {
        throw "Config not found or invalid: $SourcePath"
    }

    if ($StripPhase1Section -and $resolved -is [System.Collections.IDictionary] -and $resolved.Contains("phase1")) {
        $resolved.Remove("phase1")
    }

    $serializer = New-Phase1JsonSerializer
    $json = $serializer.Serialize($resolved)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($TargetPath, $json, $utf8NoBom)
    return $TargetPath
}

function Get-Phase1ProjectRoot {
    param(
        [string]$BaseDir = $PSScriptRoot
    )

    return (Resolve-Path (Join-Path $BaseDir "..")).Path
}

function Set-Phase1ProjectEnv {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    if (-not [string]::IsNullOrWhiteSpace($ProjectRoot)) {
        Set-Item -Path Env:PROJECT_ROOT -Value $ProjectRoot
    }
}

function Resolve-Phase1Path {
    param(
        [string]$Path,
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $Path
    }

    Set-Phase1ProjectEnv -ProjectRoot $ProjectRoot
    $expanded = [string](Resolve-Phase1ConfigValue -Value $Path)
    try {
        return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($expanded)
    } catch {
        return $expanded
    }
}

function Get-Phase1DefaultConfigPath {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    $localConfig = Join-Path $ProjectRoot "config\nanobot.local.json"
    if (Test-Path $localConfig) {
        return $localConfig
    }
    return (Join-Path $ProjectRoot "config\nanobot.example.json")
}

function Resolve-Phase1CommandPath {
    param(
        [string[]]$Candidates = @(),
        [string[]]$CommandNames = @(),
        [string[]]$EnvNames = @()
    )

    foreach ($envName in $EnvNames) {
        if ([string]::IsNullOrWhiteSpace($envName)) {
            continue
        }

        $envCandidate = Get-Phase1EnvValue -Name $envName
        if ([string]::IsNullOrWhiteSpace($envCandidate)) {
            continue
        }

        $resolved = Resolve-Phase1CommandPath -Candidates @($envCandidate)
        if ($resolved) {
            return $resolved
        }
    }

    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }

        $expanded = [string](Resolve-Phase1ConfigValue -Value $candidate)
        if (Test-Path $expanded) {
            return (Resolve-Path $expanded).Path
        }

        $command = Get-Command $expanded -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            return $command.Source
        }
    }

    foreach ($name in $CommandNames) {
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }

        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source) {
            return $command.Source
        }
    }

    return $null
}

function Resolve-Phase1PythonPath {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    return Resolve-Phase1CommandPath -Candidates @(
        (Get-Phase1EnvValue -Name "PHASE1_PYTHON_PATH"),
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        (Join-Path $ProjectRoot "venv\Scripts\python.exe")
    ) -CommandNames @("python.exe", "python")
}

function Resolve-Phase1NanobotPath {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    return Resolve-Phase1CommandPath -Candidates @(
        (Get-Phase1EnvValue -Name "PHASE1_NANOBOT_PATH"),
        (Join-Path $ProjectRoot ".venv\Scripts\nanobot.exe"),
        (Join-Path $ProjectRoot "venv\Scripts\nanobot.exe")
    ) -CommandNames @("nanobot.exe", "nanobot")
}

function Resolve-Phase1ClaudePath {
    return Resolve-Phase1CommandPath -Candidates @(
        (Get-Phase1EnvValue -Name "PHASE1_CLAUDE_PATH"),
        (Join-Path $HOME ".local\bin\claude.exe")
    ) -CommandNames @("claude.exe", "claude")
}

function Resolve-Phase1VsCodePath {
    $userPrograms = if ($env:LOCALAPPDATA) {
        Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\Code.exe"
    } else {
        $null
    }
    $systemPrograms = if ($env:ProgramFiles) {
        Join-Path $env:ProgramFiles "Microsoft VS Code\Code.exe"
    } else {
        $null
    }

    return Resolve-Phase1CommandPath -Candidates @(
        (Get-Phase1EnvValue -Name "PHASE1_VSCODE_PATH"),
        $userPrograms,
        $systemPrograms
    ) -CommandNames @("Code.exe", "code.exe", "code.cmd", "code")
}

function Get-Phase1CodexPluginRoot {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    $configured = Get-Phase1EnvValue -Name "PHASE1_CODEX_PLUGIN_ROOT"
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        return $configured
    }

    $repoLocal = Join-Path $ProjectRoot ".claude\plugins\openai-codex"
    if (Test-Path $repoLocal) {
        return $repoLocal
    }

    return ""
}

function Get-Phase1CodexCompanionPath {
    param(
        [string]$ProjectRoot = (Get-Phase1ProjectRoot)
    )

    $pluginRoot = Get-Phase1CodexPluginRoot -ProjectRoot $ProjectRoot
    if ([string]::IsNullOrWhiteSpace($pluginRoot)) {
        return ""
    }

    $candidate = Join-Path $pluginRoot "plugins\codex\scripts\codex-companion.mjs"
    if (Test-Path $candidate) {
        return $candidate
    }

    return ""
}
