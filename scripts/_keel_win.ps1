# Shared helpers for keel-up.ps1 / keel-down.ps1 (Windows local stack).
# Pid/logs live under data/run/ (same layout as observe_up.sh).

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-KeelRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-KeelPython {
    param([string]$Root)
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "python not found; create .venv or put python on PATH"
}

function Get-DotEnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Default = ""
    )
    if (-not (Test-Path $Path)) { return $Default }
    foreach ($line in Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) {
        $trim = $line.Trim()
        if (-not $trim -or $trim.StartsWith("#") -or $trim -notmatch "=") { continue }
        $eq = $trim.IndexOf("=")
        $k = $trim.Substring(0, $eq).Trim()
        if ($k -ne $Key) { continue }
        return $trim.Substring($eq + 1).Trim().Trim('"').Trim("'")
    }
    return $Default
}

function Get-KeelStackConfig {
    $root = Get-KeelRoot
    $envFile = Join-Path $root ".env"
    $runDir = if ($env:KEEL_OBSERVE_RUN_DIR) { $env:KEEL_OBSERVE_RUN_DIR } else { Join-Path $root "data\run" }
    $portRaw = Get-DotEnvValue -Path $envFile -Key "KEEL_API_PORT" -Default "8080"
    if ($env:KEEL_API_PORT) { $portRaw = $env:KEEL_API_PORT }
    $hostRaw = Get-DotEnvValue -Path $envFile -Key "KEEL_API_HOST" -Default "0.0.0.0"
    if ($env:KEEL_API_HOST) { $hostRaw = $env:KEEL_API_HOST }
    $port = [int]$portRaw
    $probeHost = if ($hostRaw -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $hostRaw }
    [pscustomobject]@{
        Root       = $root
        EnvFile    = $envFile
        RunDir     = $runDir
        Host       = $hostRaw
        ProbeHost  = $probeHost
        Port       = $port
        UiPort     = 5173
        ApiPid     = Join-Path $runDir "keel-api.pid"
        WorkerPid  = Join-Path $runDir "keel-worker.pid"
        UiPid      = Join-Path $runDir "keel-ui.pid"
        ApiOut     = Join-Path $runDir "keel-api.out.log"
        ApiErr     = Join-Path $runDir "keel-api.err.log"
        WorkerOut  = Join-Path $runDir "keel-worker.out.log"
        WorkerErr  = Join-Path $runDir "keel-worker.err.log"
        UiOut      = Join-Path $runDir "keel-ui.out.log"
        UiErr      = Join-Path $runDir "keel-ui.err.log"
        HealthUrl  = "http://${probeHost}:${port}/health"
        DocsUrl    = "http://${probeHost}:${port}/docs"
        MonitorUrl = "http://localhost:5173/"
    }
}

function Read-PidFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    $raw = (Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction SilentlyContinue)
    if (-not $raw) { return $null }
    $pidVal = 0
    if ([int]::TryParse($raw.Trim(), [ref]$pidVal) -and $pidVal -gt 0) { return $pidVal }
    return $null
}

function Write-PidFile {
    param([string]$Path, [int]$ProcessId)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Set-Content -LiteralPath $Path -Value "$ProcessId" -Encoding ascii
}

function Test-PidAlive {
    param([int]$ProcessId)
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-ProcessTree -ProcessId $_.ProcessId }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-ListenersOnPort {
    param([int]$Port)
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Sort-Object OwningProcess -Unique
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($p) { return $p.CommandLine }
    return $null
}

function Find-KeelStackProcesses {
    param([string]$Root)
    $rootEsc = [regex]::Escape($Root)
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine }
    [pscustomobject]@{
        Api = @(
            $all | Where-Object { $_.CommandLine -match "uvicorn\s+keel\.api\.app:app" }
        )
        Worker = @(
            $all | Where-Object {
                $_.CommandLine -match "-m\s+keel\.worker(\s|$)" -and
                $_.CommandLine -notmatch "keel\.worker\.cycle"
            }
        )
        Ui = @(
            $all | Where-Object {
                $_.CommandLine -match "vite" -and
                $_.CommandLine -match $rootEsc
            }
        )
    }
}

function Stop-PidFileProcess {
    param(
        [string]$Name,
        [string]$PidFile
    )
    $procId = Read-PidFile -Path $PidFile
    if (-not $procId) {
        if (Test-Path $PidFile) {
            Write-Host "${Name}: empty/stale pidfile  - removing"
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "${Name}: no pidfile"
        }
        return
    }
    if (-not (Test-PidAlive -ProcessId $procId)) {
        Write-Host "${Name}: not running (stale pid $procId)  - removing pidfile"
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    Stop-ProcessTree -ProcessId $procId
    Start-Sleep -Milliseconds 400
    if (Test-PidAlive -ProcessId $procId) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "stopped $Name pid=$procId (forced)"
    } else {
        Write-Host "stopped $Name pid=$procId"
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

function Stop-OrphanProcesses {
    param(
        [string]$Name,
        [object[]]$Procs
    )
    foreach ($p in $Procs) {
        if (-not $p) { continue }
        if (-not (Test-PidAlive -ProcessId $p.ProcessId)) { continue }
        Write-Host "stopped $Name orphan pid=$($p.ProcessId)"
        Stop-ProcessTree -ProcessId $p.ProcessId
    }
}

function Start-LoggedProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$StdOut,
        [string]$StdErr,
        [hashtable]$Environment
    )
    foreach ($key in $Environment.Keys) {
        Set-Item -Path "Env:$key" -Value $Environment[$key]
    }
    $dir = Split-Path -Parent $StdOut
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    foreach ($log in @($StdOut, $StdErr)) {
        if (Test-Path $log) { Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue }
    }
    return Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $StdOut -RedirectStandardError $StdErr
}

function Wait-HealthOk {
    param(
        [string]$Url,
        [int]$TimeoutSec = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Seconds 1
            continue
        }
        Start-Sleep -Seconds 1
    }
    return $false
}
