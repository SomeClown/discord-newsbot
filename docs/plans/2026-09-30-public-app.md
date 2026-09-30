# Plan: public app, part 1 (many servers, free tier, v3.0.0)

Source of truth: `docs/design.md` §15 (approved 2026-09-30), plus §12, §13 and §14, which §15 reshapes. Branch `feat/public-app`. Save as `docs/plans/2026-09-30-public-app.md`.

**Gate after every task:** `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q`. Check pytest's real exit code.

**Workflow:**
- Each code task goes to `implement`, then `test-engineer` for an adversarial pass on that task.
- `qa` runs after task 16. `docs` runs last (task 18). The source research (task 17) runs in parallel with everything.
- All new code is documented in the owner's voice (CLAUDE.md "Documentation voice"). Minimize dashes, and never use `--` as a dash. CLI flags are fine.

**Tripwires that bind every agent writing in `newsbot/`:**
- `tests/test_dash_rewrite_tripwire.py`: no `" -- "` in any string literal.
- `tests/test_mentions_tripwire.py`: exactly one `AllowedMentions(everyone=True, ...)` (the `_PING_EVERYONE` constant in `bot/client.py`). No `.all()`, no non-constant `everyone=`, no `**kwargs`. Role pings use a literal `everyone=False`.
- `tests/test_useragent_tripwire.py`: the literal text `User-Agent` appears nowhere in `newsbot/` except `useragent.py`, comments included. Every request goes through the shared `httpx` client.

**No new dependencies.** discord.py 2.7.1 already has everything this needs: select menus, `ChannelSelect` with `default_values`, autocomplete, `guild_only`, and `default_permissions`. Stdlib `zoneinfo` covers the time zone list. `pyproject.toml` changes only its version.

**Hard rules for every agent:**
- Never read `.env`, `.env.dev`, `config.dev.yaml`, or anything under `data/`.
- Never run `docker compose ... config`.
- Don't run the bot.
- No network, except in task 17.

---

## 1. Goal and approach

When this ships, one bot run by the owner serves any number of servers:
- `config.yaml` holds only global settings plus a `catalog:` of 15 games.
- Each server's settings live in the database and are set with slash commands.
- An hourly shared collection fetches every catalog game's sources once, stores the items tagged by game, and runs SHiFT detection once, fanning out per guild.
- A job every minute posts each server's digest at its own local time:
  - free servers get a headline list;
  - comped servers get a Claude summary computed once per game per day and reused.
- The friend's server is imported once from the v2 config as the first, comped server, and nothing visible changes for it.

**Chosen approach.** `run_daily` is retired and split into three pieces:
- `pipeline/collect.py`: collect, normalize, filter, store, SHiFT;
- `pipeline/summaries.py`: comped summaries;
- `pipeline/guild_digest.py`: a per-guild claim, render, publish and save, keeping v2's guard semantics keyed per guild.

Every new piece lands as new modules and new repo functions, tested on its own, while the running bot keeps its v2.2 single-server path. A single cutover task (13) switches the wiring and deletes the v2 path. So the branch stays shippable until task 13.

**The key trade-off: migration 005 isn't purely additive.** Two tables have to be rebuilt:
- `digests.run_date` is `UNIQUE`, so two guilds can't have a row on the same day;
- `stories.digest_id` is `NOT NULL` with no `ON DELETE`, so a comped guild could never be deleted.

Both are rebuilt as column supersets using SQLite's documented table-rebuild procedure. v2.2.0 still reads and writes them, so §15's "rollback is a TAG change" still holds. The purely additive alternative (a new `guild_digests` table) breaks two things:
- v2.2's double-post guard on rollback: v2.2 would repost a day v3 already posted;
- the stories foreign key.

### Where §15 looks wrong or underspecified (flagged, not silently changed)

1. **"`catalog:` is today's `topics` plus `sources`, regrouped per game" doesn't fit multi-game feeds.** PC Gamer, Eurogamer, GamesRadar+, PCGamesN and the 2K Newsroom are unscoped: they belong to no single game. The plan adds a top-level `shared_sources:` list, fetched once and keyword-matched against every catalog game. It also adds a top-level `web_search:` block (Brave has always been one global source). **D1.**
2. **The migration can't be additive for `digests` and `stories`** (above). The rebuild is the least risky option; details are in §3.2.
3. **"The loader accepts the old shape only for the one-time import."** Rollback by TAG needs the old keys to stay in `config.yaml`. The friend's upgrade also has to work with the unchanged prod config.
   - So v3 derives the catalog from old-shape `topics` plus `sources` whenever `catalog:` is absent.
   - When `catalog:` is present, the old keys are read only by the import and otherwise ignored, with a startup log line listing the keys that can be deleted.
4. **`/newsbot servers` "registered only in the owner's home server"** can't be a subcommand of the global `/newsbot` group: a guild copy of the group would show two `/newsbot` entries in that server. The same goes for `quote-now` in the lounge guild. **D3.**
5. **"Free: the headline list, today's fallback format (official, then reported, then rumor)."** Items carry trust (official, press, community), not story labels, and today's fallback shows no labels at all. Calling every Reddit post a "rumor" would be wrong. **D4.**
6. **"`/news recent` and `/news search` read the shared data."** Stories only exist for games a comped guild follows. So for a free server following Fortnite, `/news` would always be empty. **D2.**
7. **Lounge settings after the import.** Part 1 has no command to edit them, so after the import the owner could no longer change the welcome or quote sources. **D5.**
8. **Self-hosters.** An imported v2 self-host keeps its AI summaries (the import marks it comped). A *new* self-host that runs `/newsbot setup` would land on the free tier with no way to comp its own server. **D6.**
9. **Pending digests.** v2 alerts on a `pending` row and waits for a human. At hundreds of guilds that becomes a stuck digest nobody sees. **D7.**
10. **Source-health alerts.** The v2 threshold of 3 consecutive failures meant 3 days; with hourly collection it would mean 3 hours. **D8.**
11. **Server Members intent.** The prompt says §15 keeps it on for the whole app. §15 itself only says the lounge stays on the friend's server and the intent is revisited at verification. The plan keeps v2.2's rule, now read from the database: request the intent iff a `guild_lounge` row has welcomes on. Member chunking is limited to that guild (§3.11).

---

## 2. Owner decisions (with recommendations)

None of these blocks task 1 except D1 and D6. Each names the task it blocks.

