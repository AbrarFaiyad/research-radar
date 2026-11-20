# Registers a daily Windows scheduled task that runs scripts\run_daily.ps1.
# Usage:   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1 -Time 07:00
# Remove:  Unregister-ScheduledTask -TaskName ResearchRadarDaily -Confirm:$false
param(
    [string]$Time = "07:00",
    [string]$TaskName = "ResearchRadarDaily"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_daily.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Runs only when you are logged in (the scan uses a visible browser window), and catches up
# if the machine was asleep at the scheduled time.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered '$TaskName' daily at $Time. Logs: $repo\data\logs\"
