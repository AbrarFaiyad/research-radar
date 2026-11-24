import base64
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
import yaml

from radar.builds import scan

TEMPLATE = Path(scan.__file__).parent / "template"
FOLDER = "2026-11-20_numpy_array-basics"
FAKE_KEY = "sk-ant-oat01-" + "x" * 40
BOT = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SHA_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


# ---------- workflow ----------


def _wf():
    return yaml.safe_load((TEMPLATE / "build.yml").read_text(encoding="utf-8"))


def _steps(wf):
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def test_workflow_triggers_and_concurrency():
    wf = _wf()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads `on` as the boolean True
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(on) == {"workflow_dispatch"}
    assert set(inputs) == {"folder", "prompt"}
    assert all(i["required"] and i["type"] == "string" for i in inputs.values())
    assert wf["permissions"] == {}
    assert wf["concurrency"] == {"group": "daily-builds", "cancel-in-progress": False}
    assert wf["jobs"]["build"]["timeout-minutes"] == 60


def test_workflow_permissions_and_secret_scope():
    wf = _wf()
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert wf["jobs"]["cleanup"]["permissions"] == {"actions": "write"}
    for job in wf["jobs"].values():
        assert "secrets." not in json.dumps(job.get("env", {}))
    assert wf["jobs"]["publish"]["needs"] == "build"
    assert "secrets." not in json.dumps(wf["jobs"]["publish"])
    assert "secrets." not in json.dumps({k: v for k, v in wf.items() if k != "jobs"})
    holders = [s for s in wf["jobs"]["build"]["steps"] if "secrets." in json.dumps(s)]
    assert [h["name"] for h in holders] == [*CLAUDE_STEPS, TRIPWIRE]
    for h in holders:  # the token is in these steps' env only, under no other name
        assert h["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    for h in holders[:3]:
        assert "--permission-mode bypassPermissions" in h["run"]
    assert "ANTHROPIC_API_KEY" not in (TEMPLATE / "build.yml").read_text(encoding="utf-8")


def test_workflow_inputs_only_through_env():
    for step in _steps(_wf()):
        assert "${{" not in step.get("run", ""), step.get("run")
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    assert "${{ inputs.folder }}" in text
    assert "inputs.prompt" not in text  # the prompt is read from the event file
    for step in _steps(_wf()):
        assert "inputs.prompt" not in json.dumps(step.get("env", {}))
    assert "github.event" not in text


def test_workflow_actions_pinned_to_sha():
    uses = [s["uses"] for s in _steps(_wf()) if "uses" in s]
    assert uses
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    for u in uses:
        assert SHA_RE.match(u), u
        assert re.search(re.escape(u) + r"\s+#\s*v\d", text), u


CLAUDE_STEPS = ["Plan with Claude", "Implement with Claude", "Review with Claude"]
TRIPWIRE = "Tripwire for the subscription token"


def _build_steps():
    return _wf()["jobs"]["build"]["steps"]


def _index(name):
    return next(i for i, s in enumerate(_build_steps()) if s.get("name") == name)


def test_artifact_is_short_lived_and_visible_files_only():
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    assert "include-hidden-files" not in text
    steps = _build_steps()
    up = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert up["with"]["retention-days"] == 1 and up["with"]["path"] == "/tmp/out"
    assert steps.index(up) > _index(TRIPWIRE)
    trip = steps[_index(TRIPWIRE)]
    assert trip["if"] == "always()" and trip["id"] == "tripwire"
    assert up["if"] == "always() && steps.tripwire.outcome == 'success'"
    assert "/usr/bin/python3 -I -" in trip["run"]


def test_publish_reads_artifact_from_runner_temp():
    steps = _wf()["jobs"]["publish"]["steps"]
    assert not any("DELETE" in s.get("run", "") for s in steps)
    dl = next(s for s in steps if "download-artifact" in s.get("uses", ""))
    assert dl["with"]["path"] == "${{ runner.temp }}/artifact"
    assert '--src "$RUNNER_TEMP/artifact"' in "".join(s.get("run", "") for s in steps)
    assert _wf()["jobs"]["publish"]["timeout-minutes"] == 10


def test_cleanup_job_deletes_artifacts_even_when_cancelled():
    job = _wf()["jobs"]["cleanup"]
    assert job["needs"] == ["build", "publish"] and job["if"] == "always()"
    assert job["permissions"] == {"actions": "write"}
    (step,) = job["steps"]
    assert "-X DELETE" in step["run"] and step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "secrets." not in json.dumps(job)


def test_untrusted_code_runs_outside_home_runner_without_sudo():
    steps = _build_steps()
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    assert "/home" not in text
    for name, minutes in (
        ("Plan with Claude", 10),
        ("Implement with Claude", 25),
        ("Review with Claude", 15),
        ("Run the notebook", 20),
    ):
        step = steps[_index(name)]
        home = "/tmp/nb-home" if name == "Run the notebook" else "/tmp/build-home"
        assert step["env"]["HOME"] == home
        assert step["timeout-minutes"] == minutes
        assert "cd /tmp/out/work" in step["run"]
    sudo = next(
        i for i, s in enumerate(steps) if "sudo rm -f /etc/sudoers.d/runner" in s.get("run", "")
    )
    drop = steps[sudo]["run"]
    assert drop.index("systemctl stop docker.socket docker.service") < drop.index("sudo rm -f")
    assert sudo < _index("Plan with Claude") < _index("Run the notebook")
    # the drop is verified: the step fails if sudo or docker still works
    assert "if sudo -n true" in drop and "if docker info" in drop and drop.count("exit 1") == 2
    assert drop.index("sudo rm -f") < drop.index("if sudo -n true") < drop.index("if docker info")


def _tripwire(tmp_path, key, setup):
    run = _build_steps()[_index(TRIPWIRE)]["run"]
    code = run.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]  # YAML already dedented it
    out = tmp_path / "out"
    code = code.replace("/tmp/out", str(out).replace("\\", "/"))
    (out / "work").mkdir(parents=True)
    (out / "status.json").write_text('{"ran": true}')
    setup(out / "work")
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": key}
    subprocess.run([sys.executable, "-I", "-c", code], env=env, check=True, capture_output=True)
    return json.loads((out / "status.json").read_text()), out / "work"


