"""Read and change the Windows scheduled tasks (`radar daily`; `radar build` in separate mode)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

TASK = "ResearchRadarDaily"
BUILD_TASK = "ResearchRadarBuild"
COMMANDS = {TASK: "daily", BUILD_TASK: "build"}  # task name -> the radar command it runs
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _ps(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get(task: str = TASK) -> dict:
    if task not in COMMANDS:
        raise ValueError(f"unknown task {task!r}")
    if os.name != "nt":
        return {"supported": False}
    script = (
        f"$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{ exists=$true; enabled=($t.State -ne 'Disabled'); state=[string]$t.State; "
        "time=([datetime]$t.Triggers[0].StartBoundary).ToString('HH:mm'); "
        "next=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { $null }); "
        "last=$(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('s') } else { $null }); "
        "lastResult=$i.LastTaskResult } | ConvertTo-Json } "
        "else { '{\"exists\": false}' }"
    )
    proc = _ps(script)
    try:
        data = json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        data = {"exists": False, "error": proc.stderr.strip()[:300]}
    data["supported"] = True
    return data


def set_schedule(
    root: Path, time: str | None = None, enabled: bool | None = None, task: str = TASK
) -> dict:
    if task not in COMMANDS:
        raise ValueError(f"unknown task {task!r}")
    if os.name != "nt":
        raise RuntimeError("scheduling is supported on Windows only")
    if time is not None:
        if not _TIME.match(time):
            raise ValueError("time must be HH:MM, 24-hour")
        script = root / "scripts" / "install_schedule.ps1"
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Time",
                time,
                "-TaskName",
                task,
                "-Command",
                COMMANDS[task],
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:500] or "could not register the task")
    if enabled is not None:
        verb = "Enable" if enabled else "Disable"
        proc = _ps(f"{verb}-ScheduledTask -TaskName '{task}' | Out-Null")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:500] or f"could not {verb.lower()} the task")
    return get(task)