| # | Question | Recommendation | Blocks |
|---|---|---|---|
| D1 | Where do multi-game feeds and Brave go? | **A:** top-level `shared_sources:` (keyword-matched against every game; optional `games:` to restrict) and `web_search:` (comped-only). | T1 |
| D2 | What do free servers' `/news recent` and `/news search` show? | **A:** free servers see stored item headlines. `recent` lists items and `search` uses a new `items_fts` over title and excerpt. Comped servers keep stories. (B: stories only, so free-only games return nothing. C: stories where they exist, items otherwise, which gives away premium content.) | T2, T9 |
| D3 | Guild-only commands | Separate guild-scoped top-level groups: `/owner servers` (home guild only) and `/lounge quote-now` (lounge guilds only). `/newsbot quote-now` goes away. | T9, T12 |
| D4 | Free headline order and markers | Order official, press, community (then confident before uncertain, then newest). Prefix `🟢 OFFICIAL` on official items only. No "rumor" label on community items. "+N more, use /news" when over the limit. | T6 |
| D5 | Editing lounge settings after the import | While a `lounge:` block remains in `config.yaml`, v3 re-syncs it into the imported guild's `guild_lounge` row at every startup (logged). Delete the block and the database row stands alone. | T12 |
| D6 | Comping self-hosts and test guilds | Config `comped_guild_ids: []`. Listed guilds are set to `comped` at join and at startup. Nothing is ever downgraded automatically. | T1 |
| D7 | A `pending` digest at startup | Resume it: posted games are written through to the row as they land, so the resume posts only the missing games. Worst case is one duplicate game post if the process died mid-send. (The alternative is v2's "tell the admin and wait for run-now", per guild.) | T6 |
| D8 | Source failure alert threshold | Alert the owner once at 12 consecutive failed collections (half a day), reset on success. The daily owner report lists every failing source. | T4 |
| D9 | Home guild | `home_guild_id` defaults to the legacy `guild_id` when unset. The owner confirms whether the friend's server is also "home" (it holds the admin channel today). | T13 |
| D10 | Support contact for privacy and terms | The owner supplies it (email, or a support Discord invite). | T18 |
| D11 | Approving the 12 new catalog entries | From task 17's report. | Rollout only |


**Owner answers (2026-09-30):**
- D1 to D8 accepted as recommended.
- **D9:** the home guild is the **test server** (1552824311608512532), not the friend's server. The prod bot must be invited to the test server before cutover, and a dedicated prod-admin channel there is suggested so prod and dev alerts don't mix. The friend's server keeps its current admin channel as its own per-guild admin channel through the import.
- **D10:** the support contact is justsomeclown@gmail.com (owner, 2026-09-30), for the terms and privacy pages in task 18.
- **D11:** approved 2026-09-30: all 12 researched games as proposed in `docs/plans/2026-09-30-public-app-catalog.yaml`, with the unverified FFXIV alias "Evercold" dropped.

---

## 3. Resolved design

### 3.1 Config (v3 shape)

```yaml
home_guild_id: 123...            # optional; defaults to the legacy guild_id; owner-only commands live here
admin_channel_id: 123...         # the owner's channel: bot-wide health only
admin_permission: manage_guild   # unchanged meaning; also the commands' default_member_permissions
command_guild_ids: []            # empty = global registration (prod); ids = guild-scoped (dev, self-host)
comped_guild_ids: []             # D6
owner_report: {time: "21:00", timezone: "America/Los_Angeles"}   # daily owner summary
collection:
  interval_minutes: 60           # 15..1440
  lookback_hours: 24
  max_items_per_game: 60
ai:
  subject: "video games"         # was digest.subject; the prompt is byte-identical for the friend
run_report: true                 # was digest.report_to_admin; goes to each server's own admin channel
web_search:                      # optional; comped-followed games only, once a day
  queries_per_game: 2
  query_templates: ["{name} news", "{name} update OR patch OR leak"]
  trust: press
shift:                           # global detection; per-guild channel and ping live in the database
  games: [borderlands4]
  max_item_age_hours: 48
  max_pings_per_day: 3           # per guild, per the guild's local day
  ping_trust: [official, press]
  max_codes_per_item: 5
  allow_test_command: false
shared_sources: [...]            # D1: today's unscoped or multi-topic sources, same schema
catalog:
  - key: borderlands4
    name: "Borderlands 4"
    aliases: ["BL4", "Borderlands4"]
    entities: ["Gearbox"]
    search_queries: [...]
    match_name: true             # false for common-word names (Rust, Destiny, Apex); aliases do the matching
    sources:                     # same schema as today, minus `topics` (implied: this game)
      - {type: steam_news, name: "Borderlands 4 Steam", app_id: 1285190, trust: official}
```

**Models (`newsbot/config.py`):**
- `GameCfg`: key, name, aliases, entities, search_queries, match_name, sources.
- `SharedSourceCfg`: today's `Source` union, where `topics` is renamed `games` in the new shape (old `topics` is accepted in derived mode).
- `WebSearchCfg`, `ShiftCfg`, `CollectionCfg`, `AiCfg`, `OwnerReportCfg`.
- `LegacySetup`: the import's view of the old keys. Fields:
  - `guild_id`, `admin_channel_id`, `digest_time`, `timezone`;
  - `games: list[(key, channel_id)]`;
  - `shift_enabled`, `shift_channel_id`, `shift_ping`;
  - `lounge: LoungeCfg | None`;
  - `alerts_max_pings`.
- `AppConfig` gains `catalog`, `shared_sources`, `web_search`, `shift`, `collection`, `ai`, `home_guild_id`, `command_guild_ids`, `comped_guild_ids`, `owner_report`, `run_report` and `legacy: LegacySetup | None`.

**Until the cutover (task 13),** the v2 fields (`guild_id`, `digest`, `topics`, `sources`, `alerts`, `lounge`) stay on `AppConfig`, still optional, so the running v2 path keeps working. Task 13 deletes them from `AppConfig`. After that, the raw old keys are read only by `LegacySetup` parsing.

**Game-shaped protocol.** `filter.py`, `prompts.py`, `web_search.py` and `summarize.py` type their topic arguments as a new `GameInfo` protocol (key, name, aliases, entities, search_queries, match_name). v2's `Topic` and `GameCfg` both satisfy it. `build_matchers` leaves out the name when `match_name` is false.

**Loading rules (`load_config`):**
1. `catalog:` present means the new shape. `topics`/`sources`, if present, are legacy (import only).
2. `catalog:` absent with `topics:` plus `sources:` means the old shape. The catalog is derived:
   - each topic becomes a `GameCfg`;
   - a source whose `topics` names exactly one key goes under that game with `topics` dropped;
   - unscoped or multi-topic sources go to `shared_sources` (keeping the restriction as `games`);
   - the `web_search` source becomes `web_search:` (`queries_per_topic` becomes `queries_per_game`);
   - `alerts.{topics, max_item_age_hours, max_pings_per_day, ping_trust, max_codes_per_item, allow_test_command}` becomes `shift:`;
   - `digest.{lookback_hours, max_items_per_topic}` becomes `collection:`;
   - `digest.subject` becomes `ai.subject`;
   - `digest.report_to_admin` becomes `run_report`.
3. `LegacySetup` is built whenever `guild_id` is present.
4. **Everything new is top-level.** v2.2's `AlertsCfg` and `LoungeCfg` are `extra="forbid"`. Keeping `alerts:` and `lounge:` exactly v2.2-shaped is what lets a hybrid config (old keys plus `catalog:`) still load under `TAG=2.2.0`.

**Validation messages.** All errors are collected into one `ConfigError`, with no dashes.
- Neither shape: `config needs a catalog: (v3), or the v2 topics: and sources: to import from`.
- More than 25 games: `catalog has {n} games; the limit is 25 (a Discord select menu holds 25 options)`.
- `catalog[{i}].key {k!r} must match ^[a-z0-9_]+$`; `duplicate game key {k!r}`.
- `catalog[{i}] ({key}).sources[{j}] ({name}): remove topics; a source listed under a game belongs to that game`.
- `catalog[{i}] ({key}).sources[{j}]: web_search goes in the top-level web_search: block`.
- `shared_sources[{j}] ({name}) names unknown game {g!r}`; `shift.games names unknown game {g!r}`.
- `duplicate source name {n!r}`. This is checked across catalog plus shared sources, since `source_health` is keyed by name.
- `topics[{i}] ({key}) isn't in catalog; the v2 import needs every old game in the catalog` (both shapes present).
- `match_name is false for {key} but it has no aliases, so nothing would ever match`.
- The existing `digest.channel_id` v2.0 pre-check stays as it is. A `channel_id` inside `catalog`: `catalog[{i}].channel_id: channels are per server now; use /newsbot follow`.
- `home_guild_id`, `command_guild_ids[]` and `comped_guild_ids[]` are positive ints; bools are rejected with `_reject_bool_channel_id`.
- `owner_report.time` must be HH:MM and `owner_report.timezone` a known IANA zone. `shift.max_pings_per_day >= 0`.
- The BRAVE_API_KEY-missing warning and the Bluesky default names carry over.

**CLI in a multi-guild world** (`python -m newsbot.pipeline.run`):
- `--check-sources [--game KEY ...]`: runs catalog plus shared sources, and web search if keyed. Prints a per-source table grouped by game, then per-game match counts. `--game` restricts it to those games' own sources, plus shared sources matched only against them. It needs no database, no Anthropic key and no Discord token. This is what task 17 uses.
- `--collect`: one collection pass into `--db`. It prints the collection summary, and SHiFT fan-out goes through `PrintCodeAlertPoster`. `--sweep` stays as an alias for one release, with a stderr note.
- `--dry-run [--guild ID]` (the default mode): previews one guild's next digest from stored items. It writes nothing except the import, and only if the import hasn't run. `--guild` can be omitted when exactly one guild is set up (the self-hoster's case); otherwise it's an error listing the ids.
- `--post-to-stdout [--guild ID] [--force]`: POST mode for that guild. It claims, prints and saves, which exercises the per-guild guard.
- `--fixtures DIR`: replaces collectors and implies one `--collect` pass first, so `--fixtures --stub-llm --dry-run` stays a fully offline run.
- `--now`: the clock for every step, including the due and window math.
- `--stub-llm`: unchanged.
- Every mode except `--check-sources` runs `ensure_imported` first, so a self-hoster's first `--dry-run` after upgrading just works.

### 3.2 Migration 005 (`005_public_app.sql`)

**Runner change (`store/db.py`).** A migration whose first line is `-- newsbot: foreign-keys-off` runs as follows:
1. `PRAGMA foreign_keys = OFF`, run outside the transaction (autocommit is already on in `_apply`);
2. `BEGIN IMMEDIATE`, then the script;
3. `PRAGMA foreign_key_check`: any row means `ROLLBACK` and raise `StoreError` naming the table;
4. `COMMIT`, then `PRAGMA foreign_keys = ON` in a `finally`.

This is SQLite's documented 12-step table rebuild.

**New tables:**

```sql
CREATE TABLE guilds (
  guild_id INTEGER PRIMARY KEY CHECK (guild_id > 0),
  digest_time TEXT NOT NULL DEFAULT '09:00' CHECK (digest_time GLOB '[0-2][0-9]:[0-5][0-9]'),
  timezone TEXT NOT NULL DEFAULT 'UTC',
  admin_channel_id INTEGER CHECK (admin_channel_id IS NULL OR admin_channel_id > 0),
  tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'comped')),
  set_up INTEGER NOT NULL DEFAULT 0 CHECK (set_up IN (0, 1)),
  joined_at TEXT NOT NULL,
  imported_at TEXT,                 -- set only by the v2 import
  permission_problems TEXT,         -- last startup check's problem text, to notify only on change
  updated_at TEXT NOT NULL
);
CREATE TABLE guild_games (
  guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
  game_key TEXT NOT NULL,
  channel_id INTEGER NOT NULL CHECK (channel_id > 0),
  PRIMARY KEY (guild_id, game_key)
);
CREATE TRIGGER guild_games_limit BEFORE INSERT ON guild_games
WHEN (SELECT COUNT(*) FROM guild_games WHERE guild_id = NEW.guild_id) >= 10
BEGIN SELECT RAISE(ABORT, 'at most 10 games per guild'); END;
CREATE TABLE guild_shift (
  guild_id INTEGER PRIMARY KEY REFERENCES guilds (guild_id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  channel_id INTEGER,
  ping TEXT NOT NULL DEFAULT 'none'
    CHECK (ping IN ('none', 'everyone') OR (ping GLOB '[1-9]*' AND ping NOT GLOB '*[^0-9]*')),
  enabled_at TEXT,
  ping_day TEXT,
  ping_count INTEGER NOT NULL DEFAULT 0,
  CHECK (enabled = 0 OR channel_id IS NOT NULL)
);
CREATE TABLE guild_code_posts (
  guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
  code TEXT NOT NULL REFERENCES alerted_codes (code),
  status TEXT NOT NULL CHECK (status IN ('pending', 'posted', 'failed')),
  message_id INTEGER,
  pinged INTEGER NOT NULL DEFAULT 0 CHECK (pinged IN (0, 1)),
  from_roundup INTEGER NOT NULL DEFAULT 0 CHECK (from_roundup IN (0, 1)),
  claimed_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, code)
);
CREATE TABLE guild_lounge (
  guild_id INTEGER PRIMARY KEY REFERENCES guilds (guild_id) ON DELETE CASCADE,
  channel_id INTEGER NOT NULL CHECK (channel_id > 0),
  welcome_enabled INTEGER NOT NULL DEFAULT 0,
  welcome_message TEXT NOT NULL DEFAULT '',
  quote_enabled INTEGER NOT NULL DEFAULT 0,
  quote_time TEXT NOT NULL DEFAULT '08:00',
  quote_sources TEXT NOT NULL DEFAULT '[]',   -- JSON [{"kind", "value"}], already resolved
  last_quote_date TEXT
);
CREATE TABLE guild_notices (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000)
);
CREATE INDEX idx_guild_notices ON guild_notices (guild_id, created_at);
CREATE TABLE game_summaries (
  id INTEGER PRIMARY KEY,
  game_key TEXT NOT NULL,
  run_date TEXT NOT NULL,           -- local date of the comped digest that triggered it
  status TEXT NOT NULL CHECK (status IN ('ok', 'fallback')),
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  coverage_notes TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (game_key, run_date)
);
CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- command hashes, owner report date, import record
CREATE INDEX idx_item_topics_topic ON item_topics (topic_key, item_id);
```

**If D2 is A, also add** `items_fts`: an external-content FTS5 table over `items(title, excerpt)` with AI, AD and AU triggers, like `stories_fts`, plus a backfill `INSERT INTO items_fts(items_fts) VALUES('rebuild')`.

**Rebuilt tables** (the superset rebuild, inside the foreign-keys-off transaction):
- **`digests`:**
  - columns: `id` (preserved), `guild_id INTEGER REFERENCES guilds (guild_id) ON DELETE CASCADE` (nullable, so v2.2's inserts still work), `run_date`, `status`, `posted_message_ids`, `posted_by_game TEXT NOT NULL DEFAULT '{}'`, `window_start TEXT`, `window_end TEXT`, `attempts INTEGER NOT NULL DEFAULT 0`, `error_notes`, `input_tokens`, `output_tokens`, `created_at`, `updated_at`, and `UNIQUE (guild_id, run_date)`;
  - `INSERT INTO digests_new (...) SELECT ... FROM digests`, then `DROP TABLE digests`, then `ALTER TABLE digests_new RENAME TO digests`.
- **`stories`:**
  - same columns, but `digest_id INTEGER REFERENCES digests (id) ON DELETE SET NULL` (now nullable) plus `summary_id INTEGER REFERENCES game_summaries (id) ON DELETE SET NULL`;
  - copy the rows with ids preserved, so `stories_fts` rowids stay valid;
  - drop, rename, then recreate `idx_stories_topic_created`, `idx_stories_created_at` and the three `stories_*` triggers verbatim from 001.

**Additive `ALTER`:**
- `lounge_quotes_used` gains `guild_id INTEGER REFERENCES guilds (guild_id) ON DELETE CASCADE`.
- Its primary key stays `(source_key, quote_hash)`, which is fine for one lounge guild. A second one would need a rebuild; that's out of scope.

**Unchanged:**
- `items`, `item_topics` (already the "tagged with the games it matched" table), `source_health`, `alerted_codes` (global detection), `alert_state` (`seeded_at` and `last_sweep_*` stay global), and `lounge_state` (which v3 mirrors for rollback).

**Backfill.** The SQL can't know the friend's guild id, so the backfill happens in the Python import (§3.3), in the same transaction that writes the `guilds` row. On a fresh database there's nothing to backfill.

**Why v2.2.0 still runs against it:**
- v2.2's `migrate()` no-ops at `user_version` 5.
- Every v2.2 query names its columns, and the rebuilt tables are supersets.
- v2.2's `claim_digest` selects `WHERE run_date = ?`. Before going public, the only rows are the friend's, so its guard is correct. That covers the prod bot being in one guild; D9 covers a separate home guild.
- v2.2 inserts rows with `guild_id NULL`. On a later roll-forward, v3's startup runs an adopt step, which closes the rollback-then-forward double post:
  ```sql
  UPDATE OR IGNORE digests SET guild_id = :imported
  WHERE guild_id IS NULL
    AND :imported = (SELECT guild_id FROM guilds WHERE imported_at IS NOT NULL)
  ```
- Stories v2.2 inserts get `summary_id NULL`. Its once-per-code check reads `alerted_codes`, which v3 keeps writing globally.

### 3.3 The one-time import (`newsbot/guilds/importer.py`)

**Trigger.** `ensure_imported(db_path, cfg, now) -> ImportReport | None` runs when `cfg.legacy is not None` and the `guilds` table has no rows, checked inside the transaction. It's called by `__main__` after `migrate()` and by the CLI, both before anything else touches per-guild data.

**Idempotency:**
- One `BEGIN IMMEDIATE` transaction.
- Re-check `SELECT COUNT(*) FROM guilds` under the lock; if it's non-zero, `ROLLBACK` and return None.
- Any exception rolls everything back and the process exits 2 with the reason, since a half import would be worse than none.
- Once any `guilds` row exists, it never runs again, including after the owner deletes the old keys.

**What it writes, all in that one transaction:**
1. **`guilds`:** `guild_id`, `digest_time` and `timezone` from `digest:`, `admin_channel_id` from the top-level key, `tier='comped'`, `set_up=1`, `joined_at` and `imported_at` set to now.
2. **`guild_games`:** one row per legacy topic, as `(key, channel_id)`, in config order. More than 10 topics fails with a message: the import refuses rather than silently dropping games.
3. **`guild_shift`:** only if `alerts.enabled`. It takes the channel, `ping='everyone'` (or `'none'` when the old `max_pings_per_day` is 0), `enabled_at` set to now, and `ping_day`/`ping_count` copied from `alert_state`, so today's spent budget carries over.
4. **`guild_lounge`:** only if either lounge feature is enabled. It takes the channel, welcome flag and text, quote flag and time, `quote_sources` as JSON of the already-resolved `QuoteSourceCfg`s (the default list is materialized), and `last_quote_date` from `lounge_state`.
5. **Backfills:**
   - `UPDATE digests SET guild_id = ? WHERE guild_id IS NULL`, which makes today's already-posted v2 digest this guild's and prevents a second post on upgrade day;
   - `INSERT INTO guild_code_posts SELECT ... FROM alerted_codes WHERE status IN ('posted', 'failed')`, carrying `message_id`, `pinged` and `from_roundup` across;
   - `UPDATE lounge_quotes_used SET guild_id = ? WHERE guild_id IS NULL`.
6. **`app_state`:** `import` holds a JSON copy of the report.

**Logging.** One INFO line per written section, with game keys, channel ids, time, zone, the SHiFT ping choice, the lounge flags and the source count. Then one line: `these keys are now only read by the import and can be deleted from config.yaml: guild_id, digest.time, digest.timezone, topics[].channel_id, alerts.enabled, alerts.channel_id, lounge (plus topics and sources once catalog: is in place)`. The first `on_ready` also sends the owner channel one short alert: "Imported the v2 setup for this server (3 games, SHiFT, lounge); the details are in the log."

**Tests:**
- New fixture `tests/fixtures/config_v2_prodlike.yaml`: `config.example.yaml`'s shape and sources with fake guild and admin ids. It uses the prod channel ids already committed in the lounge plan:
  - SHiFT 1553597251933438122;
  - Borderlands 4 1452017235274240221;
  - Palworld 1542581309845799013;
  - Diablo IV 1531681353681211524;
  - lounge 1401806745898061826.

  Its lounge block has welcome on (synthetic text), the quote at 08:00, and the eight prod Wikiquote titles from CLAUDE.md. `alerts.enabled: true` with `max_pings_per_day: 3`.
- New `v22_db` fixture in `tests/conftest.py`: apply 001 to 004, then insert representative rows:
  - digests for yesterday (`ok`) and today (`ok`, with ids);
  - stories plus `story_items`;
  - `alerted_codes` in every status, `alert_state` with today's ping count;
  - `lounge_quotes_used` and `lounge_state`.

  Then migrate to 5.

### 3.4 Hourly shared collection (`newsbot/pipeline/collect.py`)

`run_collection(deps) -> CollectionOutcome` runs under the existing run lock using `run_lock_or_skip`. It's scheduled on `IntervalTrigger(minutes=collection.interval_minutes)`, first run 2 minutes after startup, with `coalesce` and `max_instances=1`. One pass:

1. **Collect** every catalog game's sources plus `shared_sources` through `run_collectors`, sharing `RateLimitState` for Reddit.
   - Each catalog source's items get `topics=(game_key,)`, making it a dedicated source.
   - Shared items keep `games` as `topics`, or `None`.
   - **No web search here.**
2. **Normalize** with the store dedupe and `lookback_hours`, as today. Then **filter** against the whole catalog with `max_items_per_game`.
3. **Store** with the new `repo.store_items(conn, items, now)`, factored out of `save_run`. It inserts into `items` and `item_topics` with `ON CONFLICT DO NOTHING` and `collected_at` set to now. Items are shared, and `item_topics` is the game tag.
4. **Source health is global.** `record_source_result` runs for every non-skipped result, reversing A9 (sweeps used to never write it). When a source reaches exactly 12 consecutive failures (D8), the owner gets one alert; the counter reset on success makes it re-arm.
5. **SHiFT detection** runs on *all* this pass's collected items, before dedupe, filtered to `shift.games`. That keeps v2's behavior of catching a code edited into an already-seen Reddit thread. Fan-out is in §3.10.
6. **Record the pass** with `record_sweep` (the summary text becomes "17/19 sources ok, 214 new items, 1 new code") and keep per-pass counts in memory for the owner's daily report.

**Web search** (comped only): `collect_web_search(deps, games)` builds `WebSearchCollector(web_search_cfg, games, key)` for exactly the games followed by a comped guild. It runs once per game per summary day, from the summaries job (§3.6), and stores items the same way. Brave usage is unchanged for the friend: 2 queries times 3 games.

**What `run_daily` becomes.** Its pieces are reused and the function itself is retired at cutover:
- collect, normalize and filter move to `collect.py`;
- summarize moves to `summaries.py`;
- claim, publish with retry, save, the report and the failure alerts move to `guild_digest.py`.

### 3.5 The per-guild digest scheduler (`newsbot/guilds/schedule.py` pure, `newsbot/pipeline/guild_digest.py` I/O)

**Every-minute job** (`IntervalTrigger(minutes=1)`, `coalesce`, `max_instances=1`, `misfire_grace_time=30`). It does nothing until the first `on_ready`, because the channel cache isn't ready before then.

**Due check.** SQLite can't do IANA time zones, so SQL fetches the candidates and a pure function decides:
```sql
SELECT g.guild_id, g.digest_time, g.timezone, g.tier,
       d.run_date, d.status, d.posted_by_game, d.attempts, d.updated_at, d.window_end
FROM guilds g
LEFT JOIN digests d ON d.id = (SELECT id FROM digests WHERE guild_id = g.guild_id
                               ORDER BY run_date DESC LIMIT 1)
WHERE g.set_up = 1 AND EXISTS (SELECT 1 FROM guild_games WHERE guild_id = g.guild_id)
```

`due_guilds(rows, now_utc, running) -> list[DueGuild]`:
- `local_date = now.astimezone(tz).date()` and `due_at = local_due_instant(local_date, time, tz)`.
- **Due** when `now >= due_at` and one of these holds:
  - no row for `local_date`;
  - the row is `failed` with nothing posted and `attempts < 3` and `updated_at` more than 10 minutes ago (the v2 clean-failure retry, bounded so it can't hammer);
  - the row is `pending`, the guild isn't in this process's `running` set, and it was left by a previous process (D7 resume).
- **Not due** if the latest `run_date` is after `local_date` (a clock step, or a time zone change eastward).
- Results are sorted by `due_at`, oldest first.

**DST handling:**
- `local_due_instant` builds `datetime.combine(day, time, tzinfo=ZoneInfo(tz))` with `fold=0` and converts it to UTC.
- A nonexistent spring-forward time (02:30) resolves to 03:30 local, which is the next instant after the gap.
- An ambiguous fall-back time (01:30) takes the first occurrence. The second can't double-run, because the row exists.
- Polling instants instead of cron sidesteps APScheduler's 00:xx spring-forward bug for digests.

**Catch-up** falls out of the same check: after downtime, the first tick finds every guild that missed its time. There's no separate `_catch_up`.

**Item window.** Windows chain, so they have no gaps or overlaps:
- `window_start` is the previous digest row's `window_end` for that guild (any status that posted), floored at `window_end - 48h`. For a first digest it's `window_end - 24h`.
- `window_end` is `due_at` for scheduled and catch-up runs, or now for `run-now` and preview.
- Items are selected by `collected_at` in `(start, end]`, joined through `item_topics` to the guild's games.
- Items collected during downtime land in the next window, so nothing is lost; §4 lists the thin-day caveat.

**Guard, per guild:**
- `claim_guild_digest(conn, guild_id, run_date, *, force, window, now)` runs under `BEGIN IMMEDIATE`, with the same semantics as `claim_digest` but keyed on `(guild_id, run_date)`. It increments `attempts`.
- `mark_guild_digest_failed` and `save_guild_digest` mirror the existing functions. `save_guild_digest` writes the status, `posted_message_ids` (the list, for v2.2 compatibility), `posted_by_game`, notes and the window.

**Resumable publisher:**
- `DiscordPublisher(client, *, nonce_scope, on_posted)`, where `nonce_scope = f"{guild_id}|{run_date}"`.
- `on_posted(game_key, message_id)` writes through to `digests.posted_by_game` after each send.
- A resume preloads `_posted` from the row, so it skips games that already landed.
- **Change from §13 D2:** a permanent per-channel error (404, 403, a non-text channel) now *skips that game and continues* instead of stopping the whole guild's digest. The skipped game and its reason go to the notes, the status becomes `partial`, and the guild notifier is told. One deleted channel shouldn't cost a server its other games, and §15 says "that part is skipped and reported".
- Transient errors keep today's backoff (2, 4 and 8 seconds, then 429 handling).

**Free render.** `format.render_headlines_embed(game, items, note=None)` generalizes `_fallback_embed`:
- D4 ordering and the `🟢 OFFICIAL` marker;
- escaped titles and `_safe_link`;
- whole lines shed from the bottom with a final `+N more, use /news` line under 4096 UTF-16 units.

A game with no items posts nothing. Games post in catalog order.

**Comped render:** `_topic_embed` over the stored stories for that game's summary (§3.6). A `fallback` summary uses `render_headlines_embed` with the "Summary unavailable" note (today's fallback wording) and status `partial`. The coverage notes from `game_summaries.coverage_notes` go in the footer (§13 D1).

**Pacing:**
- Guilds run one after another inside the tick, with `await sleep(_GUILD_PACE_S)` (1.0 s) between guilds. discord.py handles per-route 429s.
- If a tick is still running at the next minute, `max_instances=1` skips that tick. The claim guard makes an overlap harmless anyway.
- A per-guild `asyncio.Lock` (`bot.guild_lock(guild_id)`) serializes the scheduler and that guild's `run-now` and `preview`.
- The run lock (collection) isn't taken: digests only read stored data, and WAL keeps the reads concurrent with collection writes.

**Run report.** After an `ok` or `partial` post, when `run_report` is true, `render_guild_run_report` goes to the guild's admin channel only. It shows per-game counts with jump links, skipped games and duration, but no spend.

### 3.6 Comped summaries (`newsbot/pipeline/summaries.py`)

**`prepare_summaries(deps, now)`** runs on its own job every 5 minutes, so digests never wait on Claude:
- It finds each game followed by a comped guild and the earliest `due_at` among those comped guilds for that game's local day.
- When `now >= earliest_due - 30 minutes` and no reusable summary exists (below):
  1. run `collect_web_search` for those games (skipped with a coverage note if there's no key or the quota is exhausted);
  2. for each game, select the items in `(previous summary's window_end, now]` (first time: `now - 24h`, floor 48h);
  3. run `summarize_topic(llm, game, items, prior_headlines, all_topics=<games followed by any comped guild, catalog order>, subject=ai.subject)`.

  With only the friend comped, `all_topics` is Borderlands 4, Palworld and Diablo IV in the same order as today, so **the prompt is byte-identical** and no owner preview gate is triggered. A second comped guild would change that list; §4 notes it.
- **Store,** in one transaction: the `game_summaries` row (`ok`, or `fallback` after summarize's own 3 attempts), plus `stories` with `summary_id` set and `digest_id NULL`, plus `story_items`. Saving at summary time rather than at post time is safe now, because items were already stored by collection. It also makes prior headlines exist even if the post fails.

**Reuse rule.** A comped digest due at `due_at` uses the newest `game_summaries` row for the game with `window_end` in `(due_at - 24h, due_at + 30min]`. That's "computed once per game per day and reused by every comped guild following it".

**If it fails:**
- **Claude fails:** the row is `fallback`, the digest posts headlines with "Summary unavailable", status `partial`, and the owner gets one alert (`newsbot: summary for {game} fell back to headlines`). It isn't retried that day by the scheduler.
- **The prepare job didn't run** (downtime): the digest calls `ensure_summary(game, due_at)` inline, once, with no web search (coverage note: "web search skipped"). If that fails too, it falls back as above.
- **`run-now` with confirmation** on a comped guild retries a `fallback` summary once. An admin asked for it explicitly.

**Spend:** `/owner servers` and the owner report sum `game_summaries` tokens by month. v2 digest tokens stay in `digests` and get counted too.

### 3.7 Commands

**Registration** (`NewsBot.setup_hook` via a new `bot/registration.py`):
- **Global commands:** `/news` (recent, search), `/shift codes`, and `/newsbot`. All are `guild_only=True`, installable to guilds only, and the admin group carries `default_permissions=Permissions(manage_guild=True)` (from `admin_permission`).
- **`/owner servers`** is added with `tree.add_command(owner_group, guild=Object(home_guild_id))` (D3).
- **`/lounge quote-now`** is added per guild with a `guild_lounge` row where the quote is enabled (D3).
- **Production** (`command_guild_ids` empty): `tree.sync()` globally, plus `tree.sync(guild=...)` for the home and lounge guilds.
- **Dev and self-host** (`command_guild_ids` non-empty): `copy_global_to(guild)` and `sync(guild=g)` for each listed guild only, never global. Changes show up instantly instead of taking up to an hour. **Recommend this for the dev bot** (the private test guild plus the second test guild) and for any self-hoster.
- **Sync only when something changed:** hash `[c.to_dict(tree) for c in tree.get_commands(guild=...)]` per scope, compare it with `app_state['commands:<scope>']`, and sync only on a change. That avoids a global overwrite on every restart.
- **Coexistence:** the dev app has never synced globally (v2 synced per guild), so no stale global set exists. If a stale global set ever shows up, an owner step clears it once (`tree.clear_commands(guild=None)` plus a sync), documented in `docs/deploy.md`.

**Run-time checks.** Every admin handler:
- requires `interaction.guild_id` and `has_admin_permission(interaction.permissions, cfg.admin_permission)`, as today;
- loads the guild row; a missing row gets created on the fly (a join event missed during downtime);
- replies ephemerally in every case.

`/owner servers` additionally requires `await bot.is_owner(interaction.user)`.

**`/newsbot setup`** (`bot/setup_views.py`, a `SetupView(discord.ui.View)` with a 600 s timeout and owner-checked with `is_command_owner`):

| Row | Component | Options |
|---|---|---|
| 1 | `discord.ui.Select`: time zone | 24 curated zones + "Other: use /newsbot settings timezone:". Curated: UTC, Pacific/Honolulu, America/Anchorage, America/Los_Angeles, America/Phoenix, America/Denver, America/Chicago, America/Mexico_City, America/New_York, America/Halifax, America/Sao_Paulo, Europe/London, Europe/Paris, Europe/Berlin, Europe/Helsinki, Europe/Moscow, Africa/Johannesburg, Asia/Dubai, Asia/Kolkata, Asia/Singapore, Asia/Shanghai, Asia/Tokyo, Australia/Sydney, Pacific/Auckland. The current value is preselected. |
| 2 | `Select`: digest time | "00:00" to "23:00" (24 options); minutes via `/newsbot settings time:`. Default 09:00 or current. |
| 3 | `Select`: games | Catalog (≤25), `min_values=1`, `max_values=min(10, len)`, currently followed preselected. |
| 4 | `discord.ui.ChannelSelect` | `channel_types=[text, news]`, one value, `default_values` = the channel most followed games use. |
| 5 | Buttons | Save / Cancel. |

**Save:**
- Validates that each select has a value.
- In one transaction: updates `guilds` (time, zone, `set_up=1`); deletes unselected `guild_games`; upserts selected games. Newly selected games get the chosen channel. Already-followed games keep their channel unless the channel select was changed, in which case every selected game moves to it.
- Runs the permission check (§3.9) for the touched channels and edits the message with the result: a summary, missing permissions by channel, a SHiFT hint if Borderlands 4 is selected, and the time of the next digest.
- If "Other" was picked for the zone, the previous zone is kept and the reply says to use `/newsbot settings`.

**Other admin commands:**
- **`follow game:<autocomplete> channel:<TextChannel>`:**
  - autocomplete over catalog games not followed, matching by name or key, 25 at most;
  - the value is validated against the catalog server-side;
  - the 10-game limit gives "This server already follows 10 games; unfollow one first", and the database trigger backs it up;
  - an immediate permission check on `channel`.
- **`unfollow game:<autocomplete>`:** autocomplete over followed games. Unfollowing the last game leaves `set_up=1` but the digest job ignores the guild; the reply says so.
- **`games`:** followed games with `<#channel>`, then the rest of the catalog.
- **`settings time: timezone: admin_channel: clear_admin_channel:`:**
  - all options optional;
  - `time` must be HH:MM;
  - `timezone` autocompletes over `zoneinfo.available_timezones()` by substring (25 at most) and is validated with `ZoneInfo`;
  - `admin_channel` gets a permission check;
  - if the new time has already passed today with no digest yet, the reply says it will post within a minute.
- **`shift channel: enabled: ping:<none|everyone|role> role:<Role>`:**
  - `ping=role` requires `role`;
  - enabling sets `enabled_at` to now (no backlog, §3.10);
  - explains itself if Borderlands 4 isn't followed: "Saved, but nothing will post until this server follows Borderlands 4";
  - checks permissions, including Mention @everyone when the ping is `everyone` or the role isn't mentionable.
- **`status`,** scoped to the server:
  - tier, time and zone, next due time;
  - last digest (status, per-game jump links);
  - followed games with source health counts for their sources (for example "Fortnite: 4 of 5 sources ok");
  - SHiFT settings and pings today out of 3;
  - the last 5 `guild_notices`.

  No spend.
- **`preview`:** the next digest for this guild, as ephemeral "Would post in <#id>:" messages, with window `(last window_end, now]`. It writes nothing; comped guilds use a reusable summary or compute one in memory.
- **`run-now`:** v2's confirmation logic, now per guild (`needs_confirmation` on the guild's row).
- **`test-alert`:** kept, gated on `shift.allow_test_command`, fanning out to the invoking guild only.

**Owner:** `/owner servers` (home guild only) shows counts only:
- servers, set up, free/comped;
- today's digests: ok, partial, failed, pending, not yet due;
- SHiFT-enabled servers, servers with permission problems;
- month spend.

**Member commands:**
- `/news recent game:<autocomplete: followed games + All>` validates the value is a followed game and limits the query to followed keys.
- `/news search` gains a `game_keys` filter in `search_stories` (and `search_items`, if D2 is A), limited to followed games.
- `/shift codes` works when the guild follows any `shift.games` game; otherwise: "This server doesn't follow Borderlands 4."
- Before setup, all three reply: "This server hasn't set up newsbot yet; an admin can run /newsbot setup."

### 3.8 Guild lifecycle (`newsbot/guilds/lifecycle.py` pure, handlers in `client.py`)

**`on_guild_join(guild)`:**
- `INSERT OR IGNORE` a `guilds` row (free, or comped when the id is in `comped_guild_ids`; not set up; `joined_at` now).
- Only when the row was actually created, post the one first-contact message:
  - target: `guild.system_channel` if the bot can View and Send there, otherwise the first `guild.text_channels` by position where it can;
  - content: plain text, `AllowedMentions.none()`, no mentions, including the text itself. It says what the bot does and "An admin with Manage Server can run `/newsbot setup` to pick games, a channel and a time";
  - no channel available: log only.

  It's the only unprompted message.

**`on_guild_remove(guild)`:** `DELETE FROM guilds WHERE guild_id = ?`. The cascade removes games, SHiFT, lounge, notices, code posts and digests; stories' `digest_id` becomes NULL. Shared items, stories and summaries stay. Owner log line only.

**Startup reconciliation**, in the first `on_ready`, after the import and the adopt step:
- Rows for guilds the bot is no longer in are deleted.
  - **Safety valve:** if that would delete more than max(3, 10%) of rows, skip it and alert the owner. A partial guild list during an outage must never wipe every server.
  - Unavailable guilds count as present.
- Guilds with no row (joined while the bot was down) get the join treatment, first-contact message included.
- `comped_guild_ids` are applied (upgrade only).

**Deleted or unusable channels:**
- Nothing picks a new channel automatically.
- At post time the game (or the SHiFT post) is skipped and reported through the notifier (§3.9).
- `on_guild_channel_delete` doesn't change settings; it adds one notice naming what used that channel, so admins find out before the next digest.

### 3.9 Permission checks and admin routing

**`permissions.py` becomes per guild:**
- `required_channels_for_guild(settings) -> list[ChannelRequirement]`:
  - game channels: view, send, embed_links;
  - SHiFT channel when enabled: view and send, plus `mention_everyone` when the ping is `everyone`, or it's a role that isn't mentionable (checked at run time with `role.mentionable`);
  - admin channel: view and send;
  - lounge channel: view and send.
- `check_guild_channels(client, guild_id, settings) -> list[str]` reuses `_check_one` with the guild id.

**When checks run:**
- After `setup`, `follow`, `settings` and `shift`: the reply names each channel and the missing permission.
- At startup for every guild: problems go to that guild's notifier only if the text differs from `guilds.permission_problems` (stored), so every deploy doesn't re-post the same complaint. The owner gets one line: "permission problems in 4 of 38 servers".

**Routing (`newsbot/guilds/notify.py`):**
- `GuildNotifier.notify(guild_id, text)`:
  - always inserts a `guild_notices` row (escaped, capped at 2000, keeping the newest 20 per guild);
  - also posts to `guilds.admin_channel_id` if set, with `AllowedMentions.none()`, via a variant of `send_alert`;
  - never falls back to the owner channel.
- `NewsBot.alert()` (the owner channel) is reserved for:
  - source health (D8), crashes at job boundaries, Claude fallbacks;
  - the import notice;
  - the permission problem counts, the reconciliation safety valve;
  - the daily owner report.
- **Daily owner report,** a cron job at `owner_report.time` in its zone. One message:
  - line 1: `digests posted to 37 of 38 servers; 1 failed (missing permissions)` (counts from today's rows, grouped by failure note category);
  - then the collection report: sources ok/failing, items stored in 24 h, new codes, Claude and Brave use today and this month.

  `app_state['owner_report_date']` guards against sending twice.

### 3.10 SHiFT fan-out per guild (`newsbot/shift/fanout.py`, with `sweep.py` reshaped)

**Global detection** is unchanged in spirit:
- `sightings_from_items` scoped to `shift.games`, `aggregate`, and `plan_alerts` against global `known_codes` and the global seeded marker.
- `plan_alerts` gets `pings_today=0, max_pings=1` here, because pings are decided per guild. Only the silent / to_post / roundup split is used globally.
- `record_silent_codes` is unchanged.
- The new `record_released_codes(conn, to_post, roundup_to_post, now)` inserts both lists into `alerted_codes` with `status='posted'` (meaning "released to post"; per-guild outcomes live in `guild_code_posts`), with `from_roundup` set and `pinged=0`. That keeps v2.2's once-per-code check and `/shift codes` correct.

**Eligible guilds:** `set_up=1`, `guild_shift.enabled=1` with a channel, following a `shift.games` game, and `enabled_at <= now`. Only codes released *in this pass* fan out, so enabling alerts never produces a backlog.

**Per guild,** in `guild_id` order, each wrapped in try/except so one guild never blocks another, with a 0.2 s pace:
- **Ping decision:** `want_ping = guild.ping != 'none' and any(c.trusted for c in to_post)`.
- **`claim_guild_codes(conn, guild_id, codes, pinged=want_ping, local_day, max_pings=shift.max_pings_per_day, from_roundup)`:**
  - `BEGIN IMMEDIATE`;
  - resets the guild's `ping_day` in *the guild's* time zone;
  - re-checks the cap and inserts `guild_code_posts` as `pending`;
  - returns the actual ping.
- **Render:** `render_code_alerts(to_post, ping_mention=<"@everyone" | "<@&id>" | None>, test)`. `RenderedAlert.ping_prefix` is stored so `_strip_ping` removes exactly that prefix on retry. The width check uses the longer role mention as the worst case.
- **Post:** `DiscordCodeAlertPoster(client, channel_id, ping_choice, notify)`. It checks `mention_everyone` (or `role.mentionable`) before a ping; a missing permission posts anyway and notifies *the guild*, once per pass.
- Mark each guild's rows `posted` or `failed`. Failures go to that guild's notifier.
- Roundups: `render_roundup_alerts`, always unpinged, claimed with `from_roundup=1`. The cap-reached note goes to the guild notifier (only when the cap is above 0 and the ping isn't `none`).

**Startup:** `fail_pending_guild_codes()` flips `pending` to `failed` and notifies each affected guild. The owner gets a count only. The legacy `fail_pending_codes()` still runs once for pre-import rows.

**Mentions (`bot/client.py`):**
```python
_PING_EVERYONE = discord.AllowedMentions(
    everyone=True, users=False, roles=False, replied_user=False
)


def mentions_for(ping: str | None) -> discord.AllowedMentions:
    """The one place a ping choice becomes an AllowedMentions."""
    if ping == "everyone":
        return _PING_EVERYONE
    if ping and ping.isdigit():
        return discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(id=int(ping))], replied_user=False
        )
    return discord.AllowedMentions.none()
```
The tripwire still sees exactly one `everyone=True`. `test_mentions_tripwire.py` gains a test that `mentions_for` is the only caller that returns `_PING_EVERYONE`.

### 3.11 The lounge, keyed by `guild_lounge`

- **Loading:** `NewsBot` loads `guild_lounge` rows at startup into `self._lounges: dict[int, LoungeSettings]`, reloading after the import or a D5 re-sync.
- **Intents** are built in `__main__` after migrate, import and re-sync: `intents.members = any(l.welcome_enabled for l in lounges)`. The client uses `chunk_guilds_at_startup=False`; `on_ready` runs `await guild.chunk()` for each lounge guild with welcomes on, which keeps `on_member_update` working without chunking hundreds of guilds.
- **The `PrivilegedIntentsRequired` message** becomes: "a lounge welcome is enabled (guild_lounge / lounge.welcome.enabled) and needs the Server Members intent...". The 10-minute wait is unchanged.
- **Welcomes:** `on_member_join` and `on_member_update` look up `self._lounges.get(member.guild.id)`. No row, or welcomes off, means return, which replaces `welcome_action`'s `in_guild` input. Everything else is as in v2.2.
- **Quotes:**
  - one cron job per lounge guild with the quote on: `schedule_daily_quote(scheduler, guild_id, quote_time, guild_timezone, callback)`, `id=f"daily-quote-{guild_id}"`, same grace and no catch-up;
  - `build_quote_deps(guild_id)`: sources from the row, `local_day` in the guild's zone, `post` to the row's channel, `alert` to that guild's notifier (the friend's admin channel, as today).
- **`claim_quote` gains `guild_id`:**
  - the date guard reads and writes `guild_lounge.last_quote_date`, and also mirrors `lounge_state.last_quote_date` for rollback;
  - deck reads and reshuffle deletes filter on `guild_id` (plus `guild_id IS NULL` rows until the import backfills them).
- **`/lounge quote-now`,** guild-scoped (D3), with the same behavior as v2.2's `quote-now`.

### 3.12 Privacy, terms and going public

**`site/privacy.html`** (new effective date):
- per server, the bot stores: the server id, chosen channel ids, digest time and time zone, followed games, the SHiFT ping choice (which may be a role id), the tier, recent problem notes, and digest and SHiFT post records;
- all of it is deleted as soon as the bot is removed from the server;
- nothing is stored about members;
- the admin-denied log line with a user id stays as written;
- the lounge welcome and quote run only on one server (the owner's friend's), so the member-join paragraph applies only there;
- public news and Claude processing are unchanged; AI summaries run only for comped servers;
- the support contact (D10).

**`site/terms.html`** (new date):
- a public app, free tier: headline digests and SHiFT alerts;
- no service level; the owner may leave or refuse any server;
- server admins are responsible for the channels and pings they configure;
- the support contact.

The going-public order is in §9.

---

## 4. Tasks

Each task lists its files, the change, its tests (test-first where marked), and "Verify: gate". Tasks 1 to 12 add code behind the v2.2 runtime path; the bot keeps running v2 behavior until task 13.

**Parallelism:**
- 1 ∥ 2 (config.py vs `store/`);
- 4 ∥ 6 after 1, 2 and 3 (collect.py vs guild_digest.py; both touch `format.py` only in separate new functions, so coordinate on merge);
- 7 ∥ 8;
- 17 runs alongside everything.

Everything else is sequential.

### Task 1: Config v3 (catalog, shared sources, derive mode, legacy view)

**Files:** `newsbot/config.py`; a new `GameInfo` protocol used in `newsbot/pipeline/filter.py` (`match_name`), `prompts.py`, `summarize.py` and `collectors/web_search.py` (type hints only); `newsbot/collectors/base.py` (`build_catalog_collectors(cfg, secrets)` builds catalog plus shared collectors with the game tag; `build_collectors` stays for v2 until task 13); new fixtures `tests/fixtures/config_v3.yaml` and `tests/fixtures/config_v2_prodlike.yaml`.

**Change:** everything in §3.1. The v2 fields stay populated on `AppConfig` for old-shape configs. `configured_source_names` covers the catalog and shared sources.

**Tests** (test-first, `tests/test_config_v3.py`, `tests/test_config_v3_derive.py`):
- every validation message above, and several errors in one `ConfigError`;
- deriving from `config_v2_prodlike.yaml`: dedicated vs shared sources, web search, alerts to shift, digest to collection and ai;
- `LegacySetup` contents;
- a hybrid config (old keys plus `catalog:`): catalog wins, legacy is still built, and a legacy topic missing from the catalog is an error;
- `match_name=False` matching;
- `config.example.yaml` and `config.minimal.yaml` still load (derived);
- a hybrid-config test pinning the v2.2 `AlertsCfg` and `LoungeCfg` field sets, so no v3 key ever appears inside `alerts:` or `lounge:`.

**Verify:** gate. The existing suite passes unchanged.

### Task 2: Migration 005, runner, models, repo for guild tables

**Files:** `newsbot/store/db.py` (the foreign-keys-off header); `newsbot/store/migrations/005_public_app.sql` (header comment in the style of 002 to 004, saying plainly that two tables are rebuilt as supersets and why); `newsbot/store/models.py` (`GuildSettings`, `GuildGame`, `ShiftSettings`, `LoungeSettings`, `GuildDigestRow`, `GameSummaryRow`, `Notice`); `newsbot/store/repo.py` (new "Guilds (design.md §15)" section).

**New repo functions, all parameterized:**
- guilds: `create_guild`, `get_guild`, `list_set_up_guilds`, `update_guild_settings`, `delete_guild`;
- games: `set_guild_games`, `follow_game`, `unfollow_game`;
- SHiFT and lounge: `get_shift`/`set_shift`, `get_lounge`/`list_lounges`/`upsert_lounge`;
- notices: `add_notice` (prunes to 20), `recent_notices`;
- `app_state_get`/`app_state_set`;
- `adopt_orphan_digests`.

If D2 is A, also `items_fts` and `search_items`/`query_items`.

**Tests** (`tests/test_migration_005.py`, `tests/test_repo_guilds.py`, `tests/conftest.py` with the `v22_db` fixture):
- `user_version == 5`; ids and data preserved in `digests` and `stories`;
- `PRAGMA foreign_key_check` is empty, and `INSERT INTO stories_fts(stories_fts) VALUES('integrity-check')` passes;
- the FTS triggers still work after the rebuild, and `/news search` finds old stories;
- **v2.2 compatibility on a v5 database:**
  - v2.2-shaped SQL runs (`claim_digest(run_date)` inserts a NULL-guild row; `save_run` with stories; `claim_codes`; `claim_quote`);
  - two guilds can hold rows on the same date, and a duplicate `(guild, date)` is rejected;
- deleting a guild cascades to every per-guild table, and a comped guild's digest deletion nulls `stories.digest_id`;
- the 10-game trigger;
- the `guild_shift` CHECKs for ping shape and "enabled needs a channel";
- the runner rolls back on a foreign-key violation, using a synthetic migration in `tmp_path`;
- migrations are idempotent, and concurrent migrate follows `test_db_concurrency.py`.

**Verify:** gate.

### Task 3: One-time import

**Files:** new `newsbot/guilds/__init__.py` (docstring only, no imports, to avoid cycles), `newsbot/guilds/importer.py`, and repo helpers for the backfills.

**Change:** §3.3. It isn't called from `__main__` yet (task 13 wires it).

**Tests** (test-first, `tests/test_import_v2.py`, using `config_v2_prodlike.yaml` and `v22_db`):
- exactly the rows in §3.3, with values checked field by field;
- **today's v2 digest row becomes the guild's, and the due check (once task 6 exists) says "not due"** (added in task 6);
- the ping count carries over; `last_quote_date` carries over;
- `guild_code_posts` gets exactly the posted and failed codes;
- a second call is a no-op, and concurrent calls produce exactly one import;
- any `guilds` row present means it never runs; no legacy config means nothing happens;
- alerts off means no `guild_shift`; `max_pings_per_day: 0` means `ping='none'`; lounge off means no `guild_lounge`;
- 11 topics fails with nothing written (rollback proven);
- the log records list the deletable keys and contain no welcome text.

**Verify:** gate.

### Task 4: Hourly shared collection

**Files:** new `newsbot/pipeline/collect.py`; `repo.store_items` (factored out of `save_run`, which now calls it); `pipeline/run.py` keeps `run_daily` for now.

**Change:** §3.4 steps 1 to 4 and 6, plus `collect_web_search`. The SHiFT hook is an injected callable that defaults to a no-op until task 5. D8's threshold is a module constant.

**Tests** (`tests/test_collect.py`, fixture collectors, temp database):
- items are stored once with the right `item_topics`;
- shared items are matched by keyword across games; `match_name` is honored;
- dedupe across passes; the lookback drop;
- source health written on every pass;
- the owner alert fires once at exactly 12 consecutive failures and re-arms after a success;
- skipped (quota) results don't count as failures;
- web search runs only for the given games, and never inside `run_collection`;
- the run lock is skipped when held;
- a pass with every source failing stores nothing and raises nothing.

**Verify:** gate.

### Task 5: SHiFT global detection and per-guild fan-out

**Files:** new `newsbot/shift/fanout.py`; `shift/sweep.py` (`process_items` gains a fan-out path used by `collect.py`; the v2 `run_code_sweep` and `_apply_plan` stay until task 13); repo (`record_released_codes`, `claim_guild_codes`, `mark_guild_codes_posted`/`failed`, `fail_pending_guild_codes`, `eligible_shift_guilds`); `bot/format.py` (the `ping_mention` parameter and `RenderedAlert.ping_prefix`; `render_code_alerts` keeps its defaults for v2 callers); `bot/client.py` (`mentions_for`, a per-guild `DiscordCodeAlertPoster`).

**Change:** §3.10.

**Tests** (test-first where pure; `tests/test_shift_fanout.py`, `tests/test_code_alert_poster_guild.py`, plus a tripwire update):
- two guilds with different pings (everyone, role, none) each get exactly the codes, with the right prefix and `AllowedMentions`;
- per-guild caps: guild A at 3 pings posts unpinged while guild B still pings;
- caps reset on each guild's own local day;
- an untrusted-only batch never spends a ping;
- roundups are never pinged;
- guild A's send raising doesn't stop guild B, and A's failure notifies A only (never the owner);
- enabling after a code was released: that code isn't posted (no backlog);
- a guild not following Borderlands 4 gets nothing;
- a retry strips the role prefix too;
- `fail_pending_guild_codes` notifies per guild;
- the global `alerted_codes` row is written once whatever the guild count;
- the mentions tripwire still holds, and a role mention is literal `everyone=False`.

**Verify:** gate.

### Task 6: Per-guild digest core

**Files:** new `newsbot/guilds/schedule.py` (pure: `local_due_instant`, `due_guilds`, `digest_window`), new `newsbot/pipeline/guild_digest.py` (`run_guild_digest(deps, guild, *, kind, force, publisher)`, `preview_guild_digest`), repo (`due_candidates`, `claim_guild_digest`, `record_posted_game`, `save_guild_digest`, `mark_guild_digest_failed`, `items_for_window`, `last_window_end`), `bot/format.py` (`render_headlines_embed`, `render_guild_digest`, `render_guild_run_report`), `bot/client.py` (the `DiscordPublisher` write-through and skip-and-continue, behind new constructor arguments; v2 defaults unchanged).

**Change:** §3.5.

**Tests** (test-first for schedule.py, `tests/test_schedule.py`):
- due before and after the time; guilds in 5 zones at once;
- the latest row after today's date means not due;
- the spring-forward gap (02:30 resolves to 03:30), fall-back runs once;
- a UTC midnight crossing (a Pacific guild at 17:00 local);
- `attempts` and the 10-minute backoff; pending resume only when not running (D7);
- window chaining: run-now at 08:00, then tomorrow's window starts at 08:00; a DST-length day (23 h and 25 h); the 48 h floor; the first digest takes 24 h.

**Tests** (`tests/test_guild_digest.py`):
- a free guild's digest built from shared items: only its games, catalog order, D4 ordering, "+N more" at the limit, empty games post nothing, all-empty saves `ok` with `{}`;
- the write-through `posted_by_game`; a resume skips posted games;
- a 404 on one game skips it, the others post, the status is `partial`, and the guild is notified;
- transient retries resume;
- the guard: a second claim the same day is refused, and force replaces the row in place;
- the run report goes only to the guild's admin channel;
- **the import-day test from task 3:** after the import at 14:00 local, the due check says not due for today, and tomorrow at 09:00 it's due.

**Verify:** gate.

### Task 7: Comped summaries

**Files:** new `newsbot/pipeline/summaries.py`; repo (`reusable_summary`, `save_game_summary` with stories, `stories_for_summary`, `comped_games_earliest_due`); `guild_digest.py` (the comped branch).

**Change:** §3.6.

**Tests** (`tests/test_summaries.py`, `StubLLM`, counting LLM calls):
- **two comped guilds (one Pacific, one Berlin) following Palworld: one summarize call, reused by both;**
- the first guild's lead window triggers it; nothing happens before the lead;
- `all_topics` equals the comped-followed games in catalog order, and the friend-only prompt matches today's prompt byte for byte (built with v2's `build_prompt` against the same inputs);
- web search once per game per day, only for comped games;
- Claude failure: a `fallback` row, a headline render with the "Summary unavailable" note, `partial`, one owner alert, no retry that day;
- the prepare job never ran: an inline summary at digest time with a "web search skipped" note;
- run-now force retries a fallback once;
- stories get `summary_id` set and a NULL `digest_id`, and prior headlines are found the next day;
- a free guild following the same game never triggers a summary.

**Verify:** gate.

### Task 8: Admin routing and per-guild permission checks

**Files:** new `newsbot/guilds/notify.py`; `newsbot/alerts.py` (`send_to_channel`, shared by both paths); `newsbot/bot/permissions.py` (the per-guild API; the v2 `required_channels(cfg)` stays until task 13); `bot/format.py` (`render_owner_report`, `render_guild_status`).

**Change:** §3.9.

**Tests** (`tests/test_notify.py`, `tests/test_permissions_guild.py`, `tests/test_owner_report.py`):
- **routing:** a guild problem never reaches the owner channel (a spy on the owner channel stays empty) and an owner alert never reaches a guild;
- no admin channel means only a notice; notice pruning to 20; escaping and the cap;
- requirements for each ping choice (a non-mentionable role adds mention_everyone, a mentionable one doesn't);
- the startup check notifies only on change, and the owner gets counts;
- the report's line 1 format across ok, partial and failed mixes, plus the once-a-day guard.

**Verify:** gate.

### Task 9: Commands (registration, admin per guild, members, owner)

**Files:** new `newsbot/bot/registration.py`; `newsbot/bot/commands.py` (new factories `make_member_news_group`, `make_member_shift_group` and `make_guild_admin_group`, keeping the v2 factories until task 13); new `newsbot/bot/owner_commands.py`; repo read functions scoped by game keys (`query_stories`/`search_stories` gain `topic_keys`).

**Change:** §3.7, except the setup wizard (task 10).

**Tests** (extending `tests/test_command_registration.py`; new `tests/test_guild_admin_commands.py`, `tests/test_member_commands_scoped.py`):
- global vs guild-scoped sync by `command_guild_ids`; `/owner` only in the home guild; `/lounge` only in lounge guilds;
- the hash skip (no sync on a second start);
- `guild_only` and `default_permissions` on the groups;
- the run-time denial for non-admins;
- `/owner servers` refuses non-owners, even in the home guild;
- autocomplete: catalog minus followed, followed only, zone substring, 25 at most;
- `follow` at 10 is refused;
- `settings` validation (a bad zone or time);
- `shift` with `ping=role` and no role is an error, and the Borderlands 4 hint;
- **member commands see only followed games** (a story for an unfollowed game never appears, even with a crafted `game` value);
- the not-set-up replies;
- `status` never shows spend.

**Verify:** gate.

### Task 10: The `/newsbot setup` wizard

**Files:** new `newsbot/bot/setup_views.py`; `commands.py` (wires `setup`).

**Change:** §3.7's setup table and Save rules.

**Tests** (`tests/test_setup_views.py`, driving component callbacks with fake interactions as `test_permissions.py` does):
- option counts at most 25 and the curated zone list is valid IANA;
- `max_values` is 10 (or fewer with a smaller catalog), with preselection on a re-run;
- Save with nothing selected is refused;
- the "Other" zone keeps the previous zone;
- the channel semantics on a re-run (changed vs unchanged channel select);
- only the invoker can use the view;
- Save runs the permission check and reports it;
- the timeout disables the view;
- a hostile channel or guild name in the reply is escaped.

**Verify:** gate.

### Task 11: Guild lifecycle

**Files:** new `newsbot/guilds/lifecycle.py` (pure: `pick_first_contact_channel`, `reconcile_plan(db_ids, live_ids)` with the safety valve); handlers in `client.py`, not yet registered with the v2 client (task 13 turns them on).

**Change:** §3.8.

**Tests** (`tests/test_lifecycle.py`):
- join creates the row and exactly one message, via the system channel or the first sendable channel, with `AllowedMentions.none()`;
- a rejoin when the row exists sends no message; no sendable channel sends nothing;
- `comped_guild_ids` applies at join;
- remove cascades;
- reconciliation deletes stale rows, creates missing ones, trips the valve at 11% with an owner alert and no deletes, and counts unavailable guilds as present;
- a channel delete adds one notice naming the use.

**Verify:** gate.

### Task 12: The lounge keyed by `guild_lounge`

**Files:** `newsbot/lounge/daily.py` (`QuoteDeps.guild_id`), `repo.claim_quote`/`quote_deck_state` (the guild filter and the `lounge_state` mirror), `newsbot/guilds/importer.py` (the D5 `resync_lounge_from_config`), `client.py` (lounge lookups, per-guild quote job factory, `build_intents(lounges)`), `commands.py` (`/lounge quote-now` factory).

**Change:** §3.11. v2 wiring is replaced in task 13.

**Tests:**
- extend `test_lounge_client.py` and `test_repo_lounge.py`;
- a join in a non-lounge guild is ignored;
- `build_intents` sets members only when a row has welcomes on;
- the quote job's time is in the guild's zone;
- the date guard is per guild and `lounge_state` is mirrored;
- the deck is scoped by guild;
- the D5 re-sync updates the row, logs, and a no-op re-sync writes nothing;
- the intents failure message.

**Verify:** gate.

### Task 13: Cutover (bot wiring, CLI, retire `run_daily`)

**Files:**
- `newsbot/__main__.py`: migrate, `ensure_imported`, adopt orphans, D5 re-sync, load lounges, then `NewsBot`.
- `newsbot/bot/client.py`:
  - `setup_hook`: registration (task 9), jobs (collection interval, minute digests, the summaries every 5 minutes, per-lounge quote crons, retention, owner report, heartbeat), and `fail_pending_guild_codes`;
  - `on_ready` order: the import notice, pending-code notices, reconciliation, lounge chunking, then permission checks, then enabling the minute job;
  - remove `_daily_job`, `_catch_up`, `should_catch_up`, the v2 `_sweep_job` and `publisher_for_today`.
- `newsbot/pipeline/run.py`: delete `run_daily`, `build_digest`, `_run_post` and friends; the CLI per §3.1.
- `newsbot/config.py`: remove the v2 fields from `AppConfig` (legacy parsing only).
- `bot/permissions.py`, `commands.py`, `shift/sweep.py`: delete the v2 paths.
- Retention also purges `game_summaries` and notices older than 90 days.
- **Tests:** delete or port the v2-only tests (`test_run_daily_*`, `test_client_scheduling*`, the catch-up tests, v2 registration). Keep every guard, format and safety assertion by porting it to the per-guild equivalent. Expect about 150 to 250 tests touched.

**Tests** (new `tests/test_cli_v3.py`, `tests/test_client_wiring_v3.py`):
- the job list and triggers;
- the minute job is inert before `on_ready`;
- the `on_ready` order;
- **an end-to-end fixture run:** a v2.2 database plus the prod-like config lead to the import, then a collection from fixtures, then the minute tick posting the friend's digest into fake channels at 09:00 Pacific, then no second post;
- CLI: `--check-sources --game`, `--collect`, `--dry-run` with and without `--guild` (0, 1 or 2 guilds), `--post-to-stdout` guarding per guild, `--fixtures --stub-llm` fully offline, `--now` driving the due check, `--sweep` as an alias with its note;
- all three tripwires.

**Verify:** gate. `python -m newsbot.pipeline.run --config tests/fixtures/config_v2_prodlike.yaml --db <tmp>/t.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json --now 2026-09-23T17:00:00Z --dry-run` prints the friend's per-channel digest.

**Wiring notes for task 13, collected from tasks 4 and 5 (2026-09-30):**
- Build the SHiFT hook with `make_shift_hook`, passing `notify_guild` (the per-guild admin-channel routing from task 8) and `alert_owner` (the owner alert path, used for the roundup-overflow line).
- At startup, call `deliver_queued_codes(deps, startup=True)`. That fails every stale `pending` row with a per-guild notice, then delivers anything still `queued`.
- Schedule the collection job with `collection_job_options(cfg)` (`max_instances=1`, `coalesce`).
- From task 6: build the real `publisher_for` as `DiscordPublisher(bot, nonce_scope=scope, on_posted=cb, already_posted=already, skip_permanent=True)`. `send_report` and `notify_guild` come from task 8's routing. The every-minute `run_due_guilds` job must only start after `on_ready`.
- From task 6 fixes: the command layer (task 9) must catch `GuildTimeZoneError` from `todays_guild_digest` / `preview_guild_digest` / `run_guild_digest` and reply "your time zone setting is invalid; fix it with /newsbot settings". Pass no overrides for `guild_timeout_s` / `heartbeat_s`. `send_report` must use `AllowedMentions.none()`. A pending digest resumes only after its 10-minute lease goes stale, so a restart can delay a resume by up to 10 minutes.
- Known limit: delivery runs inside the hook's 120 s timeout, so at about 170 or more SHiFT-enabled guilds a big code drop drains over several hourly passes. Moving delivery into its own job would fix this if the bot ever gets that big (a load-test item, task 14).

### Task 14: Load test

**Files:** new `tests/test_load_many_guilds.py` (fast, virtual clock) and `scripts/loadtest_digests.py` (optional, real time, scratch database, fake publisher; never run against `data/`).

**Tests:**
- 300 guilds due at 09:00 across 4 zones, 1 to 10 games each, a fake publisher enforcing about 50 sends per second globally and 5 per 5 s per channel with simulated 429s;
- every guild ends `ok`; no guild is claimed twice when ticks overlap; the pacing sleeps add up as expected;
- the total virtual time is reported and asserted under 15 minutes;
- memory stays flat (no per-guild caches retained);
- a crash injected at guild 150 plus a restart resumes without double posts (D7).

**Verify:** gate. The script's timings go in the qa report.

### Task 15: Version 3.0.0

- `pyproject.toml`: `version = "3.0.0"`.
- `newsbot/useragent.py`: `_FALLBACK_VERSION = "3.0.0"`. `test_fallback_version_matches_pyproject` already exists.
- Reinstall: `pip install -e . --no-deps`.

**Verify:** gate.

### Task 16: `qa` review

Security and bug review of tasks 1 to 15. Focus areas:
- **cross-guild isolation:** every per-guild query filters on `guild_id`; every command re-checks guild membership of options (a channel from another guild); autocomplete values are never trusted;
- the mentions tripwire and role pings;
- the migration rebuild (foreign keys, FTS, v2.2 compatibility, a crash mid-migration);
- the import's atomicity; the reconciliation safety valve;
- DST and window chaining;
- the resume semantics; the rate-limit behavior under load;
- first-contact message content;
- log privacy (no member ids or names, no welcome text);
- escaping of guild-supplied strings (channel, role and guild names) in every reply and notice;
- the comped prompt being byte-identical;
- dash and user agent rules; voice.

Fixes go back through `implement` and `test-engineer`.

### Task 17: Catalog source research (parallel from day one; network allowed for this task only)

**Agent:** `investigate`, or `implement` in research mode. Network only for this task. It never reads prod or dev config.

**For each of** Fortnite, Call of Duty (current game plus Warzone, one entry), Marvel Rivals, VALORANT, Counter-Strike 2, Apex Legends, Rust, Destiny 2, Warframe, Final Fantasy XIV, Aniimo and WARDOGS:
- web research for official feeds (studio blog RSS, Steam app ids, official Bluesky profile RSS), a subreddit `/top/.rss?t=day`, and aliases and entities;
- flag names that are common words (Rust, Destiny, Apex), which need `match_name: false` plus specific aliases;
- `search_queries` for comped use.

**Then:**
- verify with `--check-sources --game <key>` against a scratch config (catalog plus `shared_sources` copied from `config.example.yaml`), serially and politely, recording items per source and errors;
- count the Reddit feeds: each adds about 35 s to every hourly pass at the shared gap, so recommend at most one subreddit per game.

**Aniimo and WARDOGS** are included only if at least one official source returns items and name matching isn't swamped by noise; otherwise they're recorded as rejected, with the reason.

**Output:**
- a new "v3 catalog (2026-10)" section in `docs/sources-research.md` with findings and rejects;
- `docs/plans/2026-09-30-public-app-catalog.yaml` (proposed catalog entries) for owner approval (D11).

**How it lands:**
- after approval, the `docs` task (18) writes the entries into `config.example.yaml`'s `catalog:`;
- the owner adds them to prod `config.yaml` during rollout step 5 (§9). Agents never touch prod config.

Estimated 1 to 2 agent days.

### Task 18: Docs (`docs` agent, owner's voice)

- **`config.example.yaml`:** rewritten in the v3 shape (catalog of the approved games, `shared_sources`, `web_search`, `shift`, `collection`, `ai`, `command_guild_ids` with a dev note, `comped_guild_ids`, `owner_report`). A commented "v2 keys, read only by the one-time import" block explains the import and the deletable keys. The lounge example moves to that block with the D5 note.
- **`config.minimal.yaml`:** a one-game catalog plus a note on `command_guild_ids` and `comped_guild_ids` for self-hosters.
- **`docs/self-host.md`:**
  - upgrading a v2 copy (the import; nothing to change);
  - new copies (`/newsbot setup`, `comped_guild_ids`, `command_guild_ids` for instant commands);
  - the §12 "One copy per server" section rewritten;
  - CLI changes (`--guild`, `--collect`, `--check-sources --game`).
- **`docs/deploy.md`:**
  - a new §19 "Upgrading to v3.0";
  - the deploy window;
  - clearing stale global commands;
  - the pre-public backup;
  - the rollback section with the thin-day caveat;
  - §10 "Recovering a stuck digest" per guild.
- **`README.md`:** the public app, commands, the free vs comped tiers.
- **`docs/design.md`:**
  - §2 layout (`guilds/`, `collect.py`, `summaries.py`, `guild_digest.py`, `fanout.py`);
  - §5 tables;
  - §15 "As built" (the resolved items from this plan's §1 flags, D1 to D8 answers, migration 005's rebuild);
  - "Superseded by §15" notes in §6, §12 A9, §13 D2 and §14.
- **`site/privacy.html` and `site/terms.html`:** per §3.12, including the D10 contact.
- **`CLAUDE.md`:** the status and release history after release; the in-progress note; the deploy window.

**Verify:** gate. `load_config` passes on `config.example.yaml` and `config.minimal.yaml`.

---

## 5. Data and schema summary

- **Migration 005:**
  - new tables `guilds`, `guild_games` (with a 10-game trigger), `guild_shift`, `guild_code_posts`, `guild_lounge`, `guild_notices`, `game_summaries`, `app_state`, and (D2 A) `items_fts`;
  - the index `item_topics(topic_key, item_id)`;
  - `digests` and `stories` rebuilt as supersets;
  - `lounge_quotes_used.guild_id` added.
- **How it's applied:** at startup by `migrate()` (and by the CLI), in one foreign-keys-off transaction with a `foreign_key_check`.
- **The backfill** to the friend's guild id is done by the Python import.
- **Rollback:** v2.2.0 no-ops at version 5 and runs against the supersets.
- **Retention:** also purges `game_summaries` and `guild_notices` older than 90 days. `alerted_codes` and `guild_code_posts` are kept forever, like v2. Backups are unchanged.
- **Config:** a breaking change in shape, but v3 still loads v2 configs (derive plus import).

**Deploy notes from task 2's adversarial round (2026-09-30):**
- Migration 005 refuses to run if the database already holds a foreign-key violation. It rolls back cleanly, stays at v4, and names the table. **Before the v3 deploy, run `PRAGMA foreign_key_check` on a copy of the prod database** (the dev database passed). Fix or delete any orphan rows first.
- **After going public, never roll back to v2.2 with `run-now` force.** v2.2 can't tell the friend's digest from a stranger's, and forcing overwrites. The runbook (task 18) says so.

## 6. Risks and open questions (ranked)

1. **The import mis-carries the friend's setup.** Mitigation: the prod-like fixture tests, one atomic transaction, logging, old keys kept, and the test-guild run against a copy of the real config (owner step). The key test is "upgrade after today's digest: no second post".
2. **The migration rebuild** (a foreign-key or FTS mistake could strand data). Mitigation: the documented procedure, `foreign_key_check`, an FTS integrity check, the v2.2 database fixture, the deploy backup, and the rollback drill.
3. **Discord rate limits at scale.** Mitigation: sequential guilds with a 1 s pace, discord.py's 429 handling, the load test, and write-through resume.
4. **Cross-guild bleed** (one server's command reading or writing another's data). Mitigation: every query takes a `guild_id`, the qa focus, and the second test guild.
5. **Source quality and volume for 12 new games.** Reddit feeds lengthen every pass by about 35 s each; common-word names can flood matching. Mitigation: task 17, `match_name`, and at most one subreddit per game.
6. **Rollback after v3 has been collecting hourly:** v2.2's first run finds most items already stored and posts a thin digest that day. This is documented and gets one line in the rollback notes.
7. **Global command propagation** (up to an hour). Mitigation: guild-scoped registration in dev, hash-gated sync, and deploying command changes well before the switch to public.
8. **A comped prompt change** if a second comped guild ever follows other games (`all_topics` changes). It's out of scope now; flagged for part 2.
9. **The Server Members intent** at verification (about 75 servers), plus member cache growth with the intent on. Mitigation: startup chunking only for the lounge guild.
10. **D7 resume** could duplicate one game's post after a crash mid-send. That's accepted if D7 is A.
11. **Global source-health alerts** tuned wrong (D8): too chatty or too quiet.
12. **The owner's time** once strangers report issues (D10 contact; out of code scope).
13. **The effort** runs above §15's estimate (below). The cutover is the long pole.

## 7. Testing strategy

- **Unit (test-first):** config, schedule (DST and windows), `lifecycle.reconcile_plan`, fan-out planning, rendering, the importer. All pure, with no Discord and no network.
- **Store:** migration 005 against the `v22_db` fixture, the repo per table, concurrency (`BEGIN IMMEDIATE` claims) in the style of `test_db_concurrency.py`.
- **Integration:** fixture collectors plus `StubLLM` plus a temp database, running the collection, the summaries, and the minute tick into fake channels. Two guilds in different zones and tiers prove no bleed. §15's automated list is covered by:

  | §15 item | Covered in |
  |---|---|
  | Import once and only once | T3 |
  | Migration 005 against a v2.2 database | T2 |
  | The per-guild scheduler across zones, DST and catch-up | T6 |
  | Digests from shared items | T6 |
  | Comped summary computed once and reused | T7 |
  | SHiFT fan-out with per-guild caps and pings | T5 |
  | Join, leave and startup cleanup | T11 |
  | The 10-game limit | T2, T9 |
  | Command permissions | T9 |
  | Admin routing | T8 |
  | Pacing under many due digests | T14 |

- **Adversarial (`test-engineer`, after each task):**
  - hostile guild, channel and role names in replies and notices;
  - forged autocomplete values;
  - channels from another guild passed as options;
  - a ping value of `@everyone` text in a role name;
  - time zones like `Etc/GMT+14` and `Asia/Kathmandu`;
  - the clock stepping backwards;
  - migrating a database with orphaned foreign keys;
  - 11 topics in the import;
  - a catalog key collision with an old topic key;
  - all three tripwires.
- **No network in tests:** every `httpx` client uses `MockTransport`. qa greps for that.
- **Load:** task 14.
- **Manual:** §8.

## 8. Owner test-guild checklist (dev bot)

**Before starting:**
- Stop every other dev process: the native `Python -m newsbot` and `docker ps`.
- The owner edits `config.dev.yaml` themselves:
  - `command_guild_ids: [test guild, second test guild]`;
  - `comped_guild_ids: []`;
  - the test guild's v2 keys stay, so the import runs.
- Back up the dev database first (owner).

**Steps:**
1. **Import against a copy of the friend's real config.** The owner copies prod `config.yaml` to a scratch location, swaps the guild and channel ids for the test guild's, and points `NEWSBOT_CONFIG` at it.
   - Start the bot: the log shows the import lines and the deletable keys; the owner channel gets one import notice.
   - `/newsbot status` shows comped, 09:00 Pacific, three games, SHiFT with an everyone ping, and the lounge.
   - Restart: no second import.
2. Upgrade day: with today's v2 digest already posted in the dev database, start v3. No second digest today; tomorrow's (or a `--now` CLI run) posts.
3. The comped digest at the configured time matches v2's look (a summary per game); run-now asks for confirmation; preview shows "Would post in".
4. **Second test guild** (owner-created), invite the dev bot:
   - one first-contact message, with no mentions;
   - `/newsbot setup`: pick Asia/Tokyo, 10:00, two games including one the first guild doesn't follow, and a channel;
   - its digest is headlines only;
   - its `/news recent` shows only its games; its `status` shows only its notices.
5. The 10-game limit through `follow`; `unfollow` autocomplete; `settings timezone:` autocomplete; setting a time already passed posts within a minute.
6. **SHiFT:**
   - enable it in guild 2 with `ping: role` (a non-mentionable role, bot without Mention @everyone): a missing-permission notice goes to guild 2 only;
   - `test-alert` with the test command on in dev: guild 1 pings everyone, guild 2 pings the role, and each has its own cap.
7. Delete guild 2's game channel: the next digest skips that game and posts the rest, with a notice in guild 2 and nothing in the owner channel.
8. Remove the bot from guild 2: its rows are gone (the `/owner servers` count drops). Re-invite: the first-contact message again.
9. Stop the bot, remove it from guild 2 while stopped, start again: reconciliation deletes the rows.
10. Lounge: an alt join in guild 1 is welcomed; one in guild 2 isn't. `/lounge quote-now` exists only in guild 1.
11. `/owner servers` exists only in the home guild and refuses a non-owner admin.
12. **Rollback drill:** stop v3, start the 2.2.0 image with the same database and the hybrid config. It starts, sees today's row, doesn't repost, and posts the quote at its time. Switch back to 3.0.0: nothing is re-imported and orphan digests are adopted.

## 9. Release, going public, rollout, rollback

**Release:**
1. Merge after qa, the checklist and docs. Tag `v3.0.0`, then **wait for the tag's CI build** (amd64 plus arm64).

**Going public** (§15's order; **owner steps in bold**):

2. **Owner:** after that day's 09:00 digest has posted, and well clear of 07:55 to 08:05:
   - on the Droplet, `cp config.yaml config.v2.yaml`;
   - `cp data/newsbot.db data/newsbot.pre-v3.db` (or run `scripts/backup.sh`);
   - note `TAG=2.2.0`.
3. **Owner:** leave prod `config.yaml` unchanged; v3 derives the catalog from it. Set `TAG=3.0.0` and run `./scripts/deploy.sh`. The deploy window stays: never 09:00 to 09:15 America/Los_Angeles, and avoid about 07:55 to 08:05.
   - The friend's server is upgraded: the import runs and nothing visible changes.
   - Check the import log lines, `/newsbot status`, no permission notices, the next morning's digest and quote, and one hourly SHiFT pass (`status` shows the last collection).
4. Observe at least two digest days and one SHiFT alert (or a dev test-alert).
5. **Owner:** approve task 17's catalog (D11). Add the `catalog:` block with all 15 games to prod `config.yaml`, validated locally with `load_config` on a scratch copy (never `docker compose ... config`). Deploy outside the windows. Watch one collection pass: pass duration, and no flood of source alerts.
6. **Owner:** merge the privacy and terms update (D10 contact). GitHub Pages publishes it; confirm the live pages.
7. **Owner:** take and keep a dated database backup from right before the switch: `cp data/newsbot.db data/newsbot.pre-public-YYYYMMDD.db`, kept outside the 7-day rotation.
8. **Owner:** in the Developer Portal:
   - Installation: guild install only, scopes `bot` plus `applications.commands`, default permissions View Channels, Send Messages and Embed Links (Mention @everyone left for admins who pick an everyone or role ping);
   - then switch **Public Bot** on;
   - share the install link.

**Rollback:**
- **Before step 8:** `TAG=2.2.0` plus deploy. The prod config still has the v2 keys (plus `catalog:`, which v2.2 ignores). No database restore is needed. Expect one thin digest the next morning, because v3 already stored recent items. Rolling forward later is safe (the adopt step, no re-import).
- **After step 8:** a TAG rollback would leave other servers with a bot that ignores their settings. Restore `newsbot.pre-public-*.db` only if you accept dropping every server's settings since then, which §15 already warns about. Prefer fixing forward. The release notes say to keep that backup.

## 10. Effort (agent time: build, test, review)

| Task | Estimate |
|---|---|
| 1 Config v3 | 0.8 day |
| 2 Migration 005 + repo | 1.0 |
| 3 Import | 0.6 |
| 4 Collection | 0.7 |
| 5 SHiFT fan-out | 1.0 |
| 6 Per-guild digest | 1.2 |
| 7 Comped summaries | 0.8 |
| 8 Routing + permissions | 0.6 |
| 9 Commands | 1.2 |
| 10 Setup wizard | 0.7 |
| 11 Lifecycle | 0.4 |
| 12 Lounge rekey | 0.5 |
| 13 Cutover + CLI + test port | 1.5 |
| 14 Load test | 0.3 |
| 15 Version | 0.05 |
| 16 qa + fixes | 0.8 |
| 17 Source research (parallel) | 1 to 2 |
| 18 Docs | 0.8 |

About **13 agent days** for the code path plus 1 to 2 days of research in parallel. That's somewhat above §15's 1.5 to 2 weeks; the cutover's test port is the likeliest overrun. On top of that, the owner needs about half a day for the checklist and rollout, plus the decisions.

## 11. Out of scope

- Parts 2 and 3: bring-your-own-key, paid subscriptions and entitlements, and any tier but `free`/`comped` (only the `tier` column and shared `game_summaries` land now).
- Custom per-server sources, a web dashboard, per-member subscriptions or DMs.
- Commands to edit lounge settings (D5 covers the owner).
- A lounge on any other server, or a per-guild lounge deck primary key.
- An owner "comp this server" command (D6 uses config).
- Per-guild pacing tuning or sharding (hundreds of guilds don't need sharding; Discord requires it at 2,500).
- Retrying failed SHiFT posts; code expiry.
- Localization; games beyond the 25-option select limit.
- Moving collection to a separate worker (Approach B).
- Changing the summarization prompt.

---

**Relevant files:**
- /Users/someclown/dev/discord-newsbot/docs/design.md (§12 to §15)
- /Users/someclown/dev/discord-newsbot/CLAUDE.md
- /Users/someclown/dev/discord-newsbot/docs/plans/2026-09-26-per-game-channels.md
- /Users/someclown/dev/discord-newsbot/docs/plans/2026-09-29-lounge.md
- /Users/someclown/dev/discord-newsbot/newsbot/config.py
- /Users/someclown/dev/discord-newsbot/newsbot/pipeline/run.py
- /Users/someclown/dev/discord-newsbot/newsbot/pipeline/filter.py
- /Users/someclown/dev/discord-newsbot/newsbot/pipeline/lock.py
- /Users/someclown/dev/discord-newsbot/newsbot/collectors/base.py
- /Users/someclown/dev/discord-newsbot/newsbot/collectors/web_search.py
- /Users/someclown/dev/discord-newsbot/newsbot/shift/sweep.py
- /Users/someclown/dev/discord-newsbot/newsbot/shift/decide.py
- /Users/someclown/dev/discord-newsbot/newsbot/bot/client.py
- /Users/someclown/dev/discord-newsbot/newsbot/bot/commands.py
- /Users/someclown/dev/discord-newsbot/newsbot/bot/permissions.py
- /Users/someclown/dev/discord-newsbot/newsbot/bot/format.py
- /Users/someclown/dev/discord-newsbot/newsbot/bot/views.py
- /Users/someclown/dev/discord-newsbot/newsbot/alerts.py
- /Users/someclown/dev/discord-newsbot/newsbot/__main__.py
- /Users/someclown/dev/discord-newsbot/newsbot/lounge/daily.py
- /Users/someclown/dev/discord-newsbot/newsbot/store/db.py
- /Users/someclown/dev/discord-newsbot/newsbot/store/repo.py
- /Users/someclown/dev/discord-newsbot/newsbot/store/models.py
- /Users/someclown/dev/discord-newsbot/newsbot/store/migrations/001_initial.sql (the `digests.run_date UNIQUE` and `stories.digest_id NOT NULL` constraints that force the rebuild)
- /Users/someclown/dev/discord-newsbot/newsbot/useragent.py
- /Users/someclown/dev/discord-newsbot/config.example.yaml
- /Users/someclown/dev/discord-newsbot/config.minimal.yaml
- /Users/someclown/dev/discord-newsbot/docs/self-host.md
- /Users/someclown/dev/discord-newsbot/docs/deploy.md
- /Users/someclown/dev/discord-newsbot/site/privacy.html
- /Users/someclown/dev/discord-newsbot/site/terms.html
- /Users/someclown/dev/discord-newsbot/scripts/deploy.sh
- /Users/someclown/dev/discord-newsbot/tests/test_mentions_tripwire.py
- /Users/someclown/dev/discord-newsbot/tests/test_dash_rewrite_tripwire.py
- /Users/someclown/dev/discord-newsbot/tests/test_useragent_tripwire.py
