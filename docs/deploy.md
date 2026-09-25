# Deploy runbook: the Droplet

This is the "what do I actually type" document for running discord-newsbot
in production on the owner's existing DigitalOcean Droplet. It assumes
you've read `docs/design.md` §7 for the why; this is the how.

Written for Ubuntu/Debian, since that's the likely OS on an existing
Droplet. Differences for other distros are called out where they matter
(mainly: package manager and default `docker` group behavior). If the
Droplet turns out to be something else entirely, the Docker and sqlite3
commands below are the same everywhere -- only the install step changes.

## 1. Prerequisites (one-time, on the Droplet)

Docker Engine plus the compose plugin (not the old standalone
`docker-compose` binary -- this repo uses `docker compose`, two words):

```bash
# Ubuntu/Debian, per Docker's own install docs:
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# log out and back in for the group change to take effect
```

Verify:

```bash
docker compose version   # should print a v2.x version
```

`sqlite3` for the backup script:

```bash
sudo apt install -y sqlite3
```

(Other distros: `dnf install sqlite`, `apk add sqlite`, etc. -- same idea.)

## 2. Directory layout

Everything lives under `/opt/newsbot`:

```
/opt/newsbot/
├── docker-compose.yml        # copied from the repo, unmodified
├── docker-compose.prod.yml   # copied from the repo, unmodified
├── config.yaml                # real config -- not in git, not the example
├── .env                       # real secrets -- not in git, chmod 600
└── data/
    ├── newsbot.db              # created by the container on first run
    └── backups/                # created by scripts/backup.sh
```

```bash
sudo mkdir -p /opt/newsbot/data
sudo chown -R "$USER":"$USER" /opt/newsbot
cd /opt/newsbot
```

