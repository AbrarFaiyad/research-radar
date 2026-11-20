# Daily Builds on GitHub Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the daily build from the local Docker sandbox to a GitHub Actions workflow in the public `daily-builds` repo that produces an executed notebook, while the PC only dispatches the public prompt and records the result.

**Architecture:** Research Radar keeps item selection, prompt rendering, records, panel, and email. A new `radar.builds.actions` module starts the workflow with `gh workflow run`, finds the run, polls it, and turns its outcome into a `BuildRecord`. The workflow (shipped as a template inside the package and installed into the repo by `radar build --init-repo`) has an untrusted build job (read-only token, API key, Claude Code, papermill) and a trusted publish job (no key, no build code: sanitize, leak-scan, index, commit as the Actions bot).

**Tech Stack:** Python 3.12, `gh` CLI, GitHub Actions (ubuntu-latest), Claude Code CLI (pinned npm version), papermill + nbformat + ipykernel, git.

**Spec:** `docs/superpowers/specs/2026-11-20-daily-builds-actions-design.md` (updates `docs/superpowers/specs/2026-11-19-daily-builds-design.md`)

## Global Constraints

- No personal information leaves the PC: the only data sent is the rendered prompt (the selected item's public fields + the committed `config/field_profile.md`) and the folder name.
- The untrusted build job has `permissions: contents: read` and receives `ANTHROPIC_API_KEY`; the publish job has `permissions: contents: write`, never receives the key, and never executes build output.
- Commits in `daily-builds` are made by `github-actions[bot]` (`41898282+github-actions[bot]@users.noreply.github.com`) with UTC dates.
- The leak scan (`radar.builds.scan`) and `sanitize_png` run in the publish job on every file before commit; the vendored copy is byte-identical to `src/radar/builds/scan.py` (a test enforces it).
- Folder name: `YYYY-MM-DD_<package>_<task>`, lowercase, `[a-z0-9-]` segments joined by `_` (unchanged `select.folder_name`).
- Size caps 5 MB per file, 20 MB per folder; allowed binary outputs `.png`, `.svg`; `.ipynb` is text (JSON) and is scanned as text after its embedded images are extracted and checked.
- Build job timeout 60 minutes; papermill per-cell timeout 600 s; polling limit 90 minutes (config `builds.poll_min`).
- Feature ships off; the API key is stored only as the repo secret `ANTHROPIC_API_KEY` (set with `gh secret set -R <repo>` via stdin), never on the PC.
- All `gh` and `git` calls use argv lists and an allowlisted environment (existing pattern in `publish._env`), never a shell string.
- Tests need no network, GitHub, Docker, or GPU; `gh` is faked.
- Ruff 0.17.6 rules; CI green on Ubuntu and Windows.

## Review Focus

1. The workflow run cannot be found after dispatch (GitHub delay, two runs the same day) → the record says "could not find the workflow run" instead of hanging or attaching to the wrong run. (Task 3)
2. A run still going at the polling limit → status `building`, and a later `radar build --sync` (and the daily run) completes the record from the finished run. (Task 3, Task 4)
3. A notebook with an embedded base64 image carrying a key or an owner marker in its outputs → publish job blocks it. (Task 2)
4. A notebook that fails mid-execution → published as `did_not_run` with the partial outputs and a banner, not dropped. (Task 2)
5. A user without the `workflow` gh scope or without the secret → health checks say exactly which one, and `radar build` skips with that reason. (Task 5)

---

### Task 1: Notebook build prompt

**Files:**
- Modify: `prompts/build_task.md`, `src/radar/builds/run.py` (only `render_prompt` callers if the placeholder set changes)
- Test: `tests/test_builds_run.py`

**Interfaces:**
- Produces: the prompt instructs Claude to create `demo.ipynb` (first cell: markdown "Executive summary" — what the package is, the problem it solves, what this demo shows, in ≤ 120 words; then sections: core ideas, install, step-by-step walkthrough with runnable cells, results, gotchas, limitations, how to adapt it; last cell: source paper, package link, license), `README.md` (title, the same executive summary, "Run it: `pip install -r requirements.txt` then open `demo.ipynb`", AI-generated educational notice), `requirements.txt` (pinned, including `papermill`, `ipykernel`), `output/` (small PNG/SVG/CSV), `BUILD_LOG.md`. CPU only; whole notebook must run in under 10 minutes; no absolute paths, user names, machine info, credentials, or environment variables in any file; virtual environments and clones under `/tmp`.

- [ ] **Step 1: Failing test** — `test_prompt_asks_for_notebook`: rendered prompt contains `demo.ipynb`, `Executive summary`, `papermill`, `under 10 minutes`, `/tmp`; still contains no `relevance`/`post_ids` (existing whitelist test keeps passing).
- [ ] **Step 2: Run** `.venv\Scripts\python -m pytest tests/test_builds_run.py -k prompt -v` — FAIL.
- [ ] **Step 3: Rewrite** `prompts/build_task.md` with the sections above (keep `{{DAY}}`, `{{FIELD_PROFILE}}`, `{{ITEM_JSON}}` placeholders and the existing safety rules).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: notebook build prompt".

### Task 2: Workflow template and publish tool

**Files:**
- Create: `src/radar/builds/template/build.yml`, `src/radar/builds/template/publish_build.py`, `src/radar/builds/template/README.md` (index skeleton), `tests/test_builds_template.py`
- Modify: `pyproject.toml` (package data for `radar/builds/template/*`)

**Interfaces:**
- Consumes: `radar.builds.scan` (`scan_tree`, `scan_bytes`, `sanitize_png`), copied into the repo as `tools/scan.py` by Task 4.
- Produces:
  - `build.yml`: `on: workflow_dispatch` with inputs `folder` (string, required) and `prompt` (string, required); `run-name: build ${{ inputs.folder }}`; `concurrency: daily-builds` (no cancel); job `build`: `runs-on: ubuntu-latest`, `timeout-minutes: 60`, `permissions: contents: read`, env `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}` on the Claude step only; steps: setup-python 3.12, setup-node 22, `npm install -g @anthropic-ai/claude-code@<pin>`, validate `inputs.folder` against `^\d{4}-\d{2}-\d{2}_[a-z0-9-]+_[a-z0-9-]+$` (fail otherwise), write the prompt input to a file via an env var (never interpolate inputs into `run:` scripts), `mkdir work && cd work && claude -p --model sonnet --max-turns 60 --permission-mode bypassPermissions < ../prompt.txt`, then `pip install -r requirements.txt && papermill demo.ipynb demo.ipynb --cwd . --execution-timeout 600` (record `ran=true|false` in `work/.status.json`; a failure keeps the partially executed notebook), remove `__pycache__`, `.ipynb_checkpoints`, `.venv`, `.git`; `actions/upload-artifact` the `work` folder (retention 3 days). Job `publish`: `needs: build`, `if: always() && needs.build.result != 'cancelled'`, `permissions: contents: write`, no secrets; checkout the repo; `actions/download-artifact`; `python tools/publish_build.py --folder "$FOLDER" --src artifact` (folder via env var); the tool commits; `git push`.
  - `publish_build.py` (stdlib + vendored `tools/scan.py` only): `main(argv) -> int`. Steps: reject links/non-regular files (lstat); drop hidden paths; for `.ipynb` files, parse JSON, extract every `image/png` output (base64) → `sanitize_png` → write back the sanitized base64, reject `image/svg+xml` outputs containing `<script` or `href=` to remote hosts; if `.status.json` says `ran=false`, insert a markdown cell at the top: "This notebook did not finish running on the build machine; outputs may be partial."; `sanitize_png` every `.png`; `scan_tree` with markers from env `RADAR_PRIVATE_MARKERS` (unset in the workflow; markers are not available on the runner) and no secrets — findings → print fixed reasons, exit 2 (nothing committed); copy into `<folder>/` (refuse if it exists); update `README.md` index (newest first: Date | Package | What it shows | Ran); configure `user.name github-actions[bot]`, `user.email 41898282+github-actions[bot]@users.noreply.github.com`, `TZ=UTC`, commit "Build <folder>"; write `<folder>/build-result.json` `{status: published|did_not_run, folder}` before committing. Exit 0 on publish.

- [ ] **Step 1: Failing tests**

```python
def test_workflow_permissions_and_secret_scope():
    wf = yaml.safe_load(template("build.yml"))  # pyyaml added to dev extras
    assert wf["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write"}
    assert "ANTHROPIC_API_KEY" not in json.dumps(wf["jobs"]["publish"])
    assert "${{ inputs." not in "".join(s.get("run", "") for s in all_steps(wf))  # inputs only via env

def test_publish_tool_blocks_key_in_notebook_image(tmp_path): ...   # PNG with tEXt containing a fake key inside a notebook output → sanitized; key in a markdown cell → exit 2
def test_publish_tool_marks_did_not_run(tmp_path): ...             # .status.json ran=false → banner cell first, build-result status did_not_run
def test_publish_tool_commits_as_bot_in_utc(tmp_path): ...          # local bare repo; author/committer bot identity; date +0000
```

  The publish tool tests import the tool with `tools/scan.py` provided by copying `src/radar/builds/scan.py` into the temp folder (the real copy is written by Task 4's init).

  Add `pyyaml` to the dev extras for the workflow test.
- [ ] **Step 2: Run** `.venv\Scripts\python -m pytest tests/test_builds_template.py -v` — FAIL.
- [ ] **Step 3: Implement** the three template files and package data.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: Actions workflow template and publish tool".

### Task 3: Dispatch and poll

**Files:**
- Create: `src/radar/builds/actions.py`, `tests/test_builds_actions.py`

**Interfaces:**
- Produces:
  - `gh(args: list[str], *, input: str | None = None, timeout: float = 60) -> CompletedProcess` (minimal env like `publish._env`: PATH, SYSTEMROOT, WINDIR, COMSPEC, PATHEXT, TEMP, TMP, GH_CONFIG_DIR when present; `encoding="utf-8", errors="replace"`).
  - `dispatch(cfg, folder: str, prompt: str, gh=gh) -> None`: writes the prompt to a temp file, `gh workflow run build.yml -R <repo> -f folder=<folder> -F prompt=@<file>`; deletes the file; fixed-phrase `ActionsError` on failure ("could not start the workflow").
  - `find_run(cfg, folder, since: dt.datetime, gh=gh, attempts=10, wait=6) -> int`: `gh run list -R <repo> --workflow build.yml --json databaseId,displayTitle,createdAt --limit 20`, picks the newest run with `displayTitle == f"build {folder}"` created at/after `since`; `ActionsError("could not find the workflow run")` after the attempts.
  - `wait_run(cfg, run_id, limit_s, gh=gh, sleep=time.sleep, clock=time.monotonic) -> str`: polls `gh run view <id> -R <repo> --json status,conclusion` every 30 s; returns `"building"` at the limit, else the conclusion.
  - `outcome(cfg, run_id, folder, gh=gh) -> tuple[str, str]`: maps the run's jobs (`gh run view --json jobs`): publish job success → read `<folder>/build-result.json` from the repo (`gh api repos/<repo>/contents/<folder>/build-result.json`) → `published` or `did_not_run`; publish job failure with exit 2 → `blocked` ("leak scan blocked the build"); build job failure and no publish → `failed` ("build job failed"); cancelled → `failed` ("workflow cancelled").
  - `URL = https://github.com/<repo>/tree/main/<folder>`.

- [ ] **Step 1: Failing tests** — fake `gh` recording argv and returning canned JSON: dispatch argv uses `-F prompt=@<file>` and the file is deleted afterwards; prompt never appears in argv; `find_run` ignores runs for other folders and older runs, raises after attempts; `wait_run` returns `building` at the limit (fake clock); `outcome` maps each case; the gh env excludes planted `GH_TOKEN`/`ANTHROPIC_API_KEY`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** "Builds: dispatch and poll the Actions workflow".

### Task 4: Rewire the build, repo setup, and remove the Docker path

**Files:**
- Modify: `src/radar/builds/run.py`, `src/radar/cli.py`, `src/radar/config.py`, `src/radar/settings.py`, `src/radar/ui/jobs.py`
- Create: `src/radar/builds/repo_init.py`, `tests/test_builds_repo_init.py`
- Delete: `src/radar/builds/sandbox.py`, `src/radar/builds/publish.py`, `src/radar/builds/keys.py`, `docker/`, `tests/test_builds_sandbox.py`, `tests/test_builds_publish.py` (move still-relevant tests: isolated git env and identity checks go to repo_init tests)
- Test: `tests/test_builds_run.py`, `tests/test_builds_cli.py`

**Interfaces:**
- Consumes: Task 3 `dispatch`, `find_run`, `wait_run`, `outcome`, `URL`.
- Produces:
  - `run_build(cfg, digest_folder, result, *, actions=actions_module, today=None, log=print) -> BuildRecord` — same skip logic (disabled, no repo, already published today, history corrupt, uncommitted profile, no candidate); then `dispatch` → `find_run` → `wait_run(limit = poll_min*60)` → `outcome`; statuses published | did_not_run | blocked | failed | skipped | building; `building` records store `run_id` in the record (new optional field) and are not added to history.
  - `sync_builds(cfg, actions=..., log=print) -> list[BuildRecord]`: for each record with status `building`, re-check the run; finish the record (and history) when done.
  - `radar build --sync` runs `sync_builds`; the daily run calls `sync_builds` before starting a new build; `radar build --init-repo` calls `repo_init.init_repo(cfg)`; remove `--prepare` and the `build-image` job action; add job actions `build-sync`, `build-init-repo`.
  - `repo_init.init_repo(cfg, git=..., gh=...) -> str`: clones the repo into a temp folder with the hardened git env (moved from publish.py: allowlisted env, hooks off, `safe.directory`, UTC dates, owner's configured noreply identity — this one commit is the owner's), writes `.github/workflows/build.yml`, `tools/publish_build.py`, `tools/scan.py` (byte-identical copy of `src/radar/builds/scan.py`), and `README.md` (only if absent), commits "Set up daily builds", pushes; returns the repo URL. Requires `gh auth status` showing the `workflow` scope, else `ActionsError("gh needs the workflow scope: run gh auth refresh -s workflow")`.
  - `repo_init.set_secret(cfg, key: str, gh=...) -> None`: `gh secret set ANTHROPIC_API_KEY -R <repo>` with the key on stdin.
  - `BuildsCfg`: keep enabled, mode, separate_time, repo, git_name, git_email (used only by init), work_dir; add `poll_min: int = 90`; remove image, cpus, memory_gb, timeout_min, max_file_mb, max_folder_mb, allow_hosts, claude_model, max_turns (the caps live in the vendored scan defaults; the model and turns are fixed in the workflow). Settings schema follows.
  - `test_vendored_scan_is_identical`: init writes `tools/scan.py` byte-identical to `src/radar/builds/scan.py`.

- [ ] **Step 1: Failing tests** — run_build with a fake actions module: published/did_not_run/blocked/failed/building paths; building not in history; `sync_builds` finishes a building record; init_repo against a local bare repo writes the four files, commits with UTC and the configured identity, refuses without the workflow scope; set_secret passes the key only on stdin; CLI `--sync`/`--init-repo` exit codes; daily run calls sync first.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement and delete the Docker path.** **Step 4: Run** the full suite — PASS.
- [ ] **Step 5: Commit** "Builds: run on GitHub Actions; repo setup; remove local sandbox".

### Task 5: Health checks, panel, email, docs

**Files:**
- Modify: `src/radar/doctor.py`, `src/radar/ui/app.py`, `src/radar/ui/static/index.html`, `src/radar/mailer.py`, `README.md`
- Test: `tests/test_doctor.py`, `tests/test_ui.py`, `tests/test_mailer.py`

**Interfaces:**
- Produces: doctor checks when builds are enabled — `gh signed in with workflow scope` (action URL to the docs line `gh auth refresh -s workflow`), `daily-builds repo` (public), `workflow installed` (`gh api repos/<repo>/contents/.github/workflows/build.yml`; action job `build-init-repo`), `Anthropic key secret` (`gh secret list -R <repo> --json name` contains `ANTHROPIC_API_KEY`; action `builds-key`); remove WSL2/Docker/image/API-key-keyring checks. Panel: the key field posts to `/api/builds/key` which calls `repo_init.set_secret` (never echoed); "Set up repo" button (job `build-init-repo`); "Sync" button (job `build-sync`); status badge `building` with the run link `https://github.com/<repo>/actions/runs/<run_id>`. Email: `building` → "Daily build still running: <run link>". README "Daily builds" section rewritten for Actions (setup: create the public repo, `gh auth refresh -s workflow`, Set up repo, store key, enable; safety model per the spec, stating plainly: open internet on runners, key readable by the build job, public logs, nothing personal sent).

- [ ] **Step 1: Failing tests** — checks present/absent with fake probes; key endpoint calls set_secret with stdin key and never echoes; building badge renders the run link escaped; email line for building.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; `node --check` the page script. **Step 4: Run** the full suite — PASS.
- [ ] **Step 5: Commit** "Builds: Actions checks, panel, email, docs".

### Task 6: First live build (owner-assisted)

- [ ] **Step 1:** Owner: create public repo `daily-builds`, run `gh auth refresh -s workflow`, create a spend-limited Anthropic key.
- [ ] **Step 2:** Panel: enable Daily builds (repo, noreply identity), "Set up repo" (approve the push), store the key.
- [ ] **Step 3:** `radar build` on the latest digest. Expected: workflow run visible in the repo's Actions tab; folder with executed `demo.ipynb` committed by the Actions bot; `build.json` written; panel shows it. Check the commit metadata (bot identity, +0000) and search the repo for the owner's email, Windows user name, and home path: no hits.
- [ ] **Step 4:** Full suite, ruff, push the branch, CI green.
