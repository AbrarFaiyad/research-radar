# Daily Builds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each day, build one small sandboxed learning demo of the digest's top open-source item with Claude Sonnet, publish it to a public `daily-builds` repo, and show it in the panel and the email.

**Architecture:** A new `radar.builds` package: select an item from the Codex result, run Claude Code and the build inside a Docker container on an internal network whose only exit is an allowlisting proxy, verify in a second container, leak-scan the exact bytes, then commit and push from an isolated clone. The daily run starts the build in a thread right after Codex returns, in parallel with the podcast, as an optional stage.

**Tech Stack:** Python 3.12, Docker Desktop on WSL2, tinyproxy (allowlist), Claude Code CLI (`claude -p --model sonnet`) inside the container, git with an isolated config, FastAPI panel, keyring.

**Spec:** `docs/superpowers/specs/2026-11-19-daily-builds-design.md`

## Global Constraints

- No personal information leaves the machine. Nothing from `data/` (database, queue, connections, onboarding, CV), `config/config.toml`, email settings, browser profiles, or the digest's `relevance`, `rejected`, `post_ids`, `podcast_focus` enters the container or the published repo.
- Container input is limited to: the selected item's public fields, the committed `config/field_profile.md`, the build prompt, and the Anthropic API key.
- Container network: internal Docker network, egress only through the proxy allowlist (default: `pypi.org`, `files.pythonhosted.org`, `github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com`, `huggingface.co`, `cdn-lfs.huggingface.co`, `readthedocs.io`, `readthedocs.org`, `github.io`, `api.anthropic.com`, `registry.npmjs.org`).
- Container limits: 4 CPUs, 8 GB memory, 45-minute wall clock, non-root user, removed after use, no host mounts except the empty per-build output folder.
- Size caps: 5 MB per file, 20 MB per build folder. Allowed binary outputs: `.png`, `.svg` only.
- Folder name: `YYYY-MM-DD_<package>_<task>`, lowercase, `[a-z0-9-]` segments joined by `_`.
- Feature ships off (`[builds] enabled = false`); needs `repo` and an API key.
- API key stored in Windows Credential Manager, keyring service `research-radar-anthropic`, never returned by the panel.
- Commits: author and committer = the owner's GitHub noreply identity from `[builds] git_name`/`git_email`; hooks and filters disabled.
- Windows 11 Home: Docker Desktop with the WSL2 engine; tests must not need Docker, network, or a GPU.
- Ruff 0.17.6 rules in `pyproject.toml`; all new code formatted; CI green on Ubuntu and Windows.

## Review Focus

1. Docker Desktop not running at 07:00 → build stage records "skipped: docker not running" and the daily run still succeeds. (Task 6, Task 9)
2. A digest with no eligible item, or an old digest without the new fields → "skipped" with the reason, nothing pushed. (Task 4)
3. Container output containing a symlink, a 30 MB file, a `.zip`, or a string like the owner's email → push blocked, build kept locally with the reason. (Task 5)
4. Clone missing, remote URL changed, or push auth failing → "blocked: publish", nothing pushed, the local build kept for Retry. (Task 8)
5. A second digest the same day (`2026-11-21-2`) → at most one published build per day. (Task 7)

---

### Task 1: Builds configuration and settings

**Files:**
- Modify: `src/radar/config.py`, `src/radar/settings.py`, `config/config.example.toml`
- Test: `tests/test_builds_config.py`

**Interfaces:**
- Produces: `BuildsCfg` dataclass on `Config.builds` with fields `enabled: bool = False`, `mode: str = "with_digest"` (`with_digest | separate`), `separate_time: str = "08:30"`, `repo: str = ""` (`owner/name`), `git_name: str = ""`, `git_email: str = ""`, `image: str = "research-radar-build:1"`, `cpus: int = 4`, `memory_gb: int = 8`, `timeout_min: int = 45`, `max_file_mb: float = 5`, `max_folder_mb: float = 20`, `allow_hosts: list[str]` (default list from Global Constraints), `claude_model: str = "sonnet"`, `max_turns: int = 60`. Property `Config.builds_dir -> data_dir / "builds"`.
- Settings fields in section `builds` for: enabled, mode (choice), separate_time, repo, git_name, git_email, cpus, memory_gb, timeout_min, max_file_mb, max_folder_mb.

