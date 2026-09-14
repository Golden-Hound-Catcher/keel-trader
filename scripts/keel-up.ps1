# Start keel-api + keel-worker + frontend monitor (Windows).
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\keel-up.ps1
#        .\scripts\keel-up.cmd
#        .\scripts\keel-up.cmd -SkipUi
param(
    [switch]$SkipUi
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_keel_win.ps1"

$cfg = Get-KeelStackConfig
Set-Location $cfg.Root
if (-not (Test-Path $cfg.RunDir)) {
    New-Item -ItemType Directory -Path $cfg.RunDir -Force | Out-Null
}

$py = Get-KeelPython -Root $cfg.Root
$envMap = @{
    PYTHONPATH       = $cfg.Root
    PYTHONUNBUFFERED = "1"
}

function Test-OurListener {
    param([int]$Port, [string]$Pattern)
    foreach ($l in (Get-ListenersOnPort -Port $Port)) {
        $cl = Get-ProcessCommandLine -ProcessId $l.OwningProcess
        if ($cl -and $cl -match $Pattern) { return $true }
    }
    return $false
}

# --- API ---
$apiPid = Read-PidFile -Path $cfg.ApiPid
if ($apiPid -and (Test-PidAlive -ProcessId $apiPid)) {
    Write-Host "keel-api already running (pid $apiPid)  - skip"
} elseif (Test-OurListener -Port $cfg.Port -Pattern "uvicorn\s+keel\.api\.app:app") {
    $found = Find-KeelStackProcesses -Root $cfg.Root
    $reuse = $found.Api | Select-Object -First 1
    if ($reuse) {
        Write-PidFile -Path $cfg.ApiPid -ProcessId $reuse.ProcessId
        Write-Host "keel-api already running (pid $($reuse.ProcessId))  - skip"
    }
} else {
    $listeners = @(Get-ListenersOnPort -Port $cfg.Port)
    if ($listeners.Count -gt 0) {
        Write-Host "ERROR: port $($cfg.Host):$($cfg.Port) already in use  - refusing to start a second keel-api." -ForegroundColor Red
        foreach ($l in $listeners) {
            $cl = Get-ProcessCommandLine -ProcessId $l.OwningProcess
            Write-Host "  listener pid $($l.OwningProcess) $($l.LocalAddress):$($cfg.Port) $cl"
        }
        Write-Host "Hint: stop the foreign process, then retry. .\scripts\keel-down.cmd only stops Keel pidfiles / keel command lines."
        exit 1
    }
    if ($apiPid) {
        Write-Host "removing stale keel-api pidfile (pid $apiPid not alive)"
        Remove-Item -LiteralPath $cfg.ApiPid -Force -ErrorAction SilentlyContinue
    }
    $proc = Start-LoggedProcess -FilePath $py `
        -ArgumentList @("-m", "uvicorn", "keel.api.app:app", "--host", $cfg.Host, "--port", "$($cfg.Port)") `
        -WorkingDirectory $cfg.Root -StdOut $cfg.ApiOut -StdErr $cfg.ApiErr -Environment $envMap
    Write-PidFile -Path $cfg.ApiPid -ProcessId $proc.Id
    Write-Host "started keel-api pid=$($proc.Id) $($cfg.Host):$($cfg.Port)"
    Start-Sleep -Seconds 1
    if (-not (Test-PidAlive -ProcessId $proc.Id)) {
        Write-Host "ERROR: keel-api pid $($proc.Id) died immediately after start." -ForegroundColor Red
        if (Test-Path $cfg.ApiErr) { Get-Content $cfg.ApiErr -Tail 40 }
        Remove-Item -LiteralPath $cfg.ApiPid -Force -ErrorAction SilentlyContinue
        exit 1
    }
}

# --- worker ---
$workerPid = Read-PidFile -Path $cfg.WorkerPid
if ($workerPid -and (Test-PidAlive -ProcessId $workerPid)) {
    Write-Host "keel-worker already running (pid $workerPid)  - skip"
} else {
    $found = Find-KeelStackProcesses -Root $cfg.Root
    $reuse = $found.Worker | Select-Object -First 1
    if ($reuse) {
        Write-PidFile -Path $cfg.WorkerPid -ProcessId $reuse.ProcessId
        Write-Host "keel-worker already running (pid $($reuse.ProcessId))  - skip"
    } else {
        if ($workerPid) {
            Write-Host "removing stale keel-worker pidfile (pid $workerPid not alive)"
            Remove-Item -LiteralPath $cfg.WorkerPid -Force -ErrorAction SilentlyContinue
        }
        $proc = Start-LoggedProcess -FilePath $py `
            -ArgumentList @("-m", "keel.worker") `
            -WorkingDirectory $cfg.Root -StdOut $cfg.WorkerOut -StdErr $cfg.WorkerErr -Environment $envMap
        Write-PidFile -Path $cfg.WorkerPid -ProcessId $proc.Id
        Write-Host "started keel-worker pid=$($proc.Id)"
        Start-Sleep -Seconds 1
        if (-not (Test-PidAlive -ProcessId $proc.Id)) {
            Write-Host "ERROR: keel-worker pid $($proc.Id) died immediately after start (often scheduler lock)." -ForegroundColor Red
            if (Test-Path $cfg.WorkerErr) { Get-Content $cfg.WorkerErr -Tail 40 }
            if (Test-Path $cfg.WorkerOut) { Get-Content $cfg.WorkerOut -Tail 40 }
            Remove-Item -LiteralPath $cfg.WorkerPid -Force -ErrorAction SilentlyContinue
            exit 1
        }
    }
}

# --- UI ---
if (-not $SkipUi) {
    $uiPid = Read-PidFile -Path $cfg.UiPid
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
    $frontend = Join-Path $cfg.Root "frontend"
    if ($uiPid -and (Test-PidAlive -ProcessId $uiPid)) {
        Write-Host "keel-ui already running (pid $uiPid)  - skip"
    } elseif (-not (Test-Path (Join-Path $frontend "package.json"))) {
        Write-Host "keel-ui: skipped (no frontend/package.json)"
    } elseif (-not $npm) {
        Write-Host "keel-ui: skipped (npm not on PATH)"
    } else {
        $found = Find-KeelStackProcesses -Root $cfg.Root
        $reuse = $found.Ui | Select-Object -First 1
        if ($reuse) {
            Write-PidFile -Path $cfg.UiPid -ProcessId $reuse.ProcessId
            Write-Host "keel-ui already running (pid $($reuse.ProcessId))  - skip"
        } else {
            $uiListen = @(Get-ListenersOnPort -Port $cfg.UiPort)
            if ($uiListen.Count -gt 0) {
                Write-Host "WARN: port $($cfg.UiPort) in use  - skip Vite. Stop the listener or use $($cfg.MonitorUrl) if it is already Keel UI."
            } else {
                if ($uiPid) {
                    Remove-Item -LiteralPath $cfg.UiPid -Force -ErrorAction SilentlyContinue
                }
                $proc = Start-LoggedProcess -FilePath $npm.Source `
                    -ArgumentList @("run", "dev") `
                    -WorkingDirectory $frontend -StdOut $cfg.UiOut -StdErr $cfg.UiErr -Environment @{}
                Write-PidFile -Path $cfg.UiPid -ProcessId $proc.Id
                Write-Host "started keel-ui pid=$($proc.Id) :$($cfg.UiPort)"
            }
        }
    }
}

$apiPidNow = Read-PidFile -Path $cfg.ApiPid
if ($apiPidNow -and -not (Test-PidAlive -ProcessId $apiPidNow)) {
    Write-Host "ERROR: keel-api pid $apiPidNow died while waiting for /health." -ForegroundColor Red
    if (Test-Path $cfg.ApiErr) { Get-Content $cfg.ApiErr -Tail 40 }
    Remove-Item -LiteralPath $cfg.ApiPid -Force -ErrorAction SilentlyContinue
    exit 1
}

if (Wait-HealthOk -Url $cfg.HealthUrl -TimeoutSec 30) {
    Write-Host "health: OK ($($cfg.HealthUrl))"
} else {
    Write-Host "ERROR: /health not ready after ~30s  - check $($cfg.ApiErr)" -ForegroundColor Red
    if (Test-Path $cfg.ApiErr) { Get-Content $cfg.ApiErr -Tail 40 }
    exit 1
}

Write-Host "Keel stack up (run_dir=$($cfg.RunDir))."
Write-Host "  API:     $($cfg.DocsUrl)"
if (-not $SkipUi) {
    Write-Host "  Monitor: $($cfg.MonitorUrl)"
}
Write-Host "  down:    .\scripts\keel-down.cmd"
