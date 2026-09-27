# Self-hosting your own copy

This is the guide for running discord-newsbot on a server you control, for
a game (or three) you pick, independent of the maintainer's own Droplet.
`docs/deploy.md` is that Droplet's own runbook and stays useful as a
worked example, but it assumes the maintainer's specific server, config,
and release habits; this document doesn't.

One thing this bot is not, and isn't trying to become: a single instance
serving many Discord servers at once. Each server that wants this bot runs
its own copy, with its own bot token, its own config, and its own database.
That's a deliberate scope limit (see `docs/design.md` §1), not a missing
feature.

## 1. Prerequisites

- **Docker Engine 20.10.10 or newer**, with the Compose plugin (`docker
  compose`, two words, not the old standalone `docker-compose` binary).
  Older Docker versions predate a `clone3`/seccomp fix that makes
  `python:3.14-slim`'s threading behave in ways that look like a Python bug
  and aren't. Check with `docker version --format '{{.Server.Version}}'`.
- **amd64 or arm64.** The published image (`ghcr.io/someclown/discord-newsbot`)
  is a multi-arch manifest covering both, so it runs unmodified on a typical
  cloud VM, a Raspberry Pi, or an Apple Silicon Mac. (Builds before this
  bot's v2.1 release were amd64-only; if you're pinning an older tag on
  arm64 hardware, you'll need to build it locally instead.)
- A Discord account with permission to create applications and invite bots
  to the server you're running this for.
- An [Anthropic API key](https://console.anthropic.com) (required; see
  Costs below).

## 2. Create the Discord application

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create a new application. This is the bot's own identity; don't reuse an
   application you're already using for something else.
2. Bot settings:
   - **Public Bot: off**, unless you actually want strangers adding this
     bot to their own servers (you almost certainly don't; see the scope
     note above).
   - No privileged intents (Message Content, Server Members, Presence).
     This bot doesn't read message content or track member lists, so it
     doesn't need any of them.
   - Copy the bot token somewhere safe. It goes into `.env` in a minute;
     never into `config.yaml`, a chat log, or a commit.
3. Turn on **Developer Mode** in your own Discord client (User Settings →
   Advanced) so you can right-click a channel and "Copy Channel ID" later.
4. Generate an invite URL (OAuth2 → URL Generator):
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels, Send Messages, Embed Links** at
     minimum, for the channels each game will post to. If you're turning on
     SHiFT code alerts (Borderlands-family games only; see below), also
     check **Mention @everyone, @here, and All Roles**.
5. Open the generated URL and invite the bot to your server.
6. **Set permissions on the bot's role, not the bot's own member
   override.** Server Settings → Roles → the bot's role is where the
   permissions from step 4 actually need to live. A per-channel or
   per-member permission override on the bot's own user silently wins over
   whatever the role says, which makes for a confusing debugging session if
   you set permissions on the role and then can't figure out why the bot
   still can't post somewhere (this is a lesson from testing, not a
   hypothetical).

## 3. Keys and costs

- **`ANTHROPIC_API_KEY`** (required): the bot summarizes each day's items
  with Claude Haiku. Rough cost against a typical few-topic config is about
  **1 to 2 cents per daily run**.
- **`BRAVE_API_KEY`** (optional): powers the `web_search` source type,
  which catches press coverage not already in your RSS list. Free tier math
  is in `docs/finding-sources.md`; without a key, `web_search` sources are
  disabled at startup with a warning, not a crash.
- **`BLUESKY_HANDLE`/`BLUESKY_APP_PASSWORD`** (optional): only needed for
  `bluesky_search` sources (searching Bluesky for mentions of your game). An
  official account's own posts are available over plain RSS with no
  password at all; see `docs/finding-sources.md`.

None of these need to be set to try the bot out with `--check-sources` and
an offline `--dry-run` (see step 6 below); you can get most of the way
through this guide before creating a single key.

## 4. `config.yaml` and `.env`

Start from the minimal example, not the full one:

```bash
cp config.minimal.yaml config.yaml
```

`config.minimal.yaml` is one topic, three sources, and SHiFT alerts left
off: enough to see a real digest without wading through the maintainer's
own fifteen-source, three-game setup first. `config.example.yaml` is that
fuller config, useful as a reference once you're adding a second or third
topic or want to see what a `web_search` block or a commented-out `alerts:`
block actually looks like. `docs/finding-sources.md` has recipes for
finding sources for your own game once you're past the placeholder ones.

Edit `config.yaml`: replace every placeholder value (marked as such in the
comments) with real ones: `guild_id`, each topic's `channel_id`, and
`admin_channel_id` if you're using one.

```bash
cp .env.example .env
```

Fill in `.env`: `DISCORD_TOKEN` from step 2, `ANTHROPIC_API_KEY` from step
3, and `BRAVE_API_KEY`/Bluesky's if you're using them. Also set
**`NEWSBOT_CONTACT`** to a URL or an email address: it goes into the
User-Agent this bot sends on every outbound request, and it's the "how do I
reach whoever's running this" line that a source's admin (Reddit's
especially) can act on instead of just blocking you. Leaving it unset
doesn't stop the bot from running, but it does log a startup warning every
time, and every request goes out identifying you as "contact unset."

