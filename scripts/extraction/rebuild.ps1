param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $projectRoot
try {
  if ($InstallDependencies) {
    python scripts/release/install_dependencies.py --role windows-client --with-xats
    if ($LASTEXITCODE) { throw 'Dependency install failed' }
    Write-Output 'Native better-sqlite3 must be built for this Node ABI before build-local.mjs; no live installation is reused.'
  }
  node components/lifecycle/build-router-package.mjs
  if ($LASTEXITCODE) { throw 'Router build failed' }
  node scripts/extraction/build-local.mjs
  if ($LASTEXITCODE) { throw 'Native/service build failed' }
} finally { Pop-Location }
