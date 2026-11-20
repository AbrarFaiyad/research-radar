# Registers a daily Windows scheduled task that runs scripts\run_daily.ps1.
# Usage:   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1 -Time 07:00
# Build task (builds.mode = separate):
#          ... -File scripts\install_schedule.ps1 -Time 08:30 -TaskName ResearchRadarBuild -Command build
# Remove:  Unregister-ScheduledTask -TaskName ResearchRadarDaily -Confirm:$false
param(
    [string]$Time = "07:00",
    [string]$TaskName = "ResearchRadarDaily",
    [string]$Command = "daily"
)
$ErrorActionPreference = "Stop"
if ($Command -notmatch '^[a-z-]+$') { throw "Command must be a radar command name such as daily or build" }
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_daily.ps1"
$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
if ($Command -ne "daily") { $argument += " -Command $Command" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument $argument -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Runs only when you are logged in (the scan uses a visible browser window), and catches up
# if the machine was asleep at the scheduled time.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered '$TaskName' (radar $Command) daily at $Time. Logs: $repo\data\logs\"
