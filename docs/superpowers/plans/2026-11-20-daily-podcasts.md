# Daily Podcasts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Publish each day's emailed podcast file as a GitHub Release in the public `daily-podcasts` repo and record where it lives.

**Architecture:** A new `radar.podcasts` module uses the allowlisted `gh` helper from `radar.builds.actions` to create the repo (once) and a release per digest folder with the email-ready episode as its asset; it writes `podcast.json` into the digest folder. The daily run calls it as an optional stage after the digest; panel, checks, email and README follow.

**Tech Stack:** Python 3.12, `gh` CLI, existing `podcast.email_ready`, `radar.builds.scan`.

**Spec:** `docs/superpowers/specs/2026-11-20-daily-podcasts-design.md`

## Global Constraints
- File published = the file `podcast.email_ready` returns for that digest (m4a if within 16 MiB, else the mono MP3); no other re-encoding.
- Release tag = digest folder name (`^\d{4}-\d{2}-\d{2}(-\d+)?$`); title `"<date>: <headline>"`; notes = kept items' titles + primary URLs only.
- Release title and notes pass `scan.scan_bytes(..., personal_markers(cfg))` with no "personal information", "local path" or "secret pattern" finding, else skip "release text contains personal information".
- All `gh` calls through `radar.builds.actions.gh` (allowlisted env, argv lists); errors are fixed phrases.
- `[podcasts] enabled = false` by default; `repo = ""` (owner/name).
- `podcast.json`: `{"status", "tag", "url", "page"}` (+ `"reason"` when not published); `url` = `https://github.com/<repo>/releases/download/<tag>/<file>`, `page` = `https://github.com/<repo>/releases/tag/<tag>`.
- Optional stage: a failure never fails the daily run. Tests use a fake `gh`; no network, no real config (conftest pattern from builds).

## Review Focus
1. Release already exists with the asset → no duplicate upload; exists without it → asset uploaded. (Task 1)
2. No audio for the day (podcast failed) → `podcast.json` skipped "no episode", nothing published. (Task 1)
3. Headline containing an owner marker → skipped, nothing sent. (Task 1)
4. gh not signed in / repo missing → fixed-phrase skip, run continues. (Task 1)
5. Builds/podcasts disabled → daily run unchanged. (Task 1)

### Task 1: Publisher, CLI, daily stage, settings
**Files:** Create `src/radar/podcasts.py`, `tests/test_podcasts.py`; modify `src/radar/config.py`, `src/radar/settings.py`, `config/config.example.toml`, `src/radar/cli.py`, `src/radar/ui/jobs.py`, `tests/conftest.py`.
**Interfaces (produces):** `PodcastsCfg(enabled=False, repo="")` on `Config.podcasts`; `podcasts.init_repo(cfg, gh=...) -> str` (create `gh repo create <repo> --public --description ...` if `gh repo view` fails; add README via `gh api -X PUT repos/<repo>/contents/README.md` with base64 content "Daily Research Radar podcast episodes, one release per day. AI-generated (NotebookLM) discussions of public research news."; fixed phrases); `podcasts.publish(cfg, folder: Path, result: dict, gh=...) -> dict` (the podcast.json dict; writes it); `radar podcast --publish [folder]`, `radar podcast --init-repo`; job actions `podcast-publish`, `podcast-init-repo`; daily run: optional stage `podcast-publish` after `digest` when podcasts enabled (uses the folder the digest stage produced; skips cleanly when no digest).
- [ ] Tests first (fake gh): create release + upload; release exists with asset → no upload; exists without → `gh release upload`; no episode → skipped; marker in headline → skipped and gh never called for release; gh failure → failed with fixed phrase; tag regex; notes contain titles/URLs and never `relevance`; disabled → no stage; settings validation for repo pattern and enabled-needs-repo.
- [ ] Implement; full suite; ruff; commit "Podcasts: publish episodes as GitHub releases".

### Task 2: Panel, checks, email, README
**Files:** `src/radar/doctor.py`, `src/radar/ui/app.py`, `src/radar/ui/static/index.html`, `src/radar/mailer.py`, `README.md`, tests.
**Interfaces:** doctor check "daily-podcasts repo" (public; action job `podcast-init-repo`) when enabled; Digests tab shows the release link (only `https://github.com/` URLs, escaped) from podcast.json; "Set up podcasts repo" button; per-digest "Publish podcast" button (job `podcast-publish` with that folder; retries a skipped/failed day); email line "Podcast online: <page>" when published; README section "Daily podcasts" (what is published, where, privacy note, setup).
**Owner rule: "Everything needs to be configurable via the GUI".** Every field of every config section dataclass (`ProfileCfg`, `BrowserCfg`, `ScanCfg`, `LlmCfg`, `DigestCfg`, `NotebookLMCfg`, `EmailCfg`, `BuildsCfg`, `PodcastsCfg`) has a `settings.SCHEMA` entry and shows in the Settings tab. Currently missing: `llm.host`, `notebooklm.length` (choice shorter/default/longer), `email.smtp_host`, `email.smtp_port`, `digest.codex_command`, `browser.mcp_command`, `browser.mcp_args` (list: one argument per line in a textarea), `browser.feed_url` (must start `https://www.linkedin.com/`), `builds.work_dir`, plus `podcasts.enabled`/`podcasts.repo` if Task 1 did not add them. Technical ones go under a collapsed "Advanced" group per section. Validation keeps the current style (ranges, patterns, `http://`/`https://` URLs).
- [ ] Tests first, including a coverage test: for each section dataclass, `{f.name for f in fields(cls)} == {schema keys for that section}` (no exclusion list); list-type round trip for `mcp_args`; `node --check`; full suite; commit "Podcasts: panel, checks, email, docs" and "Settings: every config key editable in the panel".
