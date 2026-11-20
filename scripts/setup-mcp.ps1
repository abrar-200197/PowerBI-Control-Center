<#
One-time team setup: installs the Power BI Control Center catalog MCP server
onto YOUR machine (jira-style, under ~\.ashleycode\mcp) and registers it in
your user-level AshleyCode/OpenCode config, so it appears in the MCP list in
every session, in any directory. After setup, the server does NOT need this
repo - the standalone copy under ~\.ashleycode\mcp is self-contained.

Team-scoped by construction: only people who run this script get the server;
nothing org-wide is modified.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1
      Standard: install a standalone copy + register it globally.
  powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1 -Dev
      Developers: register the repo checkout instead (live edits, no copy).

Updating later:  git pull  (in the repo), then re-run this script - it
refreshes the installed copy in place (your .env edits are preserved when
the repo has no .env).

Re-running is safe and idempotent.
#>

param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"

$serverName   = "powerbi-control-center"
$repoRoot     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoServer   = Join-Path $repoRoot "mcp_server\server.py"
$installRoot  = Join-Path $env:USERPROFILE ".ashleycode\mcp\pbi-control-center"
$installServer = Join-Path $installRoot "mcp_server\server.py"
$globalConfigCandidates = @(
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.jsonc"),
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.json")
)

Write-Host "Repo root : $repoRoot"

# ---------------------------------------------------------------- 0. checks
if (-not (Test-Path $repoServer)) {
    throw "Server not found at $repoServer - is the repo complete?"
}
try {
    $pyVersion = (& python --version) 2>&1
    Write-Host "Python    : $pyVersion"
} catch {
    throw "python not found on PATH - install Python 3.11+ and retry."
}

& python -c "import mcp, msal, dotenv, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Required Python packages are missing (mcp / msal / python-dotenv / requests)."
    $answer = Read-Host "Install them now with pip? (y/N)"
    if ($answer -match '^[Yy]') {
        & pip install -r (Join-Path $repoRoot "requirements.txt") -r (Join-Path $repoRoot "requirements-dev.txt")
        if ($LASTEXITCODE -ne 0) { throw "pip install failed - install the requirements manually and re-run." }
    } else {
        throw "Dependencies missing. In the repo root run: pip install -r requirements.txt -r requirements-dev.txt  then re-run this script."
    }
}

# ------------------------------------------------- 1. standalone local copy
if (-not $Dev) {
    Write-Host "Installing standalone server copy -> $installRoot"
    New-Item -ItemType Directory -Force $installRoot | Out-Null
    robocopy (Join-Path $repoRoot "mcp_server")     (Join-Path $installRoot "mcp_server")     /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy mcp_server failed (exit $LASTEXITCODE)." }
    robocopy (Join-Path $repoRoot "catalog_service") (Join-Path $installRoot "catalog_service") /MIR /XD __pycache__ /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy catalog_service failed (exit $LASTEXITCODE)." }
    Copy-Item (Join-Path $repoRoot "powerbi_connector.py") $installRoot -Force

    # Credentials: repo .env wins when present; otherwise seed from the
    # example (once) and tell the user to edit it. Never clobber a
    # hand-edited install .env when the repo has none.
    $repoEnv    = Join-Path $repoRoot ".env"
    $installEnv = Join-Path $installRoot ".env"
    if (Test-Path $repoEnv) {
        Copy-Item $repoEnv $installEnv -Force
        Write-Host ".env     : copied from repo checkout"
    } elseif (-not (Test-Path $installEnv)) {
        Copy-Item (Join-Path $repoRoot ".env.example") $installEnv
        Write-Warning "No repo .env - seeded $installEnv from .env.example."
        Write-Warning "Edit it and fill in TENANT_ID / CLIENT_ID / CLIENT_SECRET / SHAREPOINT_* or every tool will return 'catalog unavailable'."
    } else {
        Write-Host ".env     : keeping your existing $installEnv"
    }
}

# ----------------------------------------------------------- 2. registration
$registerPath = if ($Dev) { $repoServer } else { $installServer }
$globalConfig = $globalConfigCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $globalConfig) {
    & opencode mcp add $serverName --global -- python $registerPath
    if ($LASTEXITCODE -ne 0) { throw "opencode mcp add failed (exit $LASTEXITCODE)." }
    Write-Host "Registered in your global config: $registerPath"
} else {
    $raw       = Get-Content $globalConfig -Raw
    $normalized = $raw -replace '\\\\', '\'
    if ($normalized -notmatch [regex]::Escape('"powerbi-control-center"')) {
        & opencode mcp add $serverName --global -- python $registerPath
        if ($LASTEXITCODE -ne 0) { throw "opencode mcp add failed (exit $LASTEXITCODE)." }
        Write-Host "Registered in your global config: $registerPath"
    } elseif ($normalized.Contains($installRoot)) {
        Write-Host "Already registered (standalone install) - refreshed files, registration unchanged."
    } elseif ($normalized.Contains($repoRoot)) {
        Write-Host "Already registered (repo checkout) - registration unchanged."
        if (-not $Dev) {
            Write-Host "Standalone copy installed anyway. To switch this machine to it, remove the '$serverName' entry from:"
            Write-Host "  $globalConfig"
            Write-Host "then re-run this script."
        }
    } else {
        Write-Host "'$serverName' is already registered (unknown target) - registration unchanged."
        Write-Host "To re-point it, remove the entry from: $globalConfig  then re-run this script."
    }
}

# ------------------------------------------------------------- 3. validation
$validatePath = if ($Dev) { $repoServer } else { $installServer }
# Single line on purpose: python .bat shims (pyenv-win) mangle multi-line
# python -c arguments.
$pyCode = "import importlib.util; spec = importlib.util.spec_from_file_location('mcp_server_server', r'$validatePath'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('SERVER IMPORT OK')"
& python -c $pyCode
if ($LASTEXITCODE -ne 0) { throw "Server validation failed - check the output above." }

Write-Host ""
Write-Host "Done. '$serverName' is available in every AshleyCode session."
Write-Host "Verify with:  opencode mcp list   (or /mcps inside AshleyCode)"
if (-not $Dev) {
    Write-Host "Installed copy (self-contained): $installRoot"
    Write-Host "Update it later:  git pull  in the repo, then re-run this script."
}
