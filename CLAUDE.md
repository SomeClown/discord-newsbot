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
- **Status (2026-09-25):** Production runs **v1.2.0** (`TAG=1.2.0` in the Droplet's `.env`) with SHiFT code alerts **enabled** (`alerts: {enabled: true, topics: [borderlands4]}` in prod `config.yaml`; bot role has "Mention @everyone"; test command off). Earlier: v1.1.0 = Anthropic SDK 1.8.0 + IGN removed + upgrade tooling; v1.1.1 = status shows only configured sources. Droplet `cockwomble` (Ubuntu 20.04, Docker 28.1.1), `/opt/newsbot` git clone, `scripts/deploy.sh`; backups via systemd timer 08:30 PT plus one per deploy. The dev bot runs on the owner's Mac against the private test guild with alerts on (15-minute sweeps, test command off). Releases: wait for the tag's CI build before `deploy.sh`. Still to do: second-account permission check in the prod guild; delete `/opt/newsbot.pre-clone`; upgrade the Droplet off Ubuntu 20.04 (snapshot first); expect a Dependabot `pydantic` bump PR. Privacy policy and terms of service are published from `site/` via GitHub Pages (`.github/workflows/pages.yml`): https://someclown.github.io/discord-newsbot/privacy.html and /terms.html, for the Developer Portal's policy URL fields. Update the policy if the bot starts storing anything new about users.
- **Never run `docker compose ... config` (or anything that resolves `env_file`) against the real `.env`/`.env.dev`.** It prints every secret in plain text. Lint compose files against dummy env files in a scratch directory. (Learned the hard way on 2026-09-24; all four dev credentials had to be rotated.)
- **Never run two bot processes with the same token.** Both receive every interaction and race; the loser logs `Unknown interaction (10062)`. Stop the local dev bot before starting the Droplet copy, and vice versa. On macOS the process shows as `Python -m newsbot` (capital P), so `pkill -f "python -m newsbot"` misses it.
- **Content policy (owner, 2026-09-24):** guides and walkthroughs, deals and sales, and Shift/redeem codes are all wanted in the digest. Don't tune the prompt to drop them.

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
