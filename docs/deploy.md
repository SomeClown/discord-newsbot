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

**Since v3.0 this is a public bot's runbook,** and §19 ("Upgrading to v3.0")
is the one to read for the upgrade from v2.2 and for going public. Sections 3
to 18 are still accurate for what they describe, with the notes inline where
v3 changed something. Anything below that says "the guild" means the bot is
in one server; after §19 it's in many.

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
   - **Public Bot: OFF** (nobody outside this server should be able to add
     it). It stays off until §19's last step, which is the one that turns it
     on.
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

Edit `config.yaml`: replace the placeholder IDs (`home_guild_id`, your own
server, and `owner_channel_id`, a channel in it for the bot's health alerts)
with real ones. The v3 example holds only global settings and the game
catalog; each server's channels, digest time and SHiFT settings are set from
Discord with `/newsbot setup` afterwards, so there's nothing per-server to put
in the file. (A config in the older v2 shape, with `guild_id`, `topics:` and
a `channel_id` per game, still works: v3 imports it into the database once.
That's the upgrade path in §19, and it's why the production file on the
Droplet still looks like that.) The example file's comments explain what each
setting is for.

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
friend's digest posts at 09:00; recreating the container in the middle of
a run risks an interrupted post (v3 resumes it once the row has been quiet
for 10 minutes, which beats losing it, but it's still a delay you chose).
**Also avoid about 07:55 to 08:05**, around the lounge quote at 08:00: a
restart there can skip that day's quote, since a missed one is never caught
up. Everything else is fine, and once other servers have set their own times
there is no perfectly quiet moment anymore, only a quieter one.
`scripts/deploy.sh` enforces the 09:00 window itself (see above) and knows
nothing about 08:00; doing it by hand, just check a clock.

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

**Rolling back past v3.0.0 (the public app) is a `TAG` change too, but with
conditions,** and only while the friend's server is the only one using the
bot. Migrations 005 to 008 rebuild two tables as supersets and add the rest, so
v2.2.0 runs against them unmodified. After the bot has gone public it is no
longer safe, and there are two more gotchas on the way back in. All of it is in
§19, "Rollback"; read that before you need it.

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

Digests are per server now, and each server has its own row for its own local
day. Start with what the server's own admin sees:

```
/newsbot status
```

It shows the last digest (its date and status, with a jump link for each game
that posted), the next due time, source health for the games it follows, and
the newest problem notes. A digest that never came, or came as `failed` or
`partial`, is recoverable by an admin of that server with:

```
/newsbot run-now
```

The bot retries on its own first. A `failed` row with nothing posted is
retried every 10 minutes, at most 3 attempts. A `pending` row left by a
process that died is picked up again once it has been quiet for 10 minutes
(so a deploy mid-digest can delay a resume by that long), and only the games
that hadn't landed are posted, because progress is written to the row as each
game posts. A row that has used all its attempts is marked `failed` and that
server is told once, in its admin channel (or in `/newsbot status`, if it has
none); nothing retries it after that.

`run-now` asks for confirmation if today's digest already posted, or is
`pending`, or `failed` with something posted. **A confirmed `run-now` reposts
every game,** including the ones that already went out (there's no "only what's
missing" mode for a forced run; the automatic resume is the one that skips
what landed), and it can be used once per server per 10 minutes. If the
failure was a transient upstream issue (a source timing out, a Claude API
hiccup), this is usually all that's needed anyway. If it fails again, check
the logs (§12) for what's going wrong before retrying further.

You, as the bot's owner, don't see anyone's channels. `/owner servers` in your
home server shows counts only (servers, set up, free and comped, today's
digests by status, SHiFT servers, servers with permission problems, month
spend), and the daily owner report carries the same one-liner. To look at one
server's rows, read the database without writing to it:

```bash
cd /opt/newsbot
sudo sqlite3 -readonly data/newsbot.db \
  "SELECT guild_id, run_date, status, attempts, error_notes FROM digests ORDER BY id DESC LIMIT 20;"
```

Don't edit rows by hand to "fix" a digest. The claim guard is the thing that
keeps a server from getting two posts in a day, and it's built to trust those
rows.

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
`/new/.rss`. Collection runs hourly now, so a source that fails 12
collections in a row (half a day) sends one alert to your admin channel,
and the daily owner report lists every failing source; `/newsbot status`
in a server shows "N of M sources ok" for its games. That's the designed
behavior, not a fresh problem each time it happens. If it persists, that's an
owner decision (drop Reddit as a source, or accept the gap) per
`docs/design.md` §4, not something to patch around here. The collector spaces
Reddit feeds 35 seconds apart, so 15 subreddits add about 9 minutes to every
hourly pass; a pass that takes more than half the interval is logged
as a warning.

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

**As of v3.0 this is per server, and an admin does it from Discord:**
`/newsbot shift channel:<#channel> enabled:true ping:<none|everyone|role>`.
The bot's role needs the mention permission below only if the ping is
`everyone`, or a role that isn't mentionable, and the command's reply says
what's still missing. Steps 1 and 2 below are still how you set up your own
server's channel. Steps 3 to 5 describe the old `alerts:` config route; on v3
that block is read only by the one-time import (§19), and a config that
enables it for a server that's already been imported does nothing.

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
   `max_pings_per_day`) is fine at its default. (`allow_test_command` used to
   register a dev-only `/newsbot test-alert`. That command was removed in v3
   and the key is ignored, with a startup warning if it's true.)
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
the first minute after it's back and ready posts whatever digests came due
meanwhile; SHiFT codes that appear while it's down are picked up by the
first collection pass afterward, as long as their posts are under 48 hours
old.

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

**As of v3.0 the lounge settings live in the database** (a `guild_lounge`
row, written by the one-time import in §19), for the friend's server only.
There's still no command to edit them. While a `lounge:` block stays in
`config.yaml`, v3 re-applies it to that row at every startup and logs when
something changed, so editing the block and restarting still works the way
it did. Delete the block and the row stands alone. The steps below were the
v2.2 rollout and are kept as the record of it.

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
    Wikiquote:` link (for Wikiquote sources). `/lounge quote-now` (it was
    `/newsbot quote-now` before v3, and exists only in a server whose lounge
    has the quote on) posts one on demand if you'd rather not wait; the
    scheduled run then skips that day.

**Rollback.** Set `TAG=2.1.1` in `.env` and deploy again (§8). Switch
Discord's built-in welcome back on (and the System Messages Channel back to
the lounge, if you moved it). No database restore is needed: migration 004
is additive and v2.1.1 never reads its tables. The `lounge:` block can stay
in `config.yaml` (v2.1.1 ignores unrecognized top-level keys), and so can
the portal intent and the cache directory `/data/newsbot-lounge-cache/`
(assuming the default `newsbot.db` path).

## 19. Upgrading to v3.0 (the public app)

v3.0.0 (`docs/design.md` §15) turns the bot into one public app that any server
can install. Your server stays the first, comped one: the first v3 start moves
its setup from `config.yaml` into the database, once, and nothing visible
changes for it. This section is the whole trip: preparing, deploying,
checking, going public, and getting out if it goes wrong. The plan behind it
is `docs/plans/2026-09-30-public-app.md` (§8 is the test-guild checklist that
comes first, and §9 is where these notes started).

Two things to know before the first step.

- **Prod is still on v2.2.0** as I write this (2026-10-01), and v3.0.0 hasn't
  been released or run against the real Droplet. The commands below are the
  plan, checked against the code, and not a transcript. If a step surprises
  you, stop and read the log before the next one.
- **Never run `docker compose ... config`, or anything else that resolves
  `env_file`, against the real `.env`.** It prints every secret. Nothing
  below needs it. Where a step wants to validate a config file, it runs the
  image with only that file mounted, which never sees `.env`.

### Prepare (any time before the day)

1. **Do the test-guild checklist first** (plan §8, dev bot, `config.dev.yaml`).
   It includes the import against a copy of the friend's real config and the
   rollback drill. Then merge, tag `v3.0.0`, and **wait for the tag's CI build**
   (`gh run list --limit 3`) before step 6 or step 10: a deploy that pulls
   too early fails with `manifest unknown` and touches nothing.

2. **Give the bot a home that isn't the friend's server.** Invite the prod bot
   to your own test server (`1552824311608512532`, decision D9) and make a
   channel there just for prod alerts (a separate one from dev's, so the two
   don't mix). Give the bot's role View Channels and Send Messages in it, and
   copy the channel's id. Why it matters: your channel (the config key
   `owner_channel_id`) is where bot-wide news lands (the import notice, a source that's been dead for half a day, a
   crashed job, the daily owner report), and `/owner servers` lives in your
   home server. Without `home_guild_id` set, it defaults to the old `guild_id`,
   which is the friend's server, so all of that would show up in front of the
   friend. The friend's own admin channel is a separate thing and stays where
   it is: it keeps the `admin_channel_id` key it has today, and the import
   copies that into the friend's server's row.

3. **Write down three values from the current `config.yaml`:** `guild_id` (the
   friend's server), `admin_channel_id` (the friend's admin channel), and the
   `TAG` in `.env` (`2.2.0`). You need all three if you roll back. The config
   keeps the first two as they are.

4. **Make a copy of the config and edit the copy.** The live `config.yaml`
   stays v2.2-valid until step 10, for the same reason §17 gives:
   if something restarted the container in between, you'd want it to find a
   file it can read.

   ```bash
   cd /opt/newsbot
   cp config.yaml config.v3.yaml
   ```

   In `config.v3.yaml`, add three keys and change nothing else. Leave
   `admin_channel_id` exactly as it is: it stays the friend's admin channel,
   and the import copies it into the friend's server's row.

   ```yaml
   home_guild_id: 1552824311608512532     # your test server (D9)
   owner_channel_id: <the prod-alerts channel from step 2>
   comped_guild_ids: [<the old guild_id, the friend's server>]
   ```

   - `home_guild_id` and `owner_channel_id` are described in step 2. The
     owner channel has to be in the home server; the bot refuses to post
     owner alerts anywhere else and says so at startup if it can't. The
     friend's `admin_channel_id` is a different key for a different
     server, so the two never fight over one value.
   - `comped_guild_ids` holds **the friend's server only, never the home
     server** (decision D12). The import already comps that server, so this
     changes nothing today. It matters if the friend ever removes the bot and
     re-invites it: without it, the new row would come back free. Your home
     server stays out because the AI prompt names every game any comped server
     follows, so a second comped server following other games would change
     the friend's prompt, and a changed prompt needs an owner-reviewed
     `/newsbot preview` before it ships.
   - Don't add a `catalog:` yet. Leave the old `topics:` and `sources:` as
     they are: v3 derives its catalog from them, so the upgrade changes
     nothing about what gets collected. The 15-game catalog comes in step 16.

5. **Check the catalog order.** The comped summarizer's prompt lists the games
   in catalog order, and the derived catalog follows `topics:` order, so it has
   to read Borderlands 4, Palworld, Diablo IV with their existing keys. A
   reorder changes the friend's prompt (an owner `/newsbot preview` first).

   ```bash
   grep -n '^  - key:' config.v3.yaml
   ```

   Expect `borderlands4`, `palworld`, `diablo4` in that order. (When step 16
   adds a `catalog:`, that grep will print two lists, the old `topics:` and
   the new catalog. Check the order in both.)

6. **Validate the copy with the v3 image.** `load_config` is the same check the
   container runs at startup, and a `ConfigError` lists every problem at once.
   Mounting only the one file means the real `.env` is never involved:

   ```bash
   docker run --rm -v "$PWD/config.v3.yaml:/app/config.yaml:ro" \
     ghcr.io/someclown/discord-newsbot:3.0.0 \
     python -c "from newsbot.config import load_config; c = load_config('/app/config.yaml'); print([g.key for g in c.catalog], c.home_guild_id, c.comped_guild_ids)"
   ```

   It should print the three game keys, your test server's id and a list
   holding the friend's. A warning that `BRAVE_API_KEY` isn't set is fine: the
   container here has none, and the real one does.

7. **Check the database for orphan rows, on a copy.** Migration 005 rebuilds two
   tables and refuses to run if the database already holds a foreign-key
   violation: it rolls back, stays at version 4, names the table, and the bot
   doesn't start. The dev database passed; prod hasn't been asked yet. The
   copy is also your pre-upgrade backup, taken with sqlite's own `.backup`
   because `cp` on a live WAL database can miss what's still in the log:

   ```bash
   cd /opt/newsbot
   sudo sqlite3 -readonly data/newsbot.db ".backup 'data/newsbot.pre-v3.db'"
   sudo sqlite3 -readonly data/newsbot.pre-v3.db "PRAGMA integrity_check;"
   sudo sqlite3 -readonly data/newsbot.pre-v3.db "PRAGMA foreign_key_check;"
   ```

   The first prints `ok`. The second prints nothing when the database is
   clean. If it prints rows (each is `table|rowid|parent table|key`), those
   are orphans: stop the bot, delete them (or fix the parent), run the check
   again, and only then carry on.

8. **Keep the old config for the way back:**

   ```bash
   cp config.yaml config.v2.yaml
   ```

   (`config.v3.yaml` is the new one; `config.yaml` is still the live v2.2 file
   at this point. `config.v2.yaml` is the same as `config.yaml` and the thing
   to restore on a rollback, though step 4 leaves the existing keys alone.)

### Deploy

9. **Pick the moment.** After that day's 09:00 digest has posted (the import
   marks that day's digest as the server's, so nothing posts twice on upgrade
   day), and outside both windows: **never 09:00 to 09:15
   America/Los_Angeles** (`deploy.sh` refuses; don't `--force` past it), and
   **avoid about 07:55 to 08:05**, around the lounge quote (a missed quote is
   never caught up).

10. **Pin the tag, swap the config in, and deploy, in that order and in one
    sitting:**

    ```bash
    cd /opt/newsbot
    # edit .env with an editor: TAG=2.2.0 becomes TAG=3.0.0 (don't cat the file)
    mv config.v3.yaml config.yaml
    ./scripts/deploy.sh
    ```

    `deploy.sh` takes its own backup, pulls the image, recreates the container
    and waits for `healthy`. On the first start the bot applies migrations 005
    to 008, runs the import, and starts. A migration failure, a `ConfigError`
    or a failed import each exit with a message and a non-zero status, and
    `restart: unless-stopped` will start it again into the same wall. If you
    see that, stop it (`docker compose -f docker-compose.yml -f
    docker-compose.prod.yml stop`) and see "Rollback" below.

### Verify

11. **Read the log.** These are JSON lines; `grep` is enough:

    ```bash
    cd /opt/newsbot
    docker compose -f docker-compose.yml -f docker-compose.prod.yml logs --since 15m \
      | grep -iE 'import:|these keys|startup finished|owner alerts|synced|reconcile|guild join|collection pass|comped'
    ```

    You want, in roughly this order:

    - `import: guild <the friend's id>, comped, set up; digest at 09:00 America/Los_Angeles; admin channel <id>`
    - `import: 3 games: borderlands4 -> <channel>, palworld -> ..., diablo4 -> ...`
    - `import: SHiFT on in channel <id>, ping everyone, today's pings spent N (day ...)`
    - `import: lounge in channel <id>, welcome on, quote on at 08:00, ...`
    - `import: adopted N digest rows, M already-posted SHiFT codes, K lounge quote rows`
    - `these keys are now only read by the import and can be deleted from config.yaml: ...`
    - `guild join: new server <your test server's id>` for each server the bot
      is in that had no row yet (your test server at least; that's expected,
      and it's when its one-time hello goes out), and `synced N commands` with
      a `scope` of `global` and of the friend's server id
    - `startup finished; per-server digests are on`
    - about 30 seconds later, `collection pass finished` with a summary like
      `17/19 sources ok, 214 new items, 0 new codes`, and then one every hour

    You do **not** want `owner alerts won't arrive` (an ERROR, also printed to
    stderr): it means `home_guild_id` or `owner_channel_id` is wrong or
    the channel isn't one the bot can send in. It names the fix. The bot keeps
    running, because the friend's digests are fine, but you're deaf until you
    correct it.

    The import happens once. A restart later logs `import: already happened on
    <date>` instead.

12. **Check the database, read-only:**

    ```bash
    sudo sqlite3 -readonly data/newsbot.db "PRAGMA user_version;"
    sudo sqlite3 -readonly data/newsbot.db \
      "SELECT guild_id, tier, set_up, digest_time, timezone, imported_at IS NOT NULL FROM guilds;"
    sudo sqlite3 -readonly data/newsbot.db "SELECT guild_id, game_key, channel_id FROM guild_games;"
    sudo sqlite3 -readonly data/newsbot.db "SELECT guild_id, run_date, status FROM digests ORDER BY id DESC LIMIT 3;"
    ```

    Version `8`. A row for the friend's server (`comped`, set up, imported), a
    second row for your test server (`free`, not set up), three games, and
    today's digest still `ok` with the friend's server id on it.

13. **Check Discord.** Your prod-alerts channel got one line: "Imported the v2
    setup for this server (3 games, SHiFT, lounge); the details are in the
    log." Your test server got the bot's one-time hello, with no mentions. In
    the friend's server, `/newsbot status` shows it as comped, 09:00
    America/Los_Angeles, three games with "N of M sources ok", SHiFT on with
    `@everyone`, and today's digest with jump links. `/owner servers` works
    in your home server and nowhere else.

    The global commands (`/news`, `/shift`, `/newsbot`) can take **up to an
    hour** to show up. What the first start does immediately is replace v2's
    per-server copies of them in the friend's server (a guild sync that carries
    only `/lounge` is what clears them), so for a while that server may be
    short a command or two. Don't restart to fix it: the sync is skipped when
    nothing changed, and a restart changes nothing but the uptime.

14. **Don't run `/newsbot setup` for your test server in prod yet.** A set-up
    server gets digest rows of its own, and v2.2's double-post guard keys on the
    date alone, so after a rollback to v2.2 it can mistake another server's row
    for the friend's "today's digest" and skip the friend. Keep your test
    server un-set-up (the bot sits there and says nothing) until the friend's
    server has been through a couple of days on v3.

15. **Watch two digest days and one SHiFT alert.** The next morning: the
    lounge quote at 08:00 (it's scheduled per server now, from the database
    row), the digest at 09:00 with AI summaries in each game's own channel, and
    the run report in the friend's admin channel. SHiFT works as before: the
    detector found the friend's server's codes in the shared collection and
    posts them with the server's own ping setting and daily cap. A
    community-only code posts unpinged; if a second independent source (or an
    official or press one) sees it within 24 hours, there's a short pinged
    follow-up that spends one of the day's pings. Also expected, and on
    purpose: Brave web search runs once a day, only for the games the friend
    follows, so its cost is what it was.

### Going public

Only after step 15 looks clean. The order is deliberate: the catalog and the
pages first, the switch last.

16. **Add the 15-game catalog to prod's config.** Copy the whole `catalog:` and
    `shared_sources:` blocks, and the `web_search:` block, from
    `config.example.yaml` into a copy of the live `config.yaml` (leave the old
    keys where they are: with a `catalog:` present they're read only by the
    import, and v2.2 ignores `catalog:`, so a rollback still works). **All
    three blocks, not just the catalog:** with a `catalog:` present v3 stops
    deriving anything from the old `sources:`, so a file with only the catalog
    would quietly lose the shared press feeds, 2K Newsroom and Brave. Keep the
    first three games in v2's order with their existing keys (step 5 again,
    here the check bites hardest), and keep the source names prod already uses
    for them so source health carries over. If you ever tuned `digest.lookback_hours`,
    `digest.max_items_per_topic`, `digest.subject`, `digest.report_to_admin` or
    anything under `alerts:` away from its default, carry it into `collection:`,
    `ai:`, `run_report:` and `shift:` (the example shows each): with a `catalog:`
    present, the old keys no longer feed those blocks. Validate the copy the way step 6
    does, swap it in, and deploy outside both windows. Watch one collection
    pass: how long it took (15 subreddits at the 35 second Reddit spacing add
    up to about 9 minutes), and that source alerts don't flood your channel.

17. **Publish the privacy policy and terms.** They live in `site/` and GitHub
    Pages publishes them from `main` after the merge. Confirm the live pages
    at `https://someclown.github.io/discord-newsbot/privacy.html` and
    `/terms.html`, check the "Effective" date says what you want it to, and
    that the support contact (`justsomeclown@gmail.com`) is on both. The
    Developer Portal's privacy and terms URL fields, if you've filled them in,
    point at these.

18. **Take a dated backup, and keep it.** Right before the switch, outside the
    7-day rotation (the rotation only touches `data/backups/newsbot-*.db`, so a
    file next to the database is safe from it):

    ```bash
    cd /opt/newsbot
    sudo sqlite3 -readonly data/newsbot.db ".backup 'data/newsbot.pre-public-YYYYMMDD.db'"
    sudo sqlite3 -readonly data/newsbot.pre-public-YYYYMMDD.db "PRAGMA integrity_check;"
    ```

    (Put today's date in for `YYYYMMDD`.) Don't delete it while the app is
    young; it's the one thing that lets you undo going public.

19. **Flip the switch.** In the Discord Developer Portal, prod app:
    Installation: guild install only; scopes `bot` and `applications.commands`;
    default permissions View Channels, Send Messages and Embed Links
    (Mention @everyone stays off the default; an admin who picks an
    `everyone` or role ping grants it, and `/newsbot shift` says so). Then
    switch **Public Bot** on and share the install link. Keep the Server
    Members intent on, since the friend's lounge welcome needs it. Bots in
    about 75 servers have to go through Discord's verification, and the
    intent gets looked at then; that's a later chapter.

### Rollback

**Before step 19 (still only the friend's server):** a rollback is a `TAG`
change plus the old config. No database restore is needed.

1. In `.env`, set `TAG=2.2.0`.
2. Put the old config back: `cp config.v2.yaml config.yaml`. (v2.2 would
   ignore the new keys, and `admin_channel_id` never changed, so its run
   reports and alerts go where they always did.)
3. `./scripts/deploy.sh` (outside both windows, as always).

What to expect:

- **One thin digest the next morning.** v3 has been collecting hourly, so v2.2's
  first run finds most items already stored and has little new to say.
  It's a one-day dip, not a bug.
- **The new tables stay in the database,** and v2.2 ignores them. The
  migrations are supersets that v2.2 runs against unchanged (the compatibility
  tests run v2.2's SQL against a v8 database).
- **Rolling forward again is safe,** with one required step. The first v3 start
  already stored a hash of the commands it registered, per scope. v2.2 re-syncs
  its own per-server commands into the friend's server while it runs but knows
  nothing about that hash, so on the way back the stored hash still matches
  and v3 would skip the sync: nothing sent, and the old duplicate `/newsbot`,
  `/news` and `/shift` stay in the friend's menu. Clear the hashes first, with
  the bot stopped:

  ```bash
  cd /opt/newsbot
  docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
  sudo sqlite3 data/newsbot.db "DELETE FROM app_state WHERE key LIKE 'commands:%';"
  sudo chown -R 10001:10001 data    # sqlite3 ran as root; the container isn't
  ```

  Then set `TAG=3.0.0`, put the v3 config back, and deploy. Nothing is
  re-imported (the import record in the database says it already happened),
  and any digest v2.2 posted in the meantime is adopted by the friend's
  server on startup, which closes the double-post.

**After step 19 (other servers have joined):** don't roll back to v2.2 if you
can fix forward. Two reasons:

- **Never force `run-now` on v2.2 once the bot is public.** v2.2 can't tell the
  friend's digest from a stranger's (its guard keys on the date alone), and a
  forced run overwrites whichever row it finds.
- A `TAG` rollback leaves every other server with a bot that ignores what
  they set up. Restoring `newsbot.pre-public-*.db` brings the old database
  back and drops every server's settings made since. Do it only if you accept
  that, and expect to apologize.

### Odd things, and what they mean

- **A `/lounge`, `/newsbot` or `/news` that appears twice, or is missing for
  an hour or so.** Global command changes take up to an hour to show, and a
  v2 guild copy only goes away with a sync. If a stale **global** set ever
  turns up (the dev app has never synced globally, and prod's v2 synced per
  server, so none is expected), clear it once from your own machine with
  the bot's venv, the *right* token for that app exported in the shell for
  this one command (not by sourcing `.env`):

  ```bash
  python - <<'PY'
  import asyncio, os, discord
  from discord import app_commands

  async def main():
      client = discord.Client(intents=discord.Intents.none())
      tree = app_commands.CommandTree(client)
      async with client:
          await client.login(os.environ["DISCORD_TOKEN"])
          print(len(await tree.sync()), "global commands left")  # an empty tree wipes the set

  asyncio.run(main())
  PY
  ```

  I haven't run that one (no token to spare), so treat it as a sketch, check
  the command list in a server afterward, and then clear the stored hash
  (`DELETE FROM app_state WHERE key = 'commands:global'`, bot stopped, as
  above) so the bot registers its set again on the next start.
- **"I can't manage this server because I'm not in it."** A server installed
  the commands without the bot user (an invite with only the
  `applications.commands` scope). Its admin needs to add the bot properly.
- **A server's digest didn't post.** Its admin sees why in `/newsbot status`
  (or its own admin channel). You see counts in `/owner servers` and the
  daily owner report. See §10 for recovery.
- **The container restart-loops right after the deploy.** One of the three
  startup failures in step 10. `docker compose ... logs --tail 50` shows
  which, and it says what to fix. Stop it, then roll back as above.