- [ ] **Step 1: Write the failing tests**

```python
def test_builds_defaults_off(cfg):
    assert cfg.builds.enabled is False and cfg.builds.mode == "with_digest"
    assert "api.anthropic.com" in cfg.builds.allow_hosts and cfg.builds.timeout_min == 45

def test_builds_settings_validate(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.mode": "later", "builds.repo": "not a repo"})
    assert set(e.value.errors) == {"builds.mode", "builds.repo"}
    settings.save(tmp_root, {"builds.repo": "AbrarFaiyad/daily-builds", "builds.enabled": True})

def test_enabling_builds_requires_repo(tmp_root):
    with pytest.raises(settings.SettingsError) as e:
        settings.save(tmp_root, {"builds.enabled": True})
    assert "builds.repo" in e.value.errors
```

- [ ] **Step 2: Run** `.venv\Scripts\python -m pytest tests/test_builds_config.py -v` — expect FAIL (no `builds` attribute).
- [ ] **Step 3: Implement** `BuildsCfg`, `_section(raw, "builds", BuildsCfg)` in `load_config`, settings fields (repo validated by `^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`; `separate_time` by `^\d{2}:\d{2}$`; cross rule: enabled needs repo), and a commented `[builds]` section in the example config (enabled = false).
- [ ] **Step 4: Run** the tests — expect PASS; run the full suite.
- [ ] **Step 5: Commit** `git add src/radar/config.py src/radar/settings.py config/config.example.toml tests/test_builds_config.py` with message "Builds: configuration and validated settings".

### Task 2: Optional pipeline stages

**Files:**
- Modify: `src/radar/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `run_stages(stages, log=print, optional: set[str] = frozenset())`. `StageResult` gains `optional: bool = False` and `skipped: bool = False`. `RunRecord.ok` = all non-optional stages ok. A stage function may raise `StageSkipped(reason)`, recorded as `ok=True, skipped=True, error=reason`.

- [ ] **Step 1: Failing tests**

```python
def test_optional_stage_failure_keeps_run_ok():
    rec = run_stages({"a": lambda: {}, "b": boom}, log=lambda _: None, optional={"b"})
    assert rec.ok and not rec.stages[1].ok and rec.stages[1].optional

def test_stage_skipped_is_recorded():
    def skip(): raise StageSkipped("docker not running")
    rec = run_stages({"build": skip}, log=lambda _: None, optional={"build"})
    assert rec.ok and rec.stages[0].skipped and rec.stages[0].error == "docker not running"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Pipeline: optional and skipped stages".

### Task 3: Digest fields for builds and a post-Codex hook

**Files:**
- Modify: `src/radar/digest.py`, `prompts/codex_digest.md`
- Test: `tests/test_digest_podcast.py`

**Interfaces:**
- Produces: `CODEX_SCHEMA` item fields `code_url: str`, `license: str`, `cpu_demo: bool` (all required, strict schema). `run_digest(..., after_codex: Callable[[Path, dict], None] | None = None)` calls `after_codex(folder, result)` after `record_digest` and before audio; an exception in it is logged and does not stop the digest.

- [ ] **Step 1: Failing tests**

