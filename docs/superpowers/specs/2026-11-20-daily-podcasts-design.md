# Daily podcasts on GitHub (design)

Date: 2026-11-20. Status: draft for the owner's review.

## Intent
Publish each day's NotebookLM episode in a public GitHub repo, `daily-podcasts`, so the daily
blog post can play it. The owner chose the file the email already sends: the original `.m4a`
when it fits the 16 MiB email budget, otherwise the mono MP3 that `podcast.email_ready`
produces. No further compression.

## Decisions
- **Hosting: one GitHub Release per day** in `AbrarFaiyad/daily-podcasts`, tag = the digest
  folder name (`2026-11-21`, or `2026-11-21-2` for a second digest). The episode file is the
  release asset. Release assets do not count toward the repo's size, so the repo stays small
  for years; committing audio to git would pass 1 GB within about a year.
- **Release text:** title "<date>: <digest headline>"; notes list each kept item's title and
  primary source link. Nothing else from the digest: no "relevance"/"for your work" text, no
  rejected posts, no NotebookLM link.
- **Published as the owner** through the existing `gh` login (the owner's public GitHub
  account), from the PC, using the allowlisted `gh` environment from `radar.builds.actions`.
- **Safety check:** the release title and notes are run through the leak scan with the owner's
  personal markers before publishing; any finding skips the publish with a fixed reason. The
  audio itself discusses the digest, including the parts drawn from the field profile, which
  the owner already made public.
- **When:** in the 04:00 daily run, right after the podcast is made, as an optional stage
  `podcast-publish` (a failure never fails the run). Idempotent: an existing release with the
  asset is left alone; an existing release without it gets the asset uploaded.
- **Record:** `podcast.json` in the digest folder: `tag`, `url` (the asset's download URL,
  `https://github.com/<repo>/releases/download/<tag>/<file>`), `page` (the release page URL),
  `status` (published | skipped | failed), `reason` when not published.
- **Setup:** `[podcasts] enabled` (off by default) and `repo`; "Set up podcasts repo" button
  and `radar podcast --init-repo` create the public repo with a short README if it does not
  exist. `radar podcast --publish [folder]` retries a day.
- **Panel/checks/email:** a health check (repo exists and is public), the release link on the
  Digests tab, and the email links the release page.
- **Website:** the website session adds an audio player to each daily post that streams `url`
  from GitHub, plus a link to `page`, reading `podcast.json` (whitelisted fields).

## Non-goals
Re-encoding beyond the email version; a podcast RSS feed (possible later); committing audio
into git.

## Left to the builder
Release-notes layout, retry timing, panel wording.
