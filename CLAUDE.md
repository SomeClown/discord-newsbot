# discord-newsbot

A Discord bot that reads the internet every morning so a ~50-person server
doesn't have to: daily AI-summarized news digest for Borderlands 4, Palworld,
and Diablo IV, plus `/news recent` and `/news search` commands.

- **Design (source of truth):** `docs/design.md`; §15 (with its "As built") is the current, public-app design, and it supersedes the single-server parts of the older sections (marked where they apply).
- **Current plan:** `docs/plans/2026-09-30-public-app.md` (v3.0.0, 18 tasks, all done; D1 to D15 are the owner's decisions). The v1 plan, `docs/plans/2026-09-23-v1-implementation.md`, is history: the owner accepted every SPEC-DEV default in its section 4 on 2026-09-23 (digest at 09:00 America/Los_Angeles).
- **Tooling:** Python 3.14, plain `venv` + `pip` (no uv; the owner knows pip and that's the point).
  Setup: `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e . --no-deps`.
  `requirements.txt` is the lock file; regenerate it with `scripts/lock.sh`, never by hand.
- **Gate:** `ruff check . && ruff format --check . && pytest -q` (venv active). About 7,900 tests, about 3 minutes; expect only the 2 APScheduler xfails, and check pytest's real exit code. The suite never touches the real network: `tests/network_guard.py` answers every DNS lookup with a test address and makes a real connection raise, *and* records the attempt so a test fails at teardown even if the code under test swallowed the error (the collectors catch `Exception`, so the raise alone once let a blocked connection pass as an ordinary "source failed"). `@pytest.mark.real_network` opts a test out; `blocked_attempts.acknowledge()` is for a test that blocks one on purpose.
- Any prompt change needs an owner-reviewed `/newsbot preview` before merging.
- **Status (2026-10-02):** Production runs **v3.0.4** (v3.0.0 deployed 2026-10-02 at 13:41 PT, v3.0.1 about 16:15 PT, v3.0.2 to v3.0.4 on 2026-10-04; `TAG=3.0.4` in the Droplet's `.env`; the friend's server imported once (comped, 3 games, SHiFT `@everyone`, lounge), the owner's test server 1552824311608512532 joined as the home server, owner alerts in `#prod-alerts` 1555677520806944859; rollback copies on the Droplet: `config.v2.yaml` and `data/newsbot.pre-v3.db`, with `TAG=2.2.0`; don't `/newsbot setup` the home server until a couple of v3 days have passed). Before that, v2.2.0 (`TAG=2.2.0`, with `NEWSBOT_CONTACT` set there and in `.env.dev`; the v1 config is kept at `/opt/newsbot/config.v1.yaml` for rollback per `docs/deploy.md` §17). Each game posts one embed per day to its own channel (`topics[].channel_id`); SHiFT code alerts and unpinged roundups go to `#shift-codes` (`alerts.channel_id`) with `@everyone` rules unchanged; admin health messages and run reports go to `admin_channel_id`; members have `/shift codes`. Release history: v1.1.0 Anthropic SDK 1.8 + IGN removed + upgrade tooling; v1.1.1 status shows configured sources only; v1.2.0 SHiFT code alerts; v1.3.0 admin run reports; v2.0.0 per-game channels, SHiFT channel, `/shift codes`, startup permission check (breaking config change); v2.1.0 run-your-own-copy support (`docs/self-host.md`, `NEWSBOT_CONTACT` User-Agent, `digest.subject`, `--check-sources`, `--now`, `config.minimal.yaml`, arm64 images, YouTube feeds dropped) plus local-time last sweep in `/newsbot status`; v2.1.1 admin run report shows each failing source's whole first error line (URL included, still defused). v2.2.0 lounge: a bot-posted welcome for new members and a daily quote ("Today's pour", 08:00 PT) in `#the-speakeasy-lounge` (prod channel 1401806745898061826) from eight Wikiquote pages (Divine Comedy, Fight Club (film), Hunter S. Thompson, H. L. Mencken, F. Scott Fitzgerald, The Curious Case of Benjamin Button (film), Oscar Wilde, Mark Twain), plus `/newsbot quote-now` (now `/lounge quote-now`, see v3.0.0 below); Server Members intent on for the prod and dev apps; prod config backup before the lounge at `/opt/newsbot/config.pre-lounge.yaml`. Droplet `cockwomble` (Ubuntu 24.04 since 2026-09-27, Docker 29.8.1; network on netplan (`/etc/netplan/50-droplet.yaml`) since 2026-09-28 with IPv6 restored and ifupdown retired; see `docs/deploy.md` §16), `/opt/newsbot` git clone, `scripts/deploy.sh`; backups via systemd timer 08:30 PT plus one per deploy. The dev bot (owner's Mac, private test guild, `config.dev.yaml` in the v2 shape with per-game test channels and alerts on, edited by the owner for v3; v1 copy at `data/config.dev.v1.yaml`) is stopped when not in use. Releases: wait for the tag's CI build before `deploy.sh`; the published image is amd64 + arm64 (from v2.1 on; earlier tags are amd64-only, so local drills of those still build from the git tag). Open work: see the ranked to-do list below. Privacy policy and terms of service are published from `site/` via GitHub Pages (deploys from `main`): https://someclown.github.io/discord-newsbot/privacy.html and /terms.html. Update the policy if the bot starts storing anything new about users. The v3 versions (public app, per-server records, deletion on removal, support contact justsomeclown@gmail.com) went live when PR #19 merged on 2026-10-01.
- **v3.0.0 (released 2026-10-01, in prod 2026-10-02):** squash-merged as PR #19 (`a5e76a7`), tagged `v3.0.0`. One public bot any server can install, a 16-game catalog (15 at release; ARC Raiders joined on 2026-10-04), setup through `/newsbot setup`, per-server settings in the database (migrations 005 to 008), hourly shared collection with Reddit rotation and 429 backoff, per-server digests (free: headlines; comped: Claude summaries, computed once per game and shared), per-server SHiFT delivery with D14 follow-ups, and the friend's server imported once as the first comped server. Prod still uses the v2-shaped config (catalog derived from `topics:`); the 16-game catalog, the privacy and terms check, and the Public Bot switch are the remaining going-public steps (owner's notes: `~/ClaudeFiles/newsbot-v3-prod-migration.md`, steps 15 to 20). The operator docs deliberately carry no v2 to v3 migration or rollback steps (owner, 2026-10-01). `main` is protected (no force push or deletion; PRs need the `test` check; admin bypass on).
- **v3.0.1 Reddit takes turns** (PR #28, in prod 2026-10-02; the Droplet gets about one Reddit request per hourly pass): Reddit requests go stalest first, priority subreddits included, and a Reddit 429 logs its Retry-After.
- **v3.0.2 run report sources and Claude lines restored** (PR #32, in prod 2026-10-04): a server's run report has its Sources line back (distinct sources behind its games, counted the way `/newsbot status` counts them, with the first failing source's short error) and, for comped servers, the Claude line (summaries' cost, prepare time, "shared"), and a comped digest's count says stories again (headlines stay "items").
- **v3.0.3 Reddit codes only** (PR #33, in prod 2026-10-04; Reddit denied the Data API request on 2026-10-04): `r/Borderlands4` is the only Reddit source left and is read only to spot SHiFT codes (`codes_only: true`; any reddit.com source is codes-only by default, and one for a non-SHiFT game is skipped with a warning). Its items go to SHiFT detection and the B+ sightings and are never stored, so no digest, summary, `/news` command or prompt can show one. Design: `docs/design.md` §15, "As built".
- **v3.0.4 dependency updates** (in prod 2026-10-04): anthropic 1.11.0 (checked with a dev-bot `/newsbot preview`), ruff 0.16.10, and the GitHub Actions bumps (configure-pages 6, deploy-pages 5, upload-pages-artifact 5, setup-buildx 4, setup-qemu 4); multidict 7 is held back because aiohttp needs `multidict<7.0` (Dependabot told to skip 7.x).
- **Prod config gotcha (v3):** `admin_channel_id` stays the friend's own admin channel (the import copies it into their row), and the owner's bot-wide alert channel is its own key, `owner_channel_id`, which must be in `home_guild_id` (the owner's test server, D9: 1552824311608512532). Prod's config keeps `admin_channel_id` as in v2.2 and adds `home_guild_id` and `owner_channel_id`. With `owner_channel_id` unset the owner alerts fall back to `admin_channel_id`, which is right for single-server self-hosts and v2-shaped files and wrong for prod. Also: `comped_guild_ids` holds the friend's server only, never the home server (a second comped server following other games changes the AI prompt), and the catalog must keep Borderlands 4, Palworld, Diablo IV first, in that order, for the same reason.
- **Never run `docker compose ... config` (or anything that resolves `env_file`) against the real `.env`/`.env.dev`.** It prints every secret in plain text. Lint compose files against dummy env files in a scratch directory. (Learned the hard way on 2026-09-24; all four dev credentials had to be rotated.)
- **Never run two bot processes with the same token.** Both receive every interaction and race; the loser logs `Unknown interaction (10062)`. Stop the local dev bot before starting the Droplet copy, and vice versa. On macOS the process shows as `Python -m newsbot` (capital P), so `pkill -f "python -m newsbot"` misses it. A dev bot can also be running as a Docker container (`docker ps`; Docker Desktop's CLI is at `/Applications/Docker.app/Contents/Resources/bin/docker` if it isn't on PATH): check both before starting one. A `docker-compose.dev.yml` container from 2026-09-26 ran unnoticed for 33 hours alongside the native dev bot.
- **Content policy (owner, 2026-09-24):** guides and walkthroughs, deals and sales, and Shift/redeem codes are all wanted in the digest. Don't tune the prompt to drop them.

## To-do, ranked by effort (least first)

Ranked 2026-09-26. The effort figures are rough, pre-investigation ballparks; some may never happen. "Agent time" means build, test and review, plus a short test-guild check.

1. **Resolved 2026-10-01: prod SHiFT alerts "no longer ping `@everyone`".** Working as designed. Of the five codes since 2026-09-25, only the Steam one (official) was ever carried by a trusted source, and it pinged; the other four were only ever seen on r/Borderlands4 and the Bluesky "Borderlands 4" search (community), so they posted unpinged. Cap unused, no failures, no permission alerts. Gearbox mostly posts codes on X, which isn't a source. **Owner decision (B+, D14):** v3 pings a community code once a second, independent source confirms it (official codes still ping on their own). Built in v3.0.0: a community code posts unpinged, and a second source (a different name, any case) or an official or press one within 24 hours gets each pinging server one follow-up that spends one of its daily pings.
2. **Reddit API: denied; do not reapply** (owner, 2026-10-04). Reddit refused the Data API access request (filed 2026-10-03, draft in `~/ClaudeFiles/reddit-api-access-request.md`) under its Responsible Builder Policy, which forbids sharing Reddit data without written approval and forbids reapplying for the same use case, so there is no second request to file. The owner decided Reddit is SHiFT-codes-only through `r/Borderlands4`: v3.0.3, above. Background: unauthenticated RSS from the Droplet got about one Reddit request per hourly pass (429s), and `r/Palworld/top/.rss?t=day` returned `403 Blocked` from the Droplet's IP (that was Palworld's "8 of 9 sources ok"); the other subreddits left the config in v3.0.3. **Done 2026-10-04:** the owner purged the Reddit content stored before v3.0.3 by hand (638 items and the 16 stories built from them, out of 1,681 and 64), after a backup at `/opt/newsbot/data/newsbot.pre-reddit-purge.db`; integrity and foreign-key checks clean. The privacy page's "last updated" date (October 4, 2026) matches the v3.0.3 deploy.

**Lounge rollout done (2026-09-30).** v2.2.0's welcome was verified with a real join in the test guild (the join event arrived with Server Members intent on, and the welcome posted); prod runs the same code. Discord's built-in welcome is off on the prod server and its System Messages Channel points at the admin channel again. Deploy window: never 09:00–09:15 America/Los_Angeles, and avoid about 07:55–08:05 (quote time).

**Public app, part 1 (v3.0.0): in production since 2026-10-02, not yet public.** Designed in `docs/design.md` §15 (approved 2026-09-30; "As built" is the current description), implemented on `feat/public-app` per `docs/plans/2026-09-30-public-app.md`. One public bot that any server can install:
- a curated 16-game catalog;
- setup through slash commands;
- a free tier of headline digests plus SHiFT alerts;
- the friend's server imported as the first, comped-premium server;
- per-server settings in the database.

It uses hourly shared collection with per-server digests. Parts 2 (bring your own key) and 3 (a paid Discord subscription, possible only after verification at about 75 servers) come later. Self-hosting keeps working the same way. Done: the test-guild run, the PR and tag, prod's rollout (2026-10-02). Left: watch two v3 digest days and a SHiFT alert, then going public (the 16-game catalog, the prod app's install link with the `bot` scope, then the Public Bot switch last).

Open owner items for the v3 rollout:
- **Follow-ups not done** (none block the rollout): move SHiFT delivery into its own job if the bot gets big (it runs inside the collection hook's 120-second budget, so at about 170 or more SHiFT-enabled servers a big code drop drains over several hourly passes); Brave has no local monthly budget guard (its cost doesn't grow with servers, but nothing stops a runaway); and the load test fakes Discord (about 50 sends a second overall, 5 per 5 seconds per channel), so its timings describe the bot's pacing and not Discord's real limits.

## Documentation voice (read this before writing a docstring)

The owner wants the code documented thoughtfully, in their own voice: plain,
literate, a little irreverent, and quickest to laugh at itself. Think of a
senior engineer who has been paged at 3 a.m. by their own clever code and has
made peace with it. The full voice guide is the owner's `my-writing-style`
skill; what follows is the code-sized version.

**Accuracy first, jokes second.** A docstring's job is to tell the next reader
what the thing does, why it exists, and what will bite them. The humor rides
along on top of that; it never replaces it. If a joke would make a comment
less clear, cut the joke. Nobody debugging a failed digest at 9:04 a.m. wants
to decode a pun.

**Where the voice goes:**
1. **Module docstrings** get the most personality: a short paragraph on what
   the module is for, why it's shaped the way it is, and (where true) the
   dead end we tried first. This is where an analogy earns its keep.
2. **Public function and class docstrings** are mostly straight: one-line
   summary, then args/returns/raises where they aren't obvious from type
   hints. A dry aside in parentheses is welcome when something is genuinely
   odd.
3. **Inline comments** explain *why*, never *what*. They are where the
   self-deprecation lives: "This retry exists because I assumed the feed would
   always be valid XML. It is not. It never was."
4. **README and runbook** read like a person explaining the project to a peer
   over coffee, not like a product page.

**The grain of the voice:**
- First person is fine ("I", "we"); the author is in the code.
- Contractions always. Plain words over clever ones, except when an
  unexpectedly fancy word ("ostensibly", "vagaries", "proverbial") is the joke.
- Self-deprecation lands on the code and its author. Never on the reader,
  the server's members, or anyone trying to learn.
- Don't make a named company the butt of a joke. State facts about third
  parties plainly ("the feed returns 403 to datacenter IPs") and let the
  facts be funny on their own.
- Admit what we don't know: "As far as I can tell, this is fine. I have been
  wrong before; see git log."
- Concrete, slightly absurd analogies from outside tech are the house style
  (dedupe is "the bouncer checking whether you've already been inside
  tonight").
- Keep it PG-13. The repo may be shared with other servers.
- Em dashes rarely; colons, semicolons, and parentheses do that work.
  `--` is a dash too (not just an em dash character) and shouldn't be used
  as one either, for the same reason.
- Superlatives get softened ("one of the more fragile parts", not "the most
  fragile part").

**Hard nos:** corporate jargon (leverage, robust, best practice, deep dive,
actionable, synergy), AI-assistant filler ("It's worth noting that",
"This function simply..."), exclamation points, emoji in code comments, and
comment-per-line narration of obvious code. Humor density is roughly one
light touch per module and the occasional aside where something truly earned
it; a file where every comment is a bit is as tiring as a file with none.

**Examples of the target:**

```python
"""Normalize and dedupe collected items.

Every source has its own ideas about what a URL is. Some append tracking
parameters the way toddlers append jam to furniture; some flip between http
and https depending on the phase of the moon. This module sands all of that
down to one canonical form so the database's UNIQUE constraint can play
bouncer: if you've already been inside tonight, you're not getting back in.
"""
```

```python
# Discord gives us three seconds to acknowledge an interaction. The pipeline
# takes considerably longer than three seconds, a fact I learned the way
# everyone does. Defer first, think later.
await interaction.response.defer(ephemeral=not public)
```

```python
def canonicalize_url(url: str) -> str | None:
    """Return the canonical form of ``url``, or None if it isn't http(s).

    Strips utm_* and other tracking parameters, lowercases scheme and host,
    and drops the trailing slash. Does not merge http with https; that's a
    later problem, and future me is welcome to it.
    """
```
