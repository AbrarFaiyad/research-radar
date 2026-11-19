# Research Radar: design

Date: 2026-11-19. Status: approved in conversation (field filter, queue policy, Codex instead of NotebookLM); builder calls marked **[builder call]**.

## Intent

A local agent pipeline that keeps a computational materials scientist current on AI-for-materials research. It reads the LinkedIn feed once a day, saves posts that report real scientific progress in the field, and lists the authors worth connecting with. Over time, connecting with those authors makes the feed itself more relevant. Once a day, Codex researches the saved papers and produces an executive summary and a short two-host podcast episode on how and why each paper works.

The repository doubles as a portfolio piece to show an interviewer, so it must be clean, tested, and runnable without a LinkedIn account (fixture mode).

## Goals

- Read the feed at human pace, extract post text, author, connection degree, and outbound links.
- Classify each post with a local LLM against a written field profile. Save core and adjacent posts.
- Maintain a connection queue: authors of saved posts who are not 1st-degree connections. The user sends every request by hand.
- Daily digest: local LLM curates the saved items, Codex researches them with web search, the pipeline renders a markdown summary and a podcast audio file.
- Run unattended once a day on Windows.

## Non-goals

- Sending connection requests, likes, comments, or messages. The agent never clicks Connect, by construction.
- Scraping profiles, search results, or anything beyond the feed.
- Cloud hosting. Everything runs on the user's machine except Codex calls.

## Anti-goals (failure even if it works)

- Getting the user's LinkedIn account restricted: bursty scrolling, high volume, or automated invites.
- Committing personal LinkedIn data (names, posts, profile URLs) to the public repository.
- A digest full of congratulations, job moves, and graduation posts. Only informative scientific progress counts.

## Constraints

- Hardware: Ryzen 7 9800X3D, 32 GB RAM, RTX 5080 16 GB. Windows 11.
- Local LLM: Ollama 0.40.16 with `mimo-v2.6` (9B, 6.6 GB, tools/thinking/vision, 256K context). Runs 100% on GPU at about 64 tokens/s.
- Codex CLI 0.167.0. Web search via top-level `codex --search`. Structured output via `--output-schema`.
- LinkedIn User Agreement section 8.2 bans automated access and scraping. The user accepts this risk for low-volume reading. Pacing limits below are mandatory defaults.
- LinkedIn feed DOM (observed 2026-11-19): class names are obfuscated. Stable hooks are `div[role="listitem"][componentkey^="update-card-focus"]` per post, `[data-testid="expandable-text-box"]` for the body, `[data-testid="expandable-text-button"]` for "… more", `button[aria-label^="Open control menu for post by "]` for the author name, and the text `• 1st|2nd|3rd+` for degree. The feed scrolls inside `main#workspace`, not the window. Cards load lazily, so the scanner extracts after every scroll step and dedupes by card key. External links are wrapped as `linkedin.com/safety/go/?url=<lnkd.in short link>`.
- No system Chrome or Edge. Playwright's bundled Chromium is used.

## Field profile (approved)

- **Core:** machine-learning interatomic potentials (MACE, UMA, NequIP, Allegro, DeepMD), atomistic foundation models and datasets (FAIR Chem, OMat, OC20/OC25, MatterGen), DFT and MD workflows, active learning, enhanced sampling, generative and AI-for-chemistry/materials methods (Boltzmann generators, diffusion for materials), molten salts, 2D materials and MoS2/TMD electronics, tribology, electrochemical interfaces.
- **Adjacent:** HPC and exascale for science (ALCF, Aurora, SC conferences), scientific ML (neural operators, reduced-order models), AI agents for science, AI accelerators for scientific computing.
- **Skip:** promoted posts, LinkedIn News, general AI business news, unrelated fields, and personal milestones (graduations, defenses, new jobs, awards) unless the post reports a concrete result.

## Approaches considered