```python
def test_schema_has_build_fields():
    item = CODEX_SCHEMA["properties"]["items"]["items"]
    assert {"code_url", "license", "cpu_demo"} <= set(item["required"])
    assert item["properties"]["cpu_demo"]["type"] == "boolean"

def test_after_codex_runs_before_audio(cfg, store_with_saved_posts):
    order = []
    run_digest(cfg, store_with_saved_posts, runner=fake_codex_writing_result,
               make_audio=lambda f, r: order.append("audio"),
               after_codex=lambda f, r: order.append("build"), log=lambda _: None)
    assert order == ["build", "audio"]

def test_after_codex_error_does_not_stop_digest(...):  # raises in hook; DigestResult still returned
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; add three short instructions to the prompt: `code_url` = public code repository or package page, empty if none; `license` = SPDX id of that repository, empty if unknown; `cpu_demo` = true only if a tiny demo can run on a laptop CPU in under 10 minutes. Update `render_markdown` to show `Code:` and license when present.
- [ ] **Step 4: Run** — PASS (update existing fixtures that build Codex results to include the three fields).
- [ ] **Step 5: Commit** "Digest: code URL, license, and CPU-demo fields; post-Codex hook".

### Task 4: Pick the day's item

**Files:**
- Create: `src/radar/builds/__init__.py`, `src/radar/builds/select.py`
- Test: `tests/test_builds_select.py`

**Interfaces:**
- Produces:
  - `OSI_LICENSES: frozenset[str]` (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, MPL-2.0, LGPL-2.1, LGPL-3.0, GPL-2.0, GPL-3.0, AGPL-3.0, ISC, Unlicense, Zlib, EPL-2.0, BSL-1.0).
  - `@dataclass Candidate: rank: int; title: str; package: str; code_url: str; license: str; item: dict` where `item` holds only the public whitelist (`title, kind, authors, venue, primary_url, sources, what, how, why_it_matters, limitations, code_url, license`).
  - `pick(result: dict, built: set[str]) -> tuple[Candidate | None, str]` returns the candidate or the skip reason.
  - `package_slug(code_url: str, title: str) -> str` (repo name from a GitHub URL, else PyPI project name, else slugified title; `[a-z0-9-]`, max 40 chars).
  - `folder_name(day: str, package: str, task: str) -> str`.
  - `History(path)`: `.built() -> set[str]`, `.add_published(package, folder, url)`, `.add_attempt(package, status, reason)`, `.published_on(day) -> bool`.

- [ ] **Step 1: Failing tests**

```python
def test_pick_first_eligible_in_rank_order():
    c, _ = pick(result_with([paper_no_code, unlicensed_repo, mit_repo_cpu, apache_repo_cpu]), set())
    assert c.rank == 3 and c.license == "MIT"

def test_skip_reasons():
    assert pick(result_with([paper_no_code]), set()) == (None, "no item with public code, an open license, and a CPU demo")
    assert pick({"items": [old_item_without_fields]}, set())[0] is None
    assert pick(result_with([mit_repo_cpu]), {"mace-torch"})[0] is None  # already built

def test_candidate_item_is_whitelisted():
    c, _ = pick(result_with([mit_repo_cpu | {"relevance": "my private work", "post_ids": [3]}]), set())
    assert "relevance" not in c.item and "post_ids" not in c.item

def test_compound_or_unknown_license_skipped():  # "MIT OR GPL-3.0", "", "Custom" → ineligible
def test_folder_name_format():
    assert folder_name("2026-11-21", "mace-torch", "LoRA fine-tune: molten salt!") == "2026-11-21_mace-torch_lora-fine-tune-molten-salt"
def test_history_counts_only_published(tmp_path):  # add_attempt(failed) does not enter built(); published_on works
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: item selection, naming, and history".

### Task 5: Leak scan

**Files:**
- Create: `src/radar/builds/scan.py`
- Test: `tests/test_builds_scan.py`

