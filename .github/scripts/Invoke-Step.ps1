# Dot-source, then: Invoke-Step 'title' { native command ... }
# If the block's last native command fails, the tail of its output becomes an error annotation on the check run
# (readable without signing in, unlike job logs) and the step exits with that code. $global:LastStepLog holds the
# path of the step's full output afterwards.
function Invoke-Step([string]$Title, [scriptblock]$Block) {
    $log = Join-Path $env:RUNNER_TEMP ("step-" + [guid]::NewGuid().ToString('N') + ".log")
    $global:LastStepLog = $log
    $global:LASTEXITCODE = 0
    $failure = $null
    try {
        & $Block 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log
    } catch {
        $failure = $_
    }
    $code = $global:LASTEXITCODE
    if ($failure -and $code -eq 0) { $code = 1 }
    if ($code -ne 0) {
        $tail = @()
        if (Test-Path -LiteralPath $log) { $tail += Get-Content -LiteralPath $log -Tail 40 }
        if ($failure) { $tail += "$failure" }
        $msg = ($tail -join "`n").Replace('%', '%25').Replace("`r", '').Replace("`n", '%0A')
        Write-Output "::error title=$Title (exit $code)::$msg"
        exit $code
    }
}
