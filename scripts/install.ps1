#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$packageVersion = $env:MCP_RELAY_VERSION
if (-not [string]::IsNullOrWhiteSpace($packageVersion) -and
    $packageVersion -notmatch "^[0-9]+(\.[0-9]+){1,2}([a-z]+[0-9]+)?$") {
    throw "MCP_RELAY_VERSION must be a release version such as 0.1.0."
}

function Get-UvPath {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Refresh-ProcessPath {
    $processPath = $env:Path
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $env:Path = @($processPath, $userPath, $machinePath) -join ";"
}

function Ensure-Uv {
    $uvPath = Get-UvPath
    if ($null -ne $uvPath) {
        return $uvPath
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($null -ne $winget) {
        Write-Host "Installing uv with WinGet..."
        $wingetOutput = @(
            & $winget.Source install --id=astral-sh.uv -e --source winget --accept-source-agreements --accept-package-agreements 2>&1
        )
        $wingetExitCode = $LASTEXITCODE
        $wingetOutput | ForEach-Object { Write-Host $_ }
        if ($wingetExitCode -ne 0) {
            throw "WinGet failed to install uv with exit code $wingetExitCode."
        }
        Refresh-ProcessPath
        $uvPath = Get-UvPath
    } else {
        Write-Host "WinGet is unavailable; downloading the official uv installer..."
        $uvInstaller = Join-Path $script:temporaryRoot "uv-install.ps1"
        Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $uvInstaller
        $uvInstallerOutput = @(
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller 2>&1
        )
        $uvInstallerExitCode = $LASTEXITCODE
        $uvInstallerOutput | ForEach-Object { Write-Host $_ }
        if ($uvInstallerExitCode -ne 0) {
            throw "The official uv installer failed with exit code $uvInstallerExitCode."
        }
        Refresh-ProcessPath
        $uvPath = Get-UvPath
    }

    if ($null -eq $uvPath) {
        throw "uv was installed but could not be found on PATH. Open a new PowerShell window and rerun the installer."
    }
    return $uvPath
}

function Add-UserPathEntry([string] $entry) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @(
        $userPath -split ";" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($entries | Where-Object { $_.TrimEnd("\") -ieq $entry.TrimEnd("\") }) {
        return
    }
    [Environment]::SetEnvironmentVariable("Path", (@($entries) + $entry) -join ";", "User")
}

function Invoke-McpRelay([string[]] $Arguments) {
    & $script:mcpRelayCommand @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "mcp-relay failed with exit code $LASTEXITCODE."
    }
}

$script:temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("mcp-relay-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $script:temporaryRoot -Force | Out-Null

try {
    $projectRoot = $env:MCP_RELAY_PROJECT_ROOT
    if (-not [string]::IsNullOrWhiteSpace($projectRoot)) {
        if (-not (Test-Path -LiteralPath $projectRoot -PathType Container) -or
            -not (Test-Path -LiteralPath (Join-Path $projectRoot "pyproject.toml") -PathType Leaf)) {
            throw "MCP_RELAY_PROJECT_ROOT is not a valid MCP Relay project: $projectRoot"
        }
        $packageSpec = (Resolve-Path -LiteralPath $projectRoot).Path
    } elseif (-not [string]::IsNullOrWhiteSpace($packageVersion)) {
        $packageSpec = "mcp-relay==$packageVersion"
    } else {
        $packageSpec = "mcp-relay"
    }

    $setupMode = if ([string]::IsNullOrWhiteSpace($env:MCP_RELAY_SETUP)) {
        "prompt"
    } else {
        $env:MCP_RELAY_SETUP.ToLowerInvariant()
    }
    if ($setupMode -notin @("prompt", "skip")) {
        throw "MCP_RELAY_SETUP must be 'prompt' or 'skip'; for unattended setup, deploy config.yaml and environment variables yourself."
    }
    if ($setupMode -eq "prompt") {
        # A redirected or non-console host cannot drive the interactive CLI.
        try {
            if ([Console]::IsInputRedirected) { $setupMode = "skip" }
        } catch {
            $setupMode = "skip"
        }
    }

    $uv = Ensure-Uv

    Write-Host "Installing $packageSpec for the current user..."
    & $uv tool install --force --python 3.14 $packageSpec
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed with exit code $LASTEXITCODE."
    }

    $toolBin = (& $uv tool dir --bin).Trim()
    if ([string]::IsNullOrWhiteSpace($toolBin) -or -not (Test-Path -LiteralPath $toolBin -PathType Container)) {
        throw "uv did not report a valid tool bin directory."
    }
    if ($env:MCP_RELAY_SKIP_PATH_UPDATE -ne "1") {
        Add-UserPathEntry $toolBin
    }
    $env:Path = "$toolBin;$env:Path"

    $mcpRelayCandidates = @(Get-ChildItem -LiteralPath $toolBin -Filter "mcp-relay*" -File)
    $mcpRelay = $mcpRelayCandidates |
        Where-Object { $_.Name -in @("mcp-relay.exe", "mcp-relay.cmd", "mcp-relay.ps1") } |
        Select-Object -First 1
    if ($null -eq $mcpRelay) {
        throw "The mcp-relay command was not found in $toolBin."
    }
    $script:mcpRelayCommand = $mcpRelay.FullName

    if ($setupMode -eq "prompt") {
        Invoke-McpRelay @("onboard")
    } else {
        Write-Host "Skipping interactive onboarding. For unattended deployment, supply ~/.mcp-relay/config.yaml and Server/Client environment variables (or private ~/.mcp-relay/.env) yourself."
    }

    $installedVersion = (& $script:mcpRelayCommand --version | Out-String).Trim()
    Write-Host ""
    Write-Host "$installedVersion installed for the current user."
    if ($setupMode -eq "skip") {
        Write-Host "Run guided setup later from a terminal with: mcp-relay onboard"
    }
    Write-Host "Start the configured runtime with mcp-relay server and/or mcp-relay client."
} finally {
    if (Test-Path -LiteralPath $script:temporaryRoot) {
        Remove-Item -LiteralPath $script:temporaryRoot -Recurse -Force
    }
}
