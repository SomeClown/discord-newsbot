# Plan: SHiFT code alerts (v1.2)

Source of truth: `docs/design.md` §12. Branch `feat/shift-code-alerts`. Gate after every step: `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q`. Each step: `implement`, then `test-engineer`. New code follows the CLAUDE.md voice guide.

## 1. Goal and approach

An hourly sweep (every collector except `web_search`, no Claude, no writes to `items`/`stories`), plus the same check inside the 09:00 POST run, posts any new `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX` code to the digest channel. At most `max_pings_per_day` messages a day ping `@everyone`; every code is recorded in `alerted_codes` so it never alerts twice.

Pieces: a pure matcher, a pure planner (what to post, whether to ping), a thin orchestrator sharing the run lock, and one Discord poster that is the only code path that can enable `@everyone`.

**Record-then-post.** Codes are claimed as `pending` and the ping budget is spent in one transaction before anything is sent; marked `posted` only after the send succeeds. A crash mid-post can lose an alert (admin is told at startup) but can never double-ping `@everyone`. Mirrors the digest's claim/publish/save guard (SPEC-DEV 2).

## 2. Clarifications to design §12 (owner decisions recorded in section 9)

- **A1 Seeding.** The seeded marker alone decides. While unseeded, every code found is recorded silently; the marker is set only when the sweep was healthy (≥1 collector succeeded and ≥ half of non-skipped collectors succeeded). Losing the marker re-seeds silently (safe direction).
- **A2 State column.** `alerted_codes.status TEXT CHECK IN ('seeded','too_old','pending','posted','failed')`.
- **A3 Mixed batch wording.** All golden → "New Golden Key code(s)"; otherwise "New SHiFT code(s)" with a `Golden Key:` prefix on golden entries.
- **A4 Overflow.** First message carries the only `@everyone`; continuation messages never ping; the cap counts one ping.
- **A5 AllowedMentions.** Always `AllowedMentions(everyone=True, users=False, roles=False, replied_user=False)` (discord.py's unset fields default truthy); a test pins `to_dict() == {"parse": ["everyone"]}`.
- **A6 Game scoping.** See section 9.
- **A7 Default.** `alerts.enabled` defaults to `false` when the block is absent; `config.example.yaml` shows `true`.
- **A8 Timezone.** The cap day uses `cfg.digest.timezone` via `local_run_date`.
- **A9 Source health.** Sweeps don't write `source_health` or trigger the 3-failures alert.
- **A10 Lock.** A sweep skips if the lock is held; the daily run keeps waiting (a sweep holds it ≤ ~2 min).
- **A11 Too-old codes.** See section 9.
- **A12 Golden wording.** `\bgolden[\s_-]*keys?\b`, case-insensitive ("#GoldenKeys" yes, "golden keyboard" no).
- **A13 Untrusted text in alerts.** The item title is never shown; source name (config-derived) is escaped; the only URL is the canonical collected URL via `_safe_link`.

## 3. Steps

### Step 1: `alerts:` config model
- `AlertsCfg(extra="forbid")`: `enabled: bool = False`, `interval_minutes: int = Field(60, ge=15, le=1440)`, `max_item_age_hours: int = Field(48, ge=1)`, `max_pings_per_day: int = Field(3, ge=0)` (0 = never ping), `allow_test_command: bool = False`. `AppConfig.alerts: AlertsCfg = AlertsCfg()`. WARNING log when `allow_test_command` is true.
- Tests: missing block → disabled defaults; full block parses; unknown key → `ConfigError`; bounds; warning logged.
- Verify: `pytest -q tests/test_config.py tests/test_config_validation_messages.py`

### Step 2: Migration 002, models, repo
- `002_shift_alerts.sql`:
  ```sql
  CREATE TABLE alerted_codes (
      code TEXT PRIMARY KEY CHECK (length(code) = 29),
      first_seen_at TEXT NOT NULL,
      source_name TEXT NOT NULL,
      item_url TEXT NOT NULL,
      message_id INTEGER,
      pinged INTEGER NOT NULL DEFAULT 0 CHECK (pinged IN (0, 1)),
      status TEXT NOT NULL CHECK (status IN ('seeded','too_old','pending','posted','failed'))
  );
  CREATE TABLE alert_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  ```
  `alert_state` keys: `seeded_at`, `last_sweep_at`, `last_sweep_summary`, `ping_day`, `ping_count`.
- Models: `AlertState(seeded, last_sweep_at, last_sweep_summary, ping_day, ping_count)`, `AlertStatus(enabled, seeded, last_sweep_at, last_sweep_summary, codes_alerted, pings_today, max_pings)`.
- Repo (each write one transaction): `get_alert_state`, `known_codes` (chunked), `record_silent_codes(rows, *, now, mark_seeded)` (ON CONFLICT DO NOTHING), `claim_codes(codes, *, pinged, local_day, now)` (plain INSERT so a conflict aborts; spends the ping in the same transaction, resetting on a new `local_day`), `mark_codes_posted`, `mark_codes_failed`, `fail_pending_codes() -> list[str]`, `record_sweep(now, summary)`, `alert_status(today, enabled, max_pings)`. Retention never touches these tables.
- Tests: fresh DB → version 2; v1 DB with data upgrades intact; claim atomicity (dup code rolls back rows and ping count); ping count resets on new day; `fail_pending_codes` flips only pending; retention leaves `alerted_codes`; CHECK constraints. `tests/test_db.py` version asserts move to 2.
- Verify: `pytest -q tests/test_db.py tests/test_repo_alerts.py tests/test_repo_retention_boundaries.py`

### Step 3: Pure matcher `newsbot/shift/match.py`
- `CODE_RE = re.compile(r"(?<![^\W_])(?<!-)([A-Za-z0-9]{5}(?:-[A-Za-z0-9]{5}){4})(?![^\W_])(?!-)")` — ASCII groups, **no IGNORECASE** (it widens `[a-z]` to 'İ', 'ı', 'ſ', Kelvin sign); boundaries block any Unicode letter/digit and `-`; never NFKC-normalize first. `GOLDEN_RE = re.compile(r"\bgolden[\s_-]*keys?\b", re.I)`. `find_codes(text) -> list[str]` (uppercased, deduped, first-seen order), `is_code(s)`, `mentions_golden_key(text)`. Match against `title + "\n" + (full_text or excerpt)`. The tests are the contract.
- Must match: exact; lower/mixed case; string start/end; surrounded by space, parens, quotes, backticks, `:`, `=`, `,`, `.`, newline; two codes; duplicate returned once; all-digit groups.
- Must NOT match: 4-5-5-5-5, 5-5-5-5-4, 6-char group; 6- or 4-group chains; embedded in a longer hyphenated run; adjacent ASCII letter/digit or `é`; spaces instead of hyphens; en/em dash, U+2011, U+2212, U+FF0D; fullwidth `Ａ`/`１`, Cyrillic `А`/`О`, Greek `Ο`, Kelvin `K`, `ſ`, `ı`, `²`, Arabic-Indic digits; ZWSP or U+00AD inside.
- Golden: "Golden Keys", "GOLDEN KEY", "golden\nkey", "#GoldenKeys" true; "golden keyboard", "gold key" false.
- ReDoS sanity: 1 MB of `"AAAAA-"` and of `"A-"` each well under a second.
- Verify: `pytest -q tests/test_shift_match.py`

### Step 4: Untruncated text on `RawItem`; `canonicalize_items`
- `RawItem.full_text: str | None = field(default=None, compare=False, repr=False)` (in memory only; never persisted or sent to the LLM).
- `text.plain_text(s, limit=100_000)` sharing `_strip_markup` with `clean_text`.
- Collectors set `full_text`: RSS (all `entry.content[*].value` plus summary if different), Steam (`contents`), Bluesky (`body`), web search (`description`, snippet only). `FixtureCollector` reads optional `"full_text"`.
- `normalize.canonicalize_items(items)`: the canonicalize + in-batch dedupe phases of `normalize`, which now calls it (no store dedupe, no 24h lookback for the sweep).
- Tests: a code past 500 chars appears in `full_text` not `excerpt`; excerpts byte-identical for existing fixtures; `plain_text` entity/BBCode/span handling and cap; `normalize` unchanged; `save_run` stores no `full_text`.
- Verify: `pytest -q tests/test_collectors_rss_steam.py tests/test_collectors_bsky_brave.py tests/test_text.py tests/test_normalize.py tests/test_pipeline_integration.py`

### Step 5: Pure decision logic `newsbot/shift/decide.py`
- `CodeSighting(code, golden, source_name, item_url, trust, published_at)`, `CodeCandidate(code, golden, source_name, item_url, fresh)`, `AlertPlan(silent: list[(candidate, "seeded"|"too_old")], to_post, ping, cap_reached, mark_seeded)`.
- `sightings_from_items`, `aggregate(sightings, *, now, max_age)`, `seeding_healthy(results)`, `pings_used_today(state, today)`, `plan_alerts(candidates, *, known, seeded, seeding_ok, pings_today, max_pings)`.
- Rules: fresh if any sighting undated or `published_at >= now - max_age` (exactly 48h is fresh); golden if any sighting mentions it; shown source = best trust, then earliest dated, then first seen. Known codes dropped. Unseeded → all silent `seeded`, `mark_seeded = seeding_ok`. Seeded → stale silent `too_old`, fresh to post in first-seen order. `ping = bool(to_post) and pings_today < max_pings`; `cap_reached = bool(to_post) and not ping`. `seeding_healthy`: ≥1 non-skipped success and successes×2 ≥ non-skipped.
- Step 5b (game scoping) per section 9.
- Tests: seeding healthy/unhealthy/empty; once-per-code; 47:59/48:00 fresh, 48:01 stale; undated fresh; mixed ages → fresh; golden aggregation; source preference; cap 0/3, 2/3, 3/3, `max_pings=0`; LA-day reset (not at UTC midnight), DST fall-back and spring-forward days; deterministic order; many codes → one ping decision.
- Verify: `pytest -q tests/test_shift_decide.py`

### Step 6: Alert rendering (`format.py`)
- `RenderedAlert(content, codes, ping)`; `render_code_alerts(candidates, *, ping, test=False) -> list[RenderedAlert]`.
- First message header: `"@everyone " if ping` + `**{"[TEST] " if test}{title}**`; each code in its own fenced block (assert `is_code` first), then `{"Golden Key: " if mixed and golden}{esc(source_name)} · <{_safe_link(url)}>` (link omitted if unsafe). Split past 2000 UTF-16 units; continuations start `**(continued)**`, never contain "@everyone", `ping=False`. With `ping=False`, "@everyone" appears nowhere.
- Tests: wording single/multi/golden/mixed; no "@everyone" when unpinged; hostile source names escaped; URL metacharacters re-encoded; 12 codes × 300-char URLs split correctly, only message 1 pings, each code once; TEST prefix.
- Verify: `pytest -q tests/test_format_code_alerts.py tests/test_format.py`

### Step 7: Collector plumbing for sweeps
- `build_collectors(cfg, secrets, *, include_web_search=True)`.
- `RateLimitState(last_fetch: dict[str, float])`; `run_collectors(..., rate_limit_state=None, clock=time.monotonic)`: with a state, every keyed collector (including the first in its group) waits `max(0, gap - (clock() - last))`; `None` keeps today's behavior. `Deps.rate_limit_state` passed through `build_digest`.
- Tests: web search dropped only when excluded; cross-call gap honored with a fake clock; non-keyed never wait; `None` unchanged.
- Verify: `pytest -q tests/test_run_collectors_gap.py tests/test_collectors_rss_steam.py`

### Step 8: Lock module, sweep, daily hook, CLI
- `newsbot/pipeline/lock.py`: `_run_lock`, `is_run_in_progress()`, `run_lock_or_skip()` (yields False without waiting). `run.py` re-exports `is_run_in_progress`.
- `newsbot/shift/sweep.py`: `CodeAlertPoster` protocol (`post(alert) -> message_id`, raises `PublishError` if transient), `PrintCodeAlertPoster`, `SweepDeps`, `CodeCheckOutcome`, `process_items(...)` (lock-free core), `run_code_sweep(deps) -> outcome | None` (None = lock busy), `run_test_alert(deps, code, golden)`.
- `process_items`: canonicalize → sightings → aggregate → read state + known codes → plan → record silent (set marker if planned) → if posting: render, `claim_codes` **before any send** → post each message with the digest publisher's retry policy (PublishError only) → `mark_codes_posted`; on final failure mark that message's and later codes `failed` and admin-alert the codes (no retry on later sweeps) → admin alert if the cap withheld a ping.
- `run_code_sweep` under `run_lock_or_skip`: run collectors (timeout 20s, shared rate-limit state), `process_items`, `record_sweep(summary)` (e.g. "17/19 sources ok, 1 new code"); logs collector results but never calls `record_source_result`.
- `run_test_alert`: one synthetic official sighting (`source_name="test-alert"`, `item_url="https://shift.gearboxsoftware.com/rewards"`, `published_at=now`), treated as seeded without setting the marker, `test=True`; recorded and counted against the cap.
- Daily hook: `Deps.code_alert_poster: CodeAlertPoster | None = None`; in `_run_claimed` after the publish branch (success or failure), when enabled and a poster is set, `process_items` on the run's collected items inside try/except (log + admin alert; never changes digest outcome). POST mode only; it already holds the lock.
- CLI: `python -m newsbot.pipeline.run --sweep` with `PrintCodeAlertPoster` (works with `--fixtures`).
- Tests (temp DB, fake poster, fixture collectors, stubbed sleep): silent seed; second sweep posts with ping and records `posted`; repeat code posts nothing; 4th batch in a day posts unpinged + admin alert, next LA day pings; unhealthy first sweep doesn't set marker; poster failing 4× → `failed`, admin alert, ping stays spent, no retry; non-PublishError same; CancelledError after claim leaves `pending` which `fail_pending_codes` returns; lock held → None, no writes; daily POST hook alerts once, PREVIEW writes nothing, hook exception doesn't change digest status; sweeps write no `items`/`stories`/`digests`/`source_health`.
- Verify: `pytest -q tests/test_shift_sweep.py tests/test_run_daily_state_machine.py tests/test_pipeline_integration.py` and the `--sweep` CLI twice against `tests/fixtures/shift`.

### Step 9: Discord poster, scheduler job, startup check
- `DiscordCodeAlertPoster(client, channel_id)`: `channel.send(content, allowed_mentions=_PING_EVERYONE if alert.ping else AllowedMentions.none())`, `_PING_EVERYONE` a module constant; error classification shared with `DiscordPublisher` (factored out). Before a ping, check `channel.permissions_for(guild.me).mention_everyone`; if missing, still post and admin-alert about the missing permission.
- `NewsBot`: one `RateLimitState` shared with `build_deps`; `code_alert_poster` set when enabled; `build_sweep_deps()` builds collectors fresh each sweep with `include_web_search=False` (Bluesky JWT has no refresh).
- `setup_hook`: when enabled, `IntervalTrigger(minutes=interval)` job `code-sweep`, first run 2 minutes after start, `coalesce=True, max_instances=1, misfire_grace_time=300`; before `scheduler.start()`, `self._interrupted_codes = fail_pending_codes()`. First `on_ready`: admin alert listing interrupted codes, before catch-up. `_sweep_job`: job boundary; admin alert on the first crash only, cleared by the next success.
- Tests: `_PING_EVERYONE.to_dict() == {"parse": ["everyone"]}` and merge with `none()` equal; poster uses ping mentions only for `ping=True`; missing permission → admin alert and still posts; 5xx wraps, 4xx propagates; **mentions tripwire** (source scan: the only `AllowedMentions(` with `everyone=True` in `newsbot/` is `_PING_EVERYONE`; client default and publisher/alert sends stay `none()`); crash alert fires once then clears; job registered only when enabled.
- Verify: `pytest -q tests/test_code_alert_poster.py tests/test_mentions_tripwire.py tests/test_discord_publisher.py tests/test_client_scheduling.py`

### Step 10: `/newsbot status` and `/newsbot test-alert`
- Status: `alert_status(...)` in a thread; `render_status(snap, spend, alerts=None)` adds one "SHiFT alerts" field (`disabled`, or `last sweep … · 17/19 sources ok · 3 codes alerted · pings today 1 of 3`, `(seeding)` while unseeded).
- `/newsbot test-alert code:<Range[str,29,29]> golden:<bool=False>`, registered **only if** `alerts.allow_test_command` (absent in prod): admin check, `is_code`, busy check, defer, `run_test_alert`, ephemeral summary ("posted with ping", "posted without ping (cap)", "already alerted, nothing posted", "skipped: run in progress").
- Busy reply for run-now/preview becomes "A run or code check is in progress."
- Tests: registration iff flag; `render_status` variants; handler rejects invalid and fullwidth codes.
- Verify: `pytest -q tests/test_command_registration.py tests/test_format.py tests/test_commands_logic.py`, then the full gate.

### Step 11: Docs
- `config.example.yaml`: commented `alerts:` block (`allow_test_command: false` with a warning).
- README: "SHiFT code alerts" section; `/newsbot test-alert` (dev only); costs (no Claude, no Brave); limitations (Reddit `/top?t=day` latency, codes in images, Brave snippets, en-dash-split codes); **fix the Safety note** about `allowed_mentions=none` (all except SHiFT alerts, which allow `@everyone` only).
- `docs/deploy.md`: invite permission **Mention @everyone, @here, and All Roles** (and granting it to the existing prod bot's role or as a digest-channel override); enabling alerts; rollback (migration 002 additive, 1.1.1 runs on a v2 DB); restore caveat (may re-alert codes first seen after the backup, bounded by 48h and the cap).
- `docs/design.md`: record approved clarifications; §5 storage table; §2 module tree (`shift/`, `pipeline/lock.py`).

## 4. Data / schema
Migration 002 adds `alerted_codes` (+ `status`) and `alert_state`; applied at startup (`user_version` 1 → 2); additive and rollback-safe. `RawItem.full_text` is memory-only. Retention doesn't touch the new tables.

## 5. Risks
- **R1 Reddit / health noise.** Sweeps add 3 Reddit requests an hour, spaced by the in-group and cross-call gaps. Sweeps don't touch `source_health`; sweep health shows as `last_sweep_summary`; only a sweep crash pages (once).
- **R2 Bluesky session.** JWT expires ~2h with no refresh; collectors are rebuilt per sweep (~25 logins/day).
- **R3 09:00 overlap.** Sweeps skip when the lock is held; the daily run waits ≤ ~2 min; run-now/preview report busy.
- **R4 Restart mid-post.** Record-then-post; pending → failed at startup with an admin alert; no cross-sweep retry.
- **R5 Clock/DST.** Cap day via zoneinfo; interval job uses elapsed time.
- **R6 Marker lost.** Silent re-seed (missed alert, never a flood); DB restore caveat bounded by 48h and the cap.
- **R7 Other 25-char keys** (BL3/Wonderlands SHiFT, Windows, Xbox). See section 9.
- **R8 Listicles via the daily run.** Bounded by once-per-code, seeding, one ping per sweep, and the daily cap.
- **R9 Coupling.** `Deps` fields, `_run_claimed` hook (off by default), lock import, `RawItem` (`compare=False`), `test_db.py` version, shared error classification.

## 6. Out of scope
Reward filtering, code validation/expiry, OCR, per-source sweep exclusions, X/Twitter, separate alert channel/role pings/DMs, pruning `alerted_codes`, version bump (owner tags after the checklist).

## 7. Owner test-guild checklist before tagging v1.2.0
1. `config.dev.yaml`: `alerts: {enabled: true, interval_minutes: 15, allow_test_command: true}`; ensure no other bot uses the dev token; start the dev bot.
2. Before granting the permission: `/newsbot test-alert code:AAAAA-BBBBB-CCCCC-DDDDD-EEEEE` → posts, no notification, admin channel reports the missing permission.
3. Grant **Mention @everyone, @here, and All Roles** in the digest channel; a new code notifies a second account.
4. Same code again → "already alerted, nothing posted".
5. Two more new codes (3 pings), then a 4th → posts without ping + cap admin alert.
6. `golden:true` → "New Golden Key code".
7. `/newsbot status` shows sweep time, source summary, codes alerted, pings today; the first real sweep shows "(seeding)" and posts nothing.
8. Leave it through a sweep interval: nothing posts from old content; no Reddit 429 storm.
9. Digest header, `/news recent public:true`, and admin alerts still notify nobody.
10. (Optional) kill mid-post; on restart the admin channel lists interrupted codes and nothing re-posts.
11. `allow_test_command: false` + restart → `/newsbot test-alert` disappears.
12. Prod: grant the permission in the prod guild, add `alerts: {enabled: true}` to the Droplet's `config.yaml`, tag, wait for CI, `deploy.sh`.

## 9. Owner decisions (2026-09-25)
- **A6 game scoping:** _pending_
- **A11 too-old codes stay silent forever:** _pending_
