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
$wheels = Get-ChildItem "dist\pbi-control-center-mcp-*.whl" | Sort-Object LastWriteTime -Descending
if (-not $wheels) { throw "no wheel produced in dist/." }
Write-Host "Built: $($wheels[0].Name)"

# 2. Ensure twine.
& python -c "import twine" 2>$null
if ($LASTEXITCODE -ne 0) {
    python -m pip install --quiet twine
    if ($LASTEXITCODE -ne 0) { throw "twine install failed." }
}

# 3. Upload (PAT auth: any username + PAT as password).
& python -m twine upload --non-interactive `
    --repository-url $FeedUploadUrl `
    -u build --password $Pat `
    $wheels[0].FullName
if ($LASTEXITCODE -ne 0) { throw "twine upload failed - check the PAT (needs Packaging Read & Write) and the upload URL." }

Write-Host ""
Write-Host "Published $($wheels[0].Name)."
Write-Host "Colleagues install with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\setup-mcp.ps1 -FromFeed `"https://pkgs.dev.azure.com/ashleyfurniture/Super%20Troopers/_packaging/PbiControlCenter/pypi/simple/`""
