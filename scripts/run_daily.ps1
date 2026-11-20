# Runs one radar command from the scheduled task (default: the daily cycle of feed scan, queue
# refresh, Codex digest, podcast audio, build). Called by install_schedule.ps1. Logs to data\logs\.
param([string]$Command = "daily")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("{0}-{1:yyyy-MM-dd}.log" -f $Command, (Get-Date))

if ($Command -eq "daily") {
    # Make sure Ollama is serving before the scan needs it.
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null }
    catch { Start-Process -WindowStyle Hidden ollama "serve"; Start-Sleep -Seconds 8 }
}

& "$repo\.venv\Scripts\radar.exe" $Command *>> $log
exit $LASTEXITCODE