Copy `docker-compose.yml` and `docker-compose.prod.yml` from the repo (a
`git clone` into a scratch directory and `cp` is fine -- the Droplet
doesn't need the full checkout, just those two files. `scripts/backup.sh`
and `scripts/deploy.sh` are handy to have here too, so either copy them in
or clone the repo properly if you'd rather not hand-copy files).

**The container runs as uid 10001** (fixed in the `Dockerfile`, not "the
next free uid," specifically so this step keeps working across rebuilds):

```bash
sudo chown -R 10001:10001 /opt/newsbot/data
```

If you skip this, the first write to `/data` inside the container fails
and the bot never gets past startup.

### A file, not a directory

Before the first `docker compose up`, **`config.yaml` and `.env` must
already exist as real files.** `docker-compose.prod.yml` bind-mounts
`./config.yaml:/app/config.yaml:ro`, and Docker's bind-mount behavior is
to silently create an empty directory at that path if nothing's there yet.
An empty directory named `config.yaml` is not a config file, and the
container will crash trying to read it -- a confusing failure that looks
like a config-parsing bug but is really just "the file didn't exist yet."
(The same trap is called out in `docker-compose.dev.yml`'s comments for the
dev override, which swaps the config mount for `config.dev.yaml` for the
same reason: get the real file in place *before* `up`, not after.)

## 3. First deploy: create the prod Discord app

This is separate from the dev bot used in `.env.dev` during development --
**dev and prod must use different tokens.** Running two processes on the
same token means both receive every gateway interaction and race for it;
the loser logs `Unknown interaction (10062)` (see `CLAUDE.md`).

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create a new application for prod. Do not reuse the dev application.
2. Bot settings:
   - **Public Bot: OFF** (nobody outside this server should be able to add it).
   - No privileged intents (Message Content, Server Members, Presence) --
     the bot doesn't need them.
   - Copy the bot token into the Droplet's `.env` (see below), never into
     the repo, a chat log, or this document.
3. Generate an invite URL (OAuth2 → URL Generator):
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels, Send Messages, Send Messages in
     Threads, Embed Links, Create Public Threads**, and optionally **Read
     Message History** (only needed if you want the bot to see prior
     messages in a thread it's posting to; not required for its own posts).
4. Invite it to the community guild. The owner does this step in person
   per plan Checkpoint H -- `devops` doesn't hold the prod token.

## 4. Fill in `config.yaml` and `.env`

```bash
cp config.example.yaml /opt/newsbot/config.yaml
```

Edit `config.yaml`: replace every placeholder ID (`guild_id`,
`digest.channel_id`, `admin_channel_id` if used) with the real ones from
the prod guild. The example file's comments explain what each one is for.

```bash
cp .env.example /opt/newsbot/.env
chmod 600 /opt/newsbot/.env
```

Fill in `.env`:

```
DISCORD_TOKEN=<prod bot token>
ANTHROPIC_API_KEY=<...>
BRAVE_API_KEY=<...>
BLUESKY_HANDLE=<optional>
BLUESKY_APP_PASSWORD=<optional>
```

`GHCR_OWNER` also needs to be set somewhere `docker compose` can see it --
either add it to `.env` or `export GHCR_OWNER=someclown` in the shell
before running compose commands. `docker-compose.prod.yml` defaults it to
`owner`, which is a placeholder, not a real fallback.

**Never run `docker compose ... config` (or anything else that resolves
`env_file`) against this real `.env`.** It prints every secret in plain
text to your terminal (and probably your shell history and any logging
around it). If you need to sanity-check the merged compose config, do it
against a scratch `.env` with dummy values in a throwaway directory, the
same way CI and local dev do it -- never the real file. (This one's
learned-the-hard-way; see `CLAUDE.md`.)

## 5. GHCR image access

Package visibility (public vs. private) isn't decided yet as of this
writing. Both cases:

**If the GHCR package is public:** nothing to do. `docker compose pull`
works with no login.

**If the GHCR package is private:** log in once, from the Droplet, with a
classic Personal Access Token scoped to `read:packages` (not a fine-grained
token tied to the wrong repo, and not `write:packages` -- this Droplet only
ever pulls):

```bash
docker login ghcr.io -u <your-github-username>
# paste the PAT when prompted for a password
```

Docker stores the resulting credential in `~/.docker/config.json` on the
Droplet. Don't put the PAT in `.env` or any file that gets committed.

## 6. First deploy

```bash
cd /opt/newsbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Check it came up healthy:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
# STATUS column should progress to "healthy" within ~2 minutes
# (the healthcheck's start-period is 120s, so "starting" for a while is normal)

docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100
```

Then, per plan Checkpoint H:

1. Run `/newsbot preview` in the guild -- confirms the pipeline, the
   Claude and Brave keys, and Discord permissions all actually work,
   without posting or recording anything.
2. Run `/newsbot status` -- confirms source health and that the scheduler
   is running.
3. Install the backup cron (§9 below).
4. The following morning, confirm the digest posted at 09:00
   America/Los_Angeles and that a backup file exists under
   `data/backups/`.

`scripts/deploy.sh` wraps the pull-and-up sequence above plus a wait-for-
healthy loop, if you'd rather not type the two `docker compose` lines and
then remember to check `ps` yourself. It's optional; the runbook commands
are the source of truth.

## 7. Routine updates

Same two commands as first deploy (or `./scripts/deploy.sh`):

```bash
cd /opt/newsbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 50
```

**Don't deploy between 09:00 and roughly 09:15 America/Los_Angeles.** The
scheduled digest fires at 09:00; recreating the container in the middle of
a run risks an interrupted post. Everything before 09:00 or after about
09:15 is fine.

**Never test against prod with the dev token, or vice versa.** If you need
to poke at the prod deployment by hand (e.g. testing a permission change),
first stop whatever's running locally against the *same* token:

```bash
# Stop a local dev container:
docker compose -f docker-compose.yml -f docker-compose.dev.yml down

# Stop a local (non-container) dev process:
# on macOS the process name has a capital P -- `python -m newsbot` won't
# match it in pgrep/pkill, but `newsbot` (the module/package name) will:
pkill -f newsbot
```

Dev and prod use different tokens, so running both simultaneously is
normally fine -- this only matters if you're deliberately pointing a local
process at the prod token for some reason, which should be rare and
short-lived.

## 8. Rollback

Every image is tagged both `latest` and `sha-<short>` (see
`.github/workflows/ci.yml`). To roll back to a known-good build:

```bash
cd /opt/newsbot
TAG=sha-abc1234 docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
TAG=sha-abc1234 docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Find the short sha from GitHub Actions' build logs or `git log --oneline`
on `main`. `docker-compose.prod.yml` defaults `TAG` to `latest` when unset,
so a plain `up -d` afterward goes back to the newest image -- no separate
"undo the rollback" step needed, just redeploy without `TAG` set.

If a rollback is needed because of bad data (not just bad code), see §10,
Restore from backup, below -- rolling back the image doesn't undo anything
already written to the database.

## 9. Backups

`scripts/backup.sh` takes an online-safe snapshot with `sqlite3 .backup`
(safe to run against a live WAL-mode database -- unlike `cp`, it won't
catch the file mid-write) and keeps the 7 newest, deleting older ones.

Install the cron job (as the user that owns `/opt/newsbot`, not root,
since the script just needs read access to `data/newsbot.db`):

```bash
crontab -e
```

Add:

```cron
# Nightly newsbot backup, 08:30 local time -- before the 09:00 digest,
# so a mid-run crash never lands between "last backup" and "today's data."
30 8 * * * cd /opt/newsbot && ./scripts/backup.sh data/newsbot.db data/backups >> data/backups/backup.log 2>&1
```

(Adjust the schedule time to the Droplet's own system timezone, which may
not be America/Los_Angeles -- check with `timedatectl` or `date`. The
digest time in `config.yaml` is independent of the host's timezone; the
backup cron's time is not.)

Off-host copies (DigitalOcean snapshots, `rsync` to elsewhere) are a
sensible follow-up but explicitly out of scope for v1 -- see
`docs/design.md` §6.

### Restore from backup

1. Stop the bot so nothing writes to the database mid-restore:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
   ```
2. Move the current (possibly corrupt) database aside rather than deleting
   it -- you may want to compare or recover something from it later:
   ```bash
   mv data/newsbot.db data/newsbot.db.pre-restore-$(date +%F)
   rm -f data/newsbot.db-wal data/newsbot.db-shm
   ```
3. Copy the chosen backup into place:
   ```bash
   cp data/backups/newsbot-2026-09-20.db data/newsbot.db
   ```
4. Sanity-check it before trusting it:
   ```bash
   sqlite3 data/newsbot.db "pragma integrity_check;"
   ```
5. Bring the bot back up:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml start
   ```
6. Run `/newsbot status` to confirm it's reading the restored data
   sensibly (recent digest history will jump backward to whatever day the
   backup was taken -- that's expected, not a bug).

A restore rolls the database back to the backup's point in time (up to 24h
of digest history and dedupe state lost, worst case). It does not touch
`config.yaml`, `.env`, or the image -- only `data/newsbot.db`.

## 10. Recovering a stuck or failed digest

If `/newsbot status` shows today's digest as `failed`, `partial`, or
missing after 09:15:

```
/newsbot run-now
```

This re-runs the pipeline and posts. It asks for confirmation if today's
digest already posted, so it's safe to try even if you're not sure of the
exact state -- it won't silently double-post. If the failure was a
transient upstream issue (a source timing out, a Claude API hiccup), this
is usually all that's needed. If it fails again, check the logs (§11)
for what's actually going wrong before retrying further.

## 11. Rotating secrets

If a token or key leaks (or just on a routine schedule):

1. Generate the new credential at the source (Discord Developer Portal,
   Anthropic console, Brave's dashboard).
2. Update `/opt/newsbot/.env` with the new value.
3. Recreate the container so it picks up the new environment:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
   ```
   (`up -d` without `pull` is enough if only `.env` changed, not the
   image -- compose recreates the container with the new env either way.)
4. Revoke the old credential at the source, after confirming the bot is
   healthy on the new one.

Never commit `.env` or paste its contents anywhere, including into this
runbook, a chat log, or an issue.

## 12. Where logs live

`docker-compose.yml` sets `json-file` logging with `max-size: 10m,
max-file: 3` (30MB cap per container, rotated automatically -- no logrotate
setup needed). View them with:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f   # follow
```

Logs are structured JSON to stdout (`docs/design.md` §8), so they pipe
cleanly into `jq` if you want to filter:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 500 | jq .
```

## 13. Disk usage expectations

Rough numbers, not a hard budget:

- The image itself: under 300MB (slim Python base, no compiler in the
  final stage).
- `data/newsbot.db`: grows slowly -- it's headlines, summaries and item
  metadata for 3 topics, not full article bodies. Low tens of MB even
  after months of daily digests.
- `data/backups/`: 7 × the database size, so proportionally small next to
  the database itself.
- Container logs: capped at 30MB total by the logging config above.

A default Droplet's disk should handle this without attention for a long
time. If `df -h` ever looks tight, `data/backups/` and old Docker images
(`docker image prune`) are the first places to look, not the database.

## 14. Troubleshooting

**`Unknown interaction (10062)` in the logs.** Two instances of the bot
are running on the same token and racing for interactions -- see §7 above
and `CLAUDE.md`. Find and stop the second one (a leftover local dev
process is the usual culprit); don't touch the code, this isn't a bug in
the bot.

**Reddit returns 403 or 429.** Expected from datacenter IPs and generic
user agents; Reddit does this to a lot of cloud providers, not just this
one. The collector already sends a descriptive User-Agent and prefers
`/new/.rss`. Check `/newsbot status` -- after 3 consecutive daily
failures it's flagged there as a source health issue, which is the
designed behavior, not a fresh problem each time it happens. If it
persists, that's an owner decision (drop Reddit as a source, or accept the
gap) per `docs/design.md` §4, not something to patch around here.

**A YouTube feed 404s.** YouTube's channel RSS feeds occasionally 404
upstream for reasons outside this bot's control (a channel's video ID
changed, the feed endpoint had a bad day). Same handling as any other
single-source failure: logged, `source_health` updated, digest continues
without it. Only worth a closer look if it's the *same* feed failing
repeatedly across multiple days.

**Container is `unhealthy`.** Check
`docker inspect -f '{{json .State.Health}}' <container> | jq` for the
last few healthcheck outputs, and the logs for whether the gateway
connection is actually established. The healthcheck fails if the heartbeat
file (`/tmp/newsbot-heartbeat` inside the container) is more than 3
minutes old -- a Discord gateway outage or a stuck scheduler will show up
this way before anyone notices the digest didn't post.

**Container won't start / crashes immediately on a fresh deploy.** Check
whether `config.yaml` or `.env` ended up as an empty directory instead of
a file (see §2, "A file, not a directory") -- this is the single most
likely cause of an otherwise-inexplicable startup crash on a brand new
`/opt/newsbot` setup.