1. **Local LLM drives the browser freely** through Playwright MCP tool calls (navigate, click, scroll, snapshot). Most "agentic", but a 9B model driving a virtualized feed is flaky, and a free-roaming agent could click Connect.
2. **Plain Playwright script, LLM only for judgment.** Reliable, but drops the MCP integration the user asked for.
3. **Hybrid (chosen) [builder call]:** a Python orchestrator talks to the Playwright MCP server over stdio and may call only an allowlist of tools (`browser_navigate`, `browser_evaluate`, `browser_wait_for`, `browser_snapshot`, `browser_close`). Scrolling and extraction are deterministic JavaScript run through `browser_evaluate`. The local LLM makes the judgment calls: relevance, tier, topics, one-line summary, and daily curation. This keeps the MCP architecture, is testable, and makes "never sends invites" a property of the code, since no click tool is reachable.

The user handed the remaining how to the builder when they stepped away; approach 3 is the builder's pick.

## Design

### Components

- `radar.browser.mcp_client`: starts `npx -y @playwright/mcp@0.0.84 --user-data-dir data/browser-profile --browser chromium` over stdio with the official `mcp` Python SDK. Enforces the tool allowlist.
- `radar.browser.extract_feed.js`: per scroll step, expands "… more" inside visible cards, reads each card, returns JSON records keyed by `componentkey`.
- `radar.scanner`: scroll loop with random 600–1100 px steps and 2.5–6 s pauses. Stops at `max_posts` (default 60), `max_minutes` (default 10), or 6 steps with no new cards. Dedupes by card key and by text hash.
- `radar.links`: unwraps `safety/go` URLs, resolves `lnkd.in` short links with HTTP redirects, classifies targets as arxiv, doi, biorxiv/chemrxiv, github, journal, or other.
- `radar.llm`: Ollama `/api/chat` client with JSON-schema structured output, `think: false`, `num_ctx` 16384, and today's date in the system prompt.
- `radar.classify`: rule prefilter (promoted, empty text, company "news" boilerplate), then LLM verdict `{tier: core|adjacent|skip, kind, topics[], summary, reason}`.
- `radar.db`: SQLite at `data/radar.db`: `posts`, `authors`, `digests`.
- `radar.queue`: exports `data/queue/connect_queue.md` and `.csv`. Authors with `/in/` URLs whose degree is not 1st, from saved posts only, ranked by tier, then times seen, then recency. Status column `todo|sent|skip` set by `radar queue mark`.
- `radar.digest`: picks saved posts since the last digest, local LLM curates and dedupes (max 8 items), writes a Codex task, runs `codex --search exec -s read-only --skip-git-repo-check --ephemeral --output-schema <schema> -o <out.json> -`. Output: executive summary items plus a two-host script.
- `radar.podcast`: renders `data/digests/YYYY-MM-DD/summary.md` and `episode.wav`. TTS backends: `sapi` (Windows built-in voices, zero download, default) and `kokoro` (kokoro-onnx, higher quality, needs a one-time model download the user approves).
- `radar.cli`: `radar login | scan | queue | queue mark | digest | daily | demo`.
- `scripts/install_schedule.ps1`: registers a daily Windows Task Scheduler job running `radar daily`. Not run without the user's approval.

### Data flow

LinkedIn feed → Playwright MCP → extractor JSON → prefilter → local LLM verdict → SQLite → (a) connection queue export, (b) daily: local LLM curation → Codex web research → summary.md + podcast script → TTS → episode.wav.

### Privacy

`data/` (database, browser profile, queue, digests) is gitignored. Tests and the demo use synthetic fixtures only.

## Left to the builder

Exact prompt wording, schema field names, CLI flag names, file layout inside `src/radar/`, test granularity.

## Revision, 2026-11-19 (after the first live run)

The owner reviewed the first output and set two requirements.

- **Codex selects.** The local model is only a permissive first-pass screen. Every saved post goes to Codex, which keeps 5 to 15 items with real impact, rejects the rest with a reason, finds each item's primary source plus related coverage, and summarizes. The local 8-item curation step was removed.
- **NotebookLM-quality podcast.** Windows SAPI voices reading a Codex script were rejected. The owner chose to drive NotebookLM (rebranded "Gemini Notebook" at notebook.google.com, Nov 2026) in a second agent browser profile: create a notebook, upload `summary.md`, add source URLs, customise a Deep dive Audio Overview, download the `.m4a`. Interactive browser tools are allowed only behind a host guard for NotebookLM's hosts. The LinkedIn client stays read-only.
- **Daily email** to the owner with the digest, queue, podcast, and notebook link (Gmail SMTP, App Password in Windows Credential Manager).
