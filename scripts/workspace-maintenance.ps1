[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)][ValidateSet('Status', 'Measure', 'Release', 'Clean')][string] $Operation = 'Status',
    [string] $WorkerUrl = $env:UNITY_WORKER_URL,
    [string] $CredentialDirectory,
    [string] $SlotId,
    [switch] $State,
    [string] $TaskId,
    [ValidateSet('Library', 'Temp', 'Obj')][string] $Cache
)

$ErrorActionPreference = 'Stop'
$token = $env:UNITY_WORKER_APPROVAL_TOKEN

function Protect-Text([string] $Text) {
    if ($token) { return $Text.Replace($token, '[redacted]') }
    return $Text
}

function Show-Line([string] $Text) {
    Write-Host (Protect-Text $Text)
}

function Format-Volume($Volume) {
    if ($null -eq $Volume.free_bytes -or $null -eq $Volume.total_bytes) { return 'volume capacity unavailable' }
    return "free $($Volume.free_bytes) / $($Volume.total_bytes) bytes"
}

function Show-Remnants($Paths) {
    if ($Paths) {
        foreach ($path in $Paths) { Show-Line "Cleanup remnant: $path" }
        Show-Line 'Inspect and resolve the cleanup remnant, then restart the Worker to recheck the slot.'
    }
}

function Show-Measurement($Measurement) {
    Show-Line "$($Measurement.path) ($(Format-Volume $Measurement.volume))"
    if ($Measurement.exists -eq $false) {
        Show-Line 'Directory is missing.'
    } else {
        Show-Line "Total logical bytes: $($Measurement.logical_bytes)"
        foreach ($bucket in $Measurement.buckets.PSObject.Properties) {
            Show-Line "$($bucket.Name) $($bucket.Value) bytes"
        }
    }
    if (-not $Measurement.complete) {
        Show-Line "Warning: partial scan (skipped links $($Measurement.skipped_links), read errors $($Measurement.read_errors))."
    }
}

function Invoke-Worker([string] $Method, [string] $Path, [int] $Timeout = 15, [hashtable] $Body) {
    $request = @{
        Method = $Method
        Uri = "$baseUrl$Path"
        Headers = @{ Authorization = "Bearer $token" }
        UseBasicParsing = $true
        TimeoutSec = $Timeout
        MaximumRedirection = 0
        DisableKeepAlive = $true
    }
    if ($Body) {
        $request.ContentType = 'application/json'
        $request.Body = ConvertTo-Json -InputObject $Body -Compress
    }
    try {
        $response = Invoke-WebRequest @request
        $json = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
        ConvertFrom-Json -InputObject $json
    } catch {
        $status = [int] $_.Exception.Response.StatusCode
        $errorBody = if ($_.ErrorDetails.Message) {
            try { ConvertFrom-Json -InputObject $_.ErrorDetails.Message } catch {}
        }
        $message = if ($status) { "Worker returned HTTP $status." } else { 'Could not read the Worker response.' }
        if ($errorBody.detail -is [string]) { $message += " $($errorBody.detail)" }
        Show-Remnants $errorBody.remnant
        $failure = [InvalidOperationException]::new($message)
        $failure.Data['StatusCode'] = $status
        throw $failure
    }
}

