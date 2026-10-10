# Plan: SHiFT code details and a list of current codes (v3.1.0)

Source of truth: `docs/design.md` §16 (approved 2026-10-09), on top of §15 and its "As built". Branch `feat/shift-details`. Save as `docs/plans/2026-10-09-shift-details.md`.

**Gate after every task:** `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q`. Check pytest's real exit code; expect only the 2 APScheduler xfails.

**Workflow:**
- Each code task goes to `implement`, then `test-engineer` for an adversarial round on that task.
- `qa` runs after task 10; `docs` runs last (task 12).
- All new code is documented in the owner's voice (CLAUDE.md, "Documentation voice"). Keep dashes to a minimum, and never use `--` as a dash; CLI flags and SQL comments at the start of a line are fine.

**Tripwires that bind every agent writing in `newsbot/`:**
- `tests/test_dash_rewrite_tripwire.py`: no `" -- "` in any string literal.
- `tests/test_mentions_tripwire.py`: exactly one `AllowedMentions(everyone=True, ...)` (`_PING_EVERYONE`, client.py:233). Edits use `AllowedMentions.none()`.
- `tests/test_useragent_tripwire.py`: the literal `User-Agent` appears only in `useragent.py`. The new collector gets its headers from `user_agent_headers()`, like rss.py:36. Conditional request headers (`If-None-Match`, `If-Modified-Since`) are fine.

**No new dependencies.** The page is parsed with stdlib `html.parser`, dates with stdlib `datetime` and `zoneinfo`. `pyproject.toml` changes only its version.

**Hard rules for every agent:**
- Never read `.env*` (except `.env.example`), `config.dev.yaml`, or anything under `data/`.
- Never run `docker compose ... config`.
- Don't run the bot.
- No network: the page fixture is written by hand, and every test uses `httpx.MockTransport`.

---

## 1. Goal and approach

When this ships:
- The bot reads mentalmars' Borderlands 4 SHiFT code tables every hour, as a codes-only source.
- It keeps each code's reward, category and expiry in a new `code_details` table. Official values outrank mentalmars, field by field, and an empty value never erases a filled one.
- Each alert grows a details line. An alert posted in the last 14 days is edited in place when its details change.
- `/shift codes` becomes the list of codes that should still work, grouped the way mentalmars groups them. The old dated list moves to `/shift history`.

**Chosen approach.** Details move through the pipeline that's already there; there's no parallel path:
1. The collector attaches an `ItemDetails` to each `RawItem`, one item per table row.
2. Detection carries it on the `CodeSighting`, and the official reader adds one for single-code official posts.
3. `_release` merges the details into `code_details` before it plans.
4. Delivery reads `code_details` when it renders.

Precedence and the clock live in one pure function (`merge_details`). SQL only stores the merged row. Twelve lines of `CASE` per column would be harder to test and easier to get wrong.

**Key trade-off: edits need a record of each message.** `guild_code_posts` has `message_id` but no channel id and no header text (005_public_app.sql:75-88). Re-rendering an old message from today's settings would guess the channel and ping prefix wrong whenever a server changed them. So migration 009 adds `guild_alert_messages`, written when a message is marked posted. Alerts posted before 3.1.0 have no such row and aren't edited (E3).

### Where §16 looks wrong or underspecified (flagged, not silently changed)

