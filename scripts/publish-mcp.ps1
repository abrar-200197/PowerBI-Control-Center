<#
Publishes the pbi-control-center-mcp wheel to the private Azure Artifacts
feed. Run this after changing server code (bump the version in
pyproject.toml first, so clients can `pip install --upgrade`).

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\publish-mcp.ps1 `
      -FeedUploadUrl "https://pkgs.dev.azure.com/ashleyfurniture/Super%20Troopers/_packaging/PbiControlCenter/pypi/upload/" `
      -Pat "<PAT with Packaging Read & Write>"

Create the PAT:  Azure DevOps -> User settings -> Personal access tokens ->
New token -> scope "Packaging: Read & Write".

Design notes:
- twine runs in a dedicated venv under %TEMP% so your global Python packages
  are never upgraded/conflicted just to publish.
- The feed URL and PAT are passed to twine via TWINE_* environment
  variables: off the command line, and immune to the pyenv-win python.bat
  shim, which corrupts '%20' in URLs into a batch-parameter expansion
  ('Super%20Troopers' -> 'Supertwine0Troopers').
#>

param(
    [Parameter(Mandatory = $true)] [string]$FeedUploadUrl,
    [Parameter(Mandatory = $true)] [string]$Pat
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

# 1. Build the wheel (version comes from pyproject.toml).
python -m pip wheel --no-deps --no-build-isolation -w dist .
if ($LASTEXITCODE -ne 0) { throw "wheel build failed." }
$wheels = Get-ChildItem "dist\*.whl" |
    Where-Object { $_.Name -like "pbi_control_center_mcp-*" } |
    Sort-Object LastWriteTime -Descending
if (-not $wheels) { throw "no wheel produced in dist/." }
Write-Host "Built: $($wheels[0].Name)"

# 2. Dedicated twine venv (isolated from your global packages).
$toolVenv = Join-Path $env:TEMP "opencode\pbi-publish-venv"
if (-not (Test-Path "$toolVenv\Scripts\python.exe")) {
    python -m venv $toolVenv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed." }
}
& "$toolVenv\Scripts\python.exe" -m pip install --quiet twine
if ($LASTEXITCODE -ne 0) { throw "twine install failed (tool venv)." }

# 3. Upload via TWINE_* env vars (secret off the command line, %20-safe).
$env:TWINE_REPOSITORY_URL = $FeedUploadUrl
$env:TWINE_USERNAME       = "build"
$env:TWINE_PASSWORD       = $Pat
try {
    & "$toolVenv\Scripts\python.exe" -m twine upload --non-interactive $wheels[0].FullName
    if ($LASTEXITCODE -ne 0) {
        throw "twine upload failed - check the PAT (needs Packaging Read & Write), the feed name, and whether the feed is project- or org-scoped (the URL must match)."
    }
} finally {
    Remove-Item Env:TWINE_REPOSITORY_URL, Env:TWINE_USERNAME, Env:TWINE_PASSWORD -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Published $($wheels[0].Name)."
Write-Host "Colleagues install with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1 -FromFeed `"https://pkgs.dev.azure.com/ashleyfurniture/Super%20Troopers/_packaging/PbiControlCenter/pypi/simple/`""
