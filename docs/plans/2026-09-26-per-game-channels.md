# Plan: per-game channels and a SHiFT codes channel (v2.0.0)

Source of truth: `docs/design.md` §13. Branch `feat/per-game-channels`. Gate after every step: `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q` (check pytest's real exit code). Each step: `implement`, then `test-engineer`; `qa` after step 8. Steps are sequential (they share `format.py`, `client.py`, `run.py`, `commands.py`).

## 1. Approach

- Each topic's embed posts to `topics[].channel_id` (no header, no thread; empty topics post nothing). SHiFT alerts post to `alerts.channel_id`. Fresh roundup-only codes post unpinged under "SHiFT codes from a roundup". Members get `/shift codes`. Startup checks every configured channel and sends one admin alert listing problems.
- **`digests.posted_message_ids` stays a flat JSON list** in posting (config) order. Per-topic progress lives in memory on the `DiscordPublisher` instance (as v1's resumability already does); the run report gets `topic_key → message id` from `publish()`. No digests schema change, so v1.3.0 reads the DB unchanged.
- **One additive migration (003)**: §13's "roundup-posted codes use `posted`" can't coexist with `/shift codes` showing a "from a roundup" marker (nothing else on the row distinguishes it). `alerted_codes.from_roundup` fixes that.

## 2. Steps

### Step 1: Config, new fields (keep `digest.channel_id` temporarily)
- `Topic.channel_id: int = Field(gt=0)` (required). `AlertsCfg.channel_id: int | None = Field(None, gt=0)`; in `load_config`, alerts enabled without it → `"alerts.channel_id is required when alerts.enabled is true (v2.0: SHiFT alerts post to their own channel)"`.
- Friendly missing-field message for `("topics", i, "channel_id")`: `"topics[i] (<key>): channel_id is required -- each game posts to its own channel as of v2.0"`.
- `DigestCfg.channel_id` temporarily `int | None = None` (still read until step 3).
- `DiscordCodeAlertPoster` gets `cfg.alerts.channel_id`; `_MISSING_MENTION_PERMISSION_ALERT` says "SHiFT codes channel".
- Update test fixtures (`tests/fixtures/config_valid.yaml`, `tests/fixtures/shift/config.yaml`) and inline YAML in config/registration/scheduling tests. `config.example.yaml` waits for step 8.
- Tests: missing topic id → friendly message; alerts on without id → error; alerts off without id → fine; `0`/negative rejected; all errors in one `ConfigError`.

### Step 2: Store — migration 003, roundup marker, code query
- `003_code_roundup_flag.sql`: `ALTER TABLE alerted_codes ADD COLUMN from_roundup INTEGER NOT NULL DEFAULT 0 CHECK (from_roundup IN (0, 1));` then `UPDATE alerted_codes SET from_roundup = 1 WHERE status = 'roundup';`
- `CodeView(code, first_seen_at, source_name, item_url, status, from_roundup)`.
- `record_silent_codes` rows gain `from_roundup`; `claim_codes(..., from_roundup=False)`; new `query_codes(conn, since, limit, offset) -> (list[CodeView], total)`: `status NOT IN ('pending','failed')`, `ORDER BY first_seen_at DESC, rowid ASC`, UTC-aware `since`.
- `sweep._record_silent_sync` passes `c.roundup` (no behavior change yet).
- Tests: `user_version == 3`, backfill of existing `roundup` rows; a v1.3-shaped INSERT omitting the column still works (rollback proof); `query_codes` exclusion, window, ordering, paging, count; parameterized SQL.

### Step 3: Per-topic render and publish (largest step)
- `format.py`: `TopicMessage(topic_key, topic_name, channel_id, embed)`; `RenderedDigest(run_date, messages, coverage_notes)`. `render_digest` emits one message per topic in config order, skipping topics with nothing. Coverage notes go in each posted embed's footer ("Reduced coverage today: …", ≤512 UTF-16 units) — D1. Remove `_header`, `_HEADER_LIMIT`, `_pack_messages`. `to_text` prints one block per channel. `render_run_report` takes `posted_by_topic: Mapping[str, int]` and renders per-topic `[jump]` links only for posted topics; shedding order: notes, source detail, links, flat truncation.
- `publisher.py`: `PublishError(message, posted_ids=None, *, posted_by_topic=None, retryable=True)`; `Publisher.publish(r) -> dict[str, int]`; `PrintPublisher` returns `{}`.
- `client.py`: `DiscordPublisher(client)` with `_posted: dict[str, int]`, channel cache, read-only `posted_ids`; skips already-posted topics on retry; `AllowedMentions.none()` on every send. Transient errors → `PublishError(retryable=True)`; 4xx/NotFound/non-sendable channel → `PublishError(retryable=False)` with the ids so far (fixes a latent v1 bug where an unwrapped 4xx was recorded `failed` with `[]`). `NullPublisher` returns `{}`.
- `run.py`: `_publish_with_retry -> (dict, PublishError | None)`, no retry when not retryable; store `list(posted.values())`; run report gets `posted_by_topic`; catch-all falls back to `publisher.posted_ids` so a cancellation mid-publish records what landed.
- `commands.py`: run-now uses `DiscordPublisher(bot)` (confirmation logic unchanged; wording "check the game channels"); preview sends one ephemeral "Would post in <#id>:" message per channel, or "Nothing would post today: no game has news."
- Tests: empty topics omitted; all-empty → no messages; fallback included; config order; footer cap; publisher routes per channel, resumes only unposted topics, 4xx not retried and ids kept; mentions none; failed-with-ids recorded; all-empty day saves `ok` with `[]`; cancellation records posted ids; `should_catch_up`/`needs_confirmation` unchanged; run-report links only for posted topics, 24-topic shedding.
- Verify also: the `--dry-run` CLI prints one block per channel.

### Step 4: Remove `digest.channel_id`
- Delete the field. In `load_config`, pre-check raw YAML: `digest.channel_id` present → `"digest.channel_id was removed in v2.0 -- move it to a channel_id on each topic (topics[].channel_id); there is no fallback"`, merged with other errors into one `ConfigError`. No references left in `newsbot/` or `tests/`.

### Step 5: Roundup codes post without a ping
- `decide.py`: `AlertPlan.roundup_to_post`; `plan_alerts(..., max_roundup_codes=50)`: new roundup candidates → unseeded `seeded`, stale `too_old`, fresh → `roundup_to_post` (first-seen order, capped; overflow silent `roundup`) — D3. `ping`/`cap_reached` from normal `to_post` only. `group_roundups(cands)` by `(source_name, item_url)`.
- `format.py`: `render_roundup_alerts(candidates) -> list[RenderedAlert]`: header `**SHiFT codes from a roundup** · {esc(source)} · <{_safe_link(url)}>`, one fenced block per code, `**(continued)**` continuations, `ping=False` always, nonce `sha256("roundup|" + codes + index)`; never "@everyone".
- `sweep._apply_plan`: after the normal batch, `claim_codes(rows, pinged=False, from_roundup=True, ...)`, post each group via `_post_with_retry`, mark posted/failed, fold failures into the existing admin alert; one admin alert if the cap trimmed codes. `CodeCheckOutcome.roundup_posted`.
- Tests: roundup tests flip to posting unpinged with `from_roundup=1`; unseeded/stale roundup statuses; mixed batch → pinged normal message then separate roundup message; roundups never spend the ping budget; two roundup items → two headers; 60 fresh codes → 50 post, 10 silent + admin alert; overflow split; tripwire green.

### Step 6: `/shift codes`
- `format.render_code_page(codes, *, title, page, pages, timezone) -> Embed`: fenced code, first-seen date in `digest.timezone`, escaped source (≤100), safe link, markers (from a roundup / old post / already around when alerts started — D5), `_truncate_description`, footer "Page X of Y · I don't know when codes expire; older ones may have stopped working." Empty: "No codes seen in that window."
- `commands.make_shift_group(cfg, db_path)`: `/shift codes days:Range[1,90]=14 public:bool=False`; generalize the pager helper (`page_size`, `render` callable) so `/news` and `/shift` share it; page size 8; explicit `AllowedMentions.none()`.
- Registered only when `alerts.enabled` (D4), before `copy_global_to`/`sync`.
- Tests: registration and option bounds (absent when alerts off); markers, hostile source escaping, unsafe links, 4096 cap, footer; pager offsets.

### Step 7: Startup permission check
- New `newsbot/bot/permissions.py`: `ChannelRequirement(channel_id, purpose, needed)`; `required_channels(cfg)` (topics: view/send/embed_links; SHiFT channel if alerts on: view/send/mention_everyone; admin channel: view/send; merged by channel id); `missing(perms, needed)`; `async check_channels(client, cfg) -> list[str]` (also "not found or not visible", "not in the configured guild", "not a text channel"); `render_permission_alert(problems)` ≤2000 units.
- `on_ready` (first time): after the interrupted-codes alert, before catch-up; try/except that only logs; one alert, none if clean.
- Tests: requirement building/merging, missing diffs, each failure mode with fake clients, single alert, no alert when clean, alert failure doesn't block catch-up.

### Step 8: Docs
- `config.example.yaml` (per-topic `channel_id`, `digest.channel_id` removed with a v2 note, `alerts.channel_id`); README (digest shape, `/shift codes`, SHiFT channel, roundups, config); `docs/deploy.md` (§4 channels/permissions, no Create Public Threads; §8 rollback 2.0 → 1.3.0 needs the v1 config — v1.3.0 requires `digest.channel_id` and forbids unknown `alerts` keys; migration 003 additive, v1.3.0's `migrate()` no-ops at 3; §10 wording; §15 SHiFT channel; new "Upgrading to v2.0" section); `docs/design.md` (§13 rollback note → migration 003, §5 storage, "superseded by §13" notes in §6 and A17). Owner's voice.
- `load_config("config.example.yaml")` passes with placeholders replaced by non-zero ids.

## 3. Data / schema
- Migration 003 adds `alerted_codes.from_roundup` (backfilled from `status='roundup'`); applied at startup. v1.3.0 ignores it. From v2, `'roundup'` status is written only for over-cap overflow. `digests` unchanged.

## 4. Risks
- **Partial multi-channel publish:** transient errors resume per topic; a permanent error stops (D2) and records `failed` with posted ids (run-now asks, catch-up refuses). A confirmed run-now after a partial failure reposts all topics (unchanged v1 limitation; follow-up). QA: check whether a deterministic nonce dedupes retried topic sends.
- **All-empty day** saves `ok` with `[]`; run-now then says "already posted" (could say "already ran").
- **Limits:** one embed per message, footer capped; run-report links shed before truncation.
- **Channel deleted / permission revoked mid-run:** `retryable=False`, ids kept.
- **Roundup volume:** only codes new to `alerted_codes` post (v1 silent roundups are already known, so no backlog flood); ~48 codes per message; cap 50 per check.
- **Commands:** third top-level group is fine.
- **Test churn:** ~90–110 existing tests modified/deleted, ~60–80 new.

## 5. Owner decisions (2026-09-26)
- D1 coverage notes: _pending_
- D2 permanent per-channel error: _pending_
- D3 roundup cap: _pending_
- D4 `/shift codes` only when alerts enabled: _pending_
- D5 marker wording: _pending_
- Topics sharing a channel: allowed (no validation) unless the owner says otherwise.

## 6. Out of scope
Multi-guild config (to-do #6); persisting per-topic ids to skip posted topics on a forced rerun; `/newsbot status` changes; code expiry; roundup test-alert mode; deleting the old digest channel; CLAUDE.md status.

## 7. Owner test-guild checklist (v2.0.0)
1. Old v1 dev config → exits with the `digest.channel_id` removal message plus per-topic messages; alerts on without `alerts.channel_id` → clear error.
2. Create `#bl4`, `#palworld`, `#diablo`, `#shift-codes`; leave Embed Links off in `#palworld`; start → one admin alert naming it; grant and restart → no alert.
3. `/newsbot preview` → one ephemeral "Would post in #…" per game with news.
4. `/newsbot run-now` → one embed per game channel, no header/thread; run report has per-game jump links to the right messages.
5. `run-now` again → "already posted" confirmation; Cancel works.
6. Revoke Send Messages in `#diablo`, run-now + confirm → admin "publish failed", no run report, status `failed`; restart → partial-failure alert, no auto catch-up; restore.
7. `/newsbot test-alert` (test command on) → posts in `#shift-codes` with `@everyone`; remove Mention @everyone → posts plus missing-permission alert.
8. Roundup rendering via the `--sweep` CLI against a scratch DB (seed, then a fixture item with 8 codes) → "SHiFT codes from a roundup", no `@everyone`.
9. `/shift codes`: ephemeral default; `public:True`; second account can't page; markers; footer; no pings.
10. Rollback drill: 1.3.0 image + v1 dev config against the same dev DB works; switch back to 2.0.0.

## 8. Prod rollout
1. After that day's 09:00 digest posted and well before the next: `cp config.yaml config.v1.yaml`; note `TAG=1.3.0`.
2. Create the three game channels and `#shift-codes`; bot role: View/Send/Embed Links on game channels, View/Send/Mention @everyone on `#shift-codes`; copy the ids.
3. Edit prod `config.yaml` (remove `digest.channel_id`, add topic ids and `alerts.channel_id`); validate a copy locally with `load_config` (never `docker compose config` against the real `.env`).
4. Wait for the tag's CI build, set `TAG=2.0.0`, `./scripts/deploy.sh` (backup taken; migration 003 at startup).
5. Check: no permission alert, `/newsbot status` healthy, `/shift codes` lists existing codes with v1 roundup codes marked; next morning check each game channel and the run report.
6. Rollback: restore `config.v1.yaml`, `TAG=1.3.0`, deploy; no DB restore needed.
