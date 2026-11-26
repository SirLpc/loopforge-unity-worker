param(
    [string] $ConfigPath,
    [switch] $Pool,
    [string] $PythonExe
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) { $ConfigPath = Join-Path $root 'config.toml' }
$config = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigPath)
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if ($PythonExe) {
        & $PythonExe -m venv $venv
    } else {
        & py -3 -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Worker Python environment." }
}
& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer before initializing the Worker.' }
& $python -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install Worker dependencies.' }

if (-not (Test-Path -LiteralPath $config)) {
    $example = if ($Pool) { 'config.pools.example.toml' } else { 'config.example.toml' }
    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $config) -Force)
    [IO.File]::Copy((Join-Path $root $example), $config, $false)
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'ConfigPath must name a file.' }
Write-Output "Worker config: $config"
