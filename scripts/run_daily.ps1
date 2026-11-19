# Runs one daily cycle: feed scan, queue refresh, Codex digest, podcast audio.
# Called by the scheduled task from install_schedule.ps1. Logs to data\logs\.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("daily-{0:yyyy-MM-dd}.log" -f (Get-Date))

# Make sure Ollama is serving before the scan needs it.
try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null }
catch { Start-Process -WindowStyle Hidden ollama "serve"; Start-Sleep -Seconds 8 }

& "$repo\.venv\Scripts\radar.exe" daily *>> $log
exit $LASTEXITCODE
