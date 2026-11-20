<#
Team setup for the Power BI Control Center catalog MCP server.

Two modes:

1. ZERO-GIT (recommended for colleagues - default once the feed is live):
     powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1 -FromFeed "https://pkgs.dev.azure.com/ashleyfurniture/Super%20Troopers/_packaging/PbiControlCenter/pypi/simple/"
   Installs the wheel from the private Azure Artifacts feed (first install
   triggers a one-time browser sign-in via keyrings.artifacts), seeds the
   .env template, and registers the server globally. No git anywhere.

2. REPO MODE (developers):
     powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1
      - installs a standalone copy to ~\.ashleycode\mcp\pbi-control-center
        and registers that path globally
     powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1 -Dev
      - registers the repo checkout itself (live edits)

Team-scoped by construction: only people who run this script get the server.
Re-running is safe and idempotent.
#>

param(
    [switch]$Dev,
    [string]$FromFeed
)

$ErrorActionPreference = "Stop"

$serverName   = "powerbi-control-center"
$repoRoot     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoServer   = Join-Path $repoRoot "mcp_server\server.py"
$installRoot  = Join-Path $env:USERPROFILE ".ashleycode\mcp\pbi-control-center"
$installServer = Join-Path $installRoot "mcp_server\server.py"
$installEnv   = Join-Path $installRoot ".env"
$globalConfigCandidates = @(
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.jsonc"),
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.json")
)

if ($Dev -and $FromFeed) { throw "Use either -Dev or -FromFeed, not both." }
$mode = if ($FromFeed) { "feed" } elseif ($Dev) { "dev" } else { "standalone" }

if ($mode -ne "feed") {
    Write-Host "Repo root : $repoRoot"
}

# ---------------------------------------------------------------- 0. checks
& python -c "import sys; print('Python    : ' + sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "python not found on PATH - install Python 3.11+ and retry." }

if ($mode -eq "feed") {
    # Zero-git: the wheel + its dependencies come from the private feed;
    # everything else (mcp/msal/...) resolves from PyPI via the extra index.
    # EAP=Continue: stderr from a python.bat shim (pyenv-win) would otherwise
    # abort the script under EAP=Stop even though stderr is redirected.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & python -c "import keyrings.artifacts" 2>$null
    $keyringOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap
    if (-not $keyringOk) {
        Write-Host "Installing artifacts keyring (enables one-time browser sign-in for the private feed)..."
        & python -m pip install --quiet keyrings.artifacts
        if ($LASTEXITCODE -ne 0) { throw "keyrings.artifacts install failed." }
    }
    Write-Host "Installing pbi-control-center-mcp from the feed (browser sign-in may pop up once)..."
    & python -m pip install pbi-control-center-mcp --index-url $FromFeed --extra-index-url https://pypi.org/simple
    if ($LASTEXITCODE -ne 0) { throw "Feed install failed - check the feed URL and your sign-in." }
} else {
    if (-not (Test-Path $repoServer)) {
        throw "Server not found at $repoServer - is the repo complete?"
    }
    # EAP=Continue: stderr from a python.bat shim (pyenv-win) would otherwise
    # abort the script under EAP=Stop even though stderr is redirected.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & python -c "import mcp, msal, dotenv, requests" 2>$null
    $depsOk = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap
    if (-not $depsOk) {
        Write-Warning "Required Python packages are missing (mcp / msal / python-dotenv / requests)."
        $answer = Read-Host "Install them now with pip? (y/N)"
        if ($answer -match '^[Yy]') {
            & pip install -r (Join-Path $repoRoot "requirements.txt") -r (Join-Path $repoRoot "requirements-dev.txt")
            if ($LASTEXITCODE -ne 0) { throw "pip install failed - install the requirements manually and re-run." }
        } else {
            throw "Dependencies missing. In the repo root run: pip install -r requirements.txt -r requirements-dev.txt  then re-run this script."
        }
    }
}

