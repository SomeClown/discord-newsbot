# discord-newsbot

A Discord bot that reads the internet every morning so a ~50-person server
doesn't have to: daily AI-summarized news digest for Borderlands 4, Palworld,
and Diablo IV, plus `/news recent` and `/news search` commands.

- **Design (source of truth):** `docs/design.md`
- **Current plan:** `docs/plans/2026-09-23-v1-implementation.md`. The owner
  accepted every SPEC-DEV default in plan section 4 on 2026-09-23 (command
  shape: `/news recent` + `/news search`; digest at 09:00 America/Los_Angeles).
- **Tooling:** Python 3.14, plain `venv` + `pip` (no uv; the owner knows pip and that's the point).
  Setup: `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e . --no-deps`.
  `requirements.txt` is the lock file; regenerate it with `scripts/lock.sh`, never by hand.
- **Gate:** `ruff check . && ruff format --check . && pytest -q` (venv active)
- Any prompt change needs an owner-reviewed `/newsbot preview` before merging.
- **Status (2026-09-27):** Production runs **v2.1.1** (`TAG=2.1.1` in the Droplet's `.env`, with `NEWSBOT_CONTACT` set there and in `.env.dev`; the v1 config is kept at `/opt/newsbot/config.v1.yaml` for rollback per `docs/deploy.md` §17). Each game posts one embed per day to its own channel (`topics[].channel_id`); SHiFT code alerts and unpinged roundups go to `#shift-codes` (`alerts.channel_id`) with `@everyone` rules unchanged; admin health messages and run reports go to `admin_channel_id`; members have `/shift codes`. Release history: v1.1.0 Anthropic SDK 1.8 + IGN removed + upgrade tooling; v1.1.1 status shows configured sources only; v1.2.0 SHiFT code alerts; v1.3.0 admin run reports; v2.0.0 per-game channels, SHiFT channel, `/shift codes`, startup permission check (breaking config change); v2.1.0 run-your-own-copy support (`docs/self-host.md`, `NEWSBOT_CONTACT` User-Agent, `digest.subject`, `--check-sources`, `--now`, `config.minimal.yaml`, arm64 images, YouTube feeds dropped) plus local-time last sweep in `/newsbot status`; v2.1.1 admin run report shows each failing source's whole first error line (URL included, still defused). Droplet `cockwomble` (Ubuntu 24.04 since 2026-09-27, Docker 29.8.1; network on netplan (`/etc/netplan/50-droplet.yaml`) since 2026-09-28 with IPv6 restored and ifupdown retired; see `docs/deploy.md` §16), `/opt/newsbot` git clone, `scripts/deploy.sh`; backups via systemd timer 08:30 PT plus one per deploy. The dev bot (owner's Mac, private test guild, v2 `config.dev.yaml` with per-game test channels, alerts on, test command off; v1 copy at `data/config.dev.v1.yaml`) is stopped when not in use. Releases: wait for the tag's CI build before `deploy.sh`; the published image is amd64 + arm64 (from v2.1 on; earlier tags are amd64-only, so local drills of those still build from the git tag). Open work: see the ranked to-do list below. Privacy policy and terms of service are published from `site/` via GitHub Pages: https://someclown.github.io/discord-newsbot/privacy.html and /terms.html. Update the policy if the bot starts storing anything new about users.
- **Never run `docker compose ... config` (or anything that resolves `env_file`) against the real `.env`/`.env.dev`.** It prints every secret in plain text. Lint compose files against dummy env files in a scratch directory. (Learned the hard way on 2026-09-24; all four dev credentials had to be rotated.)
- **Never run two bot processes with the same token.** Both receive every interaction and race; the loser logs `Unknown interaction (10062)`. Stop the local dev bot before starting the Droplet copy, and vice versa. On macOS the process shows as `Python -m newsbot` (capital P), so `pkill -f "python -m newsbot"` misses it. A dev bot can also be running as a Docker container (`docker ps`; Docker Desktop's CLI is at `/Applications/Docker.app/Contents/Resources/bin/docker` if it isn't on PATH): check both before starting one. A `docker-compose.dev.yml` container from 2026-09-26 ran unnoticed for 33 hours alongside the native dev bot.
- **Content policy (owner, 2026-09-24):** guides and walkthroughs, deals and sales, and Shift/redeem codes are all wanted in the digest. Don't tune the prompt to drop them.

## To-do, ranked by effort (least first)

Ranked 2026-09-26. The effort figures are rough, pre-investigation ballparks; some may never happen. "Agent time" means build, test and review, plus a short test-guild check.

Nothing open. (Last cleared 2026-09-28: a regular member was refused `/newsbot status` in the prod guild, confirming the admin-only commands are admin-only.)

**In progress: lounge welcomes and daily quote (v2.2).** Designed in `docs/design.md` §14 (approved 2026-09-29) on branch `feat/lounge`. A bot-posted welcome and a daily fortune-style quote go to `#the-speakeasy-lounge`; everything else stays in the admin channel. Quote sources revised the same day: a mixed list of Wikiquote pages, the owner's own file and URLs, with a built-in list of public-domain authors as the default and modern copyrighted works only as commented-out examples. Plan: `docs/plans/2026-09-29-lounge.md` (12 tasks, about 4 to 4.5 agent days), awaiting owner review. Its checkpoints: A (source lists), B (wording), C (does prod use Community, Onboarding or rules screening?), D (System Messages Channel after cutover). The Server Members intent must be enabled in the Developer Portal before deploy.

**Next big feature (paused 2026-09-28, discussion stage; nothing designed yet):** a public, installable app, meaning one bot the owner runs, added to many servers from Discord. Owner chose the **hybrid** cost model:
- **Free tier, no AI:** RSS, Reddit and Bluesky headlines, at no cost to the owner.
- **AI summaries, web search and SHiFT alerts:** unlocked by the server admin's own API keys (bring your own key), or later by a paid Discord server subscription where the owner covers API costs (about $1 per server per month).

Proposed order:
1. Multi-server redesign with the free tier (about a week or more).
2. Bring your own key (2–3 days).
3. Discord Premium Apps subscription (2–4 days). It can only be switched on after Discord verifies the app, which opens at about 75 servers and includes a Stripe ID check; Discord takes 15% of the first $1M.

Resume with brainstorming, then a `docs/design.md` section, then an `architect` plan. Self-hosting (v2.1) stays as the alternative.

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