KEY = "sk-ant-api03-Zq9" + "k3Lm2" * 6


def _put(content):
    return lambda work: (work / "demo.ipynb").write_bytes(content)


def _assert_blocked(status, work, why="unscannable"):
    assert status == {"ran": False, "blocked": True, "why": why}
    assert work.is_dir() and list(work.iterdir()) == []


@pytest.mark.parametrize("form", ["raw", "hex", "b64-0", "b64-1", "b64-2", "wrapped"])
def test_tripwire_catches_key_forms(tmp_path, form):
    kb = KEY.encode()
    b64 = base64.b64encode(kb)
    blob = {
        "raw": kb,
        "hex": kb.hex().encode(),
        "b64-0": b64,
        "b64-1": base64.b64encode(b"a" + kb),
        "b64-2": base64.b64encode(b"ab" + kb),
        "wrapped": b"\n".join(b64[i : i + 12] for i in range(0, len(b64), 12)),
    }[form]
    _assert_blocked(*_tripwire(tmp_path, KEY, _put(b"xx " + blob + b" yy")), why="key")


@pytest.mark.parametrize("urlsafe", [False, True])
@pytest.mark.parametrize("before", [0, 1, 2])
@pytest.mark.parametrize("length", [40, 41, 42, 77, 120])
def test_tripwire_catches_key_with_bytes_on_both_sides(tmp_path, length, before, urlsafe):
    key = ("sk-ant-api03-" + "Zq9k3Lm2Xy7" * 12)[:length]
    enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    blob = enc(b"\xfb" * before + key.encode() + b"\xff\xfe\xfd")
    _assert_blocked(*_tripwire(tmp_path, key, _put(b'{"v": "' + blob + b'"}')), why="key")