# ------------------------------------------------- 0b. standalone local copy
if ($mode -eq "standalone") {
    Write-Host "Installing standalone server copy -> $installRoot"
    New-Item -ItemType Directory -Force $installRoot | Out-Null
    robocopy (Join-Path $repoRoot "mcp_server")      (Join-Path $installRoot "mcp_server")      /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy mcp_server failed (exit $LASTEXITCODE)." }
    robocopy (Join-Path $repoRoot "catalog_service") (Join-Path $installRoot "catalog_service") /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy catalog_service failed (exit $LASTEXITCODE)." }
    Copy-Item (Join-Path $repoRoot "powerbi_connector.py") $installRoot -Force
}

# --------------------------------------------------------- 1. credentials (.env)
# The server searches, in order: ~\.ashleycode\mcp\pbi-control-center\.env,
# repo .env (repo modes), CWD .env. Seed the standard location if needed.
if (-not (Test-Path $installEnv)) {
    New-Item -ItemType Directory -Force (Split-Path $installEnv) | Out-Null
    @'
# Power BI Control Center MCP - credentials (never commit this file).
# Fill in and re-run nothing - the server reads this file at startup.
TENANT_ID=
CLIENT_ID=
CLIENT_SECRET=

# SharePoint artifact store (catalog source of truth) - copy the
# SHAREPOINT_* block from the repo's .env.example, or from a teammate.
SHAREPOINT_TENANT_ID=
SHAREPOINT_CLIENT_ID=
SHAREPOINT_CLIENT_SECRET=
SHAREPOINT_SITE_HOSTNAME=
SHAREPOINT_SITE_PATH=
SHAREPOINT_DRIVE_NAME=
SHAREPOINT_FOLDER_PATH=
'@ | Set-Content -Path $installEnv -Encoding UTF8
    Write-Warning "Seeded $installEnv - EDIT IT with your credentials or every tool returns 'catalog unavailable'."
} else {
    Write-Host ".env     : keeping existing $installEnv"
}

# ----------------------------------------------------------- 2. registration
$registerCommand = switch ($mode) {
    "feed"       { ,@("pbi-cc-mcp") }
    "dev"        { ,@("python", $repoServer) }
    "standalone" { ,@("python", $installServer) }
}
$marker = switch ($mode) {
    "feed"       { "pbi-cc-mcp" }
    "dev"        { $repoRoot }
    "standalone" { $installRoot }
}

$globalConfig = $globalConfigCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$registered = $false
if ($globalConfig) {
    $raw = Get-Content $globalConfig -Raw
    $registered = ($raw -replace '\\\\', '\').Contains($marker) -or
                  ($raw -match [regex]::Escape('"powerbi-control-center"'))
}

if (-not $registered) {
    & opencode mcp add $serverName --global -- @registerCommand
    if ($LASTEXITCODE -ne 0) { throw "opencode mcp add failed (exit $LASTEXITCODE)." }
    Write-Host "Registered in your global config: $($registerCommand -join ' ')"
} else {
    Write-Host "Already registered - registration unchanged (files/refresh done where applicable)."
    if ($mode -eq "standalone") {
        Write-Host "To switch this machine to the standalone install, remove the '$serverName' entry from:"
        Write-Host "  $globalConfig"
        Write-Host "then re-run this script."
    }
}

# ------------------------------------------------------------- 3. validation
$pyCode = switch ($mode) {
    "feed"  { "import mcp_server.server; print('FEED INSTALL OK')" }
    default {
        $p = if ($mode -eq "dev") { $repoServer } else { $installServer }
        # Single line on purpose: python .bat shims (pyenv-win) mangle
        # multi-line python -c arguments.
        "import importlib.util; spec = importlib.util.spec_from_file_location('mcp_server_server', r'$p'); m = importlib.util.module_from_spec(spec); m.spec = spec; spec.loader.exec_module(m); print('SERVER IMPORT OK')"
    }
}
& python -c $pyCode
if ($LASTEXITCODE -ne 0) { throw "Server validation failed - check the output above." }

Write-Host ""
Write-Host "Done. '$serverName' is available in every AshleyCode session."
Write-Host "Verify with:  opencode mcp list   (or /mcps inside AshleyCode)"
if ($mode -eq "feed") {
    Write-Host "Updates:  python -m pip install --upgrade pbi-control-center-mcp --index-url <feed simple url> --extra-index-url https://pypi.org/simple"
} elseif ($mode -eq "standalone") {
    Write-Host "Installed copy (self-contained): $installRoot"
    Write-Host "Update it later:  git pull  in the repo, then re-run this script."
}