Never commit `.env`, and never run `docker compose ... config` (or anything
else that resolves `env_file`) against your real `.env`: it prints every
secret in it to your terminal in plain text. If you want to sanity-check a
compose file's merged config, do it against a scratch `.env` full of dummy
values in a throwaway directory instead.

## 5. Choose an image

Two options, in order of least to most effort:

- **Pull the maintainer's published image**, pinned to a specific tag:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
  ```
  `docker-compose.prod.yml` defaults to `ghcr.io/someclown/discord-newsbot`
  at `${TAG:-latest}`. Pin `TAG` in `.env` (e.g. `TAG=2.1.0`) rather than
  floating on `latest`, the same reasoning as `docs/deploy.md`'s own
  rollback section: knowing exactly what's running, and being able to undo
  an upgrade with a one-line `.env` edit, is worth the extra step.
- **Run your own fork's CI build.** If you've forked this repo and pushed
  your own changes, `.github/workflows/ci.yml` builds and publishes an
  image to *your* fork's GHCR namespace the same way it does for the
  maintainer's. Set `GHCR_OWNER` in `.env` to your GitHub username or
  organization so `docker-compose.prod.yml`'s image reference resolves to
  your build instead of `someclown`'s.

Either way, `config.yaml` and `.env` need to exist as real files (not empty
placeholders) before the first `up`: Docker's bind-mount behavior is to
silently create an empty directory at a mount path if nothing's there yet,
and an empty directory named `config.yaml` isn't a config file. If the
container crashes immediately on first start, that's the first thing to
check.

## 6. First run: check it before you trust it

Before touching Discord at all, run the pipeline offline to confirm the
code works on your machine:

```bash
python -m newsbot.pipeline.run --dry-run --config config.example.yaml \
  --db /tmp/nb.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json \
  --now 2026-09-23T09:00:00+00:00