def test_tripwire_leaves_clean_output(tmp_path):
    status, work = _tripwire(tmp_path, KEY, _put(b"hello"))
    assert status == {"ran": True}
    assert (work / "demo.ipynb").exists()


def test_tripwire_fails_closed_on_dangling_symlink(tmp_path):
    def setup(work):
        try:
            os.symlink(tmp_path / "missing", work / "dangling")
        except OSError:
            pytest.skip("symlinks not permitted")

    _assert_blocked(*_tripwire(tmp_path, KEY, setup))


def test_tripwire_fails_closed_on_symlink_to_outside_file(tmp_path):
    (tmp_path / "outside.txt").write_text("hello")

    def setup(work):
        try:
            os.symlink(tmp_path / "outside.txt", work / "link.txt")
        except OSError:
            pytest.skip("symlinks not permitted")

    _assert_blocked(*_tripwire(tmp_path, KEY, setup))


def test_tripwire_fails_closed_on_oversize_file(tmp_path):
    _assert_blocked(*_tripwire(tmp_path, KEY, _put(b"a" * (5 * 1024 * 1024 + 1))))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_tripwire_fails_closed_on_unreadable_file(tmp_path):
    def setup(work):
        (work / "secret.txt").write_text("x")
        (work / "secret.txt").chmod(0)

    if os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    _assert_blocked(*_tripwire(tmp_path, KEY, setup))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_tripwire_fails_closed_on_fifo(tmp_path):
    _assert_blocked(*_tripwire(tmp_path, KEY, lambda work: os.mkfifo(work / "pipe")))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_tripwire_wipes_read_only_subfolder(tmp_path):
    def setup(work):
        (work / "sub").mkdir()
        (work / "sub" / "f.txt").write_text(KEY)
        (work / "sub").chmod(0o500)

    _assert_blocked(*_tripwire(tmp_path, KEY, setup), why="key")


def test_build_job_only_fetches_the_phase_files_and_removes_the_checkout():
    steps = _build_steps()
    checkouts = [s for s in steps if "checkout" in s.get("uses", "")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"] == {"sparse-checkout": "tools/phases", "persist-credentials": False}
    keep = steps[_index("Keep the phase instructions")]["run"]
    for name in ("plan", "implement", "review"):
        assert f"tools/phases/{name}.md" in keep
    assert 'find "$GITHUB_WORKSPACE" -mindepth 1 -delete' in keep
    assert steps.index(checkouts[0]) < _index("Keep the phase instructions")
    assert _index("Keep the phase instructions") < _index("Plan with Claude")


def test_template_readme_has_index_table():
    text = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    assert "| Date | Package | What it shows | Ran |" in text


# ---------- publish tool ----------


def _chunk(kind, body=b""):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def make_png(*extra):
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + b"".join(extra)
        + _chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + _chunk(b"IEND")
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def nb(cells):
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": [text]}


def code(outputs=(), src="print(1)"):
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": 1,
        "source": [src],
        "outputs": list(outputs),
    }


def display(data):
    return {"output_type": "display_data", "metadata": {}, "data": data}


