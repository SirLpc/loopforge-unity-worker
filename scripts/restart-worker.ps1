param(
    [ValidateRange(1, 65535)]
    [int] $Port = 8765
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$eventName = 'Local\UnityWorker-{0}-{1}' -f ($root.ToUpperInvariant() -replace '\\', '/'), $Port
try {
    $restartEvent = [System.Threading.EventWaitHandle]::OpenExisting($eventName)
} catch [System.Threading.WaitHandleCannotBeOpenedException] {
    throw "No restartable Worker was found in this checkout on port $Port. Start it with the updated start_worker.ps1 in the same Windows session."
}

try {
    [void] $restartEvent.Set()
    Write-Output 'Restart requested. The Worker will restart after running jobs finish.'
} finally {
    $restartEvent.Dispose()
}