**Interfaces:**
- Produces:
  - `@dataclass Finding: path: str; reason: str` (reason never contains the matched secret itself).
  - `personal_markers(cfg: Config) -> list[str]`: email `to`/`sender`, home folder path and its forward-slash form, Windows user name, machine name (`platform.node()`), plus any strings in env `RADAR_PRIVATE_MARKERS` (comma-separated; the owner can add a phone number or name variants there, and tests use it). The CV text is not read: it is already redacted and never enters the sandbox.
  - `scan_tree(root: Path, markers: list[str], max_file_mb: float, max_folder_mb: float) -> list[Finding]`: walk with `os.lstat`/`os.scandir(follow_symlinks=False)`; reject symlinks, junctions (`is_junction`), non-regular files, hard links (`st_nlink > 1`), archives (`.zip .tar .gz .7z .whl .egg .pkl .pt .npz .h5`), binaries other than `.png`/`.svg` (null byte in first 8 KB), size caps; text checks: secret patterns (`sk-ant-`, `sk-`, `ghp_`, `github_pat_`, `gho_`, `AKIA[0-9A-Z]{16}`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`, `xox[baprs]-`, `AIza[0-9A-Za-z_-]{35}`), high-entropy tokens (≥ 32 chars of `[A-Za-z0-9+/_=-]` with Shannon entropy ≥ 4.0 bits/char, excluding hex SHA-like strings inside `requirements.txt` hashes), case-insensitive personal markers, `C:\Users\` / `/Users/` / `/home/` paths.
  - `scan_bytes(name: str, data: bytes, markers) -> list[Finding]` used for the host-written index too.

- [ ] **Step 1: Failing tests** — one per rule: clean folder → `[]`; planted `sk-ant-api03-...` → finding "secret pattern"; owner email in `BUILD_LOG.md` → "personal information"; `C:\Users\someone\x` → "local path"; symlink (skip on Windows if no privilege via `pytest.skip`) → "link"; 6 MB file → "file too large"; `.zip` → "archive"; binary `.bin` → "binary"; PNG allowed; findings never include the secret text (`assert "api03" not in str(findings)`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: leak scan".

### Task 6: Sandbox

**Files:**
- Create: `src/radar/builds/sandbox.py`, `docker/build/Dockerfile`, `docker/proxy/Dockerfile`, `docker/proxy/tinyproxy.conf.tmpl`
- Test: `tests/test_builds_sandbox.py`

**Interfaces:**
- Produces:
  - `NETWORK = "radar-builds-internal"`, `PROXY = "radar-builds-proxy"`.
  - `docker_ready(run=subprocess.run) -> tuple[bool, str]` (runs `docker info`; returns reason "docker not running" or "docker not installed").
  - `ensure_network_and_proxy(cfg, run) -> None`: `docker network create --internal radar-builds-internal` if missing; build/start the proxy image with the allowlist rendered from `cfg.builds.allow_hosts` (tinyproxy `FilterDefaultDeny Yes`, one `Filter` regex per host incl. subdomains); proxy joins both the internal network and the default bridge.
  - `ensure_image(cfg, run) -> None`: `docker build -t <image> docker/build` if the image is missing.
  - `build_argv(cfg, workdir: Path, name: str) -> list[str]` and `verify_argv(cfg, workdir: Path, name: str) -> list[str]`: `docker run --rm --name <name> --network radar-builds-internal --cpus N --memory Ng --pids-limit 512 --security-opt no-new-privileges --cap-drop ALL --user 1000:1000 --env HTTPS_PROXY=http://radar-builds-proxy:8888 --env HTTP_PROXY=... --env HOME=/home/builder --env-file <tmp env file with ANTHROPIC_API_KEY only> -v <workdir>:/work <image> <cmd>`. No other `-v`, no `--env` from the host environment.
  - `run_container(argv, timeout_s, run=subprocess.run) -> tuple[int, str]`; on timeout runs `docker kill <name>`.
  - Build image: `python:3.12-slim` + git + Node 22 + `@anthropic-ai/claude-code` pinned; user `builder` (uid 1000); `WORKDIR /work`; entry script runs `claude -p "$(cat /work/.task/prompt.md)" --model $MODEL --max-turns $TURNS --permission-mode bypassPermissions --output-format text > /work/.task/claude.log` then removes `/work/.task`.
  - Verify command: `python -m venv /tmp/v && /tmp/v/bin/pip install -r requirements.txt && /tmp/v/bin/python -m pytest -q test_demo.py` writing `/work/.verify.json` `{ "installed": bool, "tests_passed": bool }` (the host deletes `.verify.json` after reading it).

- [ ] **Step 1: Failing tests** (fake `run` recording argv; no Docker needed)

```python
def test_build_argv_isolation(cfg, tmp_path):
    argv = build_argv(cfg, tmp_path, "rb-1")
    assert argv.count("-v") == 1 and f"{tmp_path}:/work" in " ".join(argv)
    assert "--network" in argv and argv[argv.index("--network") + 1] == NETWORK
    assert "--cap-drop" in argv and "--user" in argv and "1000:1000" in argv
    assert not any(a.startswith("PATH=") or getpass.getuser().lower() in a.lower() for a in argv)

def test_env_file_holds_only_api_key(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_HOST_VAR", "nope")  # host env must not pass through
def test_docker_not_running_reason(): ...
def test_timeout_kills_container(): ...
def test_proxy_allowlist_rendering(cfg):  # each host becomes an anchored Filter line; FilterDefaultDeny Yes
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: Docker sandbox with an allowlisting proxy".

### Task 7: Build prompt and orchestrator

**Files:**
- Create: `prompts/build_task.md`, `src/radar/builds/run.py`
- Test: `tests/test_builds_run.py`

**Interfaces:**
- Consumes: `pick`, `History`, `folder_name` (Task 4); `scan_tree`, `personal_markers` (Task 5); sandbox functions (Task 6); `publish` (Task 8, injected).
- Produces:
  - `@dataclass BuildRecord: day: str; status: str  # published | did_not_run | blocked | failed | skipped; package: str = ""; folder: str = ""; url: str = ""; reason: str = ""; seconds: float = 0`.
  - `run_build(cfg, digest_folder: Path, result: dict, *, api_key: str | None, docker=sandbox, publisher=publish.publish, log=print) -> BuildRecord`. Order: skip if disabled, no key, no repo, Docker not ready, already published today, or no candidate; write `data/builds/work/<folder>/` with `.task/prompt.md` (rendered from the template with the candidate's whitelisted item and the committed field profile only) and `.task/item.json`; run build container; reject if README.md or no `*.py` (status failed); run verify container; if verify fails, prepend the "did not run" banner to README; scan; on findings status blocked (reasons listed); else publisher; write `<digest_folder>/build.json` (`asdict(record)` minus `reason` when published) and `data/builds/records/<day>.json`; update History.
  - Prompt template sections: the build goal (learning kit), fixed README section list from the spec, output file layout, size limits, "never invent results; show real output", "use only CPU", "pin versions", "no network access except package installs and docs", "write BUILD_LOG.md".

- [ ] **Step 1: Failing tests** (fake docker module and fake publisher)

```python
def test_container_input_has_no_private_data(cfg, tmp_path, private_world):
    # private_world plants email, CV text, queue names, RADAR_PRIVATE_MARKERS in data/ and env
    run_build(cfg, digest_folder, result_with_relevance, api_key="k", docker=fake_ok, publisher=fake_pub)
    blob = read_all_files(cfg.builds_dir / "work")  # what the container saw
    assert not any(m in blob for m in private_world.markers)
    assert "relevance" not in blob and "post_ids" not in blob

def test_status_paths(cfg):
    assert run_build(..., api_key=None).status == "skipped"
    assert run_build(..., docker=fake_not_running).reason == "docker not running"
    assert run_build(..., docker=fake_verify_fails).status == "did_not_run"  # and README starts with banner
    assert run_build(..., docker=fake_leaks_email).status == "blocked"  # publisher not called
    assert run_build(..., docker=fake_no_readme).status == "failed"

def test_one_published_build_per_day(cfg):  # second digest folder 2026-11-21-2 → skipped "already published today"
def test_build_json_written(cfg):  # fields day,status,package,folder,url
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: prompt and orchestrator".

### Task 8: Isolated publishing

**Files:**
- Create: `src/radar/builds/publish.py`
- Test: `tests/test_builds_publish.py`

**Interfaces:**
- Produces: `publish(cfg, work_folder: Path, record: BuildRecord, markers: list[str], git=run_git) -> str` returns the folder's GitHub URL `https://github.com/<repo>/tree/main/<folder>`; raises `PublishBlocked(reason)`.
  - Clone at `data/builds/repo` (`gh repo clone` once, or `git clone https://github.com/<repo>.git`). Every git call uses `git -c core.hooksPath=NUL -c filter.lfs.process= -c core.fsmonitor=false -c commit.gpgsign=false -c user.name=<git_name> -c user.email=<git_email> -c credential.helper= -c "credential.helper=!gh auth git-credential"` and env `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<empty temp file>`.
  - Before writing: `git status --porcelain` must be empty and `git rev-list @{u}..HEAD` empty; remote URL must equal `https://github.com/<repo>.git`; else `PublishBlocked`.
  - Copy files byte-for-byte (regular files only) into `<clone>/<folder>/`; refuse if the folder exists.
  - Update `README.md` index table (create on first run: title "Daily builds", one-paragraph AI-generated disclaimer, table `Date | Package | What it shows | License | Ran`), newest first.
  - `git add -- <folder> README.md`; commit "Build <folder>"; then verify: `git log -1 --format=%an%n%ae%n%cn%n%ce` equals the configured identity; `git diff-tree --no-commit-id -r --name-only HEAD` lists only that folder and README.md; `scan_bytes` over every blob in `git ls-tree -r HEAD -- <folder> README.md` content; any failure → `git reset --hard @{u}` and `PublishBlocked`.
  - `git push origin HEAD:main`; failure → reset and `PublishBlocked("push failed")`.

- [ ] **Step 1: Failing tests** using a real local bare repo as `origin` (no network): first publish creates README index and folder; second publish adds a row newest-first; existing folder → blocked; dirty clone → blocked; wrong remote → blocked; hook file planted in `.git/hooks/pre-commit` that writes a marker file → marker never appears; author/committer equal configured identity; index row escapes `|`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: isolated publishing with commit verification".

### Task 9: Command line, daily run, and separate schedule

**Files:**
- Modify: `src/radar/cli.py`, `src/radar/ui/jobs.py`, `src/radar/ui/schedule.py`, `scripts/install_schedule.ps1`, `scripts/run_daily.ps1`
- Test: `tests/test_builds_cli.py`

**Interfaces:**
- Produces:
  - `radar build [folder] [--retry]`: builds from the given or latest digest folder (`codex_result.json`); `--retry` re-runs a non-published day. Takes the run lock (owner "build").
  - `radar builds-key`: store the API key from a hidden prompt (keyring `research-radar-anthropic`, user `default`); `radar.builds.keys.get_key()/set_key()`.
  - Daily, `mode = with_digest`: `stage_digest` passes `after_codex` that starts `threading.Thread(target=run_build, ...)`; a new optional stage `build` joins it (timeout = `timeout_min` + 10) and returns `asdict(record)`; `StageSkipped` when the record status is `skipped`. Email stage runs after `build`.
  - Daily, `mode = separate`: no build in the daily run; `ResearchRadarBuild` scheduled task at `separate_time` runs `radar build`. `schedule.set_schedule(root, time, enabled, task="ResearchRadarDaily")` gains the task parameter; `install_schedule.ps1 -TaskName -Command`.
  - Job actions: `"build": ["build"]`, `"build-retry": ["build", "--retry"]` (folder passed as extra, validated like digest names).

- [ ] **Step 1: Failing tests**

```python
def test_daily_runs_build_in_parallel_with_audio(cfg, monkeypatch):
    # fake audio sleeps 0.3 s and fake build sleeps 0.3 s; total daily < 0.5 s; stages: scan, digest, build, email
def test_build_failure_keeps_daily_ok(cfg, monkeypatch): ...
def test_separate_mode_skips_build_in_daily(cfg): ...
def test_build_command_uses_latest_digest(cfg, tmp_path): ...
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: command, daily integration, separate schedule".

### Task 10: Health checks, key storage, and email

**Files:**
- Create: `src/radar/builds/keys.py`
- Modify: `src/radar/doctor.py`, `src/radar/mailer.py`
- Test: `tests/test_doctor.py`, `tests/test_mailer.py`

**Interfaces:**
- Produces: doctor checks (only when `builds.enabled`): `WSL2` (`wsl --status` exit 0; action URL `https://learn.microsoft.com/windows/wsl/install`), `Docker` (`docker_ready`; action URL `https://www.docker.com/products/docker-desktop/`), `build image` (action job `build-image` → `radar build --prepare` which runs `ensure_network_and_proxy` and `ensure_image`), `Anthropic API key` (action `builds-key`), `daily-builds repo` (`gh repo view <repo>`; action URL to create it). `compose_daily` adds a line from `build.json`: "**Daily build:** <package> → <url>" or "Daily build skipped: <reason>".

- [ ] **Step 1: Failing tests** — checks absent when disabled; present with correct actions when enabled; email line for published, did_not_run (with "did not run" note), skipped (reason), and absent `build.json`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: health checks, API key storage, email line".

### Task 11: Panel tab and settings

**Files:**
- Modify: `src/radar/ui/app.py`, `src/radar/ui/static/index.html`
- Test: `tests/test_ui.py`

**Interfaces:**
- Produces endpoints: `GET /api/builds` (records newest first from `data/builds/records/*.json`, plus the running job if `build`), `GET /api/builds/{day}` (record, README rendered with `safe_markdown`, build log text, list of output images), `GET /api/builds/{day}/file/{name}` (only `.png`/`.svg` under the work folder's `output/`, name `^[A-Za-z0-9._-]+$`, no traversal), `POST /api/builds/key` (stores the key; never echoes; 400 if shorter than 20 chars). Day names validated with the digest-name regex.
- Tab "Daily builds": table (date, package, task, status badge, link), detail pane (README, output images, log in a collapsible block, Retry button for non-published), "Build now" button. Settings section label "Daily builds". Setup tab: an API key field like the Gmail App Password.

- [ ] **Step 1: Failing tests** — list and detail shapes; traversal attempt `..%2F..%2Fconfig.toml` → 400; non-image file → 404; key endpoint stores via monkeypatched keyring and response contains no key; POSTs without token → 403.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; check the page's JavaScript with `node --check` on the extracted script.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Panel: Daily builds tab, settings, and key entry".

### Task 12: Docs, website brief, and first live build

**Files:**
- Modify: `README.md`
- No code in the website repo (brief only).

- [ ] **Step 1:** README: feature summary, a "Daily builds" section (what a build contains, safety model, setup: WSL2, Docker Desktop, API key with a spend limit, create `daily-builds` repo, enable in Settings), component table row, diagram node, command table rows.
- [ ] **Step 2:** Send the website session the `build.json` contract (`day, status, package, folder, url`) and ask for a highlighted "Daily build" card at the top of the day's post when `status` is `published` or `did_not_run`.
- [ ] **Step 3 (after the owner installs Docker and stores the key):** run `radar build --prepare`, then `radar build` on the latest digest. Expected: a folder pushed to `AbrarFaiyad/daily-builds`, `build.json` written, the panel tab shows it. Run the leak scan manually on the pushed clone and search the repo for the owner's email, user name, and home path: no hits.
- [ ] **Step 4:** Full test suite, ruff, push, CI green.
- [ ] **Step 5: Commit** "Docs: daily builds".
