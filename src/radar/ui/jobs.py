"""Run one `radar` command at a time as a subprocess and keep its output for the panel."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

ACTIONS: dict[str, list[str]] = {
    "daily": ["daily"],
    "scan": ["scan"],
    "digest": ["digest"],
    "podcast": ["podcast"],
    "email": ["email"],
    "reclassify": ["reclassify"],
    "doctor": ["doctor"],
    "demo": ["demo"],
    "login": ["login"],
    "notebooklm-login": ["notebooklm-login"],
}


class JobBusy(RuntimeError):
    pass


@dataclass
class Job:
    action: str
    argv: list[str]
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=5000))
    offset: int = 0  # number of lines dropped from the front of `lines`
    proc: subprocess.Popen | None = None
    guard: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.finished is None

    def summary(self) -> dict:
        return {
            "action": self.action,
            "started": self.started,
            "finished": self.finished,
            "returncode": self.returncode,
            "running": self.running,
            "lines": self.offset + len(self.lines),
        }


class JobRunner:
    def __init__(
        self,
        root: Path,
        data_dir: Path,
        python: str = sys.executable,
        config: Path | None = None,
    ):
        self.root, self.data_dir, self.python, self.config = root, data_dir, python, config
        self.current: Job | None = None
        self._lock = threading.Lock()

    def start(self, action: str, extra: list[str] | None = None) -> Job:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self._lock:
            if self.current and self.current.running:
                raise JobBusy(f"{self.current.action} is still running")
            argv = [
                self.python,
                "-m",
                "radar.cli",
                "--data-dir",
                str(self.data_dir),
                *(["--config", str(self.config)] if self.config else []),
                *ACTIONS[action],
                *(extra or []),
            ]
            job = Job(action, argv)
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            job.proc = subprocess.Popen(
                argv,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=flags,
            )
            self.current = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        for line in job.proc.stdout:
            with job.guard:
                if len(job.lines) == job.lines.maxlen:
                    job.offset += 1
                job.lines.append(line.rstrip("\n"))
        job.returncode = job.proc.wait()
        job.finished = time.time()

    def log(self, since: int = 0) -> dict:
        job = self.current
        if job is None:
            return {"job": None, "lines": [], "next": 0}
        with job.guard:
            offset, snapshot = job.offset, list(job.lines)
        start = max(since - offset, 0)
        return {"job": job.summary(), "lines": snapshot[start:], "next": offset + len(snapshot)}

    def cancel(self) -> bool:
        job = self.current
        if not job or not job.running or not job.proc:
            return False
        if os.name == "nt":  # kill the whole tree, including the agent browser
            subprocess.run(
                ["taskkill", "/PID", str(job.proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            job.proc.terminate()
        return True
