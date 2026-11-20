# Daily builds: one small working demo of the day's top open-source work

Date: 2026-11-19. Status: draft for the owner's review. Revised after a builder check (Codex):
network allowlist, isolated checks and publishing, optional stages, retry and history rules.

## Intent

The digest names new packages and techniques every day, but reading about a package is not the
same as using it. Each day, Research Radar picks one open-source item from the digest and has
Claude Sonnet build a very small, complete application with it. The build is a learning kit: it
shows what the package is for, how to install it, and how to take it from a blank folder to a
result. Builds are published to one public GitHub repo, and the day's blog post links to them.

## Goals

- One build per day, from the highest-ranked digest item that is open source and runnable on a
  CPU in minutes. No build on days with no suitable item.
- Each build teaches: what problem the package solves, its core ideas, a step-by-step walkthrough
  on a tiny task from the reader's field, real output, gotchas, and how to adapt it.
- Builds land in one public repo, one folder per build, with names detailed enough to search.
- A "Daily builds" tab in the panel lists every build with status, README, output, log, and link.
- The day's blog post on abrarfaiyad.com links to the build automatically.
- Safe to run unattended.

## Non-goals

- Production-quality code. Builds are educational, unreviewed, and labeled as such; bugs are
  acceptable.
- A review or publish step. Builds that pass the leak scan are pushed automatically.
- Large models, multi-GPU training, or checked-in datasets and weights.

## Baseline: no personal information leaves the machine

This is the owner's hard requirement and outranks every other goal.

1. **Nothing personal enters the sandbox.** The build container gets only: the selected digest
   item's public fields (title, authors, venue, URLs, what/how/why, limitations), the public
   field profile, the build instructions, and a capped API key. Never the database, queue,
   connections, CV, onboarding state, config.toml, email settings, browser profiles, or the
   digest's `relevance` and `rejected` fields. No host folders are mounted except the empty
   per-build output folder.
2. **No host environment leaks in.** The container starts with a clean environment: no host
   environment variables, no git config, no SSH keys, no GitHub token, no Windows user name in
   paths.
3. **Leak scan before every push.** The host scans every file of the build folder and blocks the
   push if it finds: API keys, tokens, or private keys (common patterns plus high-entropy
   strings); the owner's email addresses and phone number (read from local config at scan time,
   never written to the repo); Windows user paths (`C:\Users\<name>`), the machine name, or the
   local user name; any file over the size cap (default 5 MB) or folder over 20 MB; binary files
   other than small PNG/SVG output. A blocked build stays local and the panel shows the reason.
4. **Commits carry no personal metadata.** The host commits with the owner's GitHub noreply
   identity and pushes with the existing gh credential helper, outside the sandbox.
5. **Tests prove it.** Synthetic personal data is planted in the host environment and data
   folder; tests assert none of it reaches the container input or passes the scan.
6. **The sandbox can reach only an allowlist.** The container sits on an internal Docker network
   whose only exit is a small egress proxy. The proxy allows PyPI, GitHub (code and release
   downloads), Hugging Face, Read the Docs, `*.github.io`, and the Anthropic API; the list is a
   setting. Everything else is refused, including the local network, the router, and the
   Docker/WSL host. A malicious package therefore cannot probe the LAN or send data to an
   arbitrary server.
7. **The host never runs build code.** The clean-install check and smoke test run in a second,
   fresh container with the same isolation. The host only reads the output folder.
8. **The host never follows build files.** The output is copied as plain bytes; symlinks,
   hard links, device files, and archives are rejected. The scan runs on the exact file contents
   that will be committed.
