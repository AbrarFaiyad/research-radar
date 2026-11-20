# GUI-complete control panel and onboarding wizard

Date: 2026-11-19. Status: approved in conversation ("onboarding and everything else should be
possible through the gui").

## Intent

A new user should go from a fresh clone to a working daily digest without typing commands
after one install step. The panel becomes the product; the `radar` command line stays as the
engine that the panel, the scheduler, and CI call.

## Goals

- First-run wizard: CV (PDF, DOCX, TXT, MD) or a pasted description becomes a draft field
  profile and reader line; a calibration pass shows what the draft would keep or skip; the
  user corrects wrong calls; the profile is refined from the corrections; the user approves
  before anything is written.
- Every setup step has a button: config file, local model pull, Playwright Chromium, Codex
  login, LinkedIn and NotebookLM logins, Gmail App Password, schedule, test email.
- Every pipeline action has a button, including per-digest podcast retry and re-send, digest
  without audio, and the queue CSV download.
- One-command install creates the environment and a desktop shortcut that opens the panel.
  Launching twice reuses the running panel.

## Non-goals

- Installing system software (Python, Node, Ollama) from the panel. The panel links to their
  installers; winget would need agreement prompts the panel cannot show.
- Sending LinkedIn connection requests. Still queue only.

## Constraints

- Panel security model unchanged: loopback bind, Host check, token plus Origin on POST.
- The Gmail App Password may be typed into the panel; it goes straight to the OS credential
  store and is never returned, logged, or written to disk by the app.
- The CV is personal data. Contact details (emails, phone numbers) are removed before any
  model sees the text. The user picks the drafter: Codex (better, leaves the machine) or the
  local model (stays on the machine). The wizard says which.
- Calibration never writes to the posts database. It reads the live feed (read-only browser
  tools) or, for an existing install, the stored posts.
- Long work (Codex, browser, model) runs as panel jobs through `radar` subprocesses, so it
  takes the run lock and streams a log like every other job.

## Approaches

1. **Wizard over CLI jobs (chosen).** Each wizard step is a `radar onboard <step>` command run
   by the existing job runner; state lives in `data/onboarding/`. Same lock, log, cancel, and
   test path as the rest of the panel.
2. Wizard logic inside the web server. Fewer files, but long Codex and browser calls would
   block server threads, bypass the run lock, and could not be cancelled.
3. Separate desktop app (Tauri/Electron). Native feel, but a second stack to build and ship
   for no capability the browser panel lacks.

## Design

- `radar/onboard.py`: `extract_cv_text`, `redact`, `draft_profile` (Codex or local model,
  JSON schema `{reader, profile_markdown, notes}`), `calibrate` (live feed or stored posts,
  classify with the draft, no DB writes), `refine_profile` (draft + verdicts + corrections),
  `apply` (backup old profile, write new one, save reader). State file
  `data/onboarding/state.json`.
- `radar onboard draft|calibrate|refine` and `radar setup config|model|chromium|codex|codex-login`
  commands; `radar email --test`.
- Panel: "Setup" tab with the wizard and a setup checklist (doctor checks with Fix buttons);
  opens automatically when no `config/config.toml` exists. Digest tab gains podcast retry and
  re-send; queue tab gains CSV download; dashboard gains run history.
- `scripts/install.ps1`: venv, dependencies, config, desktop and Start-menu shortcuts.

## Left to the builder

Wizard layout and copy, prompt wording, which doctor checks are auto-fixable.
