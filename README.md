# discord-newsbot

A Discord bot for a ~50-person community server built around **Borderlands 4**,
**Palworld**, and **Diablo IV**. Once a day it reads the internet — official
blogs, Steam announcements, subreddits, YouTube channels, Bluesky, and a
general web search — so the server doesn't have to, and posts an
AI-summarized digest. Members can also ask it for recent news or search past
stories on demand.

It is not a general-purpose news bot. The topics and sources are entirely
config-driven (see [Configuration](#configuration) below), so pointing it at
a different set of games is a config edit, not a code change, but out of the
box it only knows about the three games above.

## What a digest looks like

A digest is a header message (date, story counts per game, any coverage
caveats) with a public thread attached for discussion, followed by one embed
per game. Something like this (illustrative only — not real scraped
content):

> **News for 2026-09-24** — Borderlands 4: 2 stories · Palworld: 0 · Diablo IV: 1
>
> 🟢 **OFFICIAL · Hotfix 5 now live**
> Fixes a crash on the Vault of the Traveler boss fight and adjusts loot drop
> rates on Mayhem 6+.
> [Gearbox forums](https://example.com) · [Steam](https://example.com)
>
> 🟡 **REPORTED · Datamined patch notes point to a new Vault Hunter**
> Several outlets are citing files pulled from the latest patch; nothing
> official yet.
> [PC Gamer](https://example.com) +2 more

Stories are sorted official, then reported, then rumor, each labeled so
nobody mistakes a leak for a patch note. A topic with nothing new just says
"No new stories today."

## Commands

- **`/news recent game:<topic|All> days:<1-30, default 7> label:<official|reported|rumor, optional> public:<bool, default false>`**
  — recent stories for a game (or all of them), optionally filtered by label.
- **`/news search query:<text> days:<1-30, default 30> public:<bool, default false>`**
  — full-text search over story headlines and summaries.
- **`/newsbot status`** (admin) — last run and its status, source health, item/story
  counts for the last 24h, and estimated Claude spend for the month.
- **`/newsbot run-now`** (admin) — runs the pipeline and posts immediately. Asks
  for confirmation if today's digest already went out.
- **`/newsbot preview`** (admin) — runs the pipeline and shows the digest only to
  the admin who ran it. Nothing is saved or posted, so a preview never
  changes what the next real run sees.

Command results default to a private (ephemeral) reply; `public:true` shows
them to the whole channel. Multi-page results get Previous/Next buttons that
only the person who ran the command can use.

## Architecture

A single container runs a single Python process: one `discord.py` client
that also drives the daily job on an in-process `APScheduler` scheduler. The
pipeline (collect → normalize/dedupe → filter by topic → summarize with
Claude → store and post) is written as plain functions with no Discord
dependency, so it can run headless from the CLI or be tested without a
gateway connection at all — the bot is a thin adapter on top of it.

```
newsbot/
  config.py        load + validate config.yaml and secrets (pydantic)
  collectors/       one module per source type -> list[RawItem]
    rss.py  steam.py  bluesky.py  web_search.py
  pipeline/
    normalize.py    URL canonicalization, dedupe vs. the store
    filter.py       keyword topic matching, "uncertain" flag, dedicated sources
    summarize.py    Claude call, prompt assembly, schema validation, fallback
    run.py          orchestrates one daily run; also the headless CLI entry point
    publisher.py    Publisher protocol (stdout for the CLI, Discord for the bot)
  store/
    migrations/     numbered .sql files
    db.py           connection, WAL, migration runner
    repo.py         all queries (no SQL anywhere else)
  bot/
    client.py       discord client, scheduler wiring, heartbeat
    commands.py     /news, /news search, /newsbot status|run-now|preview
    format.py       digest + result embeds, paging, UTF-16-aware limits
  alerts.py         admin-channel notifications
  healthcheck.py    Docker HEALTHCHECK entry point (checks the heartbeat file)
```

## Local development

Requires **Python 3.14** and plain `venv` + `pip` — no `uv`, no Poetry.
`requirements.txt` is the fully pinned lock file that Docker and CI install
from; regenerate it with `scripts/lock.sh` after changing the dependency
list in `pyproject.toml`, never by hand.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e . --no-deps
```

The gate (lint, format check, tests) that has to stay green:

```bash
ruff check . && ruff format --check . && pytest -q
```

Exercise the whole pipeline with no Discord and no network at all:

```bash
python -m newsbot.pipeline.run --dry-run --config config.example.yaml \
  --db /tmp/nb.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json
```

Drop `--fixtures`/`--stub-llm` to hit real sources and Claude with a real
config — that's how the source list and the summary prompt get tuned in
practice; see `docs/sources-research.md` for how the seed sources here were
verified.

### Running the dev bot

1. Copy `.env.example` to `.env.dev` and fill in a **dev** Discord bot token
   (a separate application from prod — never run two processes on the same
   token; they'll race each other and the loser logs `Unknown interaction
   (10062)`).
2. Copy `config.example.yaml` to `config.dev.yaml` and point it at a private
   test guild and channel IDs.
3. Run directly:
   ```bash
   set -a && . ./.env.dev && set +a
   NEWSBOT_CONFIG=config.dev.yaml python -m newsbot
   ```
   or via Docker Compose, which builds the image locally instead of pulling
   from GHCR:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
   ```

Never run `docker compose ... config` (or anything else that resolves
`env_file`) against the real `.env`/`.env.dev` — it prints every secret in
plain text to the terminal.

Any change to the summarization prompt needs an owner-reviewed
`/newsbot preview` before it merges — that's the whole point of the preview
command existing.

## Configuration

`config.yaml` (git-ignored; `config.example.yaml` is the committed template)
is mounted read-only into the container. Secrets live in `.env`, which is
also git-ignored.

Highlights of the schema — see `config.example.yaml` for a complete, real
example, and `docs/design.md` section 3 for the full spec:

- **`topics`**: each has a `key`, display `name`, `aliases`, `entities`
  (looser, "uncertain" matches), and optional `search_queries` used for that
  topic's Brave News queries instead of the global templates.
- **`sources`**: `rss`, `steam_news`, `bluesky_search`, and `web_search`.
  Each has a `trust` level (`official`, `press`, or `community`), which
  affects labeling and which items survive the per-topic cap.
- **Dedicated sources**: a source whose `topics` list names exactly one
  topic is a confident match for that topic even if the item's text never
  mentions the game by name — this is how, say, a Steam patch-notes post
  titled "v0.6.2 Update" still reaches the Palworld digest.
- **Secrets** (`.env`): `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `BRAVE_API_KEY`,
  and optionally `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD` for authenticated
  Bluesky search (see Limitations below).

## Costs

Rough running cost against `config.example.yaml`'s source list:

- **Claude (Haiku 4.5)**: about 3 summarization calls per run (one per
  topic with items), roughly **2 cents per run**.
- **Brave Search**: 2 queries per topic × 3 topics = about **6 requests per
  run**, roughly **180 requests a month** — comfortably inside Brave's free
  tier as configured.

## Limitations and known issues

- **Reddit may block datacenter IPs.** The subreddit sources were verified
  from a residential connection; Reddit rate-limits aggressively even there,
  and may reject requests outright from a hosting provider's IP range in
  production. If it happens, it shows up in `/newsbot status` as a source
  health failure, not a crash.
- **YouTube channel feeds occasionally 404 or change ID.** These are plain
  RSS reads (`youtube.com/feeds/videos.xml?channel_id=...`) with no official
  guarantee of stability.
- **Running `/newsbot run-now` a second time after today's digest already
  posted usually produces a mostly empty digest.** It re-runs the full
  pipeline; most items are already deduped against the store, and the model
  is told to mark stories that add nothing new as `relevant: false`.
- **Bluesky search needs an app password.** Unauthenticated
  `bluesky_search` requests currently get a 403 with an HTML body (not even
  a JSON error) from Bluesky's public API. Without `BLUESKY_HANDLE` and
  `BLUESKY_APP_PASSWORD` set, those sources are skipped with a coverage
  note, not treated as a failure. The three official-account RSS feeds work
  regardless — no auth needed for those.

## Safety notes

- Scraped text is **untrusted data**. It's sent to Claude clearly delimited
  as such, and any instructions embedded in it are ignored by prompt design.
- Every link posted to Discord comes only from the URLs actually collected
  from sources — the model can't introduce a new URL, and any URL the model
  invents that wasn't in its input is discarded before the digest is
  rendered.
- Any URL text that shows up inside a model-written headline or summary is
  stripped before rendering, so scraped or generated text can't grow a fake
  markdown link next to a real one.
- All messages are sent with `allowed_mentions=none` — scraped text can't
  ping `@everyone`, a role, or a user.

## Deployment

See [`docs/deploy.md`](docs/deploy.md) for the Droplet runbook (install,
secrets, rollback, backups, log viewing).

## License

Not yet chosen. Treat this as "all rights reserved" until the owner picks
one.
