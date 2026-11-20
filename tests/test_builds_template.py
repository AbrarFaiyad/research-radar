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
FAKE_KEY = "sk-ant-api03-" + "x" * 40
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
    assert set(inputs) == {"folder", "prompt"}
    assert all(i["required"] and i["type"] == "string" for i in inputs.values())
    assert wf["permissions"] == {}
    assert wf["concurrency"] == {"group": "daily-builds", "cancel-in-progress": False}
    assert wf["jobs"]["build"]["timeout-minutes"] == 60


def test_workflow_permissions_and_secret_scope():
    wf = _wf()
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert wf["jobs"]["publish"]["needs"] == "build"
    assert "secrets." not in json.dumps(wf["jobs"]["publish"])
    assert "secrets." not in json.dumps({k: v for k, v in wf.items() if k != "jobs"})
    holders = [s for s in wf["jobs"]["build"]["steps"] if "secrets." in json.dumps(s)]
    assert len(holders) == 1
    assert holders[0]["env"] == {"ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}"}
    assert "--permission-mode bypassPermissions" in holders[0]["run"]


def test_workflow_inputs_only_through_env():
    for step in _steps(_wf()):
        assert "${{" not in step.get("run", ""), step.get("run")
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    assert "${{ inputs.folder }}" in text and "${{ inputs.prompt }}" in text
    assert "github.event" not in text


def test_workflow_actions_pinned_to_sha():
    uses = [s["uses"] for s in _steps(_wf()) if "uses" in s]
    assert uses
    text = (TEMPLATE / "build.yml").read_text(encoding="utf-8")
    for u in uses:
        assert SHA_RE.match(u), u
        assert re.search(re.escape(u) + r"\s+#\s*v\d", text), u


def test_build_job_does_not_check_out_the_repo():
    for step in _wf()["jobs"]["build"]["steps"]:
        assert "checkout" not in step.get("uses", "")


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

    def run(self, folder=FOLDER):
        return self.mod.main(["--folder", folder, "--src", str(self.art), "--repo", str(self.repo)])

    def commits(self):
        return int(_git(self.repo, "rev-list", "--count", "HEAD"))

    def status(self, ran=True):
        (self.art / ".status.json").write_text(json.dumps({"ran": ran}))


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
    (e.art / name).write_text(json.dumps(nb(cells)), encoding="utf-8")


def published_nb(e):
    return json.loads((e.repo / FOLDER / "demo.ipynb").read_text(encoding="utf-8"))


def test_publish_commits_as_bot_in_utc(env):
    env.status()
    write_nb(env, [md("# Hello"), code()])
    (env.art / "requirements.txt").write_text("numpy\n")
    (env.art / ".hidden").write_text("x")
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
    assert not (env.repo / FOLDER / ".status.json").exists()
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


def test_png_file_sanitized(env):
    env.status()
    write_nb(env, [md("a")])
    dirty = make_png(_chunk(b"tEXt", b"Comment\x00hello"))
    (env.art / "plot.png").write_bytes(dirty)
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
        os.symlink(target, env.art / "link.txt")
    except OSError:  # Windows without symlink rights: a directory junction is also a link
        if sys.platform != "win32":
            pytest.skip("symlinks not permitted")
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(env.art / "link"), str(env.art.parent)],
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
    (env.art / "big.txt").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
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
    (env.art / "demo.ipynb").write_text("{not json")
    assert env.run() == 2


def test_hostile_png_in_notebook_blocks(env):
    env.status()
    bad = base64.b64encode(b"not a png at all").decode()
    write_nb(env, [code([display({"image/png": bad})])])
    assert env.run() == 2
