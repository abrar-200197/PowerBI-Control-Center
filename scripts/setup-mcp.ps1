<#
One-time team setup: register the Power BI Control Center catalog MCP server
in YOUR user-level AshleyCode/OpenCode config, so it appears in the MCP list
in every session, in any directory - like ashley-standards and jira (which
are likewise registered in each user's global config).

Team-scoped by construction: only people who run this script get the server;
nothing org-wide is modified.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1

Prerequisites (per machine):
  1. This repo cloned (the server code lives here)
  2. Python deps:  pip install -r requirements.txt -r requirements-dev.txt
  3. A local .env with Power BI/SharePoint credentials (gitignored)

Re-running is safe - it skips when the server is already registered.
#>

$ErrorActionPreference = "Stop"

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$serverPath = Join-Path $repoRoot "mcp_server\server.py"
$globalConfigCandidates = @(
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.jsonc"),
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.json")
)

Write-Host "Repo root : $repoRoot"

# 0. Idempotency - skip when already registered globally.
foreach ($cfg in $globalConfigCandidates) {
    if ((Test-Path $cfg) -and (Select-String -Path $cfg -Pattern '"powerbi-control-center"' -Quiet)) {
        Write-Host "Already registered in $cfg - nothing to do."
        Write-Host "Verify with:  opencode mcp list   (or /mcps inside AshleyCode)"
        exit 0
    }
}

# 1. Sanity checks.
if (-not (Test-Path $serverPath)) {
    throw "Server not found at $serverPath - is the repo complete?"
}
try {
    $pyVersion = (& python --version) 2>&1
    Write-Host "Python    : $pyVersion"
} catch {
    throw "python not found on PATH - install Python 3.11+ and retry."
}

# 2. Python dependencies (app deps + the mcp SDK).
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

if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    Write-Warning ".env not found in the repo root - the server will connect, but every tool will return 'catalog unavailable' until credentials are added (see .env.example)."
}

# 2. Register in the user's GLOBAL config (applies outside the repo too).
& opencode mcp add powerbi-control-center --global -- python $serverPath
if ($LASTEXITCODE -ne 0) { throw "opencode mcp add failed (exit $LASTEXITCODE)." }

Write-Host ""
Write-Host "Done. 'powerbi-control-center' is now in your global MCP list."
Write-Host "Verify with:  opencode mcp list   (or /mcps inside AshleyCode)"
