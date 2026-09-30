# discord-newsbot

A Discord bot, built for one particular ~50-person community server's three
games: **Borderlands 4**, **Palworld**, and **Diablo IV**. Once a day it
reads the internet (official blogs, Steam announcements, subreddits,
Bluesky, and a general web search) so the server doesn't have to, and posts an AI-summarized digest. Members can also ask it for
recent news or search past stories on demand.

It is not a general-purpose news bot, and it isn't one bot serving many
Discord servers at once: each server that runs this bot runs its own copy,
with its own token, config, and database. But the topics and sources are
entirely config-driven (see [Configuration](#configuration) below), so
pointing your own copy at a different game, or three different games
entirely, is a config edit, not a code change. **[Run your own
copy](#run-your-own-copy)** below is where to start if that's what brought
you here.

## Run your own copy

Two documents cover this:

- **[`docs/finding-sources.md`](docs/finding-sources.md)**: recipes for
  finding Steam, Bluesky, subreddit, and press sources for your own
  game, plus the aliasing lessons (some words match more than you'd think)
  that came out of doing this for the three games above.
- **[`docs/self-host.md`](docs/self-host.md)**: the actual setup guide:
  prerequisites, creating your own Discord application, keys and costs,
  `config.yaml` and `.env`, choosing an image, and the first run.

[`docs/deploy.md`](docs/deploy.md) is a different document: it's the maintainer's own
Droplet-specific runbook (see [Maintainer notes](#maintainer-notes) below),
useful as a worked example but not written for a general audience the way
the two documents above are.

## What a digest looks like

As of v2.0, each game gets its own channel (`topics[].channel_id`) and its
own message: no combined channel, no header, no discussion thread. A game
with nothing new that day posts nothing at all. Something like this
(illustrative only, not real scraped content), posted to that game's own
`#borderlands4`:

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
nobody mistakes a leak for a patch note. A coverage caveat ("Brave search
skipped: quota exceeded"), if there is one that day, rides along in that
game's own embed footer instead of a shared header; there's no longer a
shared message for it to live in.

## Commands

- **`/news recent game:<topic|All> days:<1-30, default 7> label:<official|reported|rumor, optional> public:<bool, default false>`**:
  recent stories for a game (or all of them), optionally filtered by label.
- **`/news search query:<text> days:<1-30, default 30> public:<bool, default false>`**:
  full-text search over story headlines and summaries.
- **`/newsbot status`** (admin): last run and its status, source health, item/story
  counts for the last 24h, and estimated Claude spend for the month.
- **`/newsbot run-now`** (admin): runs the pipeline and posts immediately. Asks
  for confirmation if today's digest already went out.
- **`/newsbot preview`** (admin): runs the pipeline and shows the digest only to
  the admin who ran it. Nothing is saved or posted, so a preview never
  changes what the next real run sees.
- **`/newsbot test-alert code:<XXXXX-XXXXX-XXXXX-XXXXX-XXXXX> golden:<bool, default false>`**
  (admin, **dev only**): posts a fake SHiFT code alert to exercise the
  sweep end to end, without waiting for a real code to show up. Only
  registered when `alerts.allow_test_command: true`; see
  [SHiFT code alerts](#shift-code-alerts) below.
- **`/shift codes days:<1-90, default 14> public:<bool, default false>`**:
  lists every SHiFT code the bot has ever seen and posted (or would have
  posted) within the window, newest first, each with a copyable code block,
  first-seen date, and source link. Only registered when `alerts.enabled:
  true`; see [SHiFT code alerts](#shift-code-alerts) below.
- **`/newsbot quote-now`** (admin): posts today's lounge quote now, and the
  scheduled one then skips today. Asks for confirmation if today's quote
  already went out. Only registered when `lounge.daily_quote.enabled: true`;
  see [Lounge](#lounge) below.

Command results default to a private (ephemeral) reply; `public:true` shows
them to the whole channel. Multi-page results get Previous/Next buttons that
only the person who ran the command can use.

## Admin-channel run reports

After every scheduled, catch-up, or `/newsbot run-now` digest that actually
posts (`ok` or `partial`), the admin channel gets a one-message plain-text
report: story counts per topic, that run's own source health, estimated
Claude spend, how long it took, and a jump link to the digest:

> ✅ **Digest posted** · Sat Sep 27 (scheduled)
> 7 stories: Borderlands 4 2 · Palworld 1 · Diablo IV 4
> Sources: 22 of 23 ok (r/diablo4: timed out)
> Claude: ~$0.02 · took 1m52s · [jump to digest](<message link>)

A `partial` run (a source skipped, or a topic fell back to a plain headline
list) swaps the emoji for ⚠️ and adds a `Notes:` line. A `failed` or
`skipped` run sends no report (those already get their own detailed admin
alert, see below), and `/newsbot preview` never reports at all, since
nothing it does is real. `run-now`'s report never says who ran it: the
privacy policy promises we don't keep user ids around.

Controlled by `digest.report_to_admin` (default `true`), and only takes
effect when `admin_channel_id` is set.

## Lounge

Optional, and off unless `config.yaml` has a `lounge:` block (v2.2). Two
things, both posted to one channel:

- **A welcome** for each new member, written by you in `config.yaml`, with
  `{member}` and `{server}` as the only placeholders. Bots are never
  welcomed, and nobody is welcomed twice in 24 hours. This needs the
  **Server Members Intent** switched on in the Developer Portal before the
  bot starts; without it the bot says so and exits after a 10-minute wait.
- **A daily quote**, fortune-cookie style, at a time you pick (default
  08:00 in `digest.timezone`). Quotes come from Wikiquote pages, a text file
  of your own, or a raw https link, mixed as you like. With no list
  configured it draws from a built-in one: Oscar Wilde, Mark Twain,
  Benjamin Franklin, William Shakespeare, Jane Austen, Edgar Allan Poe and
  Marcus Aurelius, all public-domain authors. Nothing repeats until a
  source's quotes have all been used. Modern copyrighted works are
  possible but carry real copyright risk, so they're commented-out examples
  in `config.example.yaml`, never a default.

Setup, source types, Docker paths and rollback are in
[`docs/self-host.md`](docs/self-host.md) §13; the design is
[`docs/design.md`](docs/design.md) §14.

## SHiFT code alerts

This is specifically for Borderlands-family games: it recognizes only
Gearbox's own SHiFT/Golden Key redeem code format
(`XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`) and nothing else, so it has nothing to do
if your own game doesn't use that reward system. It's opt-in for a reason
covered below (an `@everyone` ping is the one thing this bot does that's
hard to take back); `max_pings_per_day: 0` gets you codes recorded and
listable via `/shift codes` with no ping at all, a reasonable way to start
quiet.

A separate, near-real-time path alongside the daily digest (`alerts:` in
`config.yaml`, off by default): an hourly sweep runs every collector except
`web_search` (no Claude call, no `items`/`stories` writes), and the daily
09:00 run also checks its own collected items, looking for a SHiFT/Golden
Key redeem code (`XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`) in the item's full text.
A new code posts to its own dedicated channel (`alerts.channel_id`, as of
v2.0; no more sharing the digest channel) with an `@everyone` ping.

Safeguards, since a ping is the one thing this bot can do that's hard to
take back:

- **Once per code, ever.** Every code seen is recorded in `alerted_codes`;
  a code already there never alerts again, on any later sweep.
- **Silent seeding.** The first sweep after enabling the feature (or after
  losing its own "seeded" marker) records whatever codes it finds without
  posting, so months of old codes already sitting in a feed don't flood
  the channel the moment this is turned on.
- **Age limit.** A code first seen only in an item older than
  `alerts.max_item_age_hours` (default 48) is recorded but never posted.
- **Daily ping cap.** At most `alerts.max_pings_per_day` (default 3)
  messages a day carry a ping (local day, `digest.timezone`); beyond
  that, codes still post, just without `@everyone`, and an admin alert
  notes it.
- **Scoped to specific games.** `alerts.topics` (default: every topic)
  restricts the sweep to items that match those topics, the same
  confident/dedicated-source rule the digest itself uses: a Diablo IV
  patch note has never once contained a Borderlands SHiFT code.
- **Who can trigger a ping.** Every new code still posts, but only a code
  seen from a source whose trust is in `alerts.ping_trust` (default:
  `official`, `press`) is enough to make its batch carry the `@everyone`.
  A community-only code (a Reddit thread guessing at one, say) still
  posts quietly; it just isn't, on its own, the reason a ping fires. A
  batch mixing trusted and community-only codes pings once and puts the
  trusted code(s) first in the message. A batch with nothing trusted in
  it doesn't spend the daily cap either; there was nothing for the cap
  to actually stop.
- **Roundups post, but without a ping.** An item naming more than
  `alerts.max_codes_per_item` (default 5) distinct codes is a roundup or
  megathread, not a genuine single-code announcement. As of v2.0, a fresh
  code whose only sightings are roundup items still posts to the SHiFT
  channel: under a separate "SHiFT codes from a roundup" header naming
  the source, never with `@everyone`, and never spending the daily ping
  cap; capped at 50 fresh roundup codes per check; anything past that is
  recorded silently instead, with an admin note. A code that also shows up
  in a normal, non-roundup item in the same run is judged entirely by that
  normal item instead, same as before.

`/newsbot status` shows the last sweep's time and source summary, how many
codes have ever posted, and today's ping spend against the cap (plus
`(seeding)` while the marker's still unset). `/shift codes` (see
[Commands](#commands) above) lists every code the bot knows about,
including roundup ones, marked as such. `/newsbot test-alert` (dev
only, gated behind `alerts.allow_test_command`) posts one fake code
through the exact same claim/post/cap machinery a real one would use,
which is how the private test guild verifies the whole path (including the
Discord permission below) without waiting for Gearbox to hand out a code.

**Discord permission required:** the bot's role needs **View Channel**,
**Send Messages**, and **Mention @everyone, @here, and All Roles** in the
SHiFT codes channel. Without the mention permission, Discord still posts
the alert message, it just silently drops the notification; the bot
notices (it checks the permission before every ping) and sends an admin
alert instead of failing quietly. On top of that per-ping check, the bot
also checks every configured channel's permissions once at startup and
sends a single admin alert naming anything missing anywhere; see
[`docs/deploy.md`](docs/deploy.md) for how to grant it.

## Architecture

A single container runs a single Python process: one `discord.py` client
that also drives the daily job on an in-process `APScheduler` scheduler. The
pipeline (collect → normalize/dedupe → filter by topic → summarize with
Claude → store and post) is written as plain functions with no Discord
dependency, so it can run headless from the CLI or be tested without a
gateway connection at all; the bot is a thin adapter on top of it.

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
    lock.py         the one run lock, shared by the daily job and the code sweep
  shift/            SHiFT code alerts (design.md §12); separate from the digest
    match.py        pure code/Golden Key text matcher, no ReDoS surface
    decide.py       pure planner: what's new, fresh, worth a ping, worth seeding
    sweep.py        the I/O side: sweep collectors, claim/post/record, run lock
  store/
    migrations/     numbered .sql files
    db.py           connection, WAL, migration runner
    repo.py         all queries (no SQL anywhere else)
  bot/
    client.py       discord client, scheduler wiring, heartbeat, code alert poster
    commands.py     /news, /news search, /newsbot status|run-now|preview|test-alert, /shift codes
    format.py       digest + result embeds + code alerts, paging, UTF-16-aware limits
    permissions.py  startup check: does the bot have what it needs in every configured channel
  alerts.py         admin-channel notifications
  healthcheck.py    Docker HEALTHCHECK entry point (checks the heartbeat file)
```

## Local development

Requires **Python 3.14** and plain `venv` + `pip`: no `uv`, no Poetry.
`requirements.txt` is the fully pinned lock file that Docker and CI install
from; regenerate it with `scripts/lock.sh` after changing the dependency
list in `pyproject.toml`, never by hand. Dependabot (`.github/dependabot.yml`)
proposes weekly PRs bumping individual pins in `requirements.txt` directly,
which is fine; it's the same kind of edit `lock.sh` makes, just one
dependency at a time; run `scripts/lock.sh` yourself afterward if you want
to also pick up transitive-dependency updates Dependabot doesn't touch.

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
  --db /tmp/nb.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json \
  --now 2026-09-23T09:00:00+00:00
```

This runs against `config.example.yaml` rather than a `config.yaml` you may
or may not have yet, since it's the one config this repo commits and every
fixture item's topic keys (`borderlands4`, `palworld`) match; `--now` pins
the clock to the fixture data's own frozen date (2026-09-23) so this stays
deterministic no matter when you happen to run it: see `docs/self-host.md`
step 6 for why. Drop `--fixtures`/`--stub-llm`/`--now` to hit real sources
and Claude with a real config; that's how the source list and the summary
prompt get tuned in practice; see `docs/sources-research.md` for how the
seed sources here were verified.

### Running the dev bot

1. Copy `.env.example` to `.env.dev` and fill in a **dev** Discord bot token
   (a separate application from prod, never run two processes on the same
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
`env_file`) against the real `.env`/`.env.dev`; it prints every secret in
plain text to the terminal.

Any change to the summarization prompt needs an owner-reviewed
`/newsbot preview` before it merges; that's the whole point of the preview
command existing.

## Configuration

`config.yaml` (git-ignored; `config.example.yaml` is the committed template)
is mounted read-only into the container. Secrets live in `.env`, which is
also git-ignored.

Highlights of the schema: see `config.example.yaml` for a complete, real
example, and [`docs/design.md` section 3](docs/design.md#3-configuration) for the full spec:

- **`topics`**: each has a `key`, display `name`, its own `channel_id` (as
  of v2.0, every game posts to its own channel, no shared fallback),
  `aliases`, `entities` (looser, "uncertain" matches), and optional
  `search_queries` used for that topic's Brave News queries instead of the
  global templates.
- **`sources`**: `rss`, `steam_news`, `bluesky_search`, and `web_search`.
  Each has a `trust` level (`official`, `press`, or `community`), which
  affects labeling and which items survive the per-topic cap.
- **Dedicated sources**: a source whose `topics` list names exactly one
  topic is a confident match for that topic even if the item's text never
  mentions the game by name: this is how, say, a Steam patch-notes post
  titled "v0.6.2 Update" still reaches the Palworld digest.
- **`alerts.channel_id`**: required once `alerts.enabled: true`; SHiFT
  code alerts post to this dedicated channel, not a game channel or the
  admin channel.
- There is no more `digest.channel_id` or a shared `alerts.channel_id`
  fallback to it; that field was removed in v2.0 along with the combined
  digest channel. An old v1 `config.yaml` still setting it fails config
  validation with a message saying where each setting moved.
- **Secrets** (`.env`): `DISCORD_TOKEN`, `ANTHROPIC_API_KEY`, `BRAVE_API_KEY`,
  and optionally `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD` for authenticated
  Bluesky search (see Limitations below).

## Costs

Rough running cost against `config.example.yaml`'s source list:

- **Claude (Haiku 4.5)**: about 3 summarization calls per run (one per
  topic with items), roughly **2 cents per run**.
- **Brave Search**: 2 queries per topic × 3 topics = about **6 requests per
  run**, roughly **180 requests a month** (comfortably inside Brave's free
  tier as configured).
- **SHiFT code alerts**: no extra Claude or Brave cost at all: the hourly
  sweep deliberately excludes `web_search` (see
  [SHiFT code alerts](#shift-code-alerts)) and never calls the LLM. The
  only added cost is source fetches (one `GET`/sweep per RSS/Steam/
  Bluesky source, same as the digest already makes; these are requests to
  each source's own site, not to Discord's API) and, on Bluesky, about
  24 extra logins a day from rebuilding the collector fresh each sweep.

## Limitations and known issues

- **Reddit may block datacenter IPs.** The subreddit sources were verified
  from a residential connection; Reddit rate-limits aggressively even there,
  and may reject requests outright from a hosting provider's IP range in
  production. If it happens, it shows up in `/newsbot status` as a source
  health failure, not a crash.
- **YouTube channel feeds are out, for now.** They're plain RSS reads
  (`youtube.com/feeds/videos.xml?channel_id=...`) with no official
  guarantee of stability, and in late September 2026 every one of them
  started returning 404, so the example config no longer includes any.
- **Running `/newsbot run-now` a second time after today's digest already
  posted usually produces a mostly empty digest.** It re-runs the full
  pipeline; most items are already deduped against the store, and the model
  is told to mark stories that add nothing new as `relevant: false`.
- **Bluesky search needs an app password.** Unauthenticated
  `bluesky_search` requests currently get a 403 with an HTML body (not even
  a JSON error) from Bluesky's public API. Without `BLUESKY_HANDLE` and
  `BLUESKY_APP_PASSWORD` set, those sources are skipped with a coverage
  note, not treated as a failure. The three official-account RSS feeds work
  regardless: no auth needed for those.
- **SHiFT code alerts can miss or delay a code.** Reddit's `/top?t=day`
  sort can take a while to surface a brand new post, so a code posted to a
  subreddit first might not alert until it's climbed the day's top posts
  (or shown up on an official feed instead). A code embedded only in an
  image (a screenshot, a stream overlay) is invisible to this; the
  matcher only reads text. Brave News is excluded from the sweep entirely
  (see Costs above), so a code that only ever appears in a press article
  Brave indexes won't alert until the *daily* digest run's own check, if
  at all. A code split across an en dash or similar look-alike dash
  instead of a plain hyphen won't match the pattern (deliberately, see
  [`docs/design.md` §12](docs/design.md#12-shift-code-alerts-v12-approved-2026-09-25)'s clarifications on the regex). A code with no
  digits anywhere in its 25 characters won't be detected either: real
  SHiFT codes are virtually always a mix of letters and digits, and
  requiring at least one is what keeps an all-letter URL slug
  (`.../shift-codes-early-today-guide/`) or placeholder example
  (`AAAAA-BBBBB-CCCCC-DDDDD-EEEEE`) from matching as if it were a real
  code. Likewise, a code sitting directly against a `/` (a bare URL path
  segment, as opposed to a `?code=...` query value) won't match; see
  [`docs/design.md` §12](docs/design.md#12-shift-code-alerts-v12-approved-2026-09-25)'s clarifications for both rules.

## Safety notes

- Scraped text is **untrusted data**. It's sent to Claude clearly delimited
  as such, and any instructions embedded in it are ignored by prompt design.
- Every link posted to Discord comes only from the URLs actually collected
  from sources; the model can't introduce a new URL, and any URL the model
  invents that wasn't in its input is discarded before the digest is
  rendered.
- Any URL text that shows up inside a model-written headline or summary is
  stripped before rendering, so scraped or generated text can't grow a fake
  markdown link next to a real one.
- Every send is `allowed_mentions=none`: scraped text can't ping
  `@everyone`, a role, or a user, **except** a SHiFT code alert message,
  which is the one deliberate exception: it's allowed to set
  `AllowedMentions(everyone=True)`, and only when the alert pipeline
  itself (not scraped text) has decided to ping. That's a fixed module
  constant used in exactly one place, pinned by a test that scans
  `newsbot/`'s source for any other `AllowedMentions(everyone=True, ...)`
  call; see [`docs/design.md` §12](docs/design.md#12-shift-code-alerts-v12-approved-2026-09-25) and `newsbot/bot/client.py`.

## Maintainer notes

This section is about running *this* deployment (the maintainer's own
Droplet, for the maintainer's own server), not a general "how to self-host"
guide. If you're setting up your own copy, [Run your own
copy](#run-your-own-copy) above is the right starting point instead.

### Releasing

The standard path from a change to a running production bot:

1. **Feature branch, PR.** CI (`.github/workflows/ci.yml`) runs the gate
   (lint, format check, tests) on every push and PR.
2. **Merge to `main`.** CI publishes `ghcr.io/someclown/discord-newsbot:latest`
   and a `sha-<short>` tag for that specific build.
3. **Try the published image against the test guild**, with the dev bot,
   before trusting it anywhere near prod:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.staging.yml pull
   docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d
   ```
   `docker-compose.staging.yml` is the same dev token/config/database as
   `docker-compose.dev.yml` (`.env.dev`, `config.dev.yaml`, `data/dev.db`)
   but pulls the GHCR image instead of building locally; the point is to
   exercise the exact artifact that would get deployed, not a fresh local
   build of the same source. Stop `docker-compose.dev.yml` (or a local
   `python -m newsbot` against `.env.dev`) first; it's still the dev
   token, so it's still one-instance-per-token (see `CLAUDE.md`), just
   like running the dev bot any other way. It's unrelated to prod's
   token, so prod can keep running the whole time regardless.
4. **Tag a release** once the staging check looks good:
   ```bash
   git tag v1.1.0
   git push origin v1.1.0
   ```
   CI additionally publishes semver tags (`1.1.0`, `1.1`) for a tagged
   push.
5. **Deploy** with `./scripts/deploy.sh` on the Droplet: see
   [`docs/deploy.md`](docs/deploy.md). Pin `TAG=1.1.0` in the Droplet's
   `.env` for a deliberate upgrade; rolling back is changing `TAG` back
   to the previous value.

### The Droplet

See [`docs/deploy.md`](docs/deploy.md) for the Droplet runbook (install,
secrets, rollback, backups, log viewing). It's written for that specific
server, but doubles as a worked example of most of what
[`docs/self-host.md`](docs/self-host.md) describes in general terms.

## License

MIT; see [LICENSE](LICENSE). Short version: do what you like with it, keep
the notice, and don't blame me when a subreddit changes its RSS format.
