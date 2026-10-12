param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $projectRoot
try {
  if ($InstallDependencies) {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { python -m venv .venv; if ($LASTEXITCODE) { throw 'venv failed' } }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-extraction-windows.txt
    if ($LASTEXITCODE) { throw 'Python dependency install failed' }
    # Use component directories. No shared/global installation or plugin registration.
    foreach ($component in @('plugins/research-report-writer','components/lifecycle','components/xats-routing-lab/upstream')) {
      Push-Location $component
      try { pnpm install --ignore-scripts --no-frozen-lockfile; if ($LASTEXITCODE) { throw 'Node dependency install failed' } }
      finally { Pop-Location }
    }
    Write-Output 'Native better-sqlite3 must be built for this Node ABI before build-local.mjs; no live installation is reused.'
  }
  node components/lifecycle/build-router-package.mjs
  if ($LASTEXITCODE) { throw 'Router build failed' }
  node scripts/extraction/build-local.mjs
  if ($LASTEXITCODE) { throw 'Native/service build failed' }
} finally { Pop-Location }
