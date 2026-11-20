# Control panel: design

Date: 2026-11-19. Status: approved by the owner in conversation.

## Intent

One window to run and tune the daily agent without a terminal or hand-editing config files: run any step on demand, change settings and the schedule, work through the connection queue, and read or listen to past digests.

## Approaches

- **Local web panel (chosen).** `radar ui` serves a single page on `http://127.0.0.1:8765` and opens the browser. FastAPI backend (uvicorn is already a dependency through `mcp`), plain HTML/JS front end. Looks modern, shows well in a portfolio, reuses the existing Python modules directly.
- Desktop Tkinter app. No browser, but dated look and more effort for tables, audio, and live logs.
- Streamlit or NiceGUI. Fast to build, but heavier dependencies and less control over the job runner.

## Design

Tabs:

1. **Dashboard**: next scheduled run, last run result, post counts by tier, queue size, latest digest. Run-now buttons: Daily, Scan, Digest, Podcast, Email. Live log output. One job at a time.
2. **Schedule**: time picker and on/off switch; updates the `ResearchRadarDaily` Windows task.
3. **Settings**: form over `config/config.toml` (scan limits and pacing, model and thinking mode, Codex min/max items and backlog days, NotebookLM length and web sources, email on/off and recipient). Saves with comments preserved.
4. **Field profile**: editor for `config/field_profile.md`; "Save and re-judge stored posts".
5. **Connection queue**: table with profile links; mark Sent, Skip, or Todo.
6. **Digests**: list by day; summary, audio player, NotebookLM link.

Constraints:

- Binds to 127.0.0.1 only. No authentication, because only the local user can reach it.
- Jobs run as `radar` subprocesses, so the panel never blocks and a crash in a job cannot take the panel down. Output streams to the page.
- The panel never stores passwords. Logins and the App Password stay in the existing commands (`radar login`, `radar notebooklm-login`, `radar email-setup`), which the panel can launch.

## Builder calls

Exact layout and styling, endpoint names, polling versus server-sent events for logs.