9. **Publishing is isolated.** A dedicated clone used only for this repo, with git hooks and
   filters disabled, a fixed author and committer (the owner's noreply identity), and a check of
   the remote URL and of every outgoing commit's full tree before the push. The top-level index
   the host writes goes through the same scan.

Scope: this baseline covers the build and everything it publishes. The existing digest already
sends post text to Codex and the digest to NotebookLM, services the owner chose; those flows are
unchanged and private, not published. The field profile passed to the sandbox is the one already
committed in the public repo, so it adds no new disclosure. Because the sandbox receives nothing
private, its output cannot contain private information either, including in images or logs.

## Approaches

1. **Claude and the build both run inside the container (chosen).** The container holds nothing
   personal, so even a malicious package that takes over the container can steal only the
   capped API key and the public build. Simplest strong guarantee for the baseline.
2. Claude on the host, running commands only inside the container. Keeps the API credential off
   the container, but Claude's file tools would run on the host, next to personal data, and
   package text read by Claude could steer those tools. Rejected: weaker against the baseline.
3. No sandbox, a separate Python environment on the host. Rejected: fresh packages from PyPI are
   where malicious and typo-squatted packages appear.

## How it works

### Schedule

- Default: the build starts right after Codex finishes the digest, inside the same daily run,
  in parallel with the NotebookLM podcast. The run waits for both before emailing, so the email
  reports the build.
- Option in the panel (Settings, "Daily builds"): run builds separately on their own schedule
  (`mode = with_digest | separate`, `separate_time = "08:30"`).
- A failed or skipped build never fails the daily run; it is recorded as its own stage. This
  needs two pipeline changes: stages can be marked optional (their failure is recorded but does
  not flip the run's result), and the digest stage splits so the Codex step returns before the
  podcast, letting the podcast and the build run in parallel threads.
- A separate-mode build uses the latest completed digest of the local day and skips if there is
  none. At most one published build per day.

### Picking the item

Codex's digest gains three fields per item: `code_url` (public repository or package page, or
empty), `license` (SPDX id or empty), and `cpu_demo` (true if a tiny demo can run on a CPU in
under about 10 minutes). The build stage takes the highest-ranked item with a code URL, an
OSI-approved license, and `cpu_demo` true, skipping packages already built (history in
`data/builds/history.json`). No candidate: the stage records "skipped" with the reason.

- A package enters history only after a successful push. Failed and blocked attempts are kept
  separately, so a temporary failure does not exclude the package forever.
- Digests from before this change lack the new fields; their items are ineligible.
- The license must be one unambiguous OSI-approved SPDX id for the code repository itself;
  unknown, missing, or conflicting licenses are skipped.

### The sandbox

- Docker container from a pinned image (Python 3.12, git, Node for Claude Code), run inside
  WSL2. Prerequisites the owner installs once: `wsl --install` (needs an administrator and a
  reboot) and Docker Desktop. The panel's checklist checks both and links to the installers.
- Limits: 4 CPUs, 8 GB memory, 10 GB disk, 45-minute wall clock. Network on (PyPI, GitHub,
  documentation); no ports exposed; runs as a non-root user; container removed after each build.
- Claude Code runs headless inside: `claude -p --model sonnet` with a turn cap and the build
  instructions. The API key is a dedicated Anthropic Console key with a monthly spend limit set
  by the owner; it is passed only to this container.

### What a build contains

Folder name: `YYYY-MM-DD_<package>_<task>`, all lowercase, hyphens, for example
`2026-11-21_mace-torch_lora-finetune-molten-salt`.

```
README.md          learning kit, fixed sections (below)
demo.py            commented, reads top to bottom (or a small package if needed)
requirements.txt   pinned versions
test_demo.py       smoke test: the demo runs and produces its output
output/            small results: PNG plot, CSV or Markdown table
BUILD_LOG.md       what Claude tried, what failed, how it was fixed
```

README sections: AI-generated notice and educational disclaimer; what the package is and the
problem it solves; core ideas in plain words; where it fits against tools the reader knows;
install; step-by-step walkthrough of the demo; the actual output; gotchas found while building;
limitations; how to adapt it to your own work; source paper, package link, and license.

### Checks before pushing

The demo must install from `requirements.txt` in a clean environment and its smoke test must
pass (in the second container). A build that fails these is still pushed (bugs are acceptable)
but marked "did not run" at the top of its README and in the index. If generation times out or
the folder lacks a README and a demo file, nothing is pushed and the build is recorded as
failed. The leak scan always blocks a push when it finds anything.

Retry runs the same item again in a fresh container and keeps the attempt history. It never
overwrites a folder that is already published.

### Publishing

- Target: one public repo, set in config (`[builds] repo = "AbrarFaiyad/daily-builds"`). The
  host copies the build folder into a local clone, updates the top-level README index (date,
  package, what it shows, license, ran or did not run), commits, and pushes.
- The digest folder records the build's URL in `build.json` (folder name, package, URL, status).

### Blog link

The website's import script reads `build.json` and adds a highlighted "Daily build" card at the
top of that day's post. That change lives in the website repo; this project only writes
`build.json`. The website session gets a brief once this design is approved.

### Panel

- New tab "Daily builds": live list (date, package, task, status: building, published, did not
  run, blocked, skipped), and for each build its README, output images, build log, GitHub link,
  and a Retry button.
- Settings section "Daily builds": on or off, mode, separate time, target repo, size caps,
  container limits. API key entry like the Gmail App Password: stored in Windows Credential
  Manager, never shown again.
- Checklist entries: WSL2, Docker, build image, API key, target repo.

### Defaults for other users

The feature ships off. It needs a target repo and an API key before it can run. The owner's
config turns it on.

## Left to the builder

Exact prompt wording, the container image contents, secret-pattern list details, the item
picker's tie-breaking, and panel layout.
