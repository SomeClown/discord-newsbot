# Self-hosting your own copy

This is the guide for running discord-newsbot on a server you control, for
a game (or three) you pick, independent of the maintainer's own Droplet.
[`docs/deploy.md`](deploy.md) is that Droplet's own runbook and stays useful as a
worked example, but it assumes the maintainer's specific server, config,
and release habits; this document doesn't.

Since v3.0 the bot is built to serve many servers from one process: the
maintainer runs one public app that any server can install. You don't have to
use it that way. A copy of your own, with its own token, config and database,
serving your own server (or a few friends'), is still the supported way to
self-host, it keeps working the way it always did, and nothing about the public
app makes you share anything with it. What changed is how it's set up: your
config file now holds the bot's global settings and a catalog of games, and
everything that belongs to one server (its channels, digest time, time zone,
SHiFT alerts) is set from Discord with `/newsbot setup` and kept in the
database. §14 covers upgrading a v2 copy, which needs no edits, and starting a
fresh one.

**This guide describes v3.0.0 and later,** with notes where an older version
differs. `--check-sources`, `NEWSBOT_CONTACT`, and multi-arch (amd64 + arm64)
published images arrived at v2.1.0; the catalog config, `/newsbot setup` and
the rest of the per-server commands arrive at v3.0.0. The examples
say `TAG=3.0.0`, but check the
[Releases page](https://github.com/someclown/discord-newsbot/releases)
and pin whatever the latest tag actually is; this guide won't be updated
for every patch release, and I'd rather admit that now than have you
find out.

## 1. Prerequisites

- **Docker Engine 20.10.10 or newer**, with the Compose plugin (`docker
  compose`, two words, not the old standalone `docker-compose` binary).
  Older Docker versions predate a `clone3`/seccomp fix that makes
  `python:3.14-slim`'s threading behave in ways that look like a Python bug
  and aren't. Check with `docker version --format '{{.Server.Version}}'`.
- **amd64 or arm64.** From the v2.1.0 tag on, the published image
  (`ghcr.io/someclown/discord-newsbot`) is a multi-arch manifest covering
  both, so it runs unmodified on a typical cloud VM, a Raspberry Pi, or an
  Apple Silicon Mac. Builds before v2.1.0 are amd64-only; on arm64
  hardware, pinning one of those older tags means building locally instead
  (§5 below).
- **Python 3.14, if you want to run the CLI locally** (`--check-sources`,
  the offline `--dry-run`) instead of only through Docker. Entirely
  optional: §6 below has a Docker-only path for both. Setup, the same as
  the README's own "Local development" section:
  ```bash
  python3.14 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  pip install -e . --no-deps
  ```
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
     bot to their own servers (you almost certainly don't: with it off, only
     you can invite it, and you stay the only one with a stake in what it
     posts). With Public Bot off, the portal may refuse to save
     unless Installation → Install Link is also set to "None": a
     leftover install-link value from a template or an earlier edit is
     the usual cause; if the portal won't save your settings, check that
     first.
   - No privileged intents, unless you turn on lounge welcomes (§13). Then
     enable **Server Members Intent** here *before* starting the bot. If
     it's off, the bot prints a message naming the switch, waits 10
     minutes, and exits; the wait keeps a restart loop from hammering
     Discord's login limit. Message Content and Presence stay off either
     way.
   - Copy the bot token somewhere safe. It goes into `.env` in a minute;
     never into `config.yaml`, a chat log, or a commit. **The portal shows
     it exactly once, at creation**; if you navigate away without copying
     it (or just aren't sure you got it right), "Reset Token" issues a new
     one; there's no way to reveal the original again.
3. Turn on **Developer Mode** in your own Discord client (User Settings →
   Advanced) so you can right-click a server or a channel and copy its ID
   later: right-click the server's icon in the sidebar for "Copy Server
   ID" (your server's ID), or a channel's name for "Copy Channel ID".
4. Generate an invite URL (OAuth2 → URL Generator):
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels, Send Messages, Embed Links** at
     minimum, for the channels each game will post to. If you're turning on
     SHiFT code alerts (Borderlands-family games only; see below), also
     check **Mention @everyone, @here, and All Roles**.
5. Open the generated URL and invite the bot to your server.
6. **If any of the bot's channels are private** (not visible to
   `@everyone`), add the bot's role to that channel's own member/role list
   too; being invited to the server doesn't give a bot visibility into a
   channel it isn't otherwise allowed into, same as any other role.
7. **Set permissions on the bot's managed role, not a one-off override.**
   Server Settings → Roles → the bot's own role (created automatically
   when you invited it) is where the permissions from step 4 actually need
   to live. Two places can override that role's settings, and either one
   silently wins over it:
   - A **channel-level permission override on the bot's own user**
     (as opposed to its role) in that channel's Permissions tab.
   - A channel-level override on the bot's *role* that's narrower than
     the role's server-wide settings.
   Either kind of override beats the role's own server-wide permissions
   for that one channel, which makes for a confusing debugging session if
   you set permissions on the role and then can't figure out why the bot
   still can't post somewhere (this is a lesson from testing, not a
   hypothetical); check a misbehaving channel's own Permissions tab for
   an override before assuming the role itself is wrong.

