param(
    [string] $ConfigPath,
    [string] $BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int] $Port = 8765,
    [string] $CredentialDirectory
)

$ErrorActionPreference = 'Stop'
$workerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workerPython -PathType Leaf)) {
    throw 'Run scripts/initialize-worker.ps1 first.'
}
if ($ConfigPath) {
    $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
}
if ($CredentialDirectory) {
    $CredentialDirectory = (Resolve-Path -LiteralPath $CredentialDirectory).Path
}

$previous = @{}
$restartEvent = $null
foreach ($name in @('UNITY_WORKER_TOKEN', 'UNITY_WORKER_APPROVAL_TOKEN', 'UNITY_WORKER_WORKFLOW_APPROVAL_KEY', 'UNITY_WORKER_CONFIG')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($CredentialDirectory) {
        foreach ($name in @('UNITY_WORKER_TOKEN', 'UNITY_WORKER_APPROVAL_TOKEN', 'UNITY_WORKER_WORKFLOW_APPROVAL_KEY')) {
            $credential = Join-Path $CredentialDirectory "$name.dpapi"
            if ($name -ne 'UNITY_WORKER_TOKEN' -and -not (Test-Path -LiteralPath $credential)) {
                continue
            }
            $secure = Get-Content -LiteralPath $credential | ConvertTo-SecureString
            $token = [Net.NetworkCredential]::new('', $secure).Password
            [Environment]::SetEnvironmentVariable($name, $token, 'Process')
        }
    }
    if (-not $env:UNITY_WORKER_TOKEN) {
        throw 'Set UNITY_WORKER_TOKEN in the Worker account environment.'
    }
    if ($ConfigPath) {
        $env:UNITY_WORKER_CONFIG = $resolvedConfig
    }
    $eventName = 'Local\UnityWorker-{0}-{1}' -f ($PSScriptRoot.ToUpperInvariant() -replace '\\', '/'), $Port
    $created = $false
    $restartEvent = [System.Threading.EventWaitHandle]::new($false, 'AutoReset', $eventName, [ref] $created)
    if (-not $created) {
        throw "A restartable Worker is already running in this checkout on port $Port."
    }
    $PSNativeCommandUseErrorActionPreference = $false
    do {
        & $workerPython -m scripts.run_worker $BindAddress $Port $eventName
    } while ($LASTEXITCODE -eq 75) # The server requested a fresh process.
    exit $LASTEXITCODE
}
finally {
    if ($restartEvent) { $restartEvent.Dispose() }
    Pop-Location
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