$jobPath = $null
$cleanupSubmitted = $false
try {
    $hasSlot = -not [string]::IsNullOrWhiteSpace($SlotId)
    $slotBound = $PSBoundParameters.ContainsKey('SlotId')
    $hasTask = $PSBoundParameters.ContainsKey('TaskId')
    $hasCache = $PSBoundParameters.ContainsKey('Cache')
    switch ($Operation) {
        Status {
            if ($State -or $slotBound -or $hasTask -or $hasCache) {
                throw 'Status takes no target arguments.'
            }
        }
        Measure {
            $validTarget = if ($State) { -not $slotBound } else { $hasSlot }
            if (-not $validTarget -or $hasTask -or $hasCache) {
                throw 'Measure requires exactly one of -SlotId or -State.'
            }
        }
        Release {
            $taskUuid = [guid]::Empty
            if (-not [guid]::TryParse($TaskId, [ref] $taskUuid) -or $State -or $slotBound -or $hasCache) {
                throw 'Release requires a UUID in -TaskId.'
            }
        }
        Clean {
            if (-not $hasSlot -or -not $hasCache -or $State -or $hasTask) {
                throw 'Clean requires -SlotId and -Cache (Library, Temp, or Obj).'
            }
        }
    }

    $workerUri = $null
    if (-not [uri]::TryCreate($WorkerUrl, [UriKind]::Absolute, [ref] $workerUri) -or
        $workerUri.Scheme -notin @('http', 'https') -or $workerUri.UserInfo -or
        $workerUri.Query -or $workerUri.Fragment) {
        throw 'Set -WorkerUrl or UNITY_WORKER_URL to an HTTP or HTTPS base URL.'
    }
    $baseUrl = $workerUri.AbsoluteUri.TrimEnd('/')
    if ($CredentialDirectory) {
        try {
            $credential = Join-Path $CredentialDirectory 'UNITY_WORKER_APPROVAL_TOKEN.dpapi'
            $secure = Get-Content -LiteralPath $credential | ConvertTo-SecureString
            $token = [Net.NetworkCredential]::new('', $secure).Password
        } catch {
            throw 'Could not read the Operator Token DPAPI file. Use the Windows account and machine that saved it.'
        }
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'Set UNITY_WORKER_APPROVAL_TOKEN or -CredentialDirectory.'
    }

    $storagePath = '/api/v1/storage'
    if ($hasSlot) { $slotPath = "$storagePath/slots/$([uri]::EscapeDataString($SlotId))" }
    switch ($Operation) {
        Status {
            $overview = Invoke-Worker GET $storagePath
            if ($null -eq $overview.state -or $null -eq $overview.pools) { throw 'Invalid storage overview.' }
            Show-Line "Worker state: $($overview.state.path) ($(Format-Volume $overview.state.volume))"
            foreach ($pool in $overview.pools) {
                Show-Line "Pool $($pool.id) (queued $($pool.queued_count))"
                foreach ($slot in $pool.slots) {
                    $lease = if ($slot.task_id) { " task $($slot.task_id), stage $($slot.current_stage)" }
                    Show-Line "  $($slot.id) [$($slot.status)]$lease $($slot.path) ($(Format-Volume $slot.volume))"
                    Show-Remnants $slot.cleanup_remnants
                }
            }
        }
        Measure {
            $path = if ($State) { "$storagePath/state" } else { $slotPath }
            $measurement = Invoke-Worker GET $path -Timeout 300
            if ($measurement.exists -isnot [bool]) { throw 'Invalid storage measurement.' }
            Show-Measurement $measurement
        }
        Release {
            $release = Invoke-Worker POST "/api/v1/tasks/$taskUuid/release" -Body @{ request_id = [guid]::NewGuid().ToString() }
            $jobUuid = [guid]::Empty
            if (-not [guid]::TryParse($release.job_id, [ref] $jobUuid)) {
                throw 'Worker did not return a Job ID. Check the task before submitting another release.'
            }
            $jobPath = "/api/v1/jobs/$jobUuid"
            Show-Line "Release Job ID: $jobUuid (check $jobPath if polling stops)."
            $elapsed = [Diagnostics.Stopwatch]::StartNew()
            do {
                $job = Invoke-Worker GET $jobPath
                switch ($job.status) {
                    succeeded {
                        Show-Line 'Release succeeded. The slot can be assigned immediately.'
                        exit 0
                    }
                    { $_ -in @('failed', 'cancelled', 'recovery_required') } {
                        $failure = $job.error_message
                        if (-not $failure) { $failure = $job.failure_kind }
                        $jobPath = $null
                        throw "Release $($job.status). $failure"
                    }
                    { $_ -in @('queued', 'running') } { Start-Sleep -Seconds 1 }
                    default { throw 'Worker did not return a recognized Job status.' }
                }
            } while ($elapsed.Elapsed.TotalSeconds -lt 120)
            throw 'Stopped waiting for the release Job after two minutes.'
        }
        Clean {
            $Cache = @('Library', 'Temp', 'Obj') -eq $Cache | Select-Object -First 1
            $preview = Invoke-Worker GET "${slotPath}?cache=$Cache" -Timeout 300
            if ($preview.complete -isnot [bool] -or -not $preview.complete -or
                $preview.exists -isnot [bool] -or [string]::IsNullOrWhiteSpace($preview.path) -or
                $preview.read_errors -ne 0 -or $preview.skipped_links -ne 0) {
                throw 'Cache preview is incomplete. Run the preview again after resolving any scan errors or links.'
            }
            if (-not $preview.exists) {
                Show-Line "$Cache is missing at $($preview.path)."
                exit 0
            }
            if (($preview.logical_bytes -isnot [int] -and $preview.logical_bytes -isnot [long]) -or
                $preview.logical_bytes -lt 0) {
                throw 'Cache preview is incomplete. Worker did not report the logical byte count.'
            }
            Show-Line "Slot $SlotId, $Cache at $($preview.path), estimated $($preview.logical_bytes) logical bytes."
            if ($Cache -eq 'Library') { Show-Line 'Warning: Unity will reimport the project on its next open or run.' }
            if ((Read-Host 'Type YES to clean this cache') -cne 'YES') {
                throw 'Cleanup cancelled.'
            }

            $cleanupSubmitted = $true
            $result = Invoke-Worker POST "$slotPath/clean" -Timeout 600 -Body @{ cache = $Cache; confirmed = $true }
            if ($result.status -notin @('cleaned', 'missing')) { throw 'Worker returned an unrecognized cleanup result.' }
            $cleanupSubmitted = $false
            Show-Line "$Cache $($result.status): $($result.path), $($result.logical_bytes) logical bytes ($(Format-Volume $result.volume))."
        }
    }
    exit 0
} catch {
    Write-Error -ErrorAction Continue (Protect-Text $_.Exception.Message)
    if ($jobPath) { Show-Line "The release may still be running. Check GET $jobPath." }
    $status = $_.Exception.Data['StatusCode']
    if ($cleanupSubmitted -and ($status -lt 400 -or $status -ge 500 -or $status -in @(408, 499))) {
        Show-Line 'Cleanup outcome is unknown. Run Status, then preview the slot and cache again before confirming any new cleanup.'
    }
    exit 1
}