The server ID from step 3 goes in `config.yaml` three times, and they mean
different things: `home_guild_id` (your server, where the owner-only
`/owner servers` command lives and where the bot's own alerts go),
`command_guild_ids` (so the slash commands appear instantly), and
`comped_guild_ids` (so your server gets AI summaries; §4 explains each).
`admin_channel_id`, if you set one, is the bot's *own* alert channel, in your
home server: a source that has failed for half a day, a crashed job, a
daily summary of how the bot is doing. It has to be a channel in
`home_guild_id`. Your server's own problems and run reports (the
one-message summary after each digest, a channel the bot can't post in) go to
a different, per-server setting, `/newsbot settings admin_channel:`. They can
be the same channel on a one-server copy. Make it private, since it's meant
for you, not the whole server. `admin_permission` (default `manage_guild`)
names the `discord.Permissions` flag a member needs to run the admin-only
commands (`/newsbot setup`, `follow`, `settings`, `shift`, `status`,
`preview`, `run-now`); the default is a reasonable one for "whoever
administers this server," but any real Discord permission name works if you
want it narrower or broader.

## 3. Keys and costs

- **`ANTHROPIC_API_KEY`** (required to start): the bot summarizes each day's
  items with Claude Haiku, but **only for comped servers**, the ones listed in
  `comped_guild_ids`. Any other server gets the free tier, a plain headline
  list with no AI in it. On a copy that serves your own server, list your own
  server there (§4), or you'll be reading the headline version of your own bot.
  The Anthropic console requires prepaid credits before
  a key can make any calls at all (there's no pay-later or free tier for
  the API); rough cost for a comped server following a few games and one
  digest a day is about **1 to 2 cents per daily run**, so roughly
  **$0.30 to $0.60 a month** (a handful of dollars of prepaid credit
  covers a long time). Summaries are made once per game and reused, so a
  second comped server on the same schedule costs next to nothing extra.
- **`BRAVE_API_KEY`** (optional): powers the `web_search:` block, which
  catches press coverage not already in your RSS list. It runs once a day,
  for games a comped server follows. Free tier math
  is in [`docs/finding-sources.md`](finding-sources.md); without a key, `web_search` sources are
  disabled at startup with a warning, not a crash. Brave's signup has
  asked for a card on file for its free tier before; check their current
  pricing page, since this is the kind of detail that changes without
  this doc necessarily catching up.
- **`BLUESKY_HANDLE`/`BLUESKY_APP_PASSWORD`** (optional): only needed for
  `bluesky_search` sources (searching Bluesky for mentions of your game). An
  official account's own posts are available over plain RSS with no
  password at all; see [`docs/finding-sources.md`](finding-sources.md).

None of these need to be set to try the bot out with `--check-sources` and
an offline `--dry-run` (see step 6 below); you can get most of the way
through this guide before creating a single key.

## 4. `config.yaml` and `.env`

Start from the minimal example, not the full one:

```bash
cp config.minimal.yaml config.yaml
```

`config.minimal.yaml` is one game, a couple of sources, and nothing else:
enough to see a real digest without wading through the maintainer's own
fifteen-game catalog first. `config.example.yaml` is that fuller config,
useful as a reference once you're adding a second game or want to see
what a `shared_sources:` list, a `web_search:` block or the `shift:` settings
actually look like. [`docs/finding-sources.md`](finding-sources.md) has recipes for
finding sources for your own game once you're past the placeholder ones.

The file has three jobs:

- **Who you are:** `home_guild_id` (your server), `admin_channel_id` (a channel
  in it for the bot's own alerts), and `admin_permission`.
- **How the commands and tiers are wired:** `command_guild_ids` and
  `comped_guild_ids`. For a copy serving one server, put your server's ID in
  both. `command_guild_ids` copies the slash commands into just those servers,
  where they show up instantly; left empty, they're registered globally, which
  is right for a public bot and means waiting up to an hour for any change.
  `comped_guild_ids` is what turns on AI summaries and web search for a server;
  without it, your server gets plain headlines. Nothing is ever downgraded
  automatically, so removing an ID later doesn't strip a server that already
  has the tier.
- **What the bot knows about:** the `catalog:` of games, each with its own
  sources, and `shared_sources:` for wide feeds that cover many games.

Edit `config.yaml`: replace every placeholder value (marked as such in the
comments) with real ones. There are no channel IDs in it. Which channel each
game posts to, and at what time, is set from Discord once the bot is running
(§6, "Set it up from Discord").

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
values in a throwaway directory instead. If your shell has `autoenv`,
`direnv`, or anything else that auto-loads `.env` files on `cd`, be aware
it'll print those same secrets to your terminal (or your shell's history)
the moment it sources this one, the same as `docker compose ... config`
would.

### Running the CLI locally, without Docker

`python -m newsbot.pipeline.run` (`--check-sources`, the offline
`--dry-run`) reads secrets from **the process environment**, not from
`.env`: nothing in this codebase parses `.env` itself; Docker Compose's
`env_file:` is what turns `.env` into environment variables, and that
machinery doesn't exist outside a container. Export only what the command
you're running actually needs, rather than sourcing the whole file:

```bash
export BRAVE_API_KEY=... NEWSBOT_CONTACT=...
python -m newsbot.pipeline.run --config config.yaml --check-sources
```

**Don't `set -a && . ./.env && set +a` this particular file to get there.**
`.env`'s `NEWSBOT_CONFIG` and `NEWSBOT_DB` are container *paths*
(`/app/config.yaml`, `/data/newsbot.db`) that only exist inside the
container's own filesystem; sourcing the whole file points a local CLI run
at paths that don't exist on your host, which is a confusing way to
discover this. `.env.example`'s own comments say the same thing: leave
those two as-is for Docker, and pass `--config`/`--db` (or set them
locally to a real host path) for a local run instead.

## 5. Choose an image

Three options, in order of least to most effort:

- **Pull the maintainer's published image**, pinned to a specific tag:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
  ```
  `docker-compose.prod.yml` defaults to `ghcr.io/someclown/discord-newsbot`
  at `${TAG:-latest}`. Pin `TAG` in `.env` (e.g. `TAG=2.1.0`, or whatever the
  latest release is; see the version note near the top of this document) rather than
  floating on `latest`, the same reasoning as [`docs/deploy.md`](deploy.md)'s own
  rollback section: knowing exactly what's running, and being able to undo
  an upgrade with a one-line `.env` edit, is worth the extra step.
- **Run your own fork's CI build.** If you've forked this repo and pushed
  your own changes, `.github/workflows/ci.yml` builds and publishes an
  image to *your* fork's GHCR namespace the same way it does for the
  maintainer's, once you enable it: **GitHub Actions is disabled by
  default on a forked repo**, so turn it on under the fork's own Actions
  tab first, or nothing builds. **GHCR packages default to private**, so
  either make yours public (package Settings → Change visibility) or
  `docker login ghcr.io` on whatever machine is going to `pull` it. Set
  `GHCR_OWNER` in `.env` to your GitHub username or organization,
  **lowercase** (GHCR image references are case-sensitive and reject
  uppercase), so `docker-compose.prod.yml`'s image reference resolves to
  your build instead of `someclown`'s.
- **Build the image locally**, no GHCR involved at all: the most effort,
  but the only option before a multi-arch tag exists for your architecture
  (see the version note near the top) or if you'd rather not publish an
  image anywhere.
  ```bash
  docker build -t newsbot:local .
  ```
  Point `docker-compose.prod.yml` at it instead of GHCR by overriding the
  image in a small compose override file of your own (don't edit
  `docker-compose.prod.yml` itself; that's tracked):
  ```yaml
  # docker-compose.local.yml
  services:
    newsbot:
      image: newsbot:local
  ```
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml up -d
  ```
  `docker-compose.dev.yml` looks similar but isn't a shortcut for this: it's
  wired to the maintainer's own `.env.dev`/`config.dev.yaml`, for local
  development against the dev bot (see README's "Running the dev bot"),
  not a general "build and run locally" path.

Whichever option you pick, once the container's up:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
# Ctrl-C to stop following; the container keeps running.
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
# stops and removes the container; ./data (the database) is untouched.
```

Either way, `config.yaml` and `.env` need to exist as real files (not empty
placeholders) before the first `up`: Docker's bind-mount behavior is to
silently create an empty directory at a mount path if nothing's there yet,
and an empty directory named `config.yaml` isn't a config file. If the
container crashes immediately on first start, that's the first thing to
check.

**Before that first `up`, also create and hand over `./data`:**

```bash
mkdir -p data && sudo chown 10001:10001 data
```

The container runs as a fixed uid (10001, not "the next free uid," so this
keeps working across rebuilds) and writes the SQLite database there. On
Docker for Linux, a bind-mounted directory that doesn't exist yet gets
created as root the moment `up` mounts it, which the container's own
uid then can't write to; creating it yourself and handing it over first
avoids that particular failure-on-first-write.

## 6. First run: check it before you trust it

Before touching Discord at all, run the pipeline offline to confirm the
code works on your machine:

```bash
python -m newsbot.pipeline.run --dry-run --config tests/fixtures/config_v2_example.yaml \
  --db /tmp/nb.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json \
  --now 2026-09-23T09:00:00+00:00
```

This uses canned fixture data and a stubbed Claude client: no network, no
real credentials, no Discord connection. It's mostly there to confirm your
Python environment and dependencies are set up correctly. It points at
`tests/fixtures/config_v2_example.yaml`, the pre-v3 single-server example,
and not at `config.example.yaml`, because a dry run previews *a server's*
digest, and the v3 example has no server in it (servers are created from
Discord, §6 below). The old-shape file does, so the bot imports it into the
scratch database first. `--now` pins the
pipeline's clock to the fixture data's own date; the fixture files'
`published_at` values are frozen at 2026-09-23, and without `--now` this
command's output quietly goes empty once "today" moves far enough past
that date for `collection.lookback_hours` (24h, by default) to age every
fixture item out. Drop `--now` (or point it at today) once you're running
this against your own `config.yaml` and real collected items instead of
the fixtures.

Once your own `config.yaml` has real sources in it, check that they
actually work, for real, before spending a Claude call or a Discord post on
finding out the hard way:

```bash
python -m newsbot.pipeline.run --config config.yaml --check-sources
```

This runs every configured collector once (the catalog's, then the shared
sources) and prints item counts and the first error line per source, grouped
by game, plus which games matched how many items. To check just one game's
sources (plus the shared feeds, matched only against it), add
`--game yourgame`; it repeats, for several. It needs no `ANTHROPIC_API_KEY` and
no Discord token: only whatever optional keys (`BRAVE_API_KEY`, Bluesky's) the
sources you've configured actually use. Two things worth knowing about
reading its output:

- **The exit code is always 0**, whether every source came back clean or
  every one of them failed; a source that timed out or 404'd is exactly
  the kind of thing this command exists to tell you about, not a reason
  to make the command itself look like it failed. Read the `Summary:`
  line at the bottom rather than checking `$?`.
- **"Games matched" applies `collection.lookback_hours`** the same way a real
  collection pass would: an item older than that window (24h by default)
  doesn't count toward a game's total here either, even if the source that
  returned it is otherwise healthy. A source reporting items but a game
  still reading 0 is worth checking against that window before assuming
  the alias/entity matching is wrong.

No local Python install needed either, if you'd rather run it through the
image you already have:

```bash
docker run --rm -v "$PWD/config.yaml:/app/config.yaml:ro" \
  ghcr.io/someclown/discord-newsbot:latest \
  python -m newsbot.pipeline.run --config /app/config.yaml --check-sources
```

(swap in your own tag or `newsbot:local` if you built locally, per step 5).
The same trick validates `config.yaml` on its own, with no collector runs
at all, using `load_config` directly:

```bash
docker run --rm -v "$PWD/config.yaml:/app/config.yaml:ro" \
  ghcr.io/someclown/discord-newsbot:latest \
  python -c "from newsbot.config import load_config; load_config('/app/config.yaml')"
```

A `ConfigError` here lists every problem it found at once, the same as it
would on a real startup crash; no output at all means it loaded fine.

**The "it posts right away" gotcha.** A server's digest is due once its local
digest time has passed and there's no digest for that local day yet; the bot
checks every minute and doesn't care whether it just started. So if you
finish `/newsbot setup` (below) after today's digest time has passed, the
first digest posts within a minute, rather than waiting for tomorrow. That's
by design (a missed run shouldn't just get skipped silently, and there is no
separate catch-up step: it's the same check), but it means a first-ever setup
that lands after the time you picked posts a real digest right away. To avoid
that, pick a time later today, or just expect it. `/newsbot preview` (below)
gets you the same "does this actually work" confirmation without posting
anything.

With sources looking healthy and `./data` set up (step 5), bring the
container up (`docker compose ... up -d`, per step 5). Then, in Discord:

**Set it up from Discord.** The bot creates your server's record when it joins
(or, for a bot already in the server, at its first start) and posts nothing
until you set it up. As an admin (Manage Server, unless you changed
`admin_permission`):

1. **`/newsbot setup`:** a short guided form, visible only to you. Pick a time
   zone, a digest time (on the hour; use `/newsbot settings` for minutes),
   the games to follow (up to 10, from your catalog), and the channel they
   post in, then Save. It checks the bot's permissions in that channel and
   says what's missing. Run it again any time to change things; it starts from
   the current values. The default zone is UTC until you pick one.
2. **`/newsbot follow` and `unfollow`** add or drop one game, so each game can
   have its own channel (`/newsbot follow game: channel:`). `/newsbot games`
   lists what you follow and the rest of the catalog.
3. **`/newsbot settings`** changes the time (`HH:MM`), the time zone (any
   IANA zone, with autocomplete) and this server's own admin channel, where
   run reports and problems go.
4. **`/newsbot shift`** turns on SHiFT code alerts (§8) for this server.
5. **`/newsbot preview`** (admin only): builds the digest your server would get
   next, from what's already stored, and shows it only to you, without
   posting or saving anything. It's the fast way to confirm the Anthropic key,
   any optional keys, and the bot's channel permissions all work together.
   It's limited to once per server per 10 minutes (a preview of a comped
   server is a Claude call), and nothing new since the last one reuses its
   summary instead of asking Claude again.
6. **`/newsbot status`** (admin only): your server's tier, digest time and
   next due time, last digest with jump links, the games you follow with "N of
   M sources ok", SHiFT settings, and the newest problem notes.
7. **`/newsbot run-now`** posts the digest immediately; it asks first if
   today's already went out, and a confirmed re-run is also limited to once
   per 10 minutes. (The bot's owner, you, is never held to either limit.)

The first collection pass runs about 30 seconds after the bot is ready, and
then once an hour; a freshly started copy has nothing stored until it
finishes, so a preview in the first minute can honestly say nothing would
post.

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
- Put the test guild's ID in `command_guild_ids` (and, to test AI summaries,
  `comped_guild_ids`): commands then show up instantly in that guild instead
  of taking up to an hour.
- Once the test guild looks right, repeat the config and `.env` setup
  (step 4, and step 2's Discord application) for the real server, with its
  own token, its own server ID, and its own `config.yaml`; its channels and
  times you set again from Discord, since they live in each copy's database.

## 8. SHiFT code alerts are Borderlands-family only

The bot watches for a SHiFT or Golden Key redeem code (Gearbox's
five-groups-of-five-characters format, `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`) in
every source of the games listed under `shift: games:` in `config.yaml`
(default: `borderlands4`), once per collection pass. A server that has turned
alerts on with `/newsbot shift` hears about each new code in its own channel.
This is specific to the Borderlands franchise's own redeem-code system; it
doesn't recognize any other game's codes, promo keys, or giveaway formats,
and there's no general-purpose "watch for codes in any shape" mode. If your
game doesn't use SHiFT codes, leave `shift:` out (it does nothing without
Borderlands 4 in the catalog) and never turn `/newsbot shift` on.

Alerts are off for every server until an admin turns them on, and the default
ping is **none**. The admin picks the channel and who gets pinged:
`/newsbot shift channel:#codes enabled:true ping:none|everyone|role`
(`role:` names the role). Turning it on starts from codes found after that
moment; there's no backlog. A few things to know before choosing a ping:

- Understand what `@everyone` means in **your** server specifically: on a
  small server (a dozen people, say) it's a gentle nudge; on a large one
  it's a genuinely disruptive notification to a lot of people at once. It's
  the one thing this bot does that's hard to take back once it's happened.
- A ping needs the bot's role to have **Mention @everyone, @here, and All
  Roles** in that channel (for a role ping, only if the role isn't
  mentionable). The command's reply tells you if it's missing, and without
  it Discord still posts the alert and silently drops the notification.
- `shift: max_pings_per_day` (default 3) is **per server, per that server's own
  day**. `0` means codes post but never ping, a reasonable way to start quiet;
  `/shift codes` lists every code the bot has seen either way.
- A code seen only by a community source (a Reddit thread, a Bluesky search)
  posts without a ping. If a second independent source, or an official or
  press one, sees it within 24 hours, each server with pinging on gets one
  short follow-up, "Confirmed by a second source", with its chosen ping,
  which spends one of that day's pings. Roundups of many codes post but never
  ping.

`config.example.yaml`'s `shift:` block and the README's "SHiFT code alerts"
section have the rest of the safeguards (once-per-code, silent seeding, the
age limit, trust-gated pings).

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
with `sqlite3 .backup` and keeps the 7 newest, deleting older ones. It
runs on the host, not in the container, so it needs the `sqlite3` CLI
installed on whatever machine runs it (`sudo apt install -y sqlite3` on
Debian/Ubuntu, `dnf install sqlite`/`apk add sqlite` elsewhere); the image
itself already has SQLite built in, but that's the container's own copy,
not something `backup.sh` running on the host can reach.

```bash
./scripts/backup.sh data/newsbot.db data/backups
```

Two ways to run it on a schedule:

- **A generic cron job**, if your server doesn't need the DST-aware
  scheduling [`docs/deploy.md`](deploy.md)'s systemd timer provides: any daily cron
  entry that runs the command above works, adjusted for whatever backup
  directory and database path you're actually using.
- **The systemd units in `deploy/systemd/`**, the same ones
  [`docs/deploy.md` §9](deploy.md#9-backups) installs, if your server runs systemd and you want
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
[`docs/deploy.md` §2](deploy.md#2-directory-layout) for the exact `chown` commands if you want to run the
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
instead ([`docs/deploy.md` §9, "Restore from backup"](deploy.md#restore-from-backup)
has the restore procedure, database path aside). Check the version you're
rolling back past for a breaking config change first: v2.0.0, for example,
needs a v1-shaped `config.yaml` restored alongside the image tag, not just
the tag by itself. [`docs/deploy.md` §8](deploy.md#8-rollback) and
[§17](deploy.md#17-upgrading-to-v20) have the specifics for that particular
upgrade if you're coming from an older version. v3.0.0 is a breaking change
in the shape of the config, but a v2 config keeps loading (§14 below), and
rolling a copy back to 2.2.0 just works as long as you haven't deleted the old
keys from `config.yaml`; [`docs/deploy.md` §19](deploy.md#19-upgrading-to-v30-the-public-app)
has the whole rollback, including the thin first digest afterward.

## 12. One bot for several servers, or one copy each

Two ways to cover more than one Discord server, and either is fine.

- **One bot, several servers.** Since v3.0 a single bot serves any number of
  servers: invite it to each (Public Bot must be on if the servers aren't all
  yours to invite from), and each server's admins run `/newsbot setup` for
  their own games, channels, time and time zone. The catalog and the
  hourly collection are shared, so a source is fetched once however many
  servers follow its game. A server's settings are its own and are deleted
  when the bot is removed. Servers are on the free tier (headlines and SHiFT
  alerts) unless you list them in `comped_guild_ids`, where they get AI
  summaries on your keys. This is what the maintainer's own public bot is;
  `docs/design.md` §15 has the design, and be aware you become the person
  strangers write to when it breaks.
- **One copy per server.** Its own Discord application, its own token, its
  own `config.yaml`, its own database. Nothing is shared and nothing can
  bleed between servers, and it's the simpler mental model if the servers
  aren't yours. Two copies on the same host is fine as long as each one has
  its own token, its own config file, and its own data directory; just don't
  let two containers (or a container and a bare `python -m newsbot` process)
  ever share a token, for the same reason two processes on the same token
  race each other within a single deployment.

Limits worth knowing about either way: a server follows at most 10 games, a
catalog holds at most 25, and the lounge (§13) works for one imported server
only.

## 13. Lounge welcomes and a daily quote

Two optional features that share one channel: a welcome the bot posts for
each new member, and one quote a day, fortune-cookie style. Both are off
until you add a `lounge:` block to `config.yaml`. The block, with every
option explained, is at the end of `config.example.yaml`, and the design is
in [`docs/design.md` §14](design.md#14-lounge-bot-welcomes-and-a-daily-quote-approved-2026-09-29-quote-sources-revised-the-same-day).
This needs v2.2.0 or later.

**Since v3.0 the lounge is a feature of one imported server.** It has no
setup command: the `lounge:` block is read by the one-time import (§14 below)
and written into the database, and while the block stays in `config.yaml` it's
re-applied to that server's record at every start. So a copy that wants a
lounge keeps the old single-server keys (`guild_id`, `digest:`, `topics:` with a
channel each, and `lounge:`) in its config, as in the "v2 keys" block at the
end of `config.example.yaml`. Servers added later don't get a lounge.

**1. The portal step, if you want welcomes.** In the Developer Portal, open
your application's Bot page and switch on **Server Members Intent** (under
Privileged Gateway Intents) *before* you start the bot. Without it the bot
prints a message and exits after a 10-minute wait. The quote alone needs no
privileged intent. Message Content and Presence stay off.

**2. Channel permissions.** The bot's role needs **View Channel** and
**Send Messages** in the lounge channel. Nothing else: both posts are plain
text. The startup permission check adds the channel when either feature is
on and tells the admin channel what's missing.

**3. Config.** Uncomment the block from `config.example.yaml` and fill in
`lounge.channel_id`. Welcome messages support `{member}` (a mention of the
new member) and `{server}` (the server's name), and nothing else in braces;
the message has to fit Discord's 2,000-character limit, and both problems
stop the bot at startup with a message instead of showing up later as a
strange post. Bots are never welcomed, and nobody is welcomed twice within
24 hours (the bot remembers that in memory only, so a restart forgets it).
A server that uses rules screening or Onboarding gets its welcome when the
member gets through it; a standard server gets it on join.

**4. Where quotes come from.** `daily_quote.sources` is a list, and each
entry is exactly one of:

- `wikiquote: "Oscar Wilde"`: a Wikiquote page (an author, a work, or a
  theme).
- `file: /data/quotes.txt`: your own text file, quotes separated by a line
  holding only `%` (the `fortune` format). Re-read every time, so edits need
  no restart. UTF-8, 1 MB at most. A relative path is relative to the
  config file's directory. **In Docker, don't use a relative path:** the
  image is read-only, so put the file in the data directory and refer to it
  as `/data/...`, readable by uid 10001 (the same ownership as the rest of
  `./data`, §5).
- `url: https://...`: a raw text file at an https address, in the same `%`
  format. Plain `http://` is refused, and so is an address that redirects to
  one (redirects are followed by hand, at most 5). It must be served as
  `text/plain`; the link to a gist's or GitHub's web page is HTML, and the
  bot says so rather than posting a page of markup. It's fetched every time
  it's picked, with a 1 MB cap and a 10-second limit.

An empty list is a config error; to switch the quote off, set
`daily_quote.enabled: false`. Each day the bot picks a source at random,
falls back through the others if it fails, and then picks a quote that
source hasn't used yet. Every source has its own no-repeat deck, and a
reshuffle never repeats yesterday's quote. Changing a source's identity (its
title, path or address) starts a new deck for it; reordering the list
doesn't.

**5. The default list.** Leave `sources` out and the bot uses its built-in
list: Wikiquote pages for Oscar Wilde, Mark Twain, Benjamin Franklin,
William Shakespeare, Jane Austen, Edgar Allan Poe and Marcus Aurelius. These
are authors whose works are in the U.S. public domain (published 1930 or
earlier, as of 2026). A live check on 2026-09-29 found about 1,040 usable
quotes across them. "Public domain" is the author's, not always the page's:
a page can quote something published after they died, or a modern
translation of an old original, which is copyrighted even when the original
isn't. I'd call the list low-risk, not risk-free.

**6. A copyright caution for modern works.** Wikiquote hosts limited
excerpts of copyrighted works (films, recent books) under its own fair-use
policy. That policy covers Wikiquote. A bot that reposts them every morning
is a different use, and it carries real copyright risk. The example config
shows a few, such as `wikiquote: "Fight Club (film)"`, commented out on
purpose. Turning them on is your decision to make, not a default. Theme
pages like "Friendship" mix public-domain and modern quotes, so the same
caution applies. I'm not a lawyer, and this is not legal advice.

**7. How Wikiquote is used.**
- Pages are fetched through Wikiquote's public MediaWiki API, at most once a
  week per page, with the saved copy used in between. A page that fails is
  retried weekly, and its saved copy covers the gap. Set `NEWSBOT_CONTACT`
  (§4): it goes into the User-Agent, which Wikimedia's policy asks for.
- Saved copies live in `/data/<database name>-lounge-cache/` (for the
  default `newsbot.db`, `/data/newsbot-lounge-cache/`). They are not backed
  up, and deleting the directory is safe; the next run fetches again.
- Quotes over 400 characters are dropped. Sections such as "Disputed",
  "Misattributed", "Quotes about" and "External links" are skipped, since
  those quotes are known to be wrong or aren't by the subject.
- Pages whose title ends in a parenthetical mentioning film, TV, series or
  video game are treated as works and attributed "Character, Title". On
  theme pages, a quote with no cited source is dropped.
  When Wikiquote shows a quote in its original language (in italics) with an
  English translation under it, the translation is what gets posted.

**8. How a quote looks.** A header (`🥃 **Today's pour**`), the quote, and an
attribution line starting with `~ `. Wikiquote quotes add a `From
Wikiquote:` link to the page. Wikiquote's CC BY-SA license asks for that
link, so it stays. Attributions come only from the page or your file, never
from a model's memory. Quote text is posted inert: it can't ping anyone.

**9. Trying it.** `/lounge quote-now` (admin only; it exists in a server whose
lounge has the quote on; before v3 it was `/newsbot quote-now`) posts a quote
now, and the scheduled run then skips today. If today's quote already posted, it asks before posting another.
Every posted quote counts as used. A scheduled quote missed because the bot
was down is skipped; there is no catch-up.

**The clock.** `daily_quote.time` is `HH:MM` in that server's time zone
(`digest.timezone` in the old keys, and the zone `/newsbot settings` shows
afterward), default 08:00. A known issue in the scheduling library (APScheduler 3.x): a job set
between 00:00 and 00:59 is skipped on the day after the spring-forward
clock change. The default 08:00 isn't affected.

**10. Switching off Discord's built-in welcome.** Only after the bot's
welcome works. In Server Settings, System Messages, turn off "Send a random
welcome message when someone joins". Doing it in this order means there's never a moment with no welcome, and the
worst case for a while is two.

**When something goes wrong.** Members only ever see a welcome or a quote;
problems go to the admin channel. If every source fails and none has a saved
copy, that day is skipped with one admin message saying why. If a source
fell back to its saved copy, the quote still posts and the admin channel
gets one line. Admin messages never show URL credentials, query strings or
fragments. A failed post isn't retried; the admin message carries only the
error type and Discord's HTTP status and code, and the log has the rest.
Welcome failures log only the error type and status, nothing about the
member.

**Rollback.** Set `TAG` back to a release before 2.2.0 (say 2.1.1) as in §11
and switch Discord's built-in welcome back on. The database change is
additive, so no restore is needed. The `lounge:` block can stay in
`config.yaml` (older versions ignore it), and so can the Server Members
Intent in the portal.

## 14. Upgrading from v2, and the CLI since v3

### Upgrading a v2 copy

Nothing in your `config.yaml` has to change. Back up the database (§10), set
`TAG` to the v3 release, `pull` and `up -d` as in §11, and watch the log.
On the first start, the bot:

- applies the new migrations (005 to 008). Migration 005 refuses to run on a
  database that already has a foreign-key violation, rolling back and naming
  the table; to check ahead of time, on a *copy* of the database,
  `sqlite3 -readonly copy.db "PRAGMA foreign_key_check;"` should print
  nothing;
- derives a catalog from your old `topics:` and `sources:` (a source that
  names exactly one topic belongs to that game; the rest become shared
  sources), so what gets collected is the same;
- imports your server **once**, in one transaction: its games and channels,
  digest time and zone, admin channel, SHiFT settings (an `@everyone` ping,
  or none if `max_pings_per_day` was 0, with today's spent pings carried
  over) and lounge. It's marked comped, so you keep AI summaries, and today's
  digest, if it already posted, counts as posted, so nothing goes out twice. A
  v2 config with more than 10 topics fails the import with a message and
  writes nothing; trim `topics:` and start again.
- logs what it wrote (lines starting `import:`) and the old keys that can now
  be deleted. The import never runs again, even if you delete them. Keep them
  until you're sure you won't roll back to v2.2, which needs them.

Two small edits are worth making, neither required. Add `command_guild_ids:
[your server's ID]` so the commands appear in your server instantly: without it
they're registered globally, and v2's per-server copies are cleared at the
first start, so for up to an hour (Discord's delay for global commands) you
may be short a command or two. And if you want the bot's own alerts somewhere
specific, set `home_guild_id` to your server (it defaults to your old
`guild_id`) with `admin_channel_id` in it.

### Starting a fresh copy

Nothing to import: steps 4 to 6 above are the whole story. Your server shows
up as a free server with nothing set up until you run `/newsbot setup`, and
free means headlines, so list your server in `comped_guild_ids` if you want
the AI summaries.

### The CLI

`python -m newsbot.pipeline.run` reads secrets from the process environment
(§4) and has these modes:

- **`--check-sources [--game KEY ...]`** runs the real sources once and
  reports per source, grouped by game, then per-game match counts. `--game`
  restricts it to those games' own sources, plus the shared sources matched
  only against them. It needs no database, no Anthropic key and no Discord
  token. The exit code is always 0 (§6).
- **`--dry-run [--guild ID]`** (the default mode) previews one server's next
  digest from the stored items. It writes nothing: it works on a throwaway
  copy of `--db`, so the real file comes out byte-identical, even the
  one-time import and the schema migration. `--guild` can be left out when
  exactly one server is set up (your case); otherwise it's an error that
  lists the IDs. A comped server's preview needs `ANTHROPIC_API_KEY`
  (unless you pass `--stub-llm`).
- **`--collect`** runs one collection pass into `--db` and prints its
  summary. SHiFT detection runs, and any new codes are released and printed,
  but delivery is a preview: nothing is claimed, marked posted or failed, and
  no ping is spent. (`--sweep` is the old name; it still works for one
  release, with a note on stderr.)
- **`--post-to-stdout [--guild ID] [--force]`** is the same server's real
  run, printed instead of posted. It claims the day, prints, and saves, which
  exercises the double-post guard.
- **`--fixtures DIR`** swaps the collectors for canned JSON and implies one
  `--collect` pass first, so `--fixtures --stub-llm --dry-run` stays fully
  offline. **`--now`** is the clock for every step, and **`--stub-llm`** swaps
  in canned stories.

**`--collect`, `--fixtures` and `--post-to-stdout` write to `--db`.** A
collection pass stores items and releases SHiFT codes into the queue the real
bot delivers from; `--post-to-stdout` claims the day's digest for real. Point
them at a copy of your database (`sqlite3 data/newsbot.db ".backup 'copy.db'"`),
never the live file. Every mode except `--check-sources` runs the v2 import
first, so a first `--dry-run` after upgrading just works.
