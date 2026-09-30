# Deploy runbook: the Droplet

The maintainer's own Droplet runbook; see `docs/self-host.md` if you're
running your own copy on your own server. This document stays useful as a
worked example (and is where the general backup/rollback/upgrade
procedures self-host.md points back to live), but it assumes this specific
Droplet's own path, timezone, and release habits throughout.

This is the "what do I actually type" document for running discord-newsbot
in production on the owner's existing DigitalOcean Droplet. It assumes
you've read `docs/design.md` §7 for the why; this is the how.

Written for Ubuntu/Debian, since that's the likely OS on an existing
Droplet. Differences for other distros are called out where they matter
(mainly: package manager and default `docker` group behavior). If the
Droplet turns out to be something else entirely, the Docker and sqlite3
commands below are the same everywhere; only the install step changes.

By the time a change reaches this document, it should already have gone
through the standard release flow (feature branch, PR, merge to
`main`, tried against the test guild with the dev bot using the
published image, then a version tag), documented in the README's
[Releasing](../README.md#releasing) section. This document picks up
from "there's a tag or image I want running on the Droplet."

## 1. Prerequisites (one-time, on the Droplet)

**Minimum Docker Engine: 20.10.10.** Older versions (19.03, notably
what shipped on this Droplet before its 2026-09-25 upgrade) predate a
`clone3`/seccomp fix; without it, `python:3.14-slim`'s threading breaks
in ways that look like a Python bug and aren't. If `docker version`
reports anything older, upgrade before doing anything else below.

Docker Engine plus the compose plugin (not the old standalone
`docker-compose` binary; this repo uses `docker compose`, two words):

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

(Other distros: `dnf install sqlite`, `apk add sqlite`, etc.; same idea.)

**A note on the OS itself:** Ubuntu 20.04 (focal) left standard support
in mid-2025 and is now on paid Extended Security Maintenance only.
Nothing in this runbook requires an OS upgrade today, but planning one
(to 22.04 or 24.04) is recommended as a separate, deliberate piece of
work; not bundled into a routine bot deploy.

**The server's clock runs in UTC**, not America/Los_Angeles. This
matters wherever a time-of-day schedule gets translated to a cron or
timer expression below (see Backups, §9, and the deploy-window check in
`scripts/deploy.sh`); `config.yaml`'s digest time is independent of
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
├── docker-compose.yml         # tracked: updated by `git pull`
├── docker-compose.prod.yml    # tracked: updated by `git pull`
├── scripts/                   # tracked: deploy.sh, backup.sh
├── config.yaml                 # gitignored: real config, not the example
├── .env                        # gitignored: real secrets, chmod 600
└── data/                        # gitignored
    ├── newsbot.db                # created by the container on first run
    └── backups/                  # created by scripts/backup.sh
```

`config.yaml`, `.env`, and `data/` are all covered by the repo's
`.gitignore` (`config.yaml`, `.env*`, `data/`). That's what makes this
safe: `git pull` only ever fast-forwards tracked files, and none of the
three paths above are tracked, so a pull can neither overwrite nor
delete them. (`git status` inside `/opt/newsbot` after any pull is a
good habit anyway; it should only ever show those three as untracked,
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

- **Run the backup as root**: this is what the systemd service in §9
  does (`User=root`), and it's the simplest option since root can always
  read the file regardless of ownership.
- **Or add yourself to a group that can read `data/`** if you want to
  run `scripts/backup.sh` by hand without `sudo`: `sudo chown -R
  10001:"$USER" data && chmod -R g+r data` gives your login group read
  access without changing the uid the container writes as. Not required
  if you're only ever running the backup via the systemd timer.

### Migrating today's hand-copied `/opt/newsbot`

As of 2026-09-25, `/opt/newsbot` on the Droplet exists with files
copied in by hand, not a clone; and no `config.yaml`/`.env`/`data/`
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
a first deploy, because it is one; nothing in the old directory needs
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
container will crash trying to read it; a confusing failure that looks
like a config-parsing bug but is really just "the file didn't exist yet."
(The same trap is called out in `docker-compose.dev.yml`'s comments for the
dev override, which swaps the config mount for `config.dev.yaml` for the
same reason: get the real file in place *before* `up`, not after.)

## 3. First deploy: create the prod Discord app

This is separate from the dev bot used in `.env.dev` during development;
**dev and prod must use different tokens.** Running two processes on the
same token means both receive every gateway interaction and race for it;
the loser logs `Unknown interaction (10062)` (see `CLAUDE.md`).

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create a new application for prod. Do not reuse the dev application.
2. Bot settings:
   - **Public Bot: OFF** (nobody outside this server should be able to add it).
   - No privileged intents, unless you turn on lounge welcomes (§18); then
     enable **Server Members Intent** in the portal *before* starting the
     bot. Without it the bot prints a message and exits after a 10-minute
     wait, which keeps a restart loop from hammering Discord's login limit.
     Message Content and Presence stay off either way.
   - Copy the bot token into the Droplet's `.env` (see below), never into
     the repo, a chat log, or this document.
3. Generate an invite URL (OAuth2 → URL Generator):
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels, Send Messages, Embed Links**. As of
     v2.0 there's no combined digest channel and no discussion thread on
     it, so **Create Public Threads** and **Send Messages in Threads**
     aren't needed anymore; each game gets its own channel and its own
     plain message, nothing more. **Read Message History** isn't needed
     either, for the same reason (it was only ever about threads).
   - **If you're enabling SHiFT code alerts (§15, "Enabling SHiFT code
     alerts," near the end of this document), also check "Mention
     @everyone, @here, and All Roles"**: without it, the bot still
     posts a new code, it just can't actually notify anyone; §15 also
     covers granting this to a bot that's already invited.
4. Invite it to the community guild. The owner does this step in person
   per plan Checkpoint H; `devops` doesn't hold the prod token.

## 4. Fill in `config.yaml` and `.env`

```bash
cp config.example.yaml /opt/newsbot/config.yaml
```

Edit `config.yaml`: replace every placeholder ID (`guild_id`, each topic's
`channel_id`, `admin_channel_id` if used, and `alerts.channel_id` if
SHiFT alerts are enabled) with the real ones from the prod guild. There is
no `digest.channel_id` as of v2.0; see §13 of `docs/design.md` and the
"Upgrading to v2.0" section near the end of this document if you're
coming from a v1 config. The example file's comments explain what each
placeholder is for.

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
variable rather than hardcoded, in case that ever needs to change;
override it in `.env` or `export GHCR_OWNER=...` in the shell before
running compose commands if so.

**Pin `TAG` in `.env` for deliberate upgrades** (e.g. `TAG=1.1.0`)
rather than leaving it unset (which floats on `latest`, the newest build
off `main`). Pinning makes "what's running" explicit and makes a
rollback a one-line `.env` edit; see §8, Rollback, below.

**Never run `docker compose ... config` (or anything else that resolves
`env_file`) against this real `.env`.** It prints every secret in plain
text to your terminal (and probably your shell history and any logging
around it). If you need to sanity-check the merged compose config, do it
against a scratch `.env` with dummy values in a throwaway directory, the
same way CI and local dev do it; never the real file. (This one's
learned-the-hard-way; see `CLAUDE.md`.)

## 5. GHCR image access

`ghcr.io/someclown/discord-newsbot` is public, so `docker compose pull`
works with no login on the Droplet; nothing to do here.

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

(`scripts/deploy.sh` also works here: it'll print "no database yet,
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

1. Run `/newsbot preview` in the guild: confirms the pipeline, the
   Claude and Brave keys, and Discord permissions all actually work,
   without posting or recording anything.
2. Run `/newsbot status`: confirms source health and that the scheduler
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
   files (`git status --porcelain` isn't clean); a routine update
   should never silently discard or merge over something someone edited
   by hand on the Droplet.
2. `git pull --ff-only`: fails loudly on a diverged history rather
   than creating a merge commit no one asked for.
3. Runs `scripts/backup.sh` before touching the running container (skips
   gracefully with a message if there's no database yet).
4. Refuses to run between 09:00 and 09:15 America/Los_Angeles, computed
   from the host's UTC clock (`TAG=... ./scripts/deploy.sh --force`
   overrides this, for the rare case where you're certain it's safe,
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
# on macOS the process name has a capital P; `python -m newsbot` won't
# match it in pgrep/pkill, but `newsbot` (the module/package name) will:
pkill -f newsbot
```

Dev and prod use different tokens, so running both simultaneously is
normally fine; this only matters if you're deliberately pointing a local
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
build), and, for tagged releases, semver (`1.1.0`, `1.1`); see
`.github/workflows/ci.yml`. **The recommended way to run prod is to pin
`TAG` to a specific release in `.env`** (e.g. `TAG=1.1.0`), not to float
on `latest`; that makes both "what's actually running" and "how do I
undo this" a one-line answer:

