import re
import subprocess
from pathlib import Path

import pytest

from radar.builds import sandbox
from radar.builds.sandbox import (
    NETWORK,
    PROXY,
    api_key_env_file,
    build_argv,
    docker_ready,
    ensure_image,
    ensure_network_and_proxy,
    render_filter,
    render_proxy_conf,
    run_container,
    verify_argv,
)


def _proc(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class Recorder:
    def __init__(self, handler=None):
        self.calls = []
        self.envs = []
        self.handler = handler or (lambda argv: _proc())

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.envs.append(kw.get("env"))
        return self.handler(argv)


def test_build_argv_isolation(cfg, tmp_path):
    argv = build_argv(cfg, tmp_path, "rb-1", env_file=tmp_path / "x.env")
    assert argv.count("-v") == 1 and f"{tmp_path}:/work" in " ".join(argv)
    assert argv[argv.index("--network") + 1] == NETWORK
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--cpus") + 1] == "4" and argv[argv.index("--memory") + 1] == "8g"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert "no-new-privileges" in argv and "--rm" in argv
    assert f"HTTPS_PROXY=http://{PROXY}:8888" in argv and f"HTTP_PROXY=http://{PROXY}:8888" in argv
    assert "--privileged" not in argv and "host" not in argv
    assert not any("docker.sock" in a for a in argv)
    # The workdir is the only path allowed to carry a user name.
    rest = [a for a in argv if a != f"{tmp_path}:/work" and a != str(tmp_path / "x.env")]
    assert not any(a.startswith("PATH=") or "faiya" in a.lower() for a in rest)
    assert argv[-2:] == [cfg.builds.image, sandbox.ENTRY]


def test_only_fixed_env_flags(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_HOST_VAR", "nope")
    argv = build_argv(cfg, tmp_path, "rb-1", env_file=tmp_path / "x.env")
    values = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert {v.split("=")[0] for v in values} == {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "HOME",
        "PYTHONDONTWRITEBYTECODE",
        "MODEL",
        "TURNS",
        "BUILD_TIMEOUT_S",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
    assert "nope" not in " ".join(argv)
    assert "ANTHROPIC_API_KEY" not in " ".join(values)


def test_verify_argv_isolation(cfg, tmp_path):
    argv = verify_argv(cfg, tmp_path, "rv-1")
    assert argv.count("-v") == 1 and "--env-file" not in argv
    assert argv[argv.index("--network") + 1] == NETWORK
    script = argv[-1]
    assert (
        "pip install -r requirements.txt" in script
        and "pytest -q -p no:cacheprovider test_demo.py" in script
    )
    assert "/work/.verify.json" in script and "installed" in script and "tests_passed" in script


def test_env_file_holds_only_api_key(monkeypatch):
    monkeypatch.setenv("SECRET_HOST_VAR", "nope")
    with api_key_env_file("sk-test-123") as path:
        assert path.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-test-123\n"
        assert "nope" not in path.read_text(encoding="utf-8")
    assert not path.exists()


def test_env_file_deleted_on_error():
    with pytest.raises(RuntimeError), api_key_env_file("sk-test-123") as path:
        raise RuntimeError("boom")
    assert not path.exists()


def test_env_file_rejects_newline():
    with pytest.raises(ValueError), api_key_env_file("a\nB=c"):
        pass


def test_docker_cli_gets_minimal_env(cfg, monkeypatch):
    monkeypatch.setenv("SECRET_HOST_VAR", "nope")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    rec = Recorder()
    docker_ready(rec)
    ensure_image(cfg, rec)
    run_container(["docker", "run", "--name", "n"], 5, rec)
    for env in rec.envs:
        assert "SECRET_HOST_VAR" not in env and "ANTHROPIC_API_KEY" not in env
        assert "nope" not in env.values() and "leak" not in env.values()


def test_docker_not_running_reason():
    assert docker_ready(Recorder(lambda a: _proc(1, err="cannot connect"))) == (
        False,
        "docker not running",
    )
    assert docker_ready(Recorder()) == (True, "")


def test_docker_not_installed_reason():
    def boom(argv, **kw):
        raise FileNotFoundError("docker")

    assert docker_ready(boom) == (False, "docker not installed")


def test_timeout_kills_container():
    def handler(argv):
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, 1)
        raise OSError("kill failed")  # kill errors are ignored

    rec = Recorder(handler)
    code, msg = run_container(["docker", "run", "--name", "rb-9", "img"], 1, rec)
    assert code != 0 and "timed out" in msg
    assert ["docker", "kill", "rb-9"] in rec.calls


def test_run_container_returns_output():
    rec = Recorder(lambda a: _proc(3, "out", "err"))
    assert run_container(["docker", "run", "--name", "n"], 5, rec) == (3, "outerr")


def test_proxy_allowlist_rendering(cfg):
    text = render_filter(cfg.builds.allow_hosts)
    lines = text.splitlines()
    assert len(lines) == len(cfg.builds.allow_hosts)
    assert r"^(.*\.)?pypi\.org$" in lines
    assert all(re.fullmatch(r"\^\(\.\*\\\.\)\?[a-z0-9\\.\-]+\$", ln) for ln in lines)
    rx = re.compile(lines[0])
    assert rx.search("pypi.org") and rx.search("www.pypi.org")
    assert not rx.search("evilpypi.org") and not rx.search("pypi.org.evil.com")
    conf = render_proxy_conf()
    assert "FilterDefaultDeny Yes" in conf and "FilterType ere" in conf
    assert "FilterURLs Off" in conf and "ConnectPort 443" in conf


def test_ensure_image_skips_when_present(cfg):
    rec = Recorder()
    ensure_image(cfg, rec)
    assert not any(c[:2] == ["docker", "build"] for c in rec.calls)


def test_ensure_image_builds_when_missing(cfg):
    rec = Recorder(lambda a: _proc(1) if a[1:3] == ["image", "inspect"] else _proc())
    ensure_image(cfg, rec)
    build = [c for c in rec.calls if c[:2] == ["docker", "build"]]
    assert build and build[0][:4] == ["docker", "build", "-t", cfg.builds.image]
    assert Path(build[0][-1]).parts[-2:] == ("docker", "build")


def test_ensure_network_and_proxy_creates_everything(cfg):
    seen = {}

    def handler(argv):
        if argv[1:3] in (["network", "inspect"], ["inspect", "-f"]):
            return _proc(1)
        if argv[1] == "build":
            ctx = Path(argv[-1])
            seen["filter"] = (ctx / "filter").read_text(encoding="utf-8")
            seen["conf"] = (ctx / "tinyproxy.conf").read_text(encoding="utf-8")
        return _proc(0, "sha256:abc true")

    rec = Recorder(handler)
    ensure_network_and_proxy(cfg, rec)
    assert ["docker", "network", "create", "--internal", NETWORK] in rec.calls
    assert ["docker", "network", "connect", "bridge", PROXY] in rec.calls
    run = next(c for c in rec.calls if c[:2] == ["docker", "run"])
    assert NETWORK in run and PROXY in run and "-v" not in run and "--privileged" not in run
    assert seen["filter"] == render_filter(cfg.builds.allow_hosts)
    assert "FilterDefaultDeny Yes" in seen["conf"]


def test_ensure_network_and_proxy_noop_when_current(cfg):
    def handler(argv):
        if argv[1:3] == ["network", "inspect"]:
            return _proc(0, "true\n")
        if argv[1:3] == ["image", "inspect"]:
            return _proc(0, "sha256:abc")
        if argv[1] == "inspect":
            return _proc(0, "sha256:abc true")
        return _proc()

    rec = Recorder(handler)
    ensure_network_and_proxy(cfg, rec)
    assert not any(c[:3] == ["docker", "network", "create"] for c in rec.calls)
    assert not any(c[:2] == ["docker", "run"] for c in rec.calls)


def test_failure_raises(cfg):
    rec = Recorder(lambda a: _proc(1, err="denied"))
    with pytest.raises(RuntimeError, match="denied"):
        ensure_network_and_proxy(cfg, rec)


def test_docker_cli_env_passes_needed_vars(monkeypatch):
    monkeypatch.setenv("USERPROFILE", "C:/u")
    monkeypatch.setenv("PROGRAMDATA", "C:/pd")
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/x")
    env = sandbox._env()
    assert env["USERPROFILE"] == "C:/u" and env["DOCKER_HOST"] == "npipe:////./pipe/x"
    assert env["PROGRAMDATA"] == "C:/pd"


def test_filter_rejects_bad_host():
    for bad in ("a b.com", r"ev\il.com", "x.com/path", "a_b.com"):
        with pytest.raises(ValueError):
            render_filter([bad])
    assert render_filter(["a-b.example.com"]) == r"^(.*\.)?a-b\.example\.com$" + "\n"


def test_build_and_verify_no_bytecode_no_home_path(cfg, tmp_path):
    for argv in (build_argv(cfg, tmp_path, "n"), verify_argv(cfg, tmp_path, "n")):
        assert "PYTHONDONTWRITEBYTECODE=1" in argv and "HOME=/tmp/builder" in argv
        assert "/home/" not in " ".join(argv)
    assert "-p no:cacheprovider" in verify_argv(cfg, tmp_path, "n")[-1]


def test_remove_and_is_running():
    rec = Recorder(lambda a: _proc(0, "true\n"))
    sandbox.remove("c1", rec)
    assert rec.calls[0] == ["docker", "rm", "-f", "c1"]
    assert sandbox.is_running("c1", rec) is True
    assert sandbox.is_running("c1", Recorder(lambda a: _proc(0, "false\n"))) is False
    assert sandbox.is_running("c1", Recorder(lambda a: _proc(1, "", "no such"))) is False


def test_remove_and_is_running_survive_timeouts():
    def hang(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    sandbox.remove("c1", Recorder(hang))
    assert sandbox.is_running("c1", Recorder(hang)) is True


def test_run_decodes_utf8_progress_bars_on_any_locale():
    import sys

    # pip's progress bar: U+2501 is E2 94 81, and 0x81 is undefined in cp1252.
    code = r"import sys; sys.stdout.buffer.write(b'\xe2\x94\x81 done \x81')"
    proc = sandbox._run(subprocess.run, [sys.executable, "-c", code], timeout=60)
    assert proc.returncode == 0
    assert proc.stdout.startswith("\u2501 done ")


def test_build_argv_sets_in_container_time_limit_and_quiet_claude(cfg, tmp_path):
    cfg.builds.timeout_min = 7
    argv = build_argv(cfg, tmp_path, "rb-1")
    values = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "BUILD_TIMEOUT_S=420" in values
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in values
    assert "DISABLE_AUTOUPDATER=1" in values


def test_entry_script_wraps_claude_in_timeout():
    text = (sandbox.DOCKER_DIR / "build" / "entry.sh").read_text(encoding="utf-8")
    assert 'timeout --kill-after=30 "$BUILD_TIMEOUT_S" claude -p' in text


def test_env_file_prefix_and_stale_cleanup(tmp_path, monkeypatch):
    import os
    import tempfile
    import time

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    stale = tmp_path / "radar-key-old.env"
    stale.write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(stale, (old, old))
    fresh = tmp_path / "radar-key-new.env"
    fresh.write_text("ANTHROPIC_API_KEY=y\n", encoding="utf-8")
    other = tmp_path / "unrelated.env"
    other.write_text("x", encoding="utf-8")
    os.utime(other, (old, old))
    with api_key_env_file("sk-test-123") as path:
        assert path.name.startswith("radar-key-") and path.parent == tmp_path
        assert not stale.exists() and fresh.exists() and other.exists()
    assert not path.exists()


def test_remove_build_containers_removes_every_radar_build():
    def handler(argv):
        if argv[:2] == ["docker", "ps"]:
            return _proc(0, "abc123\ndef456\n")
        return _proc()

    rec = Recorder(handler)
    sandbox.remove_build_containers(rec)
    assert rec.calls[0] == ["docker", "ps", "-q", "--filter", "name=radar-build-"]
    assert rec.calls[1] == ["docker", "rm", "-f", "abc123", "def456"]
    assert all(set(env) <= set(sandbox._env()) for env in rec.envs)


def test_remove_build_containers_ignores_errors():
    def boom(argv, **kw):
        raise FileNotFoundError("docker")

    sandbox.remove_build_containers(boom)
    rec = Recorder(lambda a: _proc(0, ""))
    sandbox.remove_build_containers(rec)
    assert len(rec.calls) == 1  # nothing to remove


def test_existing_network_must_be_internal(cfg):
    def handler(argv):
        if argv[1:3] == ["network", "inspect"]:
            return _proc(0, "false\n")
        return _proc(0, "sha256:abc true")

    rec = Recorder(handler)
    with pytest.raises(RuntimeError, match="build network is not internal"):
        ensure_network_and_proxy(cfg, rec)
    assert not any(c[:2] == ["docker", "run"] for c in rec.calls)


def test_proxy_files_use_lf_newlines(cfg):
    seen = {}

    def handler(argv):
        if argv[1:3] == ["network", "inspect"]:
            return _proc(0, "true\n")
        if argv[1] == "build":
            ctx = Path(argv[-1])
            seen["filter"] = (ctx / "filter").read_bytes()
            seen["conf"] = (ctx / "tinyproxy.conf").read_bytes()
        return _proc(0, "sha256:abc true")

    ensure_network_and_proxy(cfg, Recorder(handler))
    assert b"\r\n" not in seen["filter"] and b"\r\n" not in seen["conf"]


def test_image_files_carry_pins_and_notes():
    proxy = (sandbox.DOCKER_DIR / "proxy" / "Dockerfile").read_text(encoding="utf-8")
    assert '"tinyproxy>=1.11.2"' in proxy and "FilterType" in proxy
    build = (sandbox.DOCKER_DIR / "build" / "Dockerfile").read_text(encoding="utf-8")
    assert "[builds] image" in build


def test_schedule_allows_four_hours():
    text = (sandbox.REPO_ROOT / "scripts" / "install_schedule.ps1").read_text(encoding="utf-8")
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 4)" in text