class Env:
    def __init__(self, repo, art, mod):
        self.repo, self.art, self.mod = repo, art, mod
        self.work = art / "work"
        self.work.mkdir()

    def run(self, folder=FOLDER):
        return self.mod.main(["--folder", folder, "--src", str(self.art), "--repo", str(self.repo)])

    def commits(self):
        return int(_git(self.repo, "rev-list", "--count", "HEAD"))

    def status(self, ran=True, **extra):
        (self.art / "status.json").write_text(json.dumps({"ran": ran, **extra}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(origin), str(repo))
    (repo / "tools").mkdir()
    shutil.copy(TEMPLATE / "publish_build.py", repo / "tools" / "publish_build.py")
    shutil.copy(Path(scan.__file__), repo / "tools" / "scan.py")
    shutil.copy(TEMPLATE / "README.md", repo / "README.md")
    ident = ["-c", "user.name=Seed", "-c", "user.email=seed@example.invalid"]
    _git(repo, "add", "-A")
    _git(repo, *ident, "commit", "-m", "seed")
    _git(repo, "push", "origin", "HEAD:main")
    monkeypatch.delenv("RADAR_PRIVATE_MARKERS", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    art = tmp_path / "artifact"
    art.mkdir()
    monkeypatch.syspath_prepend(str(repo / "tools"))
    monkeypatch.delitem(sys.modules, "scan", raising=False)
    spec = importlib.util.spec_from_file_location(
        "publish_build_t", repo / "tools" / "publish_build.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield Env(repo, art, mod)
    sys.modules.pop("scan", None)


def write_nb(e, cells, name="demo.ipynb"):
    (e.work / name).write_text(json.dumps(nb(cells)), encoding="utf-8")


def published_nb(e):
    return json.loads((e.repo / FOLDER / "demo.ipynb").read_text(encoding="utf-8"))


def test_publish_commits_as_bot_in_utc(env):
    env.status()
    write_nb(env, [md("# Hello"), code()])
    (env.work / "requirements.txt").write_text("numpy\n")
    (env.work / ".hidden").write_text("x")
    assert env.run() == 0
    assert env.commits() == 2
    out = _git(env.repo, "log", "-1", "--format=%an|%ae|%cn|%ce|%ad|%cd|%s", "--date=iso")
    an, ae, cn, ce, ad, cd, subj = out.split("|")
    assert (an, ae, cn, ce) == (BOT, BOT_EMAIL, BOT, BOT_EMAIL)
    assert ad.endswith("+0000") and cd.endswith("+0000")
    assert subj == f"Build {FOLDER}"
    result = json.loads((env.repo / FOLDER / "build-result.json").read_text())
    assert result == {"status": "published", "folder": FOLDER}
    assert not (env.repo / FOLDER / ".hidden").exists()
    assert not (env.repo / FOLDER / "status.json").exists()
    readme = (env.repo / "README.md").read_text(encoding="utf-8")
    assert "| 2026-11-20 | numpy | array-basics | yes |" in readme
    _git(env.repo, "push", "origin", "HEAD:main")


def test_index_newest_first(env):
    env.status()
    write_nb(env, [md("a")])
    assert env.run("2026-11-19_scipy_fft") == 0
    assert env.run(FOLDER) == 0
    rows = [ln for ln in (env.repo / "README.md").read_text().splitlines() if "| 2026-" in ln]
    assert "numpy" in rows[0] and "scipy" in rows[1]


def test_key_in_markdown_cell_blocks(env, capsys):
    env.status()
    write_nb(env, [md("token " + FAKE_KEY)])
    before = env.commits()
    assert env.run() == 2
    assert env.commits() == before
    assert not (env.repo / FOLDER).exists()
    assert FAKE_KEY not in capsys.readouterr().out


def test_png_text_chunk_in_notebook_is_sanitized(env):
    env.status()
    dirty = make_png(_chunk(b"tEXt", b"Comment\x00" + FAKE_KEY.encode()))
    lines = base64.encodebytes(dirty).decode().splitlines(keepends=True)  # list of lines
    write_nb(env, [code([display({"image/png": lines, "text/plain": ["<Figure>"]})])])
    assert env.run() == 0
    out = published_nb(env)["cells"][0]["outputs"][0]["data"]
    png = base64.b64decode(out["image/png"])
    assert png == scan.sanitize_png(dirty)
    assert b"tEXt" not in png
    assert FAKE_KEY not in (env.repo / FOLDER / "demo.ipynb").read_text()


def test_html_js_jpeg_outputs_dropped(env):
    env.status()
    data = {
        "text/html": ["<img src='http://evil.example/x'>"],
        "application/javascript": ["alert(1)"],
        "image/jpeg": "AAAA",
        "image/gif": "AAAA",
        "image/png": base64.b64encode(make_png()).decode(),
        "text/plain": ["ok"],
    }
    write_nb(env, [code([display(data)])])
    assert env.run() == 0
    out = published_nb(env)["cells"][0]["outputs"][0]["data"]
    assert set(out) == {"image/png", "text/plain"}


@pytest.mark.parametrize(
    "svg",
    [
        "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        "<svg xmlns='http://www.w3.org/2000/svg' onload='x()'></svg>",
        "<svg xmlns='http://www.w3.org/2000/svg'><a href='http://evil.example'>a</a></svg>",
        "<svg xmlns='http://www.w3.org/2000/svg'><foreignObject/></svg>",
    ],
)
def test_scripted_svg_dropped(env, svg):
    env.status()
    clean = "<svg xmlns='http://www.w3.org/2000/svg'><rect width='1' height='1'/></svg>"
    write_nb(env, [code([display({"image/svg+xml": [svg]}), display({"image/svg+xml": [clean]})])])
    assert env.run() == 0
    outs = published_nb(env)["cells"][0]["outputs"]
    assert "image/svg+xml" not in outs[0]["data"]
    assert outs[1]["data"]["image/svg+xml"]


def test_result_carries_run_id(env, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    env.status()
    write_nb(env, [md("# Hello"), code()])
    assert env.run() == 0
    result = json.loads((env.repo / FOLDER / "build-result.json").read_text())
    assert result == {"status": "published", "folder": FOLDER, "run_id": 4242}


def test_did_not_run_adds_banner(env):
    env.status(ran=False)
    write_nb(env, [md("# Title"), code()])
    assert env.run() == 0
    cells = published_nb(env)["cells"]
    assert cells[0]["cell_type"] == "markdown"
    assert "did not finish running on the build machine" in "".join(cells[0]["source"])
    assert len(cells) == 3
    result = json.loads((env.repo / FOLDER / "build-result.json").read_text())
    assert result == {"status": "did_not_run", "folder": FOLDER}
    assert "| no |" in (env.repo / "README.md").read_text()


def test_blocked_status_exits_2(env, capsys):
    env.status(ran=False, blocked=True)
    write_nb(env, [md("a")])
    before = env.commits()
    assert env.run() == 2
    assert "build output contained the subscription token" in capsys.readouterr().out
    assert env.commits() == before and not (env.repo / FOLDER).exists()


def test_banner_gets_id_for_nbformat_5(env):
    env.status(ran=False)
    write_nb(env, [md("a")])
    assert env.run() == 0
    assert published_nb(env)["cells"][0]["id"] == "build-banner"


def test_git_uses_allowlisted_env_and_no_hooks(env, monkeypatch):
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    e = env.mod._git_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in e and "GIT_TRACE" not in e
    assert e["GIT_CONFIG_NOSYSTEM"] == "1" and e["TZ"] == "UTC"
    hooks = env.repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-commit").write_text('#!/bin/sh\ntouch "$PWD/hook-ran"\n')
    (hooks / "post-commit").chmod(0o755)
    env.status()
    write_nb(env, [md("a")])
    assert env.run() == 0
    assert not (env.repo / "hook-ran").exists()


def test_png_file_sanitized(env):
    env.status()
    write_nb(env, [md("a")])
    dirty = make_png(_chunk(b"tEXt", b"Comment\x00hello"))
    (env.work / "plot.png").write_bytes(dirty)
    assert env.run() == 0
    assert (env.repo / FOLDER / "plot.png").read_bytes() == scan.sanitize_png(dirty)


def test_marker_from_env_blocks(env, monkeypatch):
    env.status()
    write_nb(env, [md("owner is fakeowner.example")])
    monkeypatch.setenv("RADAR_PRIVATE_MARKERS", "fakeowner.example")
    assert env.run() == 2


def test_symlink_in_artifact_blocks(env):
    env.status()
    write_nb(env, [md("a")])
    target = env.art.parent / "outside.txt"
    target.write_text("secret")
    try:
        os.symlink(target, env.work / "link.txt")
    except OSError:  # Windows without symlink rights: a directory junction is also a link
        if sys.platform != "win32":
            pytest.skip("symlinks not permitted")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(env.work / "link"), str(env.art.parent)],
            check=True,
            capture_output=True,
        )
    before = env.commits()
    assert env.run() == 2
    assert env.commits() == before and not (env.repo / FOLDER).exists()


def test_dotdot_folder_blocks(env):
    env.status()
    write_nb(env, [md("a")])
    assert env.run("../escape") == 2
    assert not (env.repo.parent / "escape").exists()


def test_oversized_file_blocks(env):
    env.status()
    write_nb(env, [md("a")])
    (env.work / "big.txt").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    assert env.run() == 2
    assert not (env.repo / FOLDER).exists()


def test_existing_folder_refused(env):
    env.status()
    write_nb(env, [md("a")])
    assert env.run() == 0
    before = env.commits()
    assert env.run() == 1
    assert env.commits() == before


def test_invalid_notebook_json_blocks(env):
    env.status()
    (env.work / "demo.ipynb").write_text("{not json")
    assert env.run() == 2


def test_hostile_png_in_notebook_blocks(env):
    env.status()
    bad = base64.b64encode(b"not a png at all").decode()
    write_nb(env, [code([display({"image/png": bad})])])
    assert env.run() == 2


# ---------- three Claude passes ----------


@pytest.mark.parametrize(
    "name,model,turns,phase,log",
    [
        ("Plan with Claude", "sonnet", 20, "plan", "claude-plan"),
        ("Implement with Claude", "haiku", 60, "implement", "claude-implement"),
        ("Review with Claude", "sonnet", 30, "review", "claude-review"),
    ],
)
def test_claude_passes_use_the_owner_models_and_prompt_plus_phase(name, model, turns, phase, log):
    step = _build_steps()[_index(name)]
    run = step["run"]
    flags = f"claude -p --model {model} --max-turns {turns} --permission-mode bypassPermissions"
    assert flags in run
    assert f"cat /tmp/prompt.txt /tmp/phases/{phase}.md |" in run
    assert f"> /tmp/{log}.log 2>&1" in run  # the output goes to a log that is not uploaded
    assert step["continue-on-error"] is True


def test_notebook_step_follows_the_passes_with_a_fresh_home_and_the_python3_kernel():
    steps = _build_steps()
    for name in CLAUDE_STEPS:
        assert _index(name) < _index("Run the notebook")
    run = steps[_index("Run the notebook")]["run"]
    assert run.index("rm -rf /tmp/build-home /tmp/claude*.log") < run.index("papermill")
    assert "-k python3" in run and "--execution-timeout 600" in run
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in json.dumps(steps[_index("Run the notebook")])
    assert "rm -f /tmp/out/work/PLAN.md" in steps[_index("Remove caches")]["run"]


def test_phase_files_are_pass_specific_and_the_task_prompt_is_pass_agnostic():
    from radar.config import REPO_ROOT

    phases = TEMPLATE / "tools" / "phases"
    plan = (phases / "plan.md").read_text(encoding="utf-8")
    impl = (phases / "implement.md").read_text(encoding="utf-8")
    review = (phases / "review.md").read_text(encoding="utf-8")
    for needle in ("PLAN.md", "notebook outline", "pinned", "generated in code", "Risks"):
        assert needle.lower() in plan.lower()
    assert "no other file" in plan
    assert "follow it exactly" in impl.lower()
    for needle in ("PLAN.md", "BUILD_LOG.md", "delete"):
        assert needle in review
    task = (REPO_ROOT / "prompts" / "build_task.md").read_text(encoding="utf-8")
    assert "PLAN.md" not in task and "three passes" not in task


def test_setup_python_is_the_pinned_v5():
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    uses = re.findall(r"actions/setup-python@([0-9a-f]{40}) # (v[0-9.]+)", text)
    assert len(uses) == 2 and {tag for _, tag in uses} == {"v5.6.0"}


# ---------- seam with radar.builds.actions ----------


def test_workflow_names_match_what_radar_reads():
    from radar.builds import actions

    wf = _wf()
    assert wf["run-name"] == "build ${{ inputs.folder }}"  # find_run matches "build <folder>"
    assert actions.WORKFLOW == "build.yml"
    assert set(wf["jobs"]) == {"build", "publish", "cleanup"}  # outcome reads these job names
    assert all("name" not in job for job in wf["jobs"].values())
    publish = [s.get("name") for s in wf["jobs"]["publish"]["steps"]]
    for name in (*actions.NO_OUTPUT_STEPS, "Scan and commit", actions.SCAN_BLOCK_STEP):
        assert name in publish
    assert actions.SECRET_NAME == "CLAUDE_CODE_OAUTH_TOKEN"
    assert actions.SECRET_NAME in (TEMPLATE / "build.yml").read_text(encoding="utf-8")


def test_publish_steps_separate_missing_output_publish_errors_and_scan_blocks():
    steps = {s["name"]: s for s in _wf()["jobs"]["publish"]["steps"] if "name" in s}
    assert "download-artifact" in steps["Download artifact"]["uses"]
    check = steps["Check artifact"]["run"]
    assert "ls -A" in check and '"blocked": true' in check
    scan_step = steps["Scan and commit"]
    assert scan_step["id"] == "scan" and "GITHUB_OUTPUT" in scan_step["run"]
    # exit 1 from publish_build fails this step; exit 2 is left to the next one
    assert '[ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]' in scan_step["run"]
    assert steps["Leak scan blocked the build"]["if"] == "steps.scan.outputs.rc == '2'"
    assert steps["Push"]["if"] == "steps.scan.outputs.rc == '0'"


# ---------- publish tool: blocked versus failed ----------


def test_unscannable_output_is_an_error_not_a_block(env, capsys):
    env.status(ran=False, blocked=True, why="unscannable")
    write_nb(env, [md("a")])
    assert env.run() == 1
    out = capsys.readouterr().out
    assert "build output could not be checked" in out and "token" not in out
    assert not (env.repo / FOLDER).exists()


def test_token_hit_is_a_block_with_its_own_message(env, capsys):
    env.status(ran=False, blocked=True, why="key")
    write_nb(env, [md("a")])
    assert env.run() == 2
    assert "build output contained the subscription token" in capsys.readouterr().out


def test_read_status_returns_why(env):
    env.status(ran=False, blocked=True, why="key")
    assert env.mod.read_status(env.art) == (False, True, "key")
    env.status(ran=False, blocked=True, why="surprise")
    assert env.mod.read_status(env.art) == (False, True, "")
    env.status()
    assert env.mod.read_status(env.art) == (True, False, "")


def test_lone_surrogate_notebook_is_invalid_not_a_crash(env, capsys):
    env.status()
    # json escapes the lone surrogate, so the file itself is valid UTF-8 JSON
    (env.work / "demo.ipynb").write_text(json.dumps(nb([md("bad " + chr(0xD800))])))
    assert env.run() == 2
    assert "invalid notebook" in capsys.readouterr().out
    assert not (env.repo / FOLDER).exists()


ENTITY_SVGS = [
    "<svg xmlns='http://www.w3.org/2000/svg'>&#60;script&#62;alert(1)&#60;/script&#62;</svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'>&lt;foreignObject/&gt;</svg>",
    "<svg xmlns='http://www.w3.org/2000/svg'><a href='h&#116;tp://evil.example'>a</a></svg>",
]


@pytest.mark.parametrize("svg", ENTITY_SVGS)
def test_entity_hidden_active_svg_in_a_notebook_is_dropped(env, svg):
    env.status()
    write_nb(env, [code([display({"image/svg+xml": [svg]})])])
    assert env.run() == 0
    assert "image/svg+xml" not in published_nb(env)["cells"][0]["outputs"][0]["data"]


@pytest.mark.parametrize("svg", ENTITY_SVGS)
def test_entity_hidden_active_svg_file_blocks(env, svg):
    env.status()
    write_nb(env, [md("a")])
    (env.work / "x.svg").write_text(svg, encoding="utf-8")
    assert env.run() == 2
    assert not (env.repo / FOLDER).exists()
