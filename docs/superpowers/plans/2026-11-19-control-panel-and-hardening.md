# Control Panel and Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local control panel for Research Radar, plus the reliability, portability, and CI work that makes the tool usable by anyone.

**Architecture:** A `radar.pipeline` module runs the daily stages with isolation and writes a JSON run record. A `radar.ui` package serves a FastAPI app on 127.0.0.1 with a token-guarded JSON API and one static HTML page; jobs run as `radar` subprocesses whose output streams to the page. Prompts take the reader description from config instead of hardcoding the owner.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, tomlkit, vanilla HTML/JS, pytest, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-19-control-panel-design.md` (panel) and the owner's instruction of 2026-11-19: "all the pipelines to industry standard ... a literature digest tool which anyone can use".

## Global Constraints

- Panel binds to `127.0.0.1` only, default port `8765`.
- Every state-changing request carries the per-launch token in header `X-Radar-Token` and a same-origin `Origin`; otherwise 403.
- The panel never handles passwords; logins and the App Password stay in the existing CLI commands.
- One job at a time; a second run request while one is active returns 409.
- No personal data in the repository; owner-specific text lives only in gitignored `config/config.toml` and the editable field profile.
- Windows 11 is the supported runtime; tests must pass on Ubuntu and Windows in CI.

## Review Focus

- Panel opened while a scheduled run is already active: the panel must show it and refuse a concurrent run (lock file `data/run.lock`).
- Settings saved with a bad value (negative max_posts, min_items > max_items): reject with a message, file untouched.
- Hostile web page posting to `http://127.0.0.1:8765` (CSRF): refused without the token.
- Scan stage fails (LinkedIn logged out): digest of backlog and email still run; run record marks scan failed; exit code 1.
- Config file missing comments or sections: settings save adds the section, keeps existing comments.

---

### Task 1: Resilient daily pipeline and run records

**Files:** Create `src/radar/pipeline.py`; Modify `src/radar/cli.py` (`cmd_daily` delegates); Test `tests/test_pipeline.py`.

**Interfaces:** Produces `run_daily(cfg, stages: dict[str, Callable[[], dict]]) -> RunRecord`; `RunRecord(started, finished, ok, stages: list[StageResult])`; `StageResult(name, ok, seconds, detail: dict, error: str | None)`; `save_run(cfg, record) -> Path` writing `data/runs/<timestamp>.json`; `latest_run(cfg) -> dict | None`; `RunLock(cfg)` context manager raising `LockHeld`.

- [ ] Test: a failing scan stage still runs digest and email; record `ok` is False; the scan stage error is captured.
- [ ] Test: `RunLock` refuses a second holder; stale lock older than 3 hours is taken over.
- [ ] Implement; `radar daily` exits 1 when any stage fails.

### Task 2: Reader description from config

**Files:** Modify `src/radar/config.py` (`[profile] reader`), `prompts/codex_digest.md`, `src/radar/notebooklm.py`, `config/config.example.toml`; Test in `tests/test_digest_podcast.py`, `tests/test_notebooklm.py`.

- [ ] Test: task and audio instructions contain the configured reader text and not the owner's hardcoded field list.
- [ ] Implement `{reader}` placeholder; default reader text is generic.

### Task 3: `radar doctor`

**Files:** Create `src/radar/doctor.py`; Modify `cli.py`; Test `tests/test_doctor.py`.

**Interfaces:** `run_checks(cfg) -> list[Check]`, `Check(name, ok, detail, fix)`.

- [ ] Checks: config file, field profile, Ollama reachable, model pulled, Codex on PATH, npx on PATH, Playwright Chromium present, LinkedIn profile dir, Google profile dir, email password stored (when email enabled), scheduled task present.
- [ ] Test each check with injected probes; CLI exits 1 when a required check fails.

### Task 4: Panel backend

**Files:** Create `src/radar/ui/__init__.py`, `src/radar/ui/app.py`, `src/radar/ui/jobs.py`, `src/radar/ui/schedule.py`, `src/radar/settings.py`; Test `tests/test_ui.py` (FastAPI TestClient).

**Endpoints:** `GET /api/status`, `GET /api/doctor`, `POST /api/jobs {action}`, `GET /api/jobs/current`, `GET /api/jobs/log?since=N`, `POST /api/jobs/cancel`, `GET|POST /api/schedule`, `GET|POST /api/settings`, `GET|POST /api/profile`, `POST /api/reclassify`, `GET /api/queue`, `POST /api/queue/mark`, `GET /api/digests`, `GET /api/digests/{name}`, `GET /api/digests/{name}/audio`.

- [ ] Tests: token and Origin required on POST; 409 on concurrent job; invalid settings rejected and file unchanged; settings save keeps comments; queue mark updates status; digest audio path traversal rejected.

### Task 5: Panel frontend

**Files:** Create `src/radar/ui/static/index.html`; Modify `pyproject.toml` (package data).

- [ ] Six tabs per spec; dark/light via `prefers-color-scheme`; works at 1024 px and up.
- [ ] Manual check in a browser; screenshot to `docs/images/control-panel.png`.

### Task 6: CI and docs

**Files:** Create `.github/workflows/ci.yml`, `.pre-commit-config.yaml`; Modify `README.md`, `pyproject.toml`.

- [ ] CI: ruff + pytest on ubuntu-latest and windows-latest, Python 3.12.
- [ ] README: control panel section with screenshot, `radar doctor`, CI badge.

### Task 7: Review

- [ ] Codex read-only review of the branch; fix real defects; push.