```bash
cd /opt/newsbot
# edit .env: change TAG=1.1.0 to TAG=1.0.4 (the last known-good release)
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`scripts/deploy.sh --rollback <tag>` is a small convenience for the same
thing: it sets `TAG` for that one run and reminds you to persist the
change in `.env` yourself (it doesn't edit `.env` for you; that file's
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
Restore from backup, below; rolling back the image doesn't undo anything
already written to the database.

**Rolling back past v1.2.0 (SHiFT code alerts) is safe.** Migration 002
(`alerted_codes`, `alert_state`) is purely additive; it only adds
tables, never touches `items`/`stories`/`digests`/`source_health`; and
the migration runner never rolls a schema *back* down on its own. A v1.1.1
image (pre-alerts) still starts up fine against a database already
migrated to `user_version` 2: it just never reads or writes the two new
tables, since nothing in that version's code references them. Rolling
back doesn't need `config.yaml`'s `alerts:` block removed either; an
older binary simply ignores config it doesn't know about, the same as any
other config field a newer version added.

**Rolling back past v2.0.0 (per-game channels) needs the v1 `config.yaml`
back, not just the image.** Migration 003 (`alerted_codes.from_roundup`)
is additive the same way 002 was; a pre-2.0 binary starts up fine
against a database already migrated to `user_version` 3, it just never
reads or writes that column. The database needs no restore either way.
What v1.3.0 *does* need is a v1-shaped `config.yaml`: it requires
`digest.channel_id` (v2.0 removed the field pydantic validates against)
and rejects any `alerts` key it doesn't recognize (`alerts.channel_id`
didn't exist yet). Keep the pre-upgrade config around as `config.v1.yaml`
(see "Upgrading to v2.0" below), and swap it back in alongside the
`TAG` rollback above.

**Rolling back past v2.2.0 (lounge) needs no config or database change,
but does need Discord's built-in welcome switched back on.** Migration 004
(`lounge_quotes_used`, `lounge_state`) is additive, and v2.1.1 ignores
both the new tables and an unrecognized top-level `lounge:` block in
`config.yaml`, so `TAG=2.1.1` alone rolls the code back with no database
restore. See §18 for the Discord-side steps.

## 9. Backups

`scripts/backup.sh` takes an online-safe snapshot with `sqlite3 .backup`
(safe to run against a live WAL-mode database; unlike `cp`, it won't
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
America/Los_Angeles`; systemd resolves that to the correct UTC instant
itself, DST included, because it evaluates the calendar expression in the
named zone rather than the host's. systemd has supported the trailing
timezone on `OnCalendar` since v235; Ubuntu 20.04 (focal) ships systemd
245, so no extra setup is needed. `newsbot-backup.service` runs as root
(see §2 on why, and the unit file's own comment), so it doesn't need the
uid-10001 permission workaround that running the script as your login
user would.

If `/opt/newsbot` moves or the unit files change, re-run the `cp` and
`daemon-reload` steps above; systemd doesn't watch the source files in
the repo, only its own copies under `/etc/systemd/system/`.

### Fallback: cron

Ubuntu's cron is Vixie cron, which has **no `CRON_TZ` support** (that's
a cronie/Debian-cron feature this Droplet doesn't have); so a crontab
entry has to be written in the host's own UTC time, and re-adjusted by
hand across DST if you want the backup to stay pinned to 08:30 Pacific:

```bash
sudo crontab -e
```

```cron
# 08:30 America/Los_Angeles == 15:30 UTC during PDT (roughly
# mid-March to early November) or 16:30 UTC during PST. This host's
# clock is UTC; see docs/deploy.md §1. Update the hour by hand at
# each DST transition, or use the systemd timer instead (§9 above),
# which does this automatically.
30 15 * * * cd /opt/newsbot && ./scripts/backup.sh data/newsbot.db data/backups >> data/backups/backup.log 2>&1
```

Install it in **root's** crontab (`sudo crontab -e`, as above): `data/`
belongs to the container's uid 10001, and root is the simplest user that
can read it and write `data/backups/`. Same reasoning as the systemd unit.

Off-host copies (DigitalOcean snapshots, `rsync` to elsewhere) are a
sensible follow-up but explicitly out of scope for v1; see
`docs/design.md` §6.

### Restore from backup

1. Stop the bot so nothing writes to the database mid-restore:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
   ```
2. Move the current (possibly corrupt) database aside rather than deleting
   it; you may want to compare or recover something from it later:
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
   backup was taken; that's expected, not a bug).

A restore rolls the database back to the backup's point in time (up to 24h
of digest history and dedupe state lost, worst case). It does not touch
`config.yaml`, `.env`, or the image; only `data/newsbot.db`.

**If SHiFT code alerts are enabled, a restore can cause a code to
re-alert.** `alerted_codes` rolls back with everything else; a code
first recorded *after* the backup was taken (posted or otherwise) is
gone from the restored database, and the next sweep that finds it again
treats it as new. This is bounded for a *dated* item, not open-ended:
`max_item_age_hours` (default 48) still has to consider the item fresh,
and `max_pings_per_day` still caps how many of those re-alerts can
actually carry a ping in one day. It is **not** bounded at all for an
*undated* item; undated items are always treated as fresh (the same
rule SPEC-DEV 4 and A11 apply everywhere else in this feature), so a
code whose only sighting has no `published_at` can re-alert after a
restore no matter how long ago the backup was taken; the daily ping cap
is still the only thing limiting how loud that re-alert can be. Worth a
heads-up in the channel after a restore if alerts are on, same as the
"recent digest history jumps backward" note above; it's the same
underlying rollback, just visible in a different table.

## 10. Recovering a stuck or failed digest

If `/newsbot status` shows today's digest as `failed`, `partial`, or
missing after 09:15:

```
/newsbot run-now
```

This re-runs the pipeline and posts. It asks for confirmation if today's
digest already posted, so it's safe to try even if you're not sure of the
exact state. That confirmation is about *asking before it does anything*,
not about avoiding duplicates: per-topic progress from a partial failure
lives in memory on the publisher that hit it, not in the database, so a
confirmed run-now after a partial failure reposts **every** game today,
including the ones that already went out; there's no "only repost what
didn't post" mode yet (a known limitation, not a bug). If the failure was
a transient upstream issue (a source timing out, a Claude API hiccup),
this is usually all that's needed anyway. If it fails again, check the
logs (§11) for what's actually going wrong before retrying further.

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
   image; compose recreates the container with the new env either way.)
4. Revoke the old credential at the source, after confirming the bot is
   healthy on the new one.

Never commit `.env` or paste its contents anywhere, including into this
runbook, a chat log, or an issue.

## 12. Where logs live

`docker-compose.yml` sets `json-file` logging with `max-size: 10m,
max-file: 3` (30MB cap per container, rotated automatically; no logrotate
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
- `data/newsbot.db`: grows slowly; it's headlines, summaries and item
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
are running on the same token and racing for interactions; see §7 above
and `CLAUDE.md`. Find and stop the second one (a leftover local dev
process is the usual culprit); don't touch the code, this isn't a bug in
the bot.

**Reddit returns 403 or 429.** Expected from datacenter IPs and generic
user agents; Reddit does this to a lot of cloud providers, not just this
one. The collector already sends a descriptive User-Agent and prefers
`/new/.rss`. Check `/newsbot status`: after 3 consecutive daily
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
minutes old; a Discord gateway outage or a stuck scheduler will show up
this way before anyone notices the digest didn't post.

**Container won't start / crashes immediately on a fresh deploy.** Check
whether `config.yaml` or `.env` ended up as an empty directory instead of
a file (see §2, "A file, not a directory"); this is the single most
likely cause of an otherwise-inexplicable startup crash on a brand new
`/opt/newsbot` setup.

## 15. Enabling SHiFT code alerts

New in v1.2.0 (`design.md` §12). Off by default; nothing below changes
existing behavior until you do it.

1. **Create the SHiFT codes channel** (as of v2.0, alerts no longer share
   a channel with any game's digest; see "Upgrading to v2.0" below if
   you're moving from a v1 config that still had alerts on the digest
   channel).
2. **Grant the mention permission.** The bot's role needs **View
   Channel**, **Send Messages**, and **Mention @everyone, @here, and All
   Roles** in that channel, or a ping posts silently un-pinged (the bot
   notices and sends an admin alert, but nobody gets notified that first
   time). Two ways to grant the mention permission to a bot that's
   already invited, without re-inviting:
   - **Server-wide (simplest):** Server Settings → Roles → the bot's own
     role → toggle on "Mention @everyone, @here, and All Roles".
   - **Channel-only override (narrower):** the SHiFT codes channel's own
     Settings → Permissions → add the bot's role → toggle on the same
     permission just for that channel, leaving the role's server-wide
     permissions untouched. Prefer this if the bot's role is also used
     anywhere you specifically don't want it able to ping.
3. **Turn it on in `config.yaml`:**
   ```yaml
   alerts:
     enabled: true
     channel_id: <the SHiFT codes channel's id>
     topics: [borderlands4]   # scope to the game(s) that actually use SHiFT codes
   ```
   Everything else (`interval_minutes`, `max_item_age_hours`,
   `max_pings_per_day`) is fine at its default; see
   `config.example.yaml`'s commented block for what each one does. Leave
   `allow_test_command` out (or `false`) in prod; it registers
   `/newsbot test-alert`, which is meant for the private test guild only.
4. **Redeploy** the normal way (`./scripts/deploy.sh` or the by-hand
   steps in §7); migration 002 (`alerted_codes`, `alert_state`) applies
   itself at startup the same way every other migration does; nothing
   extra to run by hand.
5. **Confirm it's live:** `/newsbot status` should show a "SHiFT alerts"
   field instead of "disabled", and startup shouldn't have sent a
   permission-check admin alert (§13 of `docs/design.md`) naming the new
   channel. The first real sweep seeds silently (shows `(seeding)`, posts
   nothing); that's expected, not a bug; see `design.md` §12's
   silent-seeding safeguard.

Rolling this back out is just `alerts.enabled: false` (or removing the
`alerts:` block entirely) and redeploying; migration 002 stays applied
(it's additive and harmless either way; see §8, Rollback, above), the
sweep simply stops running.

## 16. Upgrading the Droplet's OS (Ubuntu 20.04 → 22.04 → 24.04)

**Done 2026-09-27:** `cockwomble` is on 24.04. This section is kept as
the record of how, including the three things that went sideways (the
mirror, DNS, and IPv6), in case another Droplet ever needs the same trip.

Ubuntu 20.04 left standard support in May 2025, and this Droplet was still
on it, getting fewer security fixes every month. Ubuntu only
upgrades one LTS at a time, so this is two rounds: 20.04 → 22.04, then
22.04 → 24.04. Budget two to three hours, most of it watching progress
bars and answering the occasional prompt.

The bot runs entirely in Docker, so the host OS mostly doesn't matter to
it. The two things the upgrade *does* break are the ones that bit us last
time: the upgrader switches off third-party apt repositories (Docker's
included), and it can take Docker down with it until they're back.

**When:** start well after 09:15 Pacific. If the bot is down at 09:00,
it catches up and posts the digest when it comes back; SHiFT codes that
appear while it's down are picked up by the first sweep afterward, as long
as their posts are under 48 hours old.

### Before you start

1. **Snapshot the Droplet** in the DigitalOcean control panel
   (Droplet → Backups & Snapshots → Take Snapshot). This is the whole
   rollback plan, so wait for it to finish.
2. **Take a fresh database backup and stop the bot:**

   ```bash
   cd /opt/newsbot
   sudo ./scripts/backup.sh data/newsbot.db data/backups
   docker compose -f docker-compose.yml -f docker-compose.prod.yml down
   sudo systemctl stop newsbot-backup.timer
   ```

3. **Bring 20.04 fully up to date** (the release upgrader refuses to run
   otherwise), and reboot if it asks:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   [ -f /var/run/reboot-required ] && sudo reboot
   ```

4. Check `/etc/update-manager/release-upgrades` says `Prompt=lts`.
5. **Point apt at Ubuntu's official archive** instead of DigitalOcean's
   mirror. The upgrader doesn't recognize `mirrors.digitalocean.com`, asks
   whether to rewrite it anyway ("No valid mirror found"), and then, at
   least on 2026-09-27, aborted with "the essential package
   'ubuntu-minimal' could not be located." It rolls itself back cleanly,
   but it's an hour you don't need to spend:

   ```bash
   sudo cp /etc/apt/sources.list /etc/apt/sources.list.pre-upgrade
   sudo sed -i 's#http://mirrors.digitalocean.com/ubuntu/#http://archive.ubuntu.com/ubuntu/#' /etc/apt/sources.list
   sudo apt update
   ```

### Round 1: 20.04 → 22.04

```bash
sudo do-release-upgrade
```

- It runs inside `screen` and opens a spare SSH daemon on port 1022, in
  case your connection drops. If it does drop, SSH back in and run
  `sudo screen -r` to reattach. (If the Droplet's firewall blocks 1022,
  that backup door won't open; the `screen` reattach still works.)
- When it asks about modified config files, **keep your current version**
  (the default, `N`) unless you know you want the new one.
- It will say third-party sources (Docker, and the old `azure-cli` and
  WireGuard PPA entries) are disabled. That's expected; we re-add Docker
  below and don't need the others.
- When it offers to remove obsolete packages, skim the list; saying yes is
  normally fine. The old Python `docker-compose` 1.x may go, and we don't
  use it.
- Let it reboot at the end, then SSH back in and confirm:

  ```bash
  lsb_release -ds    # Ubuntu 22.04.x LTS
  ```

**Re-add Docker's repository.** This line reads the release name from the
OS, so the same command works after both rounds:

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

(The key at `/etc/apt/keyrings/docker.asc` survives the upgrade. If apt
complains about it, re-run the two `curl`/`chmod` lines from §1.)

Check Docker, compose, and the thread test from §1 (the `clone3` problem
that made us upgrade Docker in the first place):

```bash
docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}'
docker compose version
docker run --rm python:3.14-slim python -c "import threading; t = threading.Thread(target=lambda: None); t.start(); t.join(); print('threads ok')"
```

### Round 2: 22.04 → 24.04

Same routine, one release further:

```bash
sudo apt update && sudo apt full-upgrade -y
[ -f /var/run/reboot-required ] && sudo reboot
sudo do-release-upgrade
```

Same answers as round 1; 24.04's `needrestart` may show a "which services
should be restarted?" screen, and the defaults are fine. After the reboot,
`lsb_release -ds` should say 24.04. The upgrade converts apt sources to the
new `.sources` format: `docker.list` disappears, and third-party entries
land in files like `third-party.sources` with `Enabled: no`. Disabled
entries don't conflict with a fresh one, so re-run the same Docker
repository block (it writes `noble` this time) and the same three checks.

**Then check DNS before anything else.** On this Droplet it was broken
after round 2, and nothing complains until you try to reach a hostname:

```bash
for h in archive.ubuntu.com discord.com api.anthropic.com ghcr.io; do getent hosts $h >/dev/null && echo "ok   $h" || echo "FAIL $h"; done
```

If they all fail, it's almost certainly this: an older Droplet configures
its network through `/etc/network/interfaces` (ifupdown), not netplan, and
its DNS servers sit on a `dns-nameservers` line that the old `resolvconf`
helper used to pass along. 24.04 has no such helper, so `systemd-resolved`
runs with no upstream servers at all (`resolvectl status` shows no "DNS
Servers" line; `ls /etc/netplan/` is empty; `systemd-networkd` is
inactive). Give `systemd-resolved` the servers directly, using whatever
`grep -rh dns-nameservers /etc/network/` lists (Google's, here):

```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNS=8.8.8.8 8.8.4.4\nFallbackDNS=1.1.1.1 1.0.0.1\n' | sudo tee /etc/systemd/resolved.conf.d/droplet-dns.conf
sudo systemctl restart systemd-resolved
sudo systemctl restart docker   # so containers pick up the working resolver
```

The same setup also lost IPv6 (no global `inet6` address; `ping -6`
says "Network is unreachable"). The cause turned out to be the old
`/etc/network/interfaces.d/50-cloud-init.cfg` itself: it has two `iface
eth0 inet static` blocks (the public IP, then the anchor IP), and 24.04's
ifupdown gives up at the second one, never reaching the IPv6 block.

**The fix (done 2026-09-28): move the network onto netplan.** Snapshot
first and keep the DigitalOcean web console handy.

1. Gather what the Droplet should have: the old `.cfg`, `ip -br addr`,
   `ip route`, the interface MACs (`ip -br link`), and DigitalOcean's
   own view (`curl -s http://169.254.169.254/metadata/v1.json`, just the
   `interfaces` section). Check that cloud-init leaves the network alone
   (`network: {config: disabled}` under `/etc/cloud/`), or it'll write
   its own netplan file at boot and fight yours.
2. Write `/etc/netplan/50-droplet.yaml` (mode 600, `renderer: networkd`):
   `eth0` matched by MAC with the public IPv4, the anchor IP and the IPv6
   address, IPv4 and IPv6 default routes, `accept-ra: false`, and the DNS
   servers; `eth1` with the private address. `sudo netplan generate`
   validates it without touching anything.
3. `sudo netplan try`, and **from a second, fresh SSH session to the
   Droplet** (not a local terminal; ask me how I know) check addresses,
   `ping -6`, DNS and the bot before pressing Enter. It reverts on its own
   after 120 seconds if you don't.
4. Retire the old setup: `sudo systemctl disable networking`, move
   `50-cloud-init.cfg` out of `interfaces.d/` (it's in
   `/etc/network/retired-2026-09-28/`), and rename the
   `droplet-dns.conf` workaround above to `.retired`, since netplan now
   supplies DNS. Then reboot, and check it all comes back on its own.

### Bring the bot back

```bash
sudo systemctl enable --now newsbot-backup.timer
systemctl list-timers newsbot-backup.timer --no-pager
cd /opt/newsbot && ./scripts/deploy.sh
```

`deploy.sh` pulls the pinned image, starts the container, and waits for
healthy. Then, in Discord, `/newsbot status` should show the scheduler
alive and sources healthy; if it's past 09:00 Pacific and today's digest
hadn't posted yet, it posts within a couple of minutes.

`sqlite3` and the systemd unit files live outside the upgrade's blast
radius and should still be there; if `sqlite3` somehow went missing,
`sudo apt install -y sqlite3`.

### If it goes wrong

Restore the snapshot from the control panel, which puts the Droplet back
exactly as it was before round 1, bot and all. Then `./scripts/deploy.sh`
and carry on with your day, having learned something about Ubuntu that you
will forget by the next upgrade.

## 17. Upgrading to v2.0

v2.0.0 (`docs/design.md` §13) is a breaking config change: the combined
digest channel is gone, each game posts to its own channel, and SHiFT code
alerts move to a dedicated channel of their own. Nothing about the
database changes in a way that needs a restore; migration 003 is
additive, same as every migration before it, but `config.yaml` needs
real edits before the new image will even start, so do this on purpose,
not as a surprise the morning after a routine `deploy.sh`.

The config edit below happens on a *copy*, not on the live `config.yaml`,
and the copy only becomes `config.yaml` in the last step, right before
`deploy.sh` runs. That ordering matters: if something restarted the
container in between (a host reboot, a manual `docker compose up`), the
still-running `1.3.0` image would find a v2-shaped `config.yaml`
(missing `digest.channel_id`, carrying an `alerts.channel_id` it doesn't
recognize) and crash-loop until someone noticed. Editing a copy means
`config.yaml` stays v1.3.0-valid right up until the moment the new image
is actually the one that's going to read it.

1. **Pick a quiet moment and back up the old config.** Right after that
   day's 09:00 digest has posted, and well before the next one:
   ```bash
   cd /opt/newsbot
   cp config.yaml config.v1.yaml
   ```
   Note the currently-pinned `TAG` in `.env` too (it should read
   `TAG=1.3.0` if you've kept up with releases), that, plus
   `config.v1.yaml`, is everything the rollback at the end of this section
   needs.
2. **Create four channels:** one per game (`#borderlands4`, `#palworld`,
   `#diablo4`, or whatever names fit your server) and one for SHiFT codes
   (`#shift-codes`). Set the bot role's permissions per channel:
   - Each game channel: **View Channel, Send Messages, Embed Links**.
   - The SHiFT codes channel: **View Channel, Send Messages, Mention
     @everyone, @here, and All Roles**.
   Copy each channel's id (right-click → Copy Channel ID; Developer Mode
   has to be on in Discord's own settings for that option to show up).
3. **Copy `config.yaml` to `config.v2.yaml` and edit the copy:** remove
   `digest.channel_id` entirely, give every topic its own `channel_id`,
   and add `alerts.channel_id` if SHiFT alerts are on. `config.yaml`
   itself stays untouched here.
   ```bash
   cp config.yaml config.v2.yaml
   ```
   For example, going from a v1 shape to v2:
   ```yaml
   # before (v1, config.v2.yaml starts as a copy of this)
   digest:
     channel_id: 100000000000000001
     time: "09:00"
     timezone: "America/Los_Angeles"
   alerts:
     enabled: true
   ```
   ```yaml
   # after (v2.0, what config.v2.yaml should look like once edited)
   digest:
     time: "09:00"
     timezone: "America/Los_Angeles"
   topics:
     - key: borderlands4
       name: "Borderlands 4"
       channel_id: 100000000000000010   # #borderlands4
       # ...aliases, entities, search_queries unchanged...
     - key: palworld
       name: "Palworld"
       channel_id: 100000000000000011   # #palworld
     - key: diablo4
       name: "Diablo IV"
       channel_id: 100000000000000012   # #diablo4
   alerts:
     enabled: true
     channel_id: 100000000000000013     # #shift-codes
   ```
   `topics[].channel_id` is required for every topic now (not just the
   ones with alerts); `alerts.channel_id` is required only if
   `alerts.enabled` is `true`.
4. **Validate `config.v2.yaml` before touching prod's real config**:
   `load_config` is the same check the container runs at startup, and
   it's a lot cheaper to fail here than mid-deploy:
   ```bash
   python -c "from newsbot.config import load_config; load_config('config.v2.yaml')"
   ```
   (from a checked-out copy of this repo with the venv active, pointed at
   a copy of the edited `config.v2.yaml`, not the live file on the
   Droplet, and never via `docker compose ... config` against the real
   `.env`; see §4 above and `CLAUDE.md` for why that command specifically
   is off the table.) A `ConfigError` here lists every problem at once,
   same as it would on a real startup crash.
5. **Wait for the `v2.0.0` tag's build**, same as any other release (§7):
   `gh run list --limit 3` shows when it's done, or watch for
   `manifest unknown` if you jump the gun.
6. **Swap the config in and deploy in the same breath**: pin `TAG=2.0.0`
   in `.env`, then immediately:
   ```bash
   cd /opt/newsbot
   mv config.v2.yaml config.yaml
   ./scripts/deploy.sh
   ```
   Doing the `mv` any earlier is what step 3's warning above is about;
   doing it right before `deploy.sh` means the window where `config.yaml`
   is v2-shaped but the running container is still `1.3.0` is as close to
   zero as this process gets. `deploy.sh` takes a backup first (per §9)
   and applies migration 003 at startup; both automatic, nothing extra
   to run by hand.
7. **Check:** no startup permission-check admin alert (§13 of
   `docs/design.md`: one alert here would name exactly which channel and
   permission is missing); `/newsbot status` healthy; `/shift codes`
   lists the codes that were already in `alerted_codes` before the
   upgrade, with any old roundup-only codes marked "from a roundup". The
   next morning, check that each game channel got its own message and
   that the admin run report's jump links point at the right channels.
8. **Rollback,** if needed: restore `config.v1.yaml` over `config.yaml`,
   set `TAG` back to `1.3.0` in `.env`, and deploy again (§8, Rollback,
   above, has the general form of this). No database restore is
   necessary either direction; migration 003 stays applied and
   harmless, the same as every additive migration before it.

## 18. Enabling the lounge (v2.2)

v2.2.0 (`docs/design.md` §14) adds a bot-posted welcome for new members and
a daily quote, both in `#the-speakeasy-lounge`. Migration 004 is additive,
and nothing else about the running bot changes. The order below matters:
the portal switch has to come first, and Discord's own welcome has to stay
on until the bot's works.

1. **Developer Portal, prod app: Bot, Privileged Gateway Intents, Server
   Members Intent ON.** Do this before anything else. The bot asks for the
   intent whenever `lounge.welcome.enabled` is true; if the switch is off,
   it prints a message, waits 10 minutes (so a restart loop can't spend
   Discord's login limit) and exits. The daily quote alone needs no
   privileged intent.
2. **Wait for the `v2.2.0` tag's build**, same as any other release (§7).
3. **Settle the quote sources.** Omit `lounge.daily_quote.sources` to use
   the built-in public-domain list, or write your own. For a file on the
   Droplet, put it at `/opt/newsbot/data/quotes.txt`, readable by uid 10001,
   and refer to it as `file: /data/quotes.txt` (a relative path lands in the
   read-only image). Modern copyrighted works are the owner's call; see the
   copyright note in `config.example.yaml`.
4. **Give the bot role View Channel and Send Messages** in
   `#the-speakeasy-lounge`, and copy the channel id.
5. **Edit a copy of `config.yaml`, not the live file,** adding the
   `lounge:` block from `config.example.yaml` (§14 of the design has the
   rules). Validate the copy the way §17 step 4 does, with
   `load_config` from a checkout (never `docker compose ... config`
   against the real `.env`), then `mv` it into place.
6. **Set `TAG=2.2.0` and run `./scripts/deploy.sh`.** It takes a backup,
   and migration 004 runs at startup. **Never deploy between 09:00 and
   09:15 America/Los_Angeles** (`deploy.sh` refuses; `--force` skips the
   check, so don't), and **avoid about 07:55 to 08:05**, around the quote
   time: a restart there can skip that day's quote, since a missed one is
   never caught up.
7. **If the container is restart-looping with the intent message, stop it
   right away** (`docker compose -f docker-compose.yml -f
   docker-compose.prod.yml stop`). The 10-minute wait softens the loop but
   doesn't excuse it. Turn the portal switch on, then start it again.
8. **Check:** no startup permission-check admin alert; `/newsbot status`
   healthy; have an alt account join and confirm the bot's welcome
   appears (Discord's own will appear too, this once).
9. **Then** turn off Discord's built-in welcome: Server Settings, System
   Messages, "Send a random welcome message when someone joins". If the
   System Messages Channel was pointed at the lounge, point it back at the
   admin channel. Doing this last means there's never a gap with no
   welcome.
10. **The next morning,** confirm the quote posted with its `From
    Wikiquote:` link (for Wikiquote sources). `/newsbot quote-now` posts
    one on demand if you'd rather not wait; the scheduled run then skips
    that day.

**Rollback.** Set `TAG=2.1.1` in `.env` and deploy again (§8). Switch
Discord's built-in welcome back on (and the System Messages Channel back to
the lounge, if you moved it). No database restore is needed: migration 004
is additive and v2.1.1 never reads its tables. The `lounge:` block can stay
in `config.yaml` (v2.1.1 ignores unrecognized top-level keys), and so can
the portal intent and the cache directory `/data/newsbot-lounge-cache/`
(assuming the default `newsbot.db` path).
