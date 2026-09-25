# Deploy runbook: the Droplet

This is the "what do I actually type" document for running discord-newsbot
in production on the owner's existing DigitalOcean Droplet. It assumes
you've read `docs/design.md` §7 for the why; this is the how.

Written for Ubuntu/Debian, since that's the likely OS on an existing
Droplet. Differences for other distros are called out where they matter
(mainly: package manager and default `docker` group behavior). If the
Droplet turns out to be something else entirely, the Docker and sqlite3
commands below are the same everywhere -- only the install step changes.

By the time a change reaches this document, it should already have gone
through the standard release flow -- feature branch, PR, merge to
`main`, tried against the test guild with the dev bot using the
published image, then a version tag -- documented in the README's
[Releasing](../README.md#releasing) section. This document picks up
from "there's a tag or image I want running on the Droplet."

## 1. Prerequisites (one-time, on the Droplet)

**Minimum Docker Engine: 20.10.10.** Older versions (19.03, notably --
what shipped on this Droplet before its 2026-09-25 upgrade) predate a
`clone3`/seccomp fix; without it, `python:3.14-slim`'s threading breaks
in ways that look like a Python bug and aren't. If `docker version`
reports anything older, upgrade before doing anything else below.

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
docker version --format '{{.Server.Version}}'   # should be >= 20.10.10
```

**If a distro upgrade (or anything else) disabled Docker's apt repo,**
re-add it before `apt upgrade` will find newer Docker packages:

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Substitute the right codename for `$(lsb_release -cs)` if it's not
detecting correctly (this Droplet is `focal`).

`sqlite3` for the backup script:

```bash
sudo apt install -y sqlite3
```

(Other distros: `dnf install sqlite`, `apk add sqlite`, etc. -- same idea.)

**A note on the OS itself:** Ubuntu 20.04 (focal) left standard support
in mid-2025 and is now on paid Extended Security Maintenance only.
Nothing in this runbook requires an OS upgrade today, but planning one
(to 22.04 or 24.04) is recommended as a separate, deliberate piece of
work -- not bundled into a routine bot deploy.

**The server's clock runs in UTC**, not America/Los_Angeles. This
matters wherever a time-of-day schedule gets translated to a cron or
timer expression below (see Backups, §9, and the deploy-window check in
`scripts/deploy.sh`) -- `config.yaml`'s digest time is independent of
the host clock (the bot converts it itself), but anything driven by the
host's own scheduler is not.

## 2. Directory layout

`/opt/newsbot` **is a clone of this repo.** That's deliberate, not
incidental: it means a routine update is `git pull` plus `docker compose
pull && up -d` (see `scripts/deploy.sh`), with no separate step to keep
`docker-compose.yml`/`docker-compose.prod.yml` in sync by hand, and no
risk of drift between what's on the Droplet and what's in git.

```
/opt/newsbot/                  # git clone of github.com/SomeClown/discord-newsbot
├── docker-compose.yml         # tracked -- updated by `git pull`
├── docker-compose.prod.yml    # tracked -- updated by `git pull`
├── scripts/                   # tracked -- deploy.sh, backup.sh
├── config.yaml                 # gitignored -- real config, not the example
├── .env                        # gitignored -- real secrets, chmod 600
└── data/                        # gitignored
    ├── newsbot.db                # created by the container on first run
    └── backups/                  # created by scripts/backup.sh
```

`config.yaml`, `.env`, and `data/` are all covered by the repo's
`.gitignore` (`config.yaml`, `.env*`, `data/`). That's what makes this
safe: `git pull` only ever fast-forwards tracked files, and none of the
three paths above are tracked, so a pull can neither overwrite nor
delete them. (`git status` inside `/opt/newsbot` after any pull is a
good habit anyway -- it should only ever show those three as untracked,
never as modified-and-about-to-be-lost.)

```bash
sudo mkdir -p /opt/newsbot
sudo chown "$USER":"$USER" /opt/newsbot
git clone https://github.com/SomeClown/discord-newsbot.git /opt/newsbot
cd /opt/newsbot
mkdir -p data
```

**The container runs as uid 10001** (fixed in the `Dockerfile`, not "the
next free uid," specifically so this step keeps working across rebuilds):

```bash
sudo chown -R 10001:10001 /opt/newsbot/data
```

If you skip this, the first write to `/data` inside the container fails
and the bot never gets past startup.

`scripts/backup.sh` runs on the host (not in the container) and needs
read access to `data/newsbot.db`, which is now owned by uid 10001, not
your login user. Two ways to make that work, in order of how this
runbook actually uses them:

- **Run the backup as root** -- this is what the systemd service in §9
  does (`User=root`), and it's the simplest option since root can always
  read the file regardless of ownership.
- **Or add yourself to a group that can read `data/`** if you want to
  run `scripts/backup.sh` by hand without `sudo`: `sudo chown -R
  10001:"$USER" data && chmod -R g+r data` gives your login group read
  access without changing the uid the container writes as. Not required
  if you're only ever running the backup via the systemd timer.

### Migrating today's hand-copied `/opt/newsbot`

As of 2026-09-25, `/opt/newsbot` on the Droplet exists with files
copied in by hand, not a clone -- and no `config.yaml`/`.env`/`data/`
have been created yet (this Droplet hasn't done its first deploy). That
makes the fix a clean swap, not a merge:

```bash
sudo mv /opt/newsbot /opt/newsbot.pre-clone-2026-09-25
sudo mkdir -p /opt/newsbot
sudo chown "$USER":"$USER" /opt/newsbot
git clone https://github.com/SomeClown/discord-newsbot.git /opt/newsbot
cd /opt/newsbot
mkdir -p data
sudo chown -R 10001:10001 data
```

Then continue at §3 below (create the prod Discord app) as if this were
a first deploy, because it is one -- nothing in the old directory needs
to be carried forward. Once `/opt/newsbot` is confirmed working, `sudo
rm -rf /opt/newsbot.pre-clone-2026-09-25` cleans up the old copy (leave
it in place until then, in case something in the hand-copied version
turns out to matter that this note missed).

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
   - **If you're enabling SHiFT code alerts (§15, "Enabling SHiFT code
     alerts," near the end of this document), also check "Mention
     @everyone, @here, and All Roles"** -- without it, the bot still
     posts a new code, it just can't actually notify anyone; §15 also
     covers granting this to a bot that's already invited.
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

`docker-compose.prod.yml` defaults `GHCR_OWNER` to `someclown`, so
there's nothing to set for this repo's own Droplet. It's still a
variable rather than hardcoded, in case that ever needs to change --
override it in `.env` or `export GHCR_OWNER=...` in the shell before
running compose commands if so.

**Pin `TAG` in `.env` for deliberate upgrades** (e.g. `TAG=1.1.0`)
rather than leaving it unset (which floats on `latest`, the newest build
off `main`). Pinning makes "what's running" explicit and makes a
rollback a one-line `.env` edit -- see §8, Rollback, below.

**Never run `docker compose ... config` (or anything else that resolves
`env_file`) against this real `.env`.** It prints every secret in plain
text to your terminal (and probably your shell history and any logging
around it). If you need to sanity-check the merged compose config, do it
against a scratch `.env` with dummy values in a throwaway directory, the
same way CI and local dev do it -- never the real file. (This one's
learned-the-hard-way; see `CLAUDE.md`.)

## 5. GHCR image access

`ghcr.io/someclown/discord-newsbot` is public, so `docker compose pull`
works with no login on the Droplet -- nothing to do here.

(If that ever changes to a private package, log in once from the
Droplet with a classic Personal Access Token scoped to `read:packages`
-- not a fine-grained token tied to the wrong repo, and not
`write:packages`, since this Droplet only ever pulls: `docker login
ghcr.io -u <your-github-username>`, then paste the PAT when prompted for
a password. Docker stores the resulting credential in
`~/.docker/config.json`; don't put the PAT in `.env` or any file that
gets committed.)

## 6. First deploy

```bash
cd /opt/newsbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

(`scripts/deploy.sh` also works here -- it'll print "no database yet,
skipping backup" and continue, since there's nothing to back up on a
first deploy.)

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
3. Install the backup timer (§9 below).
4. The following morning, confirm the digest posted at 09:00
   America/Los_Angeles and that a backup file exists under
   `data/backups/`.

`scripts/deploy.sh` wraps the pull-and-up sequence above plus a wait-for-
healthy loop, if you'd rather not type the two `docker compose` lines and
then remember to check `ps` yourself. It's optional; the runbook commands
are the source of truth.

## 7. Routine updates

```bash
cd /opt/newsbot
./scripts/deploy.sh
```

`scripts/deploy.sh` is the source of truth for a routine update now (not
just a convenience wrapper around it, as it was before `/opt/newsbot`
became a clone). In order, it:

1. Refuses to run if the working tree has local changes to tracked
   files (`git status --porcelain` isn't clean) -- a routine update
   should never silently discard or merge over something someone edited
   by hand on the Droplet.
2. `git pull --ff-only` -- fails loudly on a diverged history rather
   than creating a merge commit no one asked for.
3. Runs `scripts/backup.sh` before touching the running container (skips
   gracefully with a message if there's no database yet).
4. Refuses to run between 09:00 and 09:15 America/Los_Angeles, computed
   from the host's UTC clock (`TAG=... ./scripts/deploy.sh --force`
   overrides this, for the rare case where you're certain it's safe --
   e.g. confirmed today's digest already posted).
5. `docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
   && up -d`, honoring `TAG` from `.env` or the environment.
6. Waits for the container to report `healthy` (up to ~3 minutes) and
   prints `ps` plus the last log lines.

Equivalent by hand, if you want to see each step:

```bash
cd /opt/newsbot
git pull --ff-only
./scripts/backup.sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail 50
```

**Don't deploy between 09:00 and roughly 09:15 America/Los_Angeles.** The
scheduled digest fires at 09:00; recreating the container in the middle of
a run risks an interrupted post. Everything before 09:00 or after about
09:15 is fine. `scripts/deploy.sh` enforces this itself (see above); doing
it by hand, just check a clock.

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

**After tagging a release, wait for CI to finish before deploying it.**
The `vX.Y.Z` tag triggers a build that takes a minute or two; run
`deploy.sh` too early and the pull fails with `manifest unknown`. Nothing
breaks (the script stops before touching the running container), but you
do get to run it again. `gh run list --limit 3` shows when the tag's build
is done.

## 8. Rollback

Every image is tagged `latest` (main branch), `sha-<short>` (every
build), and, for tagged releases, semver (`1.1.0`, `1.1`) -- see
`.github/workflows/ci.yml`. **The recommended way to run prod is to pin
`TAG` to a specific release in `.env`** (e.g. `TAG=1.1.0`), not to float
on `latest` -- that makes both "what's actually running" and "how do I
undo this" a one-line answer:

```bash
cd /opt/newsbot
# edit .env: change TAG=1.1.0 to TAG=1.0.4 (the last known-good release)
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`scripts/deploy.sh --rollback <tag>` is a small convenience for the same
thing: it sets `TAG` for that one run and reminds you to persist the
change in `.env` yourself (it doesn't edit `.env` for you -- that file's
contents shouldn't change from a script running unattended).

If `.env` doesn't pin `TAG` at all, `docker-compose.prod.yml` defaults it
to `latest`, and a one-off rollback works the same way with a `sha-`
value:

```bash
TAG=sha-abc1234 docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
TAG=sha-abc1234 docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Find the short sha from GitHub Actions' build logs or `git log --oneline`
on `main`; find a release's semver tag from GitHub's Releases page or
`git tag`.

If a rollback is needed because of bad data (not just bad code), see §10,
Restore from backup, below -- rolling back the image doesn't undo anything
already written to the database.

**Rolling back past v1.2.0 (SHiFT code alerts) is safe.** Migration 002
(`alerted_codes`, `alert_state`) is purely additive -- it only adds
tables, never touches `items`/`stories`/`digests`/`source_health` -- and
the migration runner never rolls a schema *back* down on its own. A v1.1.1
image (pre-alerts) still starts up fine against a database already
migrated to `user_version` 2: it just never reads or writes the two new
tables, since nothing in that version's code references them. Rolling
back doesn't need `config.yaml`'s `alerts:` block removed either; an
older binary simply ignores config it doesn't know about, the same as any
other config field a newer version added.

## 9. Backups

`scripts/backup.sh` takes an online-safe snapshot with `sqlite3 .backup`
(safe to run against a live WAL-mode database -- unlike `cp`, it won't
catch the file mid-write) and keeps the 7 newest, deleting older ones.

The Droplet's clock is UTC, but the schedule we care about ("08:30
America/Los_Angeles, before the 09:00 digest") is expressed in wall-clock
Pacific time, which shifts against UTC across DST. There are two ways to
run it; **the systemd timer is the recommended one**, because it
understands `America/Los_Angeles` natively and handles the DST shift
without anyone touching the schedule twice a year.

### Recommended: systemd timer

Unit files live in the repo at `deploy/systemd/`. Install them:

```bash
sudo cp /opt/newsbot/deploy/systemd/newsbot-backup.service \
        /opt/newsbot/deploy/systemd/newsbot-backup.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newsbot-backup.timer
```

Verify:

```bash
systemctl list-timers newsbot-backup.timer
sudo systemctl start newsbot-backup.service   # run it once by hand
journalctl -u newsbot-backup.service --since today
```

`newsbot-backup.timer` fires at `OnCalendar=*-*-* 08:30:00
America/Los_Angeles` -- systemd resolves that to the correct UTC instant
itself, DST included, because it evaluates the calendar expression in the
named zone rather than the host's. systemd has supported the trailing
timezone on `OnCalendar` since v235; Ubuntu 20.04 (focal) ships systemd
245, so no extra setup is needed. `newsbot-backup.service` runs as root
(see §2 on why, and the unit file's own comment), so it doesn't need the
uid-10001 permission workaround that running the script as your login
user would.

If `/opt/newsbot` moves or the unit files change, re-run the `cp` and
`daemon-reload` steps above -- systemd doesn't watch the source files in
the repo, only its own copies under `/etc/systemd/system/`.

### Fallback: cron

Ubuntu's cron is Vixie cron, which has **no `CRON_TZ` support** (that's
a cronie/Debian-cron feature this Droplet doesn't have) -- so a crontab
entry has to be written in the host's own UTC time, and re-adjusted by
hand across DST if you want the backup to stay pinned to 08:30 Pacific:

```bash
sudo crontab -e
```

```cron
# 08:30 America/Los_Angeles == 15:30 UTC during PDT (roughly
# mid-March to early November) or 16:30 UTC during PST. This host's
# clock is UTC -- see docs/deploy.md §1. Update the hour by hand at
# each DST transition, or use the systemd timer instead (§9 above),
# which does this automatically.
30 15 * * * cd /opt/newsbot && ./scripts/backup.sh data/newsbot.db data/backups >> data/backups/backup.log 2>&1
```

Install it in **root's** crontab (`sudo crontab -e`, as above): `data/`
belongs to the container's uid 10001, and root is the simplest user that
can read it and write `data/backups/`. Same reasoning as the systemd unit.

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

**If SHiFT code alerts are enabled, a restore can cause a code to
re-alert.** `alerted_codes` rolls back with everything else -- a code
first recorded *after* the backup was taken (posted or otherwise) is
gone from the restored database, and the next sweep that finds it again
treats it as new. This is bounded for a *dated* item, not open-ended:
`max_item_age_hours` (default 48) still has to consider the item fresh,
and `max_pings_per_day` still caps how many of those re-alerts can
actually carry a ping in one day. It is **not** bounded at all for an
*undated* item -- undated items are always treated as fresh (the same
rule SPEC-DEV 4 and A11 apply everywhere else in this feature), so a
code whose only sighting has no `published_at` can re-alert after a
restore no matter how long ago the backup was taken; the daily ping cap
is still the only thing limiting how loud that re-alert can be. Worth a
heads-up in the channel after a restore if alerts are on, same as the
"recent digest history jumps backward" note above -- it's the same
underlying rollback, just visible in a different table.

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

## 15. Enabling SHiFT code alerts

New in v1.2.0 (`design.md` §12). Off by default -- nothing below changes
existing behavior until you do it.

1. **Grant the mention permission.** The bot's role needs **Mention
   @everyone, @here, and All Roles** in the digest channel, or a ping
   posts silently un-pinged (the bot notices and sends an admin alert,
   but nobody gets notified that first time). Two ways to grant it to a
   bot that's already invited, without re-inviting:
   - **Server-wide (simplest):** Server Settings → Roles → the bot's own
     role → toggle on "Mention @everyone, @here, and All Roles".
   - **Digest-channel-only override (narrower):** the digest channel's
     own Settings → Permissions → add the bot's role → toggle on the
     same permission just for that channel, leaving the role's
     server-wide permissions untouched. Prefer this if the bot's role
     is also used anywhere you specifically don't want it able to ping.
2. **Turn it on in `config.yaml`:**
   ```yaml
   alerts:
     enabled: true
     topics: [borderlands4]   # scope to the game(s) that actually use SHiFT codes
   ```
   Everything else (`interval_minutes`, `max_item_age_hours`,
   `max_pings_per_day`) is fine at its default; see
   `config.example.yaml`'s commented block for what each one does. Leave
   `allow_test_command` out (or `false`) in prod -- it registers
   `/newsbot test-alert`, which is meant for the private test guild only.
3. **Redeploy** the normal way (`./scripts/deploy.sh` or the by-hand
   steps in §7) -- migration 002 (`alerted_codes`, `alert_state`) applies
   itself at startup the same way every other migration does; nothing
   extra to run by hand.
4. **Confirm it's live:** `/newsbot status` should show a "SHiFT alerts"
   field instead of "disabled". The first real sweep seeds silently
   (shows `(seeding)`, posts nothing) -- that's expected, not a bug; see
   `design.md` §12's silent-seeding safeguard.

Rolling this back out is just `alerts.enabled: false` (or removing the
`alerts:` block entirely) and redeploying -- migration 002 stays applied
(it's additive and harmless either way; see §8, Rollback, above), the
sweep simply stops running.