```

This uses canned fixture data and a stubbed Claude client: no network, no
real credentials, no Discord connection. It's mostly there to confirm your
Python environment and dependencies are set up correctly. `--now` pins the
pipeline's clock to the fixture data's own date; the fixture files'
`published_at` values are frozen at 2026-09-23, and without `--now` this
command's output quietly goes empty once "today" moves far enough past
that date for `digest.lookback_hours` (24h, by default) to age every
fixture item out. Drop `--now` (or point it at today) once you're running
this against your own `config.yaml` and real collected items instead of
the fixtures.

Once your own `config.yaml` has real sources in it, check that they
actually work, for real, before spending a Claude call or a Discord post on
finding out the hard way:

```bash
python -m newsbot.pipeline.run --config config.yaml --check-sources
```

This runs every configured collector once and prints item counts and the
first error line per source, plus which topics matched how many items.
It needs no `ANTHROPIC_API_KEY` and no Discord token: only whatever
optional keys (`BRAVE_API_KEY`, Bluesky's) the sources you've configured
actually use.

With sources looking healthy, bring the container up (`docker compose ...
up -d`, per step 5) and, in Discord:

1. **`/newsbot preview`** (admin only): runs the real pipeline against your
   real config and shows you the digest privately, without posting or
   saving anything. This is the fast way to confirm the Anthropic key, any
   optional keys, and the bot's channel permissions all actually work
   together.
2. **`/newsbot status`** (admin only): confirms source health and that the
   scheduler is running.

**The catch-up gotcha:** if you start the bot after today's digest time has
already passed and no digest has posted yet today, it runs and posts
immediately on startup, rather than waiting until tomorrow. That's by
design (a missed run shouldn't just get skipped silently), but it means
your first-ever startup, if it happens to land after `digest.time`, posts a
real digest right away instead of waiting for a schedule you can watch.

## 7. Test against a private guild first

Before pointing this at a server anyone actually uses, create a second
Discord application (a second bot token) and a private test guild, and run
through steps 2 through 6 against that instead. Two things worth keeping in
mind:

- **One process per token, always.** Running two copies of this bot on the
  same token means both receive every gateway interaction and race for it;
  the loser logs `Unknown interaction (10062)`, which is confusing to debug
  if you don't already know that's what it means. A test bot's token and a
  real bot's token are different tokens, so running both at once is
  normally fine; running the *same* token twice, from two different
  machines or two different terminal windows, is the thing to avoid.
- Once the test guild looks right, repeat the config and `.env` setup
  (step 4, and step 2's Discord application) for the real server, with its
  own token, its own channel IDs, and its own `config.yaml`.

## 8. SHiFT code alerts are Borderlands-family only

The `alerts:` block in `config.yaml` runs an hourly sweep that watches for a
SHiFT or Golden Key redeem code (Gearbox's five-groups-of-five-characters
format, `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`) and pings the server when it finds
one. This is specific to the Borderlands franchise's own redeem-code
system; it doesn't recognize any other game's codes, promo keys, or
giveaway formats, and there's no general-purpose "watch for codes in any
shape" mode. If your game doesn't use SHiFT codes, leave `alerts:` out of
`config.yaml` entirely (it's off by default either way).

It's opt-in for a reason: turning it on means the bot can `@everyone` your
whole server, which is the one thing this bot does that's hard to take
back once it's happened. Before enabling it:

- Understand what `@everyone` means in **your** server specifically: on a
  small server (a dozen people, say) it's a gentle nudge; on a large one
  it's a genuinely disruptive notification to a lot of people at once.
- Start with `max_pings_per_day: 0` if you want to see codes post quietly
  (still recorded, still visible via `/shift codes`) before deciding you
  actually want the ping. Setting it to `0` also means the bot's role
  doesn't need the "Mention @everyone" permission at all; the startup
  permission check knows this and won't flag it as missing.

`config.example.yaml`'s commented `alerts:` block and the README's "SHiFT
code alerts" section have the rest of the safeguards (once-per-code,
silent seeding, the age limit, trust-gated pings).

## 9. Privacy policy and terms of service

`site/` (published at the maintainer's own GitHub Pages URL) is the
maintainer's privacy policy and terms of service, written for the
maintainer's own deployment. **A private, self-hosted bot running for your
own server doesn't need a published policy URL at all**: Discord asks
publicly-listed or verified bots to have one, but a bot you invited to your
own server, that you run yourself, with `Public Bot` off, isn't in that
category.

If you're forking this repo and want your own policy page anyway (or you
are planning to make your fork more widely available), `site/` is a
reasonable starting point: copy it, edit the content to describe what your
own fork actually collects and does (don't just leave the maintainer's
wording describing a different deployment), and enable GitHub Pages for
your fork's repo settings. Whatever it says, keep it honest about what your
own config and any code changes actually do; a fork that adds a new kind of
logging or a new integration needs its own policy update, the same way the
maintainer's `CLAUDE.md` calls out updating theirs when the bot starts
storing anything new.

## 10. Backups

`scripts/backup.sh` takes an online-safe snapshot of the SQLite database
with `sqlite3 .backup` and keeps the 7 newest, deleting older ones:

```bash
./scripts/backup.sh data/newsbot.db data/backups
```

Two ways to run it on a schedule:

- **A generic cron job**, if your server doesn't need the DST-aware
  scheduling `docs/deploy.md`'s systemd timer provides: any daily cron
  entry that runs the command above works, adjusted for whatever backup
  directory and database path you're actually using.
- **The systemd units in `deploy/systemd/`**, the same ones
  `docs/deploy.md` §9 installs, if your server runs systemd and you want
  the timer to track a specific wall-clock time correctly across DST. They
  assume the maintainer's own path (`/opt/newsbot`) and time
  (08:30 America/Los_Angeles, before that Droplet's 09:00 digest); edit
  `WorkingDirectory`/`ExecStart` in `newsbot-backup.service` and
  `OnCalendar` in `newsbot-backup.timer` to match your own install path and
  digest time before installing them.

Either way, the container's data directory (wherever you've mounted `/data`
to) is owned by uid 10001 (the container's fixed non-root user); the backup
script needs read access to it, which usually means running the backup as
root or adding your own user to a group that can read that directory. See
`docs/deploy.md` §2 for the exact `chown` commands if you want to run the
backup without `sudo`.

## 11. Updating and rollback

Every published image is tagged `latest` (the newest build off `main`),
`sha-<short>` (every build), and, for tagged releases, semver
(`2.1.0`, `2.1`). Pin `TAG` in `.env` to a specific release rather than
floating on `latest`, so both "what's running" and "how do I undo this" are
one-line answers:

```bash
# edit .env: TAG=2.1.0 -> TAG=2.0.0 (or whatever the last known-good tag was)
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

A rollback undoes the running code, not anything already written to the
database; if bad data (not bad code) is the problem, restore from a backup
instead (`docs/deploy.md` §10 has the restore procedure, database path
aside). Check the version you're rolling back past for a breaking config
change first: v2.0.0, for example, needs a v1-shaped `config.yaml` restored
alongside the image tag, not just the tag by itself. `docs/deploy.md` §8
and §17 have the specifics for that particular upgrade if you're coming
from an older version.

## 12. One copy per server

If you want this bot running for more than one Discord server, run more
than one copy: its own Discord application, its own token, its own
`config.yaml`, its own database. There's no shared multi-tenant mode, and
building one is a meaningfully bigger project than pointing this bot at a
new game (see the to-do list in `CLAUDE.md` for the actual scope
difference). Two copies on the same host is fine as long as each one has
its own token, its own config file, and its own data directory; just don't
let two containers (or a container and a bare `python -m newsbot` process)
ever share a token, for the same reason two processes on the same token
race each other within a single deployment.
