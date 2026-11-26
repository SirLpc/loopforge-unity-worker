param(
    [Parameter(Mandatory = $true)][guid] $TaskID,
    [Parameter(Mandatory = $true)][ValidatePattern('^(?:[0-9a-f]{40}|[0-9a-f]{64})$')][string] $Revision,
    [Parameter(Mandatory = $true)][string] $Approver,
    [string] $WorkerUrl = $env:UNITY_WORKER_URL,
    [string] $CredentialDirectory,
    [string] $OutputPath,
    [switch] $CreatePR,
    [string] $CommitMessage
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not $WorkerUrl) { throw 'Set -WorkerUrl or UNITY_WORKER_URL.' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Run scripts/initialize-worker.ps1 first.'
}
if (-not $OutputPath) { $OutputPath = Join-Path $root "state\operator-approval-$TaskID.json" }
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
[void](New-Item -ItemType Directory -Path (Split-Path -Parent $output) -Force)
$previousUrl = $env:UNITY_WORKER_URL
$previousToken = $env:UNITY_WORKER_APPROVAL_TOKEN

try {
    if ($CredentialDirectory) {
        $credential = Join-Path $CredentialDirectory 'UNITY_WORKER_APPROVAL_TOKEN.dpapi'
        $secure = Get-Content -LiteralPath $credential | ConvertTo-SecureString
        $env:UNITY_WORKER_APPROVAL_TOKEN = [Net.NetworkCredential]::new('', $secure).Password
    }
    if (-not $env:UNITY_WORKER_APPROVAL_TOKEN) { throw 'Set UNITY_WORKER_APPROVAL_TOKEN or -CredentialDirectory.' }
    $env:UNITY_WORKER_URL = $WorkerUrl
    $options = @('approve', '--task-id', "$TaskID", '--revision', $Revision, '--approver', $Approver, '--output', $output)
    if ($CreatePR) { $options += '--create-pr' }
    if ($CommitMessage) { $options += @('--commit-message', $CommitMessage) }
    & $python (Join-Path $root 'integrations\loopforge\worker_client.py') @options
    exit $LASTEXITCODE
} finally {
    $env:UNITY_WORKER_URL = $previousUrl
    $env:UNITY_WORKER_APPROVAL_TOKEN = $previousToken
}
