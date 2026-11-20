# Daily builds on GitHub Actions (design update)

Date: 2026-11-20. Status: draft for the owner's review. Supersedes the "sandbox" and
"publishing" parts of `2026-11-19-daily-builds-design.md`; everything else there stands.

## Why

The owner does not want packages installed on their PC. GitHub Actions runs the build on a
fresh GitHub-hosted machine that is discarded afterwards, free for public repositories. No WSL,
Docker, or local installs are needed, and the owner's data never leaves the PC at all.

## What changes

- **Where the build runs:** a workflow in the public `daily-builds` repo, not a local Docker
  container.
- **What a build is:** a folder with a Jupyter notebook (`demo.ipynb`) that opens with an
  executive summary and walks through the package step by step in runnable cells, plus a short
  `README.md` (the same summary and how to run it), pinned `requirements.txt`, and small output
  images. The notebook is executed top to bottom on the runner, so the published copy shows real
  output. GitHub renders notebooks, so the blog card links straight to the folder.
- **No Colab or Binder links.** How readers run it is up to them.

## Flow

1. The daily run picks the item (unchanged) and renders the build prompt from the item's public
   fields and the committed field profile (unchanged).
2. Research Radar starts the workflow: `gh workflow run build.yml -R <repo>` with two inputs,
   the day/folder name and the prompt. Nothing else leaves the PC.
3. **Job 1, build (untrusted):** checks out nothing private, installs pinned Claude Code, runs
   `claude -p` with the prompt and the `ANTHROPIC_API_KEY` secret, then executes the notebook
   with papermill (per-cell timeout, whole-job timeout). Permissions: `contents: read` only, no
   write token. Uploads the folder as an artifact. A failed execution still uploads, marked
   "did not run".
4. **Job 2, publish (trusted):** no API key, runs no build code. Downloads the artifact, rejects
   links and non-regular files, strips PNG metadata, runs the leak scan (a copy of
   `radar.builds.scan` vendored into the repo), updates the index README, and commits as the
   `github-actions[bot]` identity (UTC). Permissions: `contents: write`.
5. Research Radar polls the run (`gh run view --json`) until it finishes (default up to
   90 minutes), then writes `build.json` (status, package, folder, URL) for the panel, the email,
   and the website import. A run still going at the limit is recorded "still building"; the next
   panel refresh or daily run picks up its result.

## Repo setup

`radar build --init-repo` (and a panel button) writes the workflow file, the vendored scan
module, the PNG sanitizer, and the index README into the `daily-builds` repo once, through a
normal commit and push the owner approves. The panel's API key field stores the key as the
repo's Actions secret with `gh secret set ANTHROPIC_API_KEY -R <repo>` (key passed on stdin).
The key is no longer stored on the PC.

## Safety

- The PC sends only public data (item fields and the committed profile). The runner never sees
  the owner's data folder, config, email, or identity.
- Commits come from the Actions bot with UTC time: no owner name, email, or timezone.
- The untrusted job has no write token, so a malicious package cannot push to the repo or alter
  the workflow. It can read the API key, so the key stays dedicated and spend-limited.
- Runners have open internet; there is no allowlist. That matters less now, because nothing
  personal is on the runner.
- Public repo Actions logs are public. They contain only the public prompt and build output;
  GitHub masks the secret.
- The leak scan still runs before every commit (defence in depth).

## What is removed from the current branch

The local Docker sandbox (`sandbox.py`, `docker/`), local publishing from the PC (`publish.py`),
the local key store (`keys.py`), the WSL/Docker/image checks, and the `build-image` action.

## What is kept

Item selection and history, the prompt template (adapted to a notebook), the leak scan and PNG
sanitizer (vendored into the workflow), run records and `build.json`, the panel tab, settings
(trimmed), the email line, the separate-schedule mode, and the README section (rewritten).

## New checks

`gh` signed in with the `workflow` scope; the repo exists, is public, and has the workflow file;
the `ANTHROPIC_API_KEY` secret exists (`gh secret list`).

## Left to the builder

Workflow YAML details, papermill timeouts, the polling interval, and panel wording.
