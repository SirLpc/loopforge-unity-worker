param(
    [string] $ConfigPath,
    [string] $BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int] $Port = 8765
)

$ErrorActionPreference = 'Stop'
if (-not $env:UNITY_WORKER_TOKEN) {
    throw 'Set UNITY_WORKER_TOKEN in the Worker account environment.'
}
if ($ConfigPath) {
    $env:UNITY_WORKER_CONFIG = (Resolve-Path -LiteralPath $ConfigPath).Path
}
$workerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workerPython -PathType Leaf)) {
    throw 'Create the .venv environment and install requirements.txt first.'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    & $workerPython -m uvicorn worker:create_app --factory --host $BindAddress --port $Port --workers 1 --no-proxy-headers
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
