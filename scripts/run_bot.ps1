$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'main.py'
$mutex = New-Object System.Threading.Mutex($false, 'Local\FvgBot_fvgbot_Supervisor')
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { exit 0 }
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python runtime not found' }
    Set-Location -LiteralPath $projectRoot
    $PID | Set-Content -LiteralPath (Join-Path $projectRoot 'data\runner.pid')
    $outLog = Join-Path $projectRoot 'logs\supervisor.out.log'
    $errLog = Join-Path $projectRoot 'logs\supervisor.err.log'
    while ($true) {
        # Foreground child of this hidden supervisor; wait and restart on exit.
        & $pythonPath -u $entryPath >> $outLog 2>> $errLog
        "$(Get-Date -Format o) Python exited; restarting in five seconds." | Add-Content -LiteralPath $outLog
        Start-Sleep -Seconds 5
    }
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