1. **Table rows would collapse into one item.** `_release` canonicalizes and dedupes by URL (fanout.py:752-757, normalize.py:141-176). `canonicalize` drops the fragment (normalize.py's docstring says so, and `urlunsplit(..., "")` does it), so every row of the page, anchor or not, has the same URL, and all but one would vanish before detection. Task 5 canonicalizes detail-bearing items one by one and skips the batch dedupe for them.
2. **Section anchors don't survive.** For the same reason, and because `_safe_link` (format.py:181) re-canonicalizes every link, an alert's link can't carry `#anchor`. **E2.**
3. **Undated rows at rollout.** The cosmetic and permanent tables have no "Added" column. An undated item counts as fresh (decide.py:184-189), so at rollout every unknown cosmetic and permanent code would post, unpinged, in every SHiFT server. That would contradict "nothing old alerted". **E1.**
4. **"via" needs names, not ranks.** `*_source` records `official` or `table`; "via mentalmars and Borderlands (Bluesky)" needs source names. 009 adds `reward_from`, `category_from` and `expires_from`.
5. **"The same transaction".** Today `_plan_and_record_sync` is one connection running several short transactions: `record_code_sightings`, `record_silent_codes` and `record_released_codes` each use `with conn:` (fanout.py:779-821). The details upsert goes in that same block, after sightings and before planning, in its own `BEGIN IMMEDIATE`. A crash between steps loses nothing that the next pass doesn't redo.
6. **The clock for a code first met elsewhere and listed later.** Read literally, §16 restarts the clock at that first listing. The plan sets `clock_start` once, when the row is created, and never moves it. A code first seen after the release (its `alerted_codes.first_seen_at` is at or after migration 009's marker) keeps that first sighting. Any older code gets the listing's time, which is the owner's rollout rule. An older code re-mentioned by a single-code official post also gets a fresh clock; Gearbox repeating it is a fair sign it's current.
7. **Alerts never truncate, and the source name has to be bounded somewhere.** The reservation is computed from the config's own source names (§3.4). A new config rule caps every source name at 100 characters, so even the worst case fits one code per message. That makes "can't happen in practice" true by construction.
8. **The 300-character cap is measured as shown.** Reward and origin are cut when read so that their escaped form (`esc()`, UTF-16 units) is at most 300. Markdown escaping can double the length, and the reservation has to be about what's actually sent.
9. **A multi-code follow-up with details can't keep 20 codes in one message.** Follow-ups are never edited, so they fit by their real size: as many queued codes as fit go out, and the rest wait for the next pass, which is the existing `queued[:FOLLOWUP_MAX_CODES]` behavior (fanout.py:507).
10. **`/shift codes` stays gated** on the server following a SHiFT game (commands.py:675-685), as today. §16 says "anyone in a server that's finished setup"; the gate is unchanged behavior, not a new rule.

---

## 2. Owner decisions (with recommendations)

**Decided 2026-10-09:** E1 to E4 as recommended. Run like v3: implement, an adversarial test-engineer round per task, the gate, then qa and docs.

| # | Question | Recommendation | Blocks |
|---|---|---|---|
| E1 | What do undated table rows (cosmetic and permanent tables) do the first time a `shift_table` source lists them? | **A:** on a source's first successful listing in this database, undated rows are recorded quietly (`too_old`); after that, an undated row that's new to the bot is fresh and posts unpinged like any community find. Dated rows always follow the 48-hour rule. (B: undated rows never alert; a new permanent code only shows up in `/shift codes`.) | T5 |
| E2 | Section anchors in links | **Page URL only.** Canonicalization drops fragments everywhere, and teaching `_safe_link` to keep one is a change to the most security-sensitive line in `format.py` for a cosmetic gain. | T1 |
| E3 | Alerts posted before 3.1.0 | **Not edited.** They have no channel or header record; their details show in `/shift codes`. (Alternative: backfill from each server's *current* SHiFT channel and ping, which is wrong for any server that changed them.) | T3 |
| E4 | Should `config.example.yaml` ship the mentalmars source under Borderlands 4? | **Yes,** with a comment. Self-hosters following Borderlands 4 get the same list. The example's codes-only set becomes `{"r/Borderlands4", "mentalmars"}`. | T2 |

---

## 3. Resolved design

### 3.1 Data shapes

```python
# newsbot/collectors/base.py
DetailRank = Literal["official", "table"]
Category = Literal["golden", "cosmetic", "permanent", "other"]


@dataclass(frozen=True, slots=True)
class ItemDetails:
    code: str  # the one code these details belong to
    rank: DetailRank
    reward: str = ""  # already capped: discord_len(esc(reward)) <= 300
    category: Category | None = None
    expires_on: date | None = None  # a Pacific calendar date
    added_on: date | None = None  # table only
    origin: str = ""  # table only, capped like reward
    listed: bool = False  # True for shift_table rows (first/last_listed_at)


# RawItem gains, after full_text:
details: ItemDetails | None = field(default=None, compare=False, repr=False)
```

- **`CodeSighting`** (decide.py:42) gains `details: ItemDetails | None = field(default=None, compare=False)`. `golden` becomes `mentions_golden_key(text) or details.category == "golden"`.
- **`CodeCandidate`** is unchanged. Details reach rendering through a mapping, not the candidate.
- **`store/models.py`** gains `CodeDetailsRow` (one `code_details` row, with `expires_on` and `added_on` as `date | None` and the timestamps as aware datetimes), `AlertMessageRecord`, `EditTarget` and `CurrentCode`.
- **`QueuedCode`** (models.py:394) and **`CodeView`** (models.py:147) each gain `details: CodeDetailsRow | None = None`. A default is required, because v3.0.5's `repo.py` snapshot constructs them (task 3's compatibility test).

**`shift/details.py`** (pure):
- `SHIFT_TZ = ZoneInfo("America/Los_Angeles")`, `CLOCK_DAYS = 180`, `RANK = {"": 0, "table": 1, "official": 2}`;
- `DetailObservation(code, details, source_name, published_at)`;
- `merge_details(old, obs, *, now, first_seen_at, details_since) -> CodeDetailsRow`;
- `end_of_day(d) -> datetime` (the end of `d` in Pacific, as UTC);
- `current_until(row, first_seen_at) -> datetime | None` (None means permanent);
- `is_current(...)`.

### 3.2 Migration 009 (`009_shift_details.sql`, additive)

```sql
-- SHiFT code details, and a record of every alert message so it can be edited
-- (design.md §16, v3.1.0). Additive only: new tables, one nullable column, one
-- app_state row. v3.0.5 and v2.2.0 never name any of it.

CREATE TABLE code_details (
    code TEXT PRIMARY KEY CHECK (length(code) = 29),
    reward TEXT NOT NULL DEFAULT '' CHECK (length(reward) <= 300),
    reward_source TEXT NOT NULL DEFAULT '' CHECK (reward_source IN ('', 'official', 'table')),
    reward_from TEXT NOT NULL DEFAULT '',
    category TEXT CHECK (category IS NULL OR category IN ('golden', 'cosmetic', 'permanent', 'other')),
    category_source TEXT NOT NULL DEFAULT '' CHECK (category_source IN ('', 'official', 'table')),
    category_from TEXT NOT NULL DEFAULT '',
    expires_on TEXT CHECK (expires_on IS NULL OR expires_on GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    expires_source TEXT NOT NULL DEFAULT '' CHECK (expires_source IN ('', 'official', 'table')),
    expires_from TEXT NOT NULL DEFAULT '',
    added_on TEXT CHECK (added_on IS NULL OR added_on GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    origin TEXT NOT NULL DEFAULT '' CHECK (length(origin) <= 300),
    clock_start TEXT NOT NULL,
    first_listed_at TEXT,
    last_listed_at TEXT,
    updated_at TEXT NOT NULL,
    details_changed_at TEXT
);

CREATE TABLE guild_alert_messages (
    guild_id INTEGER NOT NULL REFERENCES guilds (guild_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    channel_id INTEGER NOT NULL CHECK (channel_id > 0),
    header TEXT NOT NULL CHECK (length(header) BETWEEN 1 AND 200),
    golden_prefix INTEGER NOT NULL DEFAULT 0 CHECK (golden_prefix IN (0, 1)),
    posted_at TEXT NOT NULL,
    rendered_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'gone')),
    PRIMARY KEY (guild_id, message_id)
);
CREATE INDEX idx_guild_alert_messages_posted ON guild_alert_messages (posted_at);
CREATE INDEX idx_guild_code_posts_message ON guild_code_posts (guild_id, message_id);

ALTER TABLE guild_shift ADD COLUMN edits_blocked_at TEXT;

INSERT INTO app_state (key, value)
VALUES ('shift_details_since', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
ON CONFLICT(key) DO NOTHING;
```

- `length()` counts characters, and the read-time cap (escaped UTF-16 at most 300) implies at most 300 raw characters, so the CHECK can't reject an honest row.
- `app_state` is `(key TEXT PRIMARY KEY, value TEXT NOT NULL)` (005:131).
- `strftime` produces `...T16:00:00+00:00`, which sorts correctly against Python's `isoformat()` timestamps because `+` sorts before `.`.
- **The codes in a message come from `guild_code_posts`:** `WHERE guild_id = ? AND message_id = ? AND from_roundup = 0 ORDER BY rowid`. Rowid order is release order, and release order is the order `render_code_alerts` packed them in. Normal candidates come from `queued_guild_codes ... ORDER BY p.rowid` (repo.py:1621-1641), and a partial claim keeps that order (fanout.py:425).
- **Expired on arrival** is recorded as `status = 'too_old'`. `alerted_codes.status`'s CHECK (002_shift_alerts.sql:17-19) stays as it is. `/shift history` derives "expired" from `code_details.expires_on`, not from the status, so it marks every known-expired code, whenever it was recorded.
- **Old databases:** `db.migrate` applies only files numbered above `user_version` (db.py:42-59), so v3.0.5 on a v9 database migrates nothing and never touches the new tables. v3.0.5 writes `guild_shift` with named columns (repo.py:1203; importer.py:174), so the new nullable column is invisible to it.

### 3.3 Precedence and the clock (`merge_details`)

For each of reward, category and expiry, with an incoming value `v` at rank `r` from source `n`:
- if `v` is empty or None, keep the old value. A missing value never replaces a present one;
- otherwise, if `RANK[r] >= RANK[old.<f>_source]`, take `v`, set `<f>_source = r` and `<f>_from = n`. "At least as high" means the same rank's newer reading wins, so mentalmars can correct itself;
- otherwise keep the old value.

The other fields:
- **`added_on`, `origin`:** table only; a present value replaces, an absent one never erases.
- **`first_listed_at`:** set once, on the first `listed` observation.
- **`last_listed_at`:** moves on every `listed` observation.
- **`updated_at`:** always now.
- **`details_changed_at`:** moves to now only when reward, category or expires_on differ from the old row's. For a new row, only if one of them is non-empty.
- **`clock_start`:** set only on row creation:
  - `first_seen_at` if the code is in `alerted_codes` with `first_seen_at >= details_since`;
  - otherwise now (a brand-new code, or the rollout rule for an older one).

Rows are created only by observations that carry details: table rows and single-code official posts. A code with no row uses `alerted_codes.first_seen_at`.

**Order inside one pass:** observations sorted by `(published_at or min, input order)`. Rank decides across ranks whatever the order; within a rank the later one wins.

**`repo.upsert_code_details(conn, observations, *, now) -> list[str]`:**
- under `_immediate` (repo.py:94), it reads the existing rows plus `alerted_codes.first_seen_at` for those codes and `app_state['shift_details_since']`;
- folds each observation through `merge_details`;
- writes each row with `INSERT ... ON CONFLICT(code) DO UPDATE SET <every column> = excluded.<column>`;
- returns the codes whose `details_changed_at` moved, for the log.

**Current until:**
- `category = 'permanent'`: forever;
- else a known `expires_on`: `end_of_day(expires_on)`;
- else `clock_start` plus 180 days, or `alerted_codes.first_seen_at` plus 180 days when there's no row.

### 3.4 The details line and worst-case packing (2000 UTF-16 units)

**The line:** `format.details_line(details, *, now) -> str | None`. Its parts:
- **reward:** `esc(reward)`, if any;
- **expiry:**
  - `permanent` for a permanent code;
  - else, for a known expiry, based on its Pacific date: `expires in N hours` (or `expires in under an hour`) on the same Pacific date as `now`, `expires tomorrow` on the next date, `expired` once past, otherwise `expires Oct 14, 2026`;
- **via:** `via A` or `via A and B`, from `reward_from` and `expires_from` (or `category_from` when "permanent" fills the expiry slot), deduped and escaped.

The parts are joined with ` · `. The line is omitted when there's neither a reward nor an expiry part, so alerts look like today's until details exist.

**Reservation:** `format.details_reserve(names: Iterable[str]) -> int`, computed once in `FanoutDeps` from the config. `names` = every source whose items can carry details: all `trust: official` sources (catalog and shared, after `_with_default_name`) plus every `shift_table` source. Let V be the largest `discord_len(esc(name))` among them. Then:

```
reserve = 1 ("\n") + 300 (reward) + 3 (" · ") + EXPIRY_MAX (24) + 7 (" · via ") + V + 5 (" and ") + V
```

`EXPIRY_MAX` is the longest expiry phrase, `"expires Sept 30, 2026"` padded to a constant. A test pins `discord_len(details_line(x)) <= reserve` over hostile inputs.

**Packing change in `render_code_alerts`** (format.py:511-611), new keyword arguments `details: Mapping[str, CodeDetailsRow] | None`, `now: datetime | None` and `reserve: int = 0`:
- each entry's *slot* is `discord_len(block_without_details) + reserve`; the content carries the real details line, which is never longer than the reserve;
- the greedy loop packs by slot size (plus the `"\n\n"` joiner), so any later edit re-renders into the same message and fits;
- `solo_budget` is checked against the slot. If a slot doesn't fit alone, the link is dropped first (as today); if it still doesn't fit, it raises `ValueError`;
- the source-name hard truncation step (format.py:496-503) is removed, per §16. The 100-character name cap makes the raise unreachable: worst case, header 48 + joiner 2 + code fence 37 + newline 1 + `Golden Key: ` 12 + escaped name 200 + reserve (1+300+3+24+7+200+5+200 = 740) = 1040, under 2000;
- roundups keep `reserve = 0` (they're never edited).

**The visible cost:** with prod's source names (V about 19), reserve is about 378 and a slot about 530, so an alert holds about 3 codes per message instead of about 10. A big multi-code Gearbox drop posts as more messages; only the first pings. The owner accepted this with "never truncate".

**`RenderedAlert`** (format.py:428) gains `header: str = ""` (the exact first paragraph, ping prefix included) and `golden_prefix: bool = False` (the batch's `mixed` flag).

**`render_alert_edit(header, candidates, *, golden_prefix, details, now, reserve) -> str`:**
- re-renders one stored message: the same header, the same blocks (`_alert_block(c, golden_prefix=...)`) plus the current details lines;
- if it somehow exceeds 2000 (the config gained a longer source name since), it drops links; if it still doesn't fit, it raises.

**Follow-ups:**
- `render_followup_alert` gains `details` and `now`. With any details present the format is `@everyone Confirmed by a second source:` followed by one line per code (`` `CODE` · <details line> ``). With none, today's one-liner stays, so existing tests stand.
- New `fit_followup_codes(codes, *, ping_mention, details, now) -> list[str]` returns the longest prefix that fits 2000; the rest stay queued.

### 3.5 The edit pass and the time budget

The hook's budget is unchanged: `_SHIFT_HOOK_TIMEOUT_S = 120` (collect.py:103). The walk stops starting new work at `started + 100` and is hard-stopped at `started + 115` (fanout.py:116-119, 723-727).

`detect_and_fan_out` wraps one coroutine, `_deliver_then_edit(deps, deadline)`, in the same `asyncio.wait_for(..., timeout=hard_stop)`:
1. `deliver_queued_codes(deps, deadline=deadline)`: deliveries and follow-ups, as today.
2. Then, only if `deps.clock() < deadline` and not `deps.preview`: `edit_changed_alerts(deps, deadline=deadline)`, under `deps.delivery_lock`.

`shift/edits.py`, `edit_changed_alerts`:
- **Candidates:** `repo.alert_messages_to_edit(conn, now=..., window=timedelta(days=14), limit=200)`:

  ```sql
  SELECT m.guild_id, m.message_id, m.channel_id, m.header, m.golden_prefix, m.rendered_at
  FROM guild_alert_messages m JOIN guild_shift s ON s.guild_id = m.guild_id
  WHERE m.state = 'live' AND m.posted_at >= :cutoff AND s.edits_blocked_at IS NULL
    AND EXISTS (SELECT 1 FROM guild_code_posts p JOIN code_details d ON d.code = p.code
                WHERE p.guild_id = m.guild_id AND p.message_id = m.message_id
                  AND p.from_roundup = 0 AND d.details_changed_at > m.rendered_at)
  ORDER BY m.guild_id, m.posted_at, m.message_id
  LIMIT :limit
  ```

  All timestamps are UTC ISO text through `_utc_iso` (repo.py:118).
- **For each target:** stop if `deps.clock() >= deadline`, and sleep `deps.pace_s` between edits. Then read the message's codes and their details (`repo.alert_message_codes`), note `snapshot = deps.now()`, render with `render_alert_edit`, and call `deps.editor_for(guild_id, channel_id).edit(message_id, content)`.
- **Outcomes:**
  - success: `rendered_at = snapshot`;
  - `NotFound` 10008 (unknown message) or 10003 (unknown channel): `state = 'gone'`, quietly;
  - `Forbidden` (any 403): `repo.block_guild_edits(conn, guild_id, now) -> bool`. It sets `edits_blocked_at` only when it was NULL; when it returns True, one notice goes through `deps.notify_guild`: "newsbot: I couldn't update a SHiFT alert in <#channel> with new code details (missing permission). I'll stop trying until /newsbot shift is saved again." That server's later targets this pass are skipped;
  - `PublishError` (5xx, timeout): left as is for the next pass;
  - a render `ValueError`: logged at error level, and `rendered_at = snapshot`, so the same change doesn't fail every hour.
- **Unblocking:** `repo.set_shift` (repo.py:1184-1210) adds `edits_blocked_at = NULL` to its `DO UPDATE`, so any save of `/newsbot shift` unblocks edits.
- **Order and carry-over:** guild id, then oldest message first. Whatever the deadline cuts off stays flagged (its `rendered_at` didn't move) and goes first next pass. No edits at startup.

**The editor** (`bot/client.py`, `DiscordCodeAlertEditor(client, guild_id)`):
- it resolves the channel like `DiscordCodeAlertPoster.post` (client.py:638-646, foreign-channel check included);
- then `channel.get_partial_message(message_id).edit(content=..., allowed_mentions=discord.AllowedMentions.none())`. A partial message needs no Read Message History. Errors are classified as above (reuse `_classify_send_error` for the retryable ones);
- `FanoutDeps` gains `editor_for: Callable[[int, int], CodeAlertEditor] | None = None` and `details_reserve: int = 0`. `client._wire` (client.py:1009-1016) sets both.

**Recording a posted message:**
- `_post_batch` (fanout.py:206-264) gains an optional `record: Callable[[RenderedAlert, bool], AlertMessageRecord] | None`, used only for normal alerts;
- `mark_guild_codes_posted` (repo.py:1761-1775) gains `message: AlertMessageRecord | None = None` and inserts the `guild_alert_messages` row (`ON CONFLICT DO NOTHING`) in the same `with conn:`, only when `message_id` is not None;
- the stored header is `alert.header`, minus `alert.ping_prefix` if `on_ping_stripped` fired for that alert. What's stored is what posted;
- `rendered_at` is the time `_load_sync` read the details.

### 3.6 Detection changes (`_release`, fanout.py:749-836)

1. **Split the items.** Items with `details` are canonicalized one by one with `canonicalize(url)` (dropped if None, no batch dedupe). The rest go through `canonicalize_items` as today.
2. **`sightings_from_items(...)`** (decide.py:111-172), per scoped item with codes:
   - `item.details` with `len(codes) == 1 and codes[0] == details.code`: attach it;
   - else `item.trust == "official"` and exactly one code: attach `read_official_details(text, code, published_at)` (rank `official`), or None;
   - else no details.
   - A roundup sighting never carries details.
3. **First listings (E1 A).** `first_listing = {names of table sources in this batch} - repo.table_listed_sources(conn, names)` (app_state keys `shift_table_listed:<name>`). `aggregate` gains `undated_stale_sources: frozenset[str] = frozenset()`, and `_is_fresh` returns False for an undated sighting from one of them.
4. **In `_plan_and_record_sync`, in this order:**
   - `record_code_sightings`;
   - `upsert_code_details(observations)`;
   - `details = code_details_for(conn, candidate codes)`;
   - **expired on arrival:** a candidate not in `known` whose `details.expires_on` is before today's Pacific date gets `fresh=False` through `replace()`, so `plan_alerts` records it `too_old`;
   - `plan_alerts`, the silent and released records, and `queue_confirmed_followups`, unchanged;
   - finally, `mark_table_listed(conn, name, now)` for each table source that produced at least one listed item this pass.

**What doesn't change:** the B+ follow-up (D14). mentalmars sightings go into `code_sightings` like any other, so mentalmars seeing a Reddit or Bluesky code within 24 hours of its first sighting confirms it. Two names in the same pass make the code `trusted` on its first post (decide.py:238-240). Every table item holds exactly one code, so a 30-row page never trips the roundup rule (`max_codes_per_item`).

### 3.7 The source and the collector

**Config:**
- `ShiftTableSource(BaseModel)`: `type: Literal["shift_table"]`, `name: str` (required, not blank), `url: HttpUrl`, `topics: list[str] | None = None`, `trust: Trust`, `codes_only: bool | None = None`.
- It joins the `Source` union (config.py:294-296). It also joins `SharedSourceCfg` (config.py:315-318), only so a stray one parses far enough to get its own error, the way `web_search` does.
- `effective_codes_only` (config.py:286-291) returns True for `shift_table` unless it's explicitly set.
- `build_catalog_collectors.one()` (collectors/base.py:288-293) gets an explicit `ShiftTableSource` branch, imported lazily like the others (base.py:277-280). Its `else` currently falls through to Bluesky, so this branch is required.

**Fetching:**
- rss.py's fetch grows a sibling, `_fetch(http, url, *, sleep, retry_429, source_name, headers=None) -> Fetched(status, body, etag, last_modified)`. It's the same loop, private-redirect check, 429 retries and 5 MB cap. A 304 returns an empty body with `status == 304`, never calling `raise_for_status`.
- `_fetch_body` becomes a thin wrapper returning `.body`, so the tests that import `_fetch_body`, `_reject_private_redirect` and `_RETRY_BACKOFFS_S` (test_collectors_rss_steam.py:16, test_network_guard*.py) keep passing untouched.
- Validators live in a `PageValidators` dict object owned by `NewsBot` (next to `_rate_limit_state`). `build_collection_collectors(cfg, secrets, validators=...)` passes it through. The collectors are rebuilt every pass (client.py:1376), so the collector itself can't hold them. A restart forgets them, as §16 says.
- The ETag and Last-Modified are stored only after a parse that found rows.

**`collectors/shift_table.py`, `ShiftTableCollector`:** `source_type = "shift_table"`, `rate_limit_key = None`.
- `collect()` fetches with `If-None-Match` and `If-Modified-Since` when known. A 304 is a healthy pass with no items. Otherwise it parses in `asyncio.to_thread`.
- **The parser** is an `html.parser.HTMLParser` subclass:
  - it tracks the latest `h2`, `h3` or `h4` text, ignores `script` and `style`, and collects `table > tr > th|td` text with whitespace collapsed (`convert_charrefs=True`);
  - each cell's text is bounded at 4,000 characters while it's collected;
  - at most 1,000 rows per page.
- **Tables:**
  - a table is a code table if a header cell mentions "code" (case-insensitive; the first such column);
  - its other columns are mapped by words: "reward", "added", "expire", "source";
  - its kind comes from the heading: "permanent", then "cosmetic", else main.
- **Rows:** a row is a code row if its code cell holds exactly one `find_codes` match (the `is_code` regex, which tolerates a "NEW" badge in the cell). Other rows are skipped and counted.
- **Category:** permanent table, then cosmetic table or a reward matching `\b(cosmetic|skin|emote|trinket|outfit|banner|head)\b`, then golden (`GOLDEN_RE`), else other.
- **The same code in several tables:** merged into one item. The category takes the most specific table. The reward comes from the most specific table that has one, expiry and "Added" from whichever row has them, origin from the permanent or cosmetic "source" column.
- **Dates:** `^(month)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$`, where the month is a 3-to-9-letter prefix of a month name ("Sept" included). "Unknown", "???" and empty become None. Anything else unparseable becomes None and is counted.
- **Item:**
  - `url` = the page URL (E2);
  - `title` = `excerpt` = `full_text` = the code, so `find_codes` sees exactly that code and nothing from the reward text;
  - `published_at` = the start of the "Added" date in Pacific, else None;
  - `topics` from the game;
  - `details = ItemDetails(rank="table", listed=True, ...)`.
- **Caps:** reward and origin go through `format.cap_shown(text, 300)`, which trims raw text until `discord_len(esc(text)) <= 300` and adds `…` when it cuts.
- **Health:** zero code rows raises `ValueError("no SHiFT code tables found")`. One INFO line per pass: rows read, skipped and unparseable dates.

**The official reader** (`shift/official.py`, `read_official_details(text, code, published_at, *, now) -> ItemDetails | None`):
- **Golden count:** `\b(\d{1,2}|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+golden[\s_-]*keys?\b`, case-insensitive. Two different counts in the text means give up.
- **Expiry:**
  - `\b(expires?|expiring|valid\s+until|through)\b[\s:]*(on\s+)?(\w+day,?\s+)?`, followed by a month-name date with an optional year, or `\d{1,2}/\d{1,2}/\d{4}` (US order, four-digit year only);
  - a date without a year takes the next occurrence on or after `published_at` (or `now`);
  - two different dates means give up.
- With neither part found, it returns None. A count sets the reward to `"N Golden Key(s)"` and the category to `golden`.

### 3.8 Commands

**`/shift codes type:<all|golden|cosmetic|permanent, default all> public:<bool, default false>`.** The `days` option goes away.

**`repo.current_codes(conn, *, now) -> list[CurrentCode]`:**

```sql
SELECT a.code, a.first_seen_at, d.*
FROM alerted_codes a LEFT JOIN code_details d ON d.code = a.code
WHERE a.status NOT IN ('pending', 'failed')
  AND (d.category = 'permanent'
       OR (d.expires_on IS NOT NULL AND d.expires_on >= :today_pt)
       OR (d.expires_on IS NULL AND COALESCE(d.clock_start, a.first_seen_at) > :clock_cutoff))
```

- `:today_pt` is the Pacific date of `now`. "Current through the end of its expiry date" is exactly `expires_on >= today_pt`.
- `:clock_cutoff` is `_utc_iso(now - 180 days)`.

Python then:
- groups by Golden Keys, Cosmetics, Permanent, Other (`category` NULL or `other`);
- sorts newest first by `added_on` (as the start of that Pacific day), else `first_seen_at`;
- filters by `type`.

The code count is small (hundreds), so it fetches everything.

**`/shift history days:<1-90, default 14> public:<bool, default false>`:**
- today's `/shift codes`: `query_codes` (repo.py:430-469) reshaped to `history_codes(conn, since) -> list[CodeView]` with the details joined, newest first;
- **markers:** `expired` (a known `expires_on` before today's Pacific date) wins over "from a roundup", "old post" and "already around when alerts started" (format.py:338-354);
- **lines:** the details line under the source line;
- `query_codes` stays in place for v2.2 and v3.0.5 compatibility only if a test references it; otherwise it's renamed.

**Paging by size:**
- `format.render_current_pages(entries, *, kind, now, footer, empty) -> list[discord.Embed]` and `format.render_history_pages(views, *, timezone, now, title) -> list[discord.Embed]` fill each description greedily up to 4096 units, group headings included;
- one entry never splits: an entry is at most 31 + 1 + 300 + 3 + 300, under 700;
- the footer is `Codes from Gearbox and <hosts of the shift_table sources> · redeem at shift.gearboxsoftware.com · Page i of n`, which is §16's text for prod;
- **empty list:** `No current codes. I'll post new ones in <#channel> when they show up.`, with the channel part left out when the server's alerts are off.

**Entry (current):** `` `CODE` ``, a newline, then reward (or `reward unknown`), ` · `, then:
- `expires Oct 14 (in 5 days)` (the year shown when it isn't this Pacific year);
- `expires in N hours` or `expires tomorrow`;
- `expiry unknown`;
- for permanent, the escaped `origin`, else `permanent`.

**Paging glue:**
- `commands._pages_view(owner_id, embeds) -> (embed, PagerView)` replaces `_first_page_and_view` for the two `/shift` commands. `/news` keeps it;
- `PagerView` (views.py:27) already takes an async `render_page`;
- `_SHIFT_PAGE_SIZE` (commands.py:87-91) and `render_code_page`'s equal-share budget (format.py:405-417) are deleted.

**Registration:**
- both commands live in `make_member_shift_group` (commands.py:647-704), with new descriptions: `codes` "SHiFT codes that should still work right now."; `history` "Every SHiFT code seen in the last N days."
- `commands_hash` (registration.py:131-138) hashes the command JSON, so the change is picked up by itself: the first 3.1.0 start syncs once and stores the new hash. Prod registers globally, and Discord can take up to an hour to show it everywhere; dev uses `command_guild_ids`, which is instant.
- No manual hash work. Tests that pin the `/shift` tree (`test_command_registration*.py`, `test_commands_shift_codes_handler.py`, `test_member_commands_*`) are updated in task 9.

### 3.9 Config validation and the example

**New errors** (collected into the one `ConfigError`, no dashes):
- `{where} ({name}): codes_only can't be false for a shift_table source; it's read only for SHiFT codes and their details. Remove the setting or set it to true` (in `_codes_only_problems`, config.py:1022-1052);
- `shared_sources[{j}] ({name}): shift_table goes under its game in catalog:` (in `_v3_problems`, config.py:1133);
- `{where} ({name}): shift_table only feeds SHiFT detection, and {game} isn't in shift.games`. This runs after `_v3_shift_games` (config.py:1003-1019), which fills the default; it doesn't run when `shift.games` is empty in the v2 sense (every game);
- v2 shape: `source {name!r}: a shift_table source must name exactly one topic`, checked before `_derive_catalog`, whose `_SHARED_BY_TYPE[source.type]` (config.py:993) would otherwise raise `KeyError`;
- every source: `source name {name!r} is {n} characters; the limit is 100 (it's shown in SHiFT alerts)`.

**`config.example.yaml`** (E4), under `borderlands4.sources`, inside the `catalog-step` markers, with a comment in the owner's voice:

```yaml
      - {type: shift_table, name: "mentalmars", url: "https://mentalmars.com/game-news/borderlands-4-shift-codes/", trust: community}
```

**What that means for the example and prod-step tests:**
- `test_config_examples_v3.py` gains `test_borderlands4_reads_mentalmars_for_codes_and_details` (type, URL, trust community, effectively codes-only, and nothing stored);
- the expected codes-only set becomes `{"r/Borderlands4", "mentalmars"}` in test_codes_only.py:286-291 and test_prod_catalog_step.py:339;
- the prod-step health check is a superset check, so it still passes with one more source;
- the AI prompt is unchanged, because codes-only items are never stored (`storable_items`, collect.py:397-411).

---

## 4. Tasks

**Parallelism:**
- 1 ∥ 3;
- then 2 ∥ 4 ∥ 6 (2 needs 1; 4 needs 1's `ItemDetails`; 6 needs 3's `CodeDetailsRow`);
- 5 after 2, 3 and 4;
- 7 after 5 and 6; 8 after 7;
- 9 after 3 and 6, and can run alongside 5 to 8 (it touches `commands.py` and the read functions in `repo.py`; coordinate on merge in `repo.py`, which 7 and 8 also touch in other sections).

### Task 1: The collector, the fetch, and `ItemDetails`

**Files:**
- `newsbot/collectors/base.py`: `ItemDetails`, `DetailRank`, `Category`, `RawItem.details`;
- `newsbot/config.py`: the `ShiftTableSource` class only, not yet in any union;
- `newsbot/collectors/rss.py`: `_fetch`/`Fetched`, with `_fetch_body` as a wrapper;
- new `newsbot/collectors/shift_table.py` (`ShiftTableCollector`, `PageValidators`, the parser, the date parser);
- `newsbot/bot/format.py`: `cap_shown` only;
- new fixtures `tests/fixtures/shift_table/page.html` (hand-made from the shape in §3.7: the three tables with the real headers, "Unknown" and "???", a code in two tables, cells with `<strong>`, links and classes, and no copied article text), `shuffled.html`, `no_tables.html`, `hostile.html` (`&#64;everyone`, `<@&123...>`, markdown, a 5,000-character reward, astral characters, a nested table, a `<script>` holding a code).

**Change:** §3.7, except the wiring.

**Tests** (test-first; `tests/test_shift_table_collector.py`, `tests/test_fetch_conditional.py`):
- every row of `page.html`, with category, reward, expiry, added and origin field by field;
- the code in two tables takes the most specific category;
- shuffled columns parse the same; no tables raises the health error;
- unparseable dates are None, counted once;
- the caps hold in shown units, and `@everyone` survives as text (escaping happens at render);
- the `<script>` code isn't read;
- 304 means no items and no error; the validators are sent on the second call and stored only after a good parse;
- a 429 retries; a redirect to a private address is refused; the 5 MB cap holds;
- the existing rss tests are unchanged.

**Verify:** gate.

### Task 2: Config wiring

**Files:**
- `newsbot/config.py`: the unions, `effective_codes_only`, the validations in §3.9, the name cap;
- `newsbot/collectors/base.py`: `one()`'s branch, `build_catalog_collectors(..., validators=None)`;
- `newsbot/pipeline/collect.py`: `build_collection_collectors` passes `validators`;
- `newsbot/bot/client.py`: `self._page_validators`, passed at client.py:1376;
- `config.example.yaml` (E4);
- the tests named in §3.9.

**Tests** (`tests/test_config_shift_table.py`):
- every new message;
- a v2-shaped shift_table scoped to one topic derives under that game;
- `--check-sources` lists the source and leaves its items out of the counts (`run_check_sources` already uses `storable_items`, run.py:787-791);
- `one()` never hands a shift_table source to Bluesky;
- the example and prod-step updates.

**Verify:** gate.

### Task 3: Migration 009, the details store, compatibility

**Files:**
- `newsbot/store/migrations/009_shift_details.sql` (§3.2, with a header comment in the style of 006);
- `newsbot/store/models.py` (`CodeDetailsRow`, `AlertMessageRecord`, `EditTarget`, `CurrentCode`, plus defaulted `details` on `QueuedCode` and `CodeView`);
- new `newsbot/shift/details.py` (§3.3, pure);
- `newsbot/store/repo.py`, a new "SHiFT code details (design.md §16)" section: `upsert_code_details`, `code_details_for`, `table_listed_sources`, `mark_table_listed`;
- retention: `purge_guild_data_older_than` also deletes `guild_alert_messages` older than 30 days;
- `tests/fixtures/v305_repo_snapshot.txt` (exactly `git show v3.0.5:newsbot/store/repo.py`, checked in like `v22_repo_snapshot.txt`).

**Tests:**
- `tests/test_details_merge.py` (test-first):
  - each field's precedence: official beats table, table never beats official, the same rank's newer wins, empty never erases;
  - `details_changed_at` moves only on a shown change;
  - the three clock starting points; `current_until` for permanent, a known expiry and the clock;
  - `end_of_day` across both DST changes.
- `tests/test_migration_009.py`:
  - a v8 database, populated with posted, too_old and seeded codes, `guild_code_posts` with message ids, and `guild_shift`, migrates to 9 with all data intact;
  - the marker row is present; CHECKs reject a bad category, date or 301-character reward;
  - a deleted guild cascades `guild_alert_messages`; migrating twice is idempotent.
- `tests/test_migration_009_v305_compat.py`:
  - v3.0.5's repo, loaded from the snapshot, runs against a v9 database: `record_released_codes` with queueing, `claim_guild_codes`, `mark_guild_codes_posted`, `set_shift`, `query_codes`, `queue_confirmed_followups`, `delete_guild`;
  - v3.0.5's `migrate()` (current db.py is unchanged since) leaves `user_version` at 9.
- The v2.2 compatibility suite still passes on v9.

**Verify:** gate.

### Task 4: The official reader and detection's pure changes

**Files:**
- new `newsbot/shift/official.py`;
- `newsbot/shift/decide.py`: `CodeSighting.details`, golden from category, the attach rules in §3.6 step 2, and `aggregate(..., undated_stale_sources=...)`.

**Tests** (test-first; `tests/test_shift_official.py`, additions to `tests/test_shift_decide.py`):
- "3 Golden Keys", "a Golden Key", "five Golden Keys";
- "expires October 14", "valid until Oct 14, 2026", "through 10/14/2026";
- a year-less date before the publish date rolls over to next year;
- it gives up on two counts, two dates, "10/14" with no year, "through the SHiFT site", and a two-code post (still sighted, both codes, no details);
- a community post gets no reader;
- a table item whose code doesn't match its details carries none;
- a roundup carries none;
- the undated stale rule.

**Verify:** gate.

### Task 5: Detection wiring

**Files:** `newsbot/shift/fanout.py` (`_release` per §3.6).

**Tests** (`tests/test_shift_details_detection.py`, real temp SQLite, fake posters, injected clock):
- a 30-row page in one pass gives 30 sightings, not 1 (the dedupe bug);
- **the rollout scenario:**
  - a database seeded like prod (seeded, posted with message ids, a ping spent today) plus the full fixture page;
  - every listed code gets a `code_details` row with a fresh clock;
  - nothing already known is queued; no pings; old dated rows are `too_old`; undated rows on the first listing are `too_old` (E1 A);
  - a row added within 48 hours that's new to the bot is queued unpinged;
  - a second pass posts nothing;
- an undated new row after the first listing posts unpinged;
- expired on arrival is `too_old` and never queued;
- **B+:** a Reddit code posted unpinged, then mentalmars lists it 3 hours later, queues the follow-up;
- mentalmars plus Reddit in the same pass is trusted and pings on the first post;
- an official single-code post's expiry overrides mentalmars', while an official multi-code post records and queues every code and leaves the details alone.

**Verify:** gate.

### Task 6: Rendering

**Files:** `newsbot/bot/format.py` (§3.4: `details_line`, `details_reserve`, the packing change, `RenderedAlert.header`/`golden_prefix`, `render_alert_edit`, follow-up details and `fit_followup_codes`, plus the two page renderers from §3.8).

**Tests** (test-first; `tests/test_format_details.py`, `tests/test_format_alert_packing.py`, `tests/test_format_shift_pages.py`):
- every details-line shape; "via A and B"; within 48 hours in both DST weeks;
- a property test: `discord_len(details_line(x)) <= details_reserve(names)` for hostile inputs (300-unit astral rewards, markdown-dense rewards, 100-character names);
- every alert, re-rendered with the worst-case details for each code, stays at or under 2000, header with a role prefix included;
- link shedding still works; the truncation step is gone;
- the follow-up fits by size;
- size paging with 300-unit entries never cuts an entry and never exceeds 4096 or the 6000 embed total;
- headings, footer, empty text.

**Verify:** gate.

### Task 7: Delivery with details

**Files:**
- `newsbot/store/repo.py`: `queued_guild_codes` LEFT JOINs `code_details`; `mark_guild_codes_posted(..., message=)`;
- `newsbot/shift/fanout.py`: `_deliver_one` passes `details`, `now` and `deps.details_reserve`, and records messages (§3.5); `_deliver_followups` uses details and `fit_followup_codes`; `FanoutDeps.details_reserve` and `editor_for`;
- `newsbot/bot/client.py`: sets the reserve in `_wire`.

**Tests** (`tests/test_shift_delivery_details.py`):
- the first alert carries the details line;
- a `guild_alert_messages` row per message, with the right channel, header, golden prefix and code order (rowid);
- a stripped ping stores the header without the prefix;
- roundups and follow-ups write no row;
- a follow-up shows details and leaves the overflow queued;
- the CLI preview (`--collect`) records nothing new.

**Verify:** gate.

### Task 8: Edits in place

**Files:**
- new `newsbot/shift/edits.py`;
- `newsbot/store/repo.py`: `alert_messages_to_edit`, `alert_message_codes`, `mark_alert_message_rendered`, `mark_alert_message_gone`, `block_guild_edits`, and the `set_shift` clear;
- `newsbot/shift/fanout.py`: `_deliver_then_edit` inside the existing `wait_for`;
- `newsbot/bot/client.py`: `DiscordCodeAlertEditor` and `editor_for`;
- `tests/v3_fakes.py`: a fake editor.

**Tests** (`tests/test_shift_edits.py`, `tests/test_shift_edits_adversarial.py` from the test-engineer):
- a details change edits every server's message holding the code, re-rendered whole in the same order, with the header kept and `AllowedMentions.none()`;
- day 13 is edited, day 15 isn't;
- 10008 marks it gone, quietly, and it's never retried;
- a 403 sends one notice, ever, until `set_shift` clears it; the same server's other messages are skipped that pass;
- a 5xx stays flagged;
- **budget:** deliveries first; a deadline mid-edits leaves the rest flagged, and the next pass does them;
- a render overflow logs and stops retrying;
- the mentions tripwire still has exactly one `everyone=True`.

**Verify:** gate.

### Task 9: `/shift codes` and `/shift history`

**Files:**
- `newsbot/bot/commands.py` (§3.8, `_pages_view`, the `now` injection for tests through an optional argument to `make_member_shift_group`);
- `newsbot/store/repo.py`: `current_codes`, `history_codes`;
- updated command-tree tests.

**Tests** (`tests/test_commands_shift_current.py`, updates to `test_commands_shift_codes_handler.py`):
- groups in order, newest first, the `type` filter, the empty text with and without the channel;
- the current boundary at Pacific midnight of the expiry date; the clock at 180 days for a row and for a code with no row; permanent forever;
- history markers with `expired` winning;
- paging by size with long entries; `public` posts with mentions off;
- the registration hash changes and the sync sends once.

**Verify:** gate.

### Task 10: Version 3.1.0

**Files:** `pyproject.toml:3`, `newsbot/useragent.py:36` (`_FALLBACK_VERSION`).

**Verify:** gate.

### Task 11: `qa` review

**Focus:**
- hostile page content end to end, from a cell to an alert, an edit and `/shift codes`;
- cross-server bleed in edits (each query is keyed by `guild_id`);
- edit pacing and budget;
- rollout with no pings;
- rollback to v3.0.5;
- a grep that every test client uses `MockTransport`.

### Task 12: Docs (`docs` agent, owner's voice)

- **README:** the `/shift` commands (README.md:104-108, 267); the details line; edits (the bot needs View Channel and Send Messages in the SHiFT channel, as now; an edit needs no extra permission); the `shift_table` source type.
- **`docs/design.md`:** an "As built (date)" under §16 covering §1 above's list, E1 to E4, the reserve numbers, `guild_alert_messages`, `edits_blocked_at` and the follow-up fit.
- **`docs/self-host.md`:** the source type and its rules.
- **`docs/deploy.md`:** the runbook in §9.
- **`site/privacy.html`:**
  - the per-server list (line 30-33) gains "for each SHiFT alert, the channel and message id and its header line, so it can be updated with new details for 14 days";
  - "Public news" (line 42) gains mentalmars' code list page, read only for its tables and never kept as news, and that the code list now keeps each code's reward and expiry;
  - the sources list (line 52) gains it too;
  - the "last updated" date changes.
  - It goes live at merge, so the owner reads it first.
- **`site/terms.html`:** line 29 ("The bot doesn't check") becomes: reward and expiry come from Gearbox or mentalmars and may be wrong.
- **CLAUDE.md:** the current plan pointer, the 3.1.0 status line, and the to-do list's v3.1.0 entry.

---

## 5. Data and schema summary

- **Migration 009:** `code_details`, `guild_alert_messages`, two indexes, `guild_shift.edits_blocked_at`, and `app_state['shift_details_since']`. Purely additive.
- **How it's applied:** at startup by `migrate()` (db.py:42-59), in one `BEGIN IMMEDIATE`. No foreign-keys-off header is needed.
- **Retention:** `code_details` is kept forever, like `alerted_codes`; `guild_alert_messages` goes after 30 days; a removed server's rows cascade.
- **In-memory only:** ETag and Last-Modified values (a restart refetches in full).
- **Config:** a new source type. v3.0.5 refuses a config that contains it.

## 6. Risks (ranked)

1. **mentalmars blocks the Droplet or changes its layout.** The pass fails ("no SHiFT code tables found" or a 403) and shows up in the owner's nightly report, as Lodestone did. Mitigations: rollout step 1, and `--check-sources --game borderlands4` from the Droplet.
2. **A rollout flood or a repeat ping.** Mitigations: E1, the rollout test in task 5 against a prod-like database, and the dev-bot run.
3. **Fewer codes per message** (about 3 with prod's names) because of the reservation. It's visible but accepted; flagged here so nobody files it as a bug.
4. **Edit volume at scale.** Edits share the 100-second walk and go after deliveries; at about 170 or more SHiFT servers a mentalmars correction drains over several passes, the same as the existing follow-up note.
5. **Wrong details.** A community table can be wrong; official outranks it, and the official reader gives up when unsure.
6. **Mention highlight on edit.** An edit sent with `AllowedMentions.none()` may lose the `@everyone` highlight in some clients. The text stays, and nobody is notified either way.
7. **Rollback.** v3.0.5 runs on a v9 database (task 3 proves it) but refuses a config with `shift_table`, so roll back the config too.
8. **Scraping etiquette.** One conditional GET an hour, identified by `NEWSBOT_CONTACT`; check `robots.txt` at rollout.

## 7. Testing strategy

| §16 test item | Covered in |
|---|---|
| The page fixture (three tables, shuffled, Unknown and ???, twice-listed, no tables, hostile, oversized) | T1 |
| Gearbox's posts, give-up cases | T4 |
| Field-by-field precedence, the clock, current-until | T3 |
| Rollout with no repeat pings and nothing old alerted; expired on arrival | T5 |
| Edits: 14 days, deleted message, permissions and the single notice, re-render fitting, budget carry-over | T6, T8 |
| Both commands with size paging and 300-character entries | T6, T9 |
| 009 on a v3.0.5 database; v3.0.5 on a migrated one | T3 |

- **Unit, test-first:** the parser, the official reader, `merge_details`, `details_line` and the reserve, packing, page rendering.
- **Integration:** real temp SQLite with migrations, fake posters and editors, injected clock (the style of `test_shift_fanout.py`).
- **Adversarial:** after every task. Hostile cells; the clock stepping backwards between passes; two passes racing an edit; a server removed mid-walk; `Etc/GMT+14` servers against Pacific expiry; a 100-character name with markdown; the three tripwires.

## 8. Owner checklist (dev bot, PacketQueue Dev)

**Before starting:**
- Stop any other dev process (`Python -m newsbot` and `docker ps`).
- Repoint `config.dev.yaml` and `~/newsbot-scratch/config.v3test.yaml` at PacketQueue Dev, remove the dev bot from PacketQueue, start `data/dev.db` fresh (the CLAUDE.md to-do).
- Add the mentalmars line under Borderlands 4.

**Steps:**
1. Start the bot and run `/newsbot setup` with Borderlands 4 and SHiFT on (ping: a role). The first pass logs rows read; no alerts post (E1 A plus the 48-hour rule), nothing pings.
2. `/shift codes`: the groups, details, footer and paging; `type:permanent`; `public:true`. `/shift history days:90`: markers, with `expired` where it applies.
3. Edit a row's expiry in a copy of the fixture served locally? Not available to the bot. Instead, run `python -m newsbot.pipeline.run --collect --fixtures` against a copy of `dev.db` to rehearse a details change, then confirm that the live dev bot edits a real alert after a real change, when one happens.
4. Remove the bot's View Channel in the SHiFT channel and force a details change: one notice, then quiet until `/newsbot shift` is saved.

## 9. Release and rollout runbook

1. **Before merging:** from the Droplet, `curl -sS -o /dev/null -w '%{http_code}\n' https://mentalmars.com/game-news/borderlands-4-shift-codes/` (expect 200) and `curl -sS https://mentalmars.com/robots.txt`. Then, with the 3.1.0 checkout, `python -m newsbot.pipeline.run --check-sources --game borderlands4 --config <scratch copy>` (it needs no database or credentials).
2. **Before tagging:** build and check the image locally (start Docker Desktop, build, run `--check-sources` in it, stop Docker Desktop). Tag `v3.1.0` and wait for the tag's CI build (amd64 plus arm64).
3. **Production** (owner), outside 09:00 to 09:15 and about 07:55 to 08:05 Pacific:

   ```bash
   cd /opt/newsbot
   cp config.yaml config.pre-shift-details.yaml
   ```

   Add this line under `catalog:` → `borderlands4` → `sources:`, after the `r/Borderlands4` line, in a copy of the config. Validate the copy with `load_config` from a 3.1.0 checkout (never `docker compose ... config`), then `mv` it into place:

   ```yaml
         - {type: shift_table, name: "mentalmars", url: "https://mentalmars.com/game-news/borderlands-4-shift-codes/", trust: community}
   ```

   Set `TAG=3.1.0` in `.env` and run `./scripts/deploy.sh`; it takes the database backup and migration 009 runs at startup.
4. **Check:**
   - the first pass's log line (rows read, zero skipped);
   - no SHiFT posts in Rage Quit Tavern from the listing;
   - `/shift codes` in PacketQueue;
   - the nightly owner report shows mentalmars healthy.
   - The new `/shift history` can take up to an hour to appear.
5. **Rollback:** `cp config.pre-shift-details.yaml config.yaml`, then `TAG=3.0.5` and `./scripts/deploy.sh`. No database restore is needed. Add `config.pre-shift-details.yaml` to the cleanup list after a clean week.

## 10. Effort (agent time: build, test, review)

| Task | Estimate (days) |
|---|---|
| 1 Collector and fetch | 0.8 |
| 2 Config wiring | 0.5 |
| 3 Migration 009, details store, compatibility | 0.9 |
| 4 Official reader, decide | 0.5 |
| 5 Detection wiring | 0.8 |
| 6 Rendering | 0.8 |
| 7 Delivery with details | 0.6 |
| 8 Edits in place | 0.9 |
| 9 Commands | 0.9 |
| 10 Version | 0.05 |
| 11 qa and fixes | 0.6 |
| 12 Docs | 0.5 |

About **8.5 agent days**, plus about half a day of the owner's time for the checklist and rollout.

## 11. Out of scope

- Reading details from community free text (Reddit, Bluesky search, press); it's on the to-do list.
- A game column for codes; editing roundups; editing alerts posted before 3.1.0 (E3).
- Section anchors in links (E2).
- Images, graphics and color (v3.2.0).
- Persisting ETag and Last-Modified across restarts.
- Tracking codes that drop off mentalmars' list.

## 12. Decisions made during the build (for the docs task and design "As built")

- **Via names count as a displayed change** (owner, 2026-10-09). Agreeing same-rank sources don't swap the name; a higher rank confirming a value takes it.
- **The first listing is quiet per table kind,** not per source (`shift_table_kind_listed:<kind>:<name>`), so a partial first page or a table added later can't flood.
- **No pinged follow-up for a code known to be expired,** at queue time and again at send time (skipped, no ping spent).
- **Permanent codes are never expired on arrival.**
- **A table's expiry can't silence Gearbox:** expired on arrival is skipped when an official source saw the code this pass, unless the expiry itself is official.
- **Flood guard:** more than 5 fresh new codes from one table source in one pass go out as a roundup (unpinged, capped at 50, no follow-ups). `TABLE_FLOOD_LIMIT` in `fanout.py`.
- **Sightings are scoped by item, not URL,** so nothing borrows a table's game match.
- **Source names:** no control or invisible-only characters, no padding, 100 characters at most; case twins warn. Tables must be community trust.
- **Queued codes that expire before delivery are skipped** (no post, no ping), with the same believability rule as expired on arrival: a trusted code with only a table's expiry still posts.
- **`guild_alert_messages.rendered_at` is a version stamp:** the largest `details_changed_at` among the message's codes as rendered (the epoch when none had details), not a clock reading. An edit is due when any code's `details_changed_at` is newer.
- **The edit reserve covers stored names too:** delivery sizes it over the configured official and table names plus every name already stored in `code_details`, so a renamed source can't overflow an edit.
- **Edits are fair across servers:** round-robin, at most 20 per server per pass (200 overall), least recently tried first (`guild_alert_messages.edit_tried_at`); a server whose edit fails is skipped for the rest of that pass; a deleted channel marks all its messages gone at once.
- **A forbidden edit blocks that server's edits until any save of `/newsbot shift`,** including one that changes nothing.
