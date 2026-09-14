# Stop keel-api + keel-worker + frontend monitor started by keel-up.ps1.
# Also sweeps leftover processes matching Keel command lines (no pidfile).
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\keel-down.ps1
#        .\scripts\keel-down.cmd
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_keel_win.ps1"

$cfg = Get-KeelStackConfig
Set-Location $cfg.Root

Stop-PidFileProcess -Name "keel-ui" -PidFile $cfg.UiPid
Stop-PidFileProcess -Name "keel-worker" -PidFile $cfg.WorkerPid
Stop-PidFileProcess -Name "keel-api" -PidFile $cfg.ApiPid

$found = Find-KeelStackProcesses -Root $cfg.Root
Stop-OrphanProcesses -Name "keel-ui" -Procs $found.Ui
Stop-OrphanProcesses -Name "keel-worker" -Procs $found.Worker
Stop-OrphanProcesses -Name "keel-api" -Procs $found.Api

Write-Host "Keel stack down."
