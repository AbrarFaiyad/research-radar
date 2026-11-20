"""Docker sandbox for builds: internal network, allowlisting proxy, locked-down container.

Privacy rules: the container gets no host environment. The docker CLI itself runs with a
minimal environment, and the API key travels only in a temporary --env-file that is deleted
once the container exits.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from radar.config import REPO_ROOT

NETWORK = "radar-builds-internal"
PROXY = "radar-builds-proxy"
PROXY_IMAGE = "radar-builds-proxy:1"
PROXY_URL = f"http://{PROXY}:8888"
DOCKER_DIR = REPO_ROOT / "docker"
ENTRY = "/usr/local/bin/entry.sh"
_HOST_RE = re.compile(r"[a-z0-9.-]+")

VERIFY_SCRIPT = (
    "cd /work && rm -f .verify.json; installed=false; passed=false; "
    "if python -m venv /tmp/v && /tmp/v/bin/pip install -r requirements.txt; then "
    "installed=true; "
    "if /tmp/v/bin/python -m pytest -q -p no:cacheprovider test_demo.py; then passed=true; fi; "
    "fi; "
    'printf \'{ "installed": %s, "tests_passed": %s }\\n\' "$installed" "$passed" '
    "> /work/.verify.json"
)


def _env() -> dict[str, str]:
    """Minimal environment for the docker CLI: nothing from the host beyond what it must have."""
    keep = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    )
    # os.environ keys are upper-cased on Windows, so the names above match there too.
    return {k: os.environ[k] for k in keep if k in os.environ}


def _run(run, argv: list[str], timeout: float | None = None):
    return run(argv, env=_env(), capture_output=True, text=True, timeout=timeout)


def _check(proc, what: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"docker {what} failed: {detail}")


def docker_ready(run=subprocess.run) -> tuple[bool, str]:
    try:
        proc = _run(run, ["docker", "info"], timeout=30)
    except FileNotFoundError:
        return False, "docker not installed"
    except subprocess.TimeoutExpired:
        return False, "docker not running"
    if proc.returncode != 0:
        return False, "docker not running"
    return True, ""


def render_filter(hosts: list[str]) -> str:
    """One anchored regex per host; it also matches subdomains."""
    lines = []
    for host in hosts:
        host = host.strip().lower()
        if not host:
            continue
        if not _HOST_RE.fullmatch(host):
            raise ValueError(f"invalid allow_hosts entry: {host!r}")
        lines.append("^(.*\\.)?" + host.replace(".", "\\.") + "$")
    return "\n".join(lines) + "\n"


def render_proxy_conf() -> str:
    return (DOCKER_DIR / "proxy" / "tinyproxy.conf.tmpl").read_text(encoding="utf-8")


def ensure_network_and_proxy(cfg, run=subprocess.run) -> None:
    if _run(run, ["docker", "network", "inspect", NETWORK]).returncode != 0:
        _check(_run(run, ["docker", "network", "create", "--internal", NETWORK]), "network create")

    with tempfile.TemporaryDirectory() as ctx:
        ctx_dir = Path(ctx)
        shutil.copy(DOCKER_DIR / "proxy" / "Dockerfile", ctx_dir / "Dockerfile")
        (ctx_dir / "tinyproxy.conf").write_text(render_proxy_conf(), encoding="utf-8")
        (ctx_dir / "filter").write_text(render_filter(cfg.builds.allow_hosts), encoding="utf-8")
        _check(_run(run, ["docker", "build", "-t", PROXY_IMAGE, str(ctx_dir)]), "proxy build")

    image_id = _run(run, ["docker", "image", "inspect", "-f", "{{.Id}}", PROXY_IMAGE])
    current = _run(run, ["docker", "inspect", "-f", "{{.Image}} {{.State.Running}}", PROXY])
    if current.returncode == 0 and current.stdout.strip() == f"{image_id.stdout.strip()} true":
        return
    _run(run, ["docker", "rm", "-f", PROXY])
    start = ["docker", "run", "-d", "--name", PROXY, "--network", NETWORK]
    start += ["--restart", "unless-stopped", "--cap-drop", "ALL", PROXY_IMAGE]
    _check(_run(run, start), "proxy start")
    _check(_run(run, ["docker", "network", "connect", "bridge", PROXY]), "proxy bridge")


def image_exists(cfg, run=subprocess.run) -> bool:
    try:
        return (
            _run(run, ["docker", "image", "inspect", cfg.builds.image], timeout=30).returncode == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ensure_image(cfg, run=subprocess.run) -> None:
    if image_exists(cfg, run):
        return
    build = ["docker", "build", "-t", cfg.builds.image, str(DOCKER_DIR / "build")]
    _check(_run(run, build), "image build")


@contextlib.contextmanager
def api_key_env_file(api_key: str) -> Iterator[Path]:
    """Temp env file holding only the API key, owner-only, deleted on exit."""
    if not api_key or "\n" in api_key or "\r" in api_key:
        raise ValueError("invalid API key")
    fd, name = tempfile.mkstemp(prefix="radar-env-", suffix=".env")  # mkstemp: mode 0600
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"ANTHROPIC_API_KEY={api_key}\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _base_argv(cfg, name: str) -> list[str]:
    b = cfg.builds
    argv = ["docker", "run", "--rm", "--name", name, "--network", NETWORK]
    argv += ["--cpus", str(b.cpus), "--memory", f"{b.memory_gb}g", "--pids-limit", "512"]
    argv += ["--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--user", "1000:1000"]
    argv += ["--env", f"HTTPS_PROXY={PROXY_URL}", "--env", f"HTTP_PROXY={PROXY_URL}"]
    argv += ["--env", "HOME=/tmp/builder", "--env", "PYTHONDONTWRITEBYTECODE=1"]
    return argv


def build_argv(cfg, workdir: Path, name: str, env_file: Path | None = None) -> list[str]:
    """argv for the Claude build container. `env_file` carries ANTHROPIC_API_KEY only."""
    argv = _base_argv(cfg, name)
    argv += ["--env", f"MODEL={cfg.builds.claude_model}", "--env", f"TURNS={cfg.builds.max_turns}"]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return argv + ["-v", f"{workdir}:/work", cfg.builds.image, ENTRY]


def verify_argv(cfg, workdir: Path, name: str) -> list[str]:
    """argv for the verify container: install requirements, run the demo test.

    No API key: verification never needs it.
    """
    argv = _base_argv(cfg, name)
    return argv + ["-v", f"{workdir}:/work", cfg.builds.image, "sh", "-c", VERIFY_SCRIPT]


def run_container(argv: list[str], timeout_s: float, run=subprocess.run) -> tuple[int, str]:
    name = argv[argv.index("--name") + 1]
    try:
        proc = _run(run, argv, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            _run(run, ["docker", "kill", name], timeout=30)
        return 124, f"timed out after {int(timeout_s)} s"
    except FileNotFoundError:
        return 127, "docker not installed"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def remove(name: str, run=subprocess.run) -> None:
    """Force-remove a container; a missing one is fine, and so is a daemon that times out."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        _run(run, ["docker", "rm", "-f", name], timeout=60)


def is_running(name: str, run=subprocess.run) -> bool:
    """True if the container runs, or if that cannot be determined (timeout)."""
    try:
        proc = _run(run, ["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=30)
    except subprocess.TimeoutExpired:
        return True
    return proc.returncode == 0 and proc.stdout.strip() == "true"
