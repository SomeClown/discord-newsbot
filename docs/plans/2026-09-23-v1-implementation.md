# discord-newsbot v1 implementation plan

Source of truth: `/Users/someclown/dev/discord-newsbot/docs/design.md`. Where this plan departs from the spec, the departure is labelled **[SPEC-DEV n]** and listed in section 4 for the owner to decide. Every step below uses the plan's proposed default, so work is not blocked.

---

## 1. Goal and approach

The finished v1 is a single-container Python 3.14 process. One `discord.py` client posts a daily AI-summarized digest for Borderlands 4, Palworld and Diablo IV. It also answers `/news` and search slash commands against a SQLite store (WAL mode, FTS5 full-text search), and gives admins `/newsbot status | run-now | preview`. It deploys to the owner's Droplet from GHCR through `docker compose`.

The build goes from the inside out, in pure layers first: config, store, collectors, normalize, filter, summarize (with a stub Claude client), format. A headless orchestrator comes next. The CLI `python -m newsbot.pipeline.run --dry-run` exercises the whole pipeline with no Discord at all. The bot is a thin adapter on top of that.

The key decision is a `Publisher` protocol. The pipeline hands a rendered digest to a publisher: the CLI's publisher prints it, and the bot's publisher posts it to Discord. So the double-post guard, storage, retries and fallback are written once and tested without Discord. This follows the spec's Approach A/B separation. The cost is about one extra interface.

**Dependencies to add to what the brief lists** (the spec implies them but doesn't name them):
- Runtime: `pyyaml` (the config is YAML) and `tzdata` (zoneinfo data can't be assumed in the slim image).
- Dev: `pytest-asyncio`.
- HTTP mocking uses the built-in `httpx.MockTransport`, so `respx` isn't needed.
- APScheduler is pinned to `>=3.10,<4`. Version 4 has a different API and is still pre-release or new; the 3.x `AsyncIOScheduler` is the well-trodden path with discord.py.

---

## 2. Steps

Conventions for every step:
- **Tooling (owner decision 2026-09-23): Python 3.14, plain `venv` + `pip`, no uv.** Every command below assumes `source .venv/bin/activate` has been run.
- Commit after each step.
- `test-engineer` runs after each `implement` step. It adds edge-case tests beyond the TDD tests the implementer wrote first, then runs the full gate: `ruff check . && ruff format --check . && pytest -q`.
- **All timestamps are stored as ISO-8601 UTC text.** `run_date` is the *local* date in `digest.timezone`.
- Code that depends on time takes an injected `now: Callable[[], datetime]`.

### Step 0: Owner decision checkpoint (non-blocking)
**OWNER CHECKPOINT A.** Read section 4 and accept or override SPEC-DEV 1–9, and confirm the digest time and timezone (default 09:00 America/Los_Angeles). Steps 1–10 go ahead on the defaults. A decision that arrives later changes only the step named in that item.

### Step 1: Project scaffold (`implement`)
- **Goal:** empty but runnable package; lint and test gate green.
- **Files:**
  - `pyproject.toml`: project `newsbot`, `requires-python = ">=3.14,<3.15"`. Lists the direct runtime deps with compatible ranges (`discord.py`, `apscheduler>=3.10,<4`, `httpx`, `feedparser`, `pydantic>=2`, `anthropic`, `pyyaml`, `tzdata`) and tool config only. Installed editable with `pip install -e .`.
  - `requirements.txt`: **every** runtime package (direct and transitive) pinned with `==`, produced by `scripts/lock.sh` (creates a throwaway venv, `pip install .`, `pip freeze --exclude-editable`). This is the lock file; Docker and CI install from it.
  - `requirements-dev.txt`: `-r requirements.txt` plus pinned `pytest`, `pytest-asyncio`, `ruff`.
  - `scripts/lock.sh`: regenerates `requirements.txt`, commented in the voice guide's style so the owner can run it without remembering why it exists.
    - `[tool.ruff]`: line-length 100, rules `E,F,I,UP,B,S,ASYNC`.
    - `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`.
  - `.python-version` containing `3.14` (informational).
  - `.gitignore`: `.env*` except `.env.example`, `config.yaml`, `config.dev.yaml`, `data/`, `.venv/`, `__pycache__/`.
  - `.env.example`: `DISCORD_TOKEN=`, `ANTHROPIC_API_KEY=`, `BRAVE_API_KEY=`, plus `BLUESKY_HANDLE=` and `BLUESKY_APP_PASSWORD=` (see SPEC-DEV 7), `NEWSBOT_CONFIG=/app/config.yaml`, `NEWSBOT_DB=/data/newsbot.db`.
  - Package skeleton, all `__init__.py`: `newsbot/`, `newsbot/collectors/`, `newsbot/pipeline/`, `newsbot/store/`, `newsbot/store/migrations/`, `newsbot/bot/`.
  - `newsbot/logging_setup.py` with `configure_logging(level: str = "INFO") -> None`. It uses a stdlib `logging.Formatter` subclass that emits one JSON object per line (ts, level, logger, msg, plus `extra` fields).
  - `tests/test_smoke.py`.
- **Tests:** smoke import of the package; the JSON formatter outputs parseable JSON and includes `extra` keys.
- **Verify:** `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e . --no-deps && ruff check . && pytest -q`. Also confirm every dependency installs cleanly on 3.14 (the one real risk of choosing 3.14; report any that don't).

### Step 2: Config (`implement`)
- **Goal:** load and validate `config.yaml` and the environment. An invalid config exits non-zero with a readable message.
- **Files:** `newsbot/config.py`, `tests/test_config.py`, `tests/fixtures/config_valid.yaml`
- **Interfaces:**
  ```python
  Trust = Literal["official", "press", "community"]
  class Topic(BaseModel): key: str; name: str; aliases: list[str] = []; entities: list[str] = []
  class DigestCfg(BaseModel): channel_id: int; time: str; timezone: str; lookback_hours: int = 24; max_items_per_topic: int = 60
  class RssSource(BaseModel):   type: Literal["rss"]; name: str; url: HttpUrl; topics: list[str] | None = None; trust: Trust
  class SteamSource(BaseModel): type: Literal["steam_news"]; name: str; app_id: int; topics: list[str] | None = None; trust: Trust
  class BlueskySource(BaseModel): type: Literal["bluesky_search"]; name: str | None = None; query: str; topics: list[str] | None = None; trust: Trust
  class WebSearchSource(BaseModel): type: Literal["web_search"]; name: str = "Brave Search"; queries_per_topic: int = 2
      query_templates: list[str] = ["{name} news", "{name} update OR patch OR leak"]; trust: Trust
  Source = Annotated[RssSource | SteamSource | BlueskySource | WebSearchSource, Field(discriminator="type")]
  class AppConfig(BaseModel): guild_id: int; admin_channel_id: int | None = None; admin_permission: str = "manage_guild"
      digest: DigestCfg; topics: list[Topic]; sources: list[Source]
  class Secrets(BaseModel): discord_token: SecretStr; anthropic_api_key: SecretStr; brave_api_key: SecretStr | None
      bluesky_handle: str | None; bluesky_app_password: SecretStr | None
  def load_config(path: Path) -> AppConfig
  def load_secrets(env: Mapping[str, str] = os.environ, *, require_discord: bool = True) -> Secrets
  class ConfigError(Exception)   # message lists every validation failure
  ```
- **Validators:**
  - `time` matches `HH:MM`. `timezone` resolves through `ZoneInfo`.
  - `admin_permission` is a valid `discord.Permissions` flag name. Check with `hasattr(discord.Permissions, name)`; the import is fine.
  - Topic keys are unique, at most 25 topics (the Discord choice limit), and each key matches `^[a-z0-9_]+$`.
  - Every `source.topics` entry names a known topic key.
  - Source names are unique after defaults are derived. A Bluesky source with no name gets `f"Bluesky: {query}"`. Names are needed because `source_health.source_name` is the primary key.
  - `web_search` is configured but `BRAVE_API_KEY` is missing: log a warning and disable that source, rather than failing.
  - `SecretStr` means `repr` never leaks tokens.
- **Tests:**
  - A valid fixture loads.
  - Each validator rejects bad input: bad tz, bad time, unknown topic ref, duplicate names, bad permission.
  - Secrets never appear in `repr(Secrets)`.
  - The discriminated union routes each source type.
- **Verify:** `pytest tests/test_config.py -q`

### Step 3: Store, connection and migrations (`implement`)
- **Goal:** SQLite with WAL, foreign keys and the migration runner, plus FTS5 verified at startup.
- **Files:** `newsbot/store/db.py`, `newsbot/store/migrations/001_initial.sql`, `tests/test_db.py`
- **Interfaces:**
  ```python
  def connect(path: str | Path) -> sqlite3.Connection   # WAL, foreign_keys=ON, busy_timeout=5000, row_factory=Row
  def migrate(conn) -> int                               # applies NNN_*.sql > PRAGMA user_version, each in a txn; returns new version
  def assert_fts5(conn) -> None                          # CREATE VIRTUAL TABLE temp.x USING fts5(a); raises StoreError if missing
  ```
- **Schema in `001_initial.sql`:** the spec's tables, with SPEC-DEV 1 and 2 applied.
  - `items(id, url UNIQUE, title, excerpt, source_name, trust, published_at NULL, collected_at)`. `topic_key` moves out of this table.
  - `item_topics(item_id FK CASCADE, topic_key, uncertain INTEGER, PK(item_id, topic_key))`
  - `stories(id, topic_key, headline, summary, label CHECK IN (...), is_update_of FK stories ON DELETE SET NULL, digest_id FK digests, created_at)`
  - `story_items(story_id FK CASCADE, item_id FK CASCADE, PK(story_id, item_id))`
  - `digests(id, run_date UNIQUE, status CHECK IN ('pending','ok','partial','failed'), posted_message_ids TEXT DEFAULT '[]', error_notes, input_tokens, output_tokens, created_at, updated_at)`
  - `source_health(...)` as in the spec.
  - `stories_fts` is an FTS5 external-content table (`content='stories', content_rowid='id'`) over `headline, summary`, kept in sync by AFTER INSERT/DELETE/UPDATE triggers on `stories`.
  - Indexes on `stories(topic_key, created_at)`, `stories(created_at)` and `items(collected_at)`.
- **Connection model:** one short-lived connection per unit of work (`with closing(connect(path))`), called from async code through `asyncio.to_thread`. This avoids `check_same_thread` issues and keeps event-loop blocking bounded.
- **Tests:**
  - Migrating a fresh temp DB gives `user_version == 1`, and running it again is a no-op.
  - WAL mode and foreign keys are on.
  - The FTS triggers keep search in sync on insert and delete.
  - `assert_fts5` passes locally.
- **Verify:** `pytest tests/test_db.py -q && python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(a)');print('fts5 ok')"`

### Step 4: Store repo, write path and guards (`implement`)
- **Goal:** every query the pipeline needs, with no SQL anywhere else.
- **Files:** `newsbot/store/repo.py`, `newsbot/store/models.py` (row dataclasses), `tests/test_repo_write.py`
- **Interfaces:**
  ```python
  @dataclass(frozen=True) class PriorStory: id: int; headline: str; created_at: datetime
  @dataclass(frozen=True) class DigestRow: id: int; run_date: date; status: str; posted_message_ids: list[int]; error_notes: str | None
  def existing_urls(conn, urls: Iterable[str]) -> set[str]
  def recent_headlines(conn, topic_key: str, since: datetime) -> list[PriorStory]
  def get_digest(conn, run_date: date) -> DigestRow | None
  def claim_digest(conn, run_date: date, *, force: bool) -> int | None
      # returns digest_id with status 'pending'; None if blocked by guard (ok/partial exists and not force, or pending exists)
      # force=True: UPDATE the existing row in place (keeps id), status→pending
  def save_run(conn, digest_id: int, items: list[StoredItem], stories: list[StoryToSave],
               status: str, message_ids: list[int], notes: str | None, usage: Usage) -> None   # ONE transaction
  def mark_digest_failed(conn, digest_id: int, notes: str, message_ids: list[int]) -> None
  def record_source_result(conn, source_name: str, now: datetime, error: str | None) -> int  # returns consecutive_failures
  def purge_older_than(conn, cutoff: datetime) -> tuple[int, int]   # (items, stories) deleted
  ```
  `StoredItem` carries the canonical URL, the RawItem fields, and `topics: dict[str, bool]` (topic key to its uncertain flag). `StoryToSave` carries `topic_key, headline, summary, label, item_urls, update_of_story_id`. `save_run` resolves URLs to item ids inside the transaction.
- **Tests:**
  - The guard: `ok` and `partial` block; `failed` allows a re-claim; `pending` blocks; `force` replaces in place with the same id.
  - `save_run` is atomic: inject a failure halfway and nothing persists.
  - `consecutive_failures` increments and then resets on success.
  - Retention cascades to `story_items`, `item_topics` and FTS, and nulls `is_update_of`.
  - `existing_urls` works with batches over 999 (the SQLite variable limit, so chunk).
- **Verify:** `pytest tests/test_repo_write.py -q`

### Step 5: Store repo, read path for commands and status (`implement`)
- **Files:** `newsbot/store/repo.py` (extend), `tests/test_repo_read.py`
- **Interfaces:**
  ```python
  @dataclass(frozen=True) class StoryView: id; topic_key; headline; summary; label; created_at; urls: list[str]; is_update_of: int | None
  def query_stories(conn, topic_keys: list[str], since: datetime, label: str | None, limit: int, offset: int) -> tuple[list[StoryView], int]  # (page, total)
  def search_stories(conn, query: str, since: datetime, limit: int, offset: int) -> tuple[list[StoryView], int]
  def fts_escape(user_query: str) -> str   # tokenizes on whitespace, wraps each token in double quotes (escaping "), joins with space
  def status_snapshot(conn, now: datetime, month_start: datetime) -> StatusSnapshot
      # last digest row, all source_health rows, items/stories counts last 24h, month input/output tokens
  ```
- **Tests:**
  - Paging totals are correct.
  - Topic, label and days filters work.
  - Raw user input like `foo" OR bar*`, `NEAR(`, or an empty string never raises `sqlite3.OperationalError`. Empty input returns nothing.
  - Search ranks by `bm25` and then recency.
  - Month token sums are correct across a month boundary.
- **Verify:** `pytest tests/test_repo_read.py -q`

### Step 6: Collector base, RSS and Steam (`implement`)
- **Files:**
  - `newsbot/collectors/base.py`, `newsbot/collectors/rss.py`, `newsbot/collectors/steam.py`
  - `newsbot/text.py`: `clean_text(html_or_bbcode: str, limit: int = 500) -> str`. It strips HTML with stdlib `html.parser`, strips Steam BBCode `[tag]...[/tag]`, collapses whitespace, and truncates on a word boundary with an ellipsis.
  - `tests/fixtures/feeds/`: a committed sample Atom feed, RSS 2.0, a subreddit `.rss`, a YouTube `videos.xml`, and Steam `GetNewsForApp` JSON.
  - `tests/test_collectors_rss_steam.py`, `tests/test_text.py`
- **Interfaces:**
  ```python
  @dataclass(frozen=True, slots=True)
  class RawItem:
      url: str; title: str; excerpt: str; source_name: str; trust: Trust
      published_at: datetime | None          # tz-aware UTC or None
      topics: tuple[str, ...] | None = None  # SPEC-DEV 3: restricts/assigns topic matching; None = all topics
  @dataclass(frozen=True)
  class CollectorResult:
      source_name: str; source_type: str; items: list[RawItem]
      error: str | None = None               # failure → source_health
      skipped: str | None = None             # e.g. "quota" → coverage note, NOT a health failure
  class Collector(Protocol):
      name: str; source_type: str
      async def collect(self, http: httpx.AsyncClient) -> list[RawItem]   # raises on failure
  def build_collectors(cfg: AppConfig, secrets: Secrets) -> list[Collector]
  async def run_collectors(collectors, http, timeout_s: float = 20.0) -> list[CollectorResult]
      # asyncio.gather; each wrapped in asyncio.timeout; exceptions → CollectorResult(error=...); QuotaExceeded → skipped="quota"
  class QuotaExceeded(Exception)
  ```
- **RSS:**
  - Fetch with httpx using a descriptive `User-Agent` (`discord-newsbot/1.0 (+contact)`). This is needed for Reddit.
  - Pass the bytes to `feedparser.parse` so the network goes through httpx and can be timed out and mocked.
  - `published_parsed` or `updated_parsed` becomes a UTC datetime. Use `summary` and `content` for the excerpt.
  - A bozo feed with zero entries counts as an error.
- **Steam:** `GET https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={id}&count=20&maxlength=0&feeds=steam_community_announcements`.
  - `feeds=` keeps the results to official announcements. Without it Steam mixes in third-party press feeds, which should not be labelled `trust: official`.
  - `date` is epoch seconds.
- **Tests:**
  - Each fixture parses to the expected RawItems, using an `httpx.MockTransport`.
  - Items with no date give `published_at=None`.
  - A 500 or timeout turns into `CollectorResult.error` without raising.
  - One slow collector doesn't block the others.
  - `clean_text` handles nested HTML, BBCode, entities and truncation.
- **Verify:** `pytest tests/test_collectors_rss_steam.py tests/test_text.py -q`

### Step 7: Bluesky and web-search collectors (`implement`)
- **Files:** `newsbot/collectors/bluesky.py`, `newsbot/collectors/web_search.py`, fixtures `tests/fixtures/bluesky_search.json` and `tests/fixtures/brave_news.json`, `tests/test_collectors_bsky_brave.py`
- **Bluesky:** `app.bsky.feed.searchPosts` with `q`, `sort=latest`, `limit=25`.
  - If handle and app password are set, call `com.atproto.server.createSession` on `https://bsky.social` and query the PDS with the bearer token. Otherwise try `https://public.api.bsky.app` without auth. See the risk in section 4.
  - A post's URL is `https://bsky.app/profile/{handle}/post/{rkey}`. The title is the first line of the text, truncated to 120 characters. The excerpt is the full text.
  - A 401 or 403 with no credentials becomes `QuotaExceeded`-style `skipped="auth"`: a coverage note, not a health failure.
- **Brave:** one collector instance expands into `queries_per_topic` queries per topic, using `query_templates` formatted with `{name}`.
  - Use `GET https://api.search.brave.com/res/v1/news/search?q=...&freshness=pd&count=20` with header `X-Subscription-Token`.
  - Queries run **sequentially with a 1.1 s gap**, because the free tier allows about 1 request per second.
  - An HTTP 429, or a 402 on quota, raises `QuotaExceeded`.
  - Each RawItem gets `topics=(topic.key,)` from the query that produced it.
  - `page_age` is parsed when present; otherwise `published_at` is None.
- **Tests:**
  - Fixture parsing for both.
  - The auth path calls createSession once and reuses the token.
  - A 429 gives `skipped="quota"` and does not raise.
  - Query expansion count equals topics times `queries_per_topic`.
  - The pacing sleep is injected so tests run fast.
- **Verify:** `pytest tests/test_collectors_bsky_brave.py -q`

### Step 8: Normalize and filter (`implement`)
- **Files:** `newsbot/pipeline/normalize.py`, `newsbot/pipeline/filter.py`, `tests/test_normalize.py`, `tests/test_filter.py`
- **Interfaces:**
  ```python
  def canonicalize(url: str) -> str | None   # None if not http(s) → item dropped (security: no javascript:/data: URLs reach Discord)
  def normalize(items: list[RawItem], known_urls: Callable[[set[str]], set[str]], now: datetime, lookback: timedelta) -> list[RawItem]
      # canonicalize, in-batch dedupe (first seen wins, prefer higher trust), drop known, drop published_at < now-lookback
      # published_at None → kept (URL dedupe prevents repeats) — SPEC-DEV 4
  @dataclass(frozen=True) class TopicItem: item: RawItem; topic_key: str; uncertain: bool
  def build_matchers(topics: list[Topic]) -> dict[str, TopicMatcher]   # precompiled regex
  def filter_items(items: list[RawItem], topics: list[Topic], max_per_topic: int) -> dict[str, list[TopicItem]]
  ```
- **Canonicalization:**
  - Lowercase the scheme and host.
  - Drop the fragment and the default port.
  - Remove only tracking params: `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`, `ref_src`, `igshid`, `si`, `feature`. **Other query params are kept.** YouTube `watch?v=` and Steam `?appid` depend on them.
  - Strip a trailing slash from a non-root path.
  - `http` and `https` are **not** merged. The spec doesn't ask for it; note this and leave it alone.
- **Matching:**
  - Use `(?<!\w)` + `re.escape(term)` + `(?!\w)` with `re.IGNORECASE`, not `\b`, so terms that start or end with non-word characters still work.
  - A name or alias match is confident. An entity-only match is `uncertain`.
  - If `item.topics` is set, only those topics are considered.
  - **Dedicated sources** (SPEC-DEV 3): if `item.topics` has exactly one key, the item is a confident match for that topic even without a keyword hit.
  - Per-topic cap ordering (SPEC-DEV 5): trust, then recency.
- **Tests:**
  - Canonicalization table, including utm stripping, preserved YouTube `v`, host case, trailing slash, rejecting `javascript:`, and idempotency.
  - "D4" doesn't match "D40". "2K" matches "2K Games". "Diablo IV" matches, and "Diablo 4" matches through its alias.
  - Entity-only gives uncertain.
  - One item matching two topics appears under both.
  - Dedicated-source items with no keyword match are kept.
  - The cap keeps official items over newer community ones.
  - The lookback boundary.
- **Verify:** `pytest tests/test_normalize.py tests/test_filter.py -q`

### Step 9: Summarize, with a stubbed Claude client (`implement`)
- **Files:** `newsbot/pipeline/summarize.py`, `newsbot/pipeline/prompts.py`, `tests/test_summarize.py`
- **Interfaces:**
  ```python
  MODEL = "claude-haiku-4-5-20251001"
  PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK = 1.00, 5.00   # verify against current pricing page at implementation time
  class StoryOut(BaseModel):
      headline: str = Field(min_length=1, max_length=200); summary: str = Field(min_length=1, max_length=400)
      label: Literal["official", "reported", "rumor"]; item_urls: list[str]; relevant: bool
      update_of_headline: str | None = None
  class StoriesOut(BaseModel): stories: list[StoryOut]      # tool input must be an object → wraps the spec's array
  @dataclass class LLMResult: data: dict; input_tokens: int; output_tokens: int
  class LLMClient(Protocol):
      async def emit_stories(self, system: str, user: str) -> LLMResult
  class AnthropicLLM:   # AsyncAnthropic(api_key, max_retries=0, timeout=60); forced tool_choice={"type":"tool","name":"emit_stories"},
                        # tool input_schema = StoriesOut.model_json_schema(); max_tokens=4096; temperature=0.2
  @dataclass class StoryDraft: headline; summary; label; item_urls: list[str]; update_of_story_id: int | None
  @dataclass class TopicSummary: topic_key: str; stories: list[StoryDraft]; fallback: bool; note: str | None; usage: Usage
  def build_prompt(topic: Topic, items: list[TopicItem], prior: list[PriorStory]) -> tuple[str, str]
  def postprocess(out: StoriesOut, items: list[TopicItem], prior: list[PriorStory]) -> list[StoryDraft]
  async def summarize_topic(llm, topic, items, prior, *, sleep=asyncio.sleep) -> TopicSummary
  ```
- **Prompt:**
  - The system prompt carries all the spec's rules.
  - The user message holds items as a JSON array: `{n, url, title, excerpt ≤500, source, trust, uncertain}`, inside `<items>` delimiters. It says explicitly that the contents are untrusted data.
  - It includes prior headlines from the last 3 days for this topic.
  - SPEC-DEV 6: the prompt tells the model to mark a story `relevant: false` when it adds nothing new over a prior headline.
- **Postprocessing, in order:**
  1. Drop `relevant: false`.
  2. Keep only `item_urls` that exactly match an input URL. Also canonicalize the model's URLs before comparing.
  3. Drop stories left with no URLs.
  4. Enforce `official` in code. If none of the linked items has `trust == official`, downgrade the label to `reported`. The spec states this only as a prompt rule; this is defense in depth.
  5. Match `update_of_headline` to a prior story (SPEC-DEV 8). Normalize both sides with casefold, NFKC, stripped punctuation and collapsed whitespace, then require an exact match; on ties take the newest. No match means None.
- **Retry:** 3 attempts with backoff of 1 s, 2 s and 4 s. A retry is triggered by `anthropic.APIError`, a missing `tool_use` block, or a pydantic `ValidationError`. After that, return `fallback=True` with no stories, and the note "Summary unavailable; showing headlines."
  - The formatter renders fallback topics from their `TopicItem` titles and URLs (SPEC-DEV 9).
  - Token usage adds up across attempts.
- **Tests (stub LLM, no network):**
  - The prompt contains the untrusted-data warning, prior headlines, truncated excerpts and the trust flags.
  - Hallucinated URLs are removed. A story with only bad URLs is dropped.
  - `official` without an official item is downgraded.
  - Update-headline matching: an exact normalized match links; a near miss is ignored.
  - Invalid JSON twice and then valid succeeds, with usage summed.
  - Three failures give a fallback.
  - An `APIError` triggers a retry.
  - An injection string inside an excerpt reaches the model only inside the items block.
- **Verify:** `pytest tests/test_summarize.py -q`

### Step 10: Format (`implement`)
- **Files:** `newsbot/bot/format.py`, `tests/test_format.py`. `discord.Embed` is plain data, so no gateway is needed.
- **Interfaces:**
  ```python
  @dataclass class RenderedDigest: header: str; embed_messages: list[list[discord.Embed]]   # each inner list = one message
  def render_digest(run_date: date, topics: list[Topic], summaries: dict[str, TopicSummary],
                    fallback_items: dict[str, list[TopicItem]], coverage_notes: list[str]) -> RenderedDigest
  def render_story_page(stories: list[StoryView], topics_by_key, title: str, page: int, pages: int) -> discord.Embed
  def render_status(snap: StatusSnapshot, spend_usd: float) -> discord.Embed
  def to_text(r: RenderedDigest) -> str     # for the CLI publisher
  def esc(s: str) -> str                    # discord.utils.escape_markdown + escape_mentions
  ```
- **Rules:**
  - Sort order is official, then reported, then rumor. Updates sort with their own label and are prefixed `🔁 UPDATE`, linking to the original story's first URL.
  - Within a label, stories with more items come first.
  - "Least important" (cut first) is the reverse of display order.
  - Show up to 3 links, then "+N more".
  - Colors are fixed per topic. Derive them from a hash of `topic.key` into a fixed palette of 6.
  - Limits: description ≤4096, title ≤256, total ≤6000 per message, ≤10 embeds per message, header ≤2000.
  - When a story is cut, add the line `+N more, use /news`.
  - Pack embeds into messages greedily under 6000 in total.
  - An empty topic shows "No new stories today."
  - Coverage notes (for example, web search skipped because of quota) go in the header.
  - Every scraped string passes through `esc()`. URLs are placed only in link targets, and they come only from `StoryDraft.item_urls`, which step 9 has already validated.
- **Tests:**
  - Sort order and update placement.
  - A 4096-boundary case with 40 long stories, checking the "+N more" line and that the total is ≤6000.
  - Three large topics split into more than one message.
  - The empty topic.
  - The fallback rendering.
  - `@everyone` and `**bold**` in a headline are escaped.
  - Coverage note in the header.
- **Verify:** `pytest tests/test_format.py -q`

### Step 11: Pipeline orchestration and headless CLI (`implement`)
- **Files:** `newsbot/pipeline/run.py`, `newsbot/pipeline/publisher.py`, `tests/test_pipeline_integration.py`, fixtures `tests/fixtures/integration/` (fixture feeds and canned LLM JSON)
- **Interfaces:**
  ```python
  class RunMode(StrEnum): POST = "post"; PREVIEW = "preview"
  class Publisher(Protocol):
      async def publish(self, r: RenderedDigest) -> list[int]      # returns message ids; raises PublishError
  class PrintPublisher: ...  # stdout via format.to_text; returns []
  @dataclass class Deps: cfg; db_path; http: httpx.AsyncClient; llm: LLMClient; collectors: list[Collector]; now: Callable; alert: Callable[[str], Awaitable[None]]
  @dataclass class PipelineOutcome: status: Literal["ok","partial","failed","skipped"]; rendered: RenderedDigest | None; notes: list[str]; usage: Usage
  async def build_digest(deps, run_date) -> tuple[RenderedDigest, list[StoredItem], list[StoryToSave], status, notes, usage, list[CollectorResult]]  # steps 1-4, no writes
  async def run_daily(deps, publisher, *, mode: RunMode, force: bool = False) -> PipelineOutcome
  _run_lock: asyncio.Lock   # module-level; serializes scheduled, run-now, preview
  def main(argv=None) -> int   # python -m newsbot.pipeline.run
  ```
- **`run_daily` in POST mode** (SPEC-DEV 2):
  1. Take the lock.
  2. `claim_digest` in a thread. If the guard returns None, the outcome is `skipped`.
  3. `build_digest`.
  4. Record source health: failures increment, successes reset, skipped doesn't touch the row. When a source's count first reaches **exactly 3**, send an admin alert, so there's no daily spam.
  5. Publish with 3 retries (backoff 2, 4 and 8 s).
  6. On success, `save_run` in one transaction with status `ok`, or `partial` if any topic fell back or any source was skipped.
  7. On a publish failure, `mark_digest_failed` (items and stories are not saved, so a retry recollects), and send an alert.
  8. Wrap everything in a job-boundary try/except. It logs, alerts, and on an exception after the claim marks the digest failed.
- **`run_daily` in PREVIEW mode:** no claim, no health writes, no saves. It still reads the store for dedupe and prior headlines.
- **CLI flags:**
  - `--config PATH` (default `$NEWSBOT_CONFIG`) and `--db PATH` (default `$NEWSBOT_DB`, or `./data/newsbot.db`).
  - `--dry-run` (PREVIEW mode with PrintPublisher, the default).
  - `--post-to-stdout` (POST mode with PrintPublisher against the given DB, which exercises the guard and save).
  - `--fixtures DIR` (collectors replaced by file-backed ones) and `--stub-llm FILE` (canned JSON), for fully offline runs.
  - `--force`.
  - Runs `migrate` and `assert_fts5` first.
- **Integration test:** fixture feeds, stub LLM and a temp DB.
  - Check the rendered text.
  - Check that the DB holds items, stories, links and a digest with `ok` status.
  - A second run the same day is `skipped`.
  - `force=True` replaces the row and finds 0 new items, which renders "No new stories today."
  - The publisher raising 3 times gives `failed` with nothing saved, and a rerun succeeds.
  - Preview writes nothing: compare row counts before and after.
  - A fallback topic gives `partial`.
- **Verify:**
  - `pytest -q`
  - `python -m newsbot.pipeline.run --dry-run --config tests/fixtures/config_valid.yaml --db /tmp/nb.db --fixtures tests/fixtures/integration --stub-llm tests/fixtures/integration/llm.json`

### Step 12: Seed source research and `config.example.yaml` (`investigate` with web access; may start any time after step 2)
- **Goal:** a `config.example.yaml` that validates, holding real sources for the 3 topics, plus a short notes file.
- **Files:** `config.example.yaml`, `docs/sources.md` (a table of each source with URL, type, trust, the date it was verified to return entries, and notes like "no official RSS; covered via Steam")
- **Scope:**
  - Official: Gearbox/Borderlands news, the 2K newsroom, the Pocketpair and Palworld site/blog, Blizzard news for Diablo IV (confirm whether an RSS feed exists at all; see the risks), and the official X-mirror alternatives.
  - Steam: confirm the app IDs by fetching `GetNewsForApp`. Expected values are Palworld 1623730, Borderlands 4 1285190 and Diablo IV 2344520, but check each.
  - Subreddits as `https://www.reddit.com/r/<sub>/new/.rss`, with trust community: r/Borderlands, r/borderlands3 if it's still active for BL4, r/Palworld, r/diablo4.
  - Official YouTube channel feeds as `https://www.youtube.com/feeds/videos.xml?channel_id=UC…` for Gearbox/Borderlands, Pocketpair/Palworld and Diablo.
  - Press: a gaming news RSS or two with wide coverage (PC Gamer, Eurogamer, GamesRadar, IGN, Rock Paper Shotgun), with no `topics` so they're keyword-matched. Official sources get `topics: [<one key>]`, which makes them dedicated.
  - One Bluesky search per topic.
  - One `web_search` block.
- **Verify:** `python -c "from newsbot.config import load_config; load_config('config.example.yaml')"`, then a live `--dry-run` with `--stub-llm` confirms each source returns ≥1 item or has a documented reason it doesn't.
- **OWNER CHECKPOINT B: the owner reviews and approves the source list.** Launch is blocked until this is done.

### Step 13: Live headless run (M1 exit)
- **OWNER CHECKPOINT C:** the owner creates the Anthropic API key and the Brave Search API key (and Bluesky app password if SPEC-DEV 7 is accepted), and puts them in `.env.dev`.
- **What to do** (`implement` runs it; the owner reads the output):
  1. Copy `config.example.yaml` to `config.dev.yaml` with placeholder Discord IDs.
  2. Run `set -a && . ./.env.dev && set +a && python -m newsbot.pipeline.run --dry-run --config config.dev.yaml --db ./data/dev.db`.
  3. Tune the prompt and the source list.
- **OWNER CHECKPOINT D (first pass):** the owner judges summary quality, labels, and merged duplicates in the printed digest.
- **Verify:**
  - The run completes in under 2 minutes.
  - Per-source results are logged.
  - Token usage is printed. It should be well under 30k input tokens per topic.

### Step 14: Bot client, scheduler, heartbeat and alerts (`implement`)
- **OWNER CHECKPOINT E:** before this step's verification, the owner creates **two** Discord applications, `newsbot` (prod) and `newsbot-dev`.
  - Default intents only.
  - The owner creates a private test guild and invites `newsbot-dev` with Send Messages, Embed Links, Create Public Threads, Use Application Commands, and also Send Messages in Threads, which thread discussion needs.
  - Tokens go in `.env` and `.env.dev`. The owner also creates `config.dev.yaml` with the test guild's channel IDs.
- **Files:**
  - `newsbot/bot/client.py`, `newsbot/alerts.py`
  - `newsbot/__main__.py`: `python -m newsbot` loads the config (exits 2 with a `ConfigError` message on failure), loads secrets, configures logging, migrates, runs `assert_fts5`, and runs the bot.
  - `newsbot/healthcheck.py`
  - `tests/test_client_scheduling.py`
- **Interfaces:**
  ```python
  class NewsBot(discord.Client):
      def __init__(self, cfg, secrets, db_path): intents=discord.Intents.default(); allowed_mentions=AllowedMentions.none()
      tree: app_commands.CommandTree
      async def setup_hook(self): # runs inside the bot's loop
          # create httpx.AsyncClient, AnthropicLLM; register commands; tree.copy_global_to(guild); await tree.sync(guild=Object(guild_id))
          # scheduler = AsyncIOScheduler(timezone=ZoneInfo(tz)); add daily CronTrigger(hour,minute,tz), misfire_grace_time=3600, coalesce=True, max_instances=1
          # add retention CronTrigger(hour=3, minute=30); add heartbeat IntervalTrigger(seconds=60); scheduler.start()
      async def on_ready(self): # once-only flag (on_ready fires on every reconnect) → catch-up check
      async def close(self): scheduler.shutdown(wait=False); await http.aclose(); await super().close()
  class DiscordPublisher(Publisher): posts header to digest channel, create_thread(name=f"News {date}"), then embed messages; returns ids
  def should_catch_up(now_local: datetime, digest_time: time, existing: DigestRow | None) -> bool   # pure, tested
  async def send_alert(client, admin_channel_id: int | None, text: str) -> None   # no-op if unset; never raises
  HEARTBEAT = Path("/tmp/newsbot-heartbeat")   # written iff client.is_ready() and not client.is_closed() and scheduler.running
  # healthcheck.py: exit 0 iff mtime < 180s old
  ```
- **Tests (pure, no gateway):**
  - `should_catch_up`: before the time, after the time with no row, after with `ok`, after with `failed`. A `pending` row gives no catch-up plus an admin alert, since it may already have been posted.
  - DST: the CronTrigger next-fire for the days around the March and November transitions in America/Los_Angeles lands on local 09:00.
  - `run_date` comes from the local date, not UTC. Near midnight UTC the two differ.
  - Healthcheck staleness logic.
- **Verify:**
  - `pytest tests/test_client_scheduling.py -q`
  - Manually: `set -a && . ./.env.dev && set +a && python -m newsbot` with `NEWSBOT_CONFIG=config.dev.yaml` connects, logs "synced N commands to guild", and the heartbeat file updates every 60 s.

### Step 15: Member commands (`implement`)
- **Files:** `newsbot/bot/commands.py`, `newsbot/bot/views.py`, `tests/test_commands_logic.py`
- **Commands** (SPEC-DEV 10: Discord doesn't allow `/news` to be both runnable and a parent of `/news search`):
  - `/news recent game:<choice> days:<1-30=7> label:<official|reported|rumor, optional> public:<bool=false>`
  - `/news search query:<text, 1-100 chars> days:<1-30=30> public:<bool=false>`
- **How they're built:**
  - Game choices come from `cfg.topics` plus "All". The commands are built in a factory, `make_news_group(cfg, db_path) -> app_commands.Group`, after the config loads.
  - Every handler starts with `await interaction.response.defer(ephemeral=not public)`, then runs the DB query through `asyncio.to_thread`, then sends a followup with the page embed and a `PagerView`.
  - `PagerView(discord.ui.View)` has a timeout of 600 s, Prev and Next buttons, and `interaction_check` returning `interaction.user.id == owner_id`. Anyone else gets an ephemeral "Only the requester can page." Buttons disable at the ends.
  - Page size: 6 stories.
- **Tests:** keep the logic pure. `resolve_query_args(...) -> (topic_keys, since, label)`, paging math, and the pager's owner check through a fake interaction object.
- **Verify:** `pytest tests/test_commands_logic.py -q`, then manually in the test guild once step 16 is done.

### Step 16: Admin commands and scheduled jobs (`implement`)
- **Files:** `newsbot/bot/commands.py` (the `/newsbot` group), `newsbot/bot/views.py` (a `ConfirmView`), `newsbot/bot/client.py` (job callbacks), `tests/test_admin_logic.py`
- **Permissions:**
  - `@app_commands.default_permissions(**{cfg.admin_permission: True})` on the group, for the UI.
  - A **server-side check** on every handler: `getattr(interaction.permissions, cfg.admin_permission)`. On failure it sends an ephemeral denial and logs a warning.
- **`/newsbot status`:** runs `status_snapshot`, computes `spend = in*PRICE_IN/1e6 + out*PRICE_OUT/1e6`, and renders with `render_status`. Sources with `consecutive_failures >= 3` are flagged.
- **`/newsbot run-now`:**
  - If today's row is `ok` or `partial`, show an ephemeral `ConfirmView` ("Today's digest already posted. Post again?"); on confirm, run with `force=True`.
  - Otherwise, defer and run `run_daily(POST)`. A followup reports the outcome.
  - If the lock is held, reply "A run is in progress."
- **`/newsbot preview`:** defer ephemerally, run `run_daily(PREVIEW)`, then send the header and embed messages as ephemeral followups. Keep the whole run inside the 15-minute interaction-token window; collector timeouts of 20 s plus LLM retries fit easily.
- **Jobs:**
  - The daily job calls `run_daily(POST)` with `DiscordPublisher`.
  - The retention job calls `purge_older_than(now - 90d)`.
  - Both catch everything at the boundary and send an alert.
- **Tests:** permission check logic with fake permissions; the confirm flow's decision function; spend computation.
- **Verify:** `pytest -q`, then the step 17 manual checklist.

### Step 17: Test-guild acceptance (M2 exit)
**OWNER CHECKPOINT D (final) + E-verify.** Run the bot locally with `.env.dev` and `config.dev.yaml`. The owner and the implementer walk through this list together:
- [ ] Commands appear only in the test guild.
- [ ] A non-admin can't see or run `/newsbot`. A forged call is still denied (test by removing `default_permissions` temporarily or using a second account).
- [ ] `/newsbot preview` shows the digest privately, and the DB counts are unchanged.
- [ ] `/newsbot run-now` posts the header, the thread and the embeds. A second `run-now` asks for confirmation.
- [ ] `/news recent` and `/news search` page correctly. Another user can't page.
- [ ] A headline containing `@everyone` doesn't ping anyone.
- [ ] Changing the config time to 2 minutes ahead fires the scheduled job. Restarting after that time with no row triggers catch-up.
- [ ] Revoking the Brave key or pointing one source at a 404 shows up in the coverage note and in `/newsbot status`.

**The owner approves preview quality here.** This is the launch gate for the prompt.

### Step 18: Docker and compose (`devops`)
- **Files:** `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker-compose.dev.yml` (optional local override using `.env.dev` and `config.dev.yaml`)
- **Dockerfile:**
  - Builder stage: `python:3.14-slim`. `python -m venv /app/.venv`, copy `requirements.txt` and `pip install --no-cache-dir -r requirements.txt` (cached layer), then copy the source and `pip install --no-deps .`.
  - Runtime stage: `python:3.14-slim`. Copy `/app/.venv` and the source. `useradd -u 10001 newsbot`, `mkdir /data && chown 10001 /data`, then `USER newsbot`.
  - `ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1`.
  - **A build-time check `RUN python -c "import sqlite3;sqlite3.connect(':memory:').execute('create virtual table t using fts5(a)')"`**, so the build fails if the image's sqlite lacks FTS5.
  - `HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 CMD python -m newsbot.healthcheck`.
  - `CMD ["python","-m","newsbot"]`.
- **Compose:** as the spec lists (`restart: unless-stopped`, the config mount read-only, `./data:/data`, `env_file: .env`, json-file logging 10m×3), plus `image: ghcr.io/<owner>/discord-newsbot:${TAG:-latest}`, `read_only: true` with `tmpfs: /tmp`, and `security_opt: [no-new-privileges:true]`.
- **Verify:**
  - `docker build -t newsbot:local .` succeeds, including the FTS5 check.
  - `docker run --rm newsbot:local python -c "import newsbot"`.
  - `docker compose -f docker-compose.yml -f docker-compose.dev.yml up`: the bot connects to the test guild and `docker inspect --format '{{.State.Health.Status}}'` becomes `healthy` within about 3 minutes.
  - A bad config exits non-zero, and `docker compose ps` shows the restart loop with a clear log line.

### Step 19: CI to GHCR (`devops`)
- **OWNER CHECKPOINT F:** the owner creates the GitHub repo and pushes. Then the owner decides whether the GHCR package is private (the Droplet then needs `docker login ghcr.io` with a PAT scoped `read:packages`) or public.
- **Files:** `.github/workflows/ci.yml`
  - Trigger on push and PR.
  - A job using `actions/setup-python` (3.14, pip cache keyed on the requirements files): `pip install -r requirements-dev.txt && pip install -e . --no-deps`, `ruff check .`, `ruff format --check .`, `pytest -q`.
  - A build job on `main` only, `needs: test`, with `permissions: packages: write`. It uses `docker/login-action` with `GITHUB_TOKEN`, then `docker/build-push-action`, tagging `latest` and `sha-<short>` so rollback is possible.
- **Verify:** a push to a branch shows a green test job. A merge to main pushes the image, and `docker pull ghcr.io/<owner>/discord-newsbot:sha-xxxx` works.

### Step 20: QA review (`qa`), then fixes (`implement`)
- **Scope:**
  - Token handling: never logged. `SecretStr` is used everywhere, and the discord.py and httpx loggers don't print auth headers; set the httpx logger to WARNING.
  - Server-side admin checks.
  - Escaping and `allowed_mentions` on every send path, including ephemeral followups and alerts.
  - That URL provenance can't be bypassed.
  - FTS input handling.
  - Prompt-injection handling.
  - SQL parameterization.
  - Lock and concurrency between the scheduled job, run-now and preview.
  - The `pending`-status crash window.
  - Container user, mounts and the read-only root.
  - Dependency pinning.
- **Verify:** QA report findings are each resolved or accepted by the owner. **OWNER CHECKPOINT G:** the owner triages any finding marked accept-risk.

### Step 21: Runbook, backup cron and docs (`devops` for runbook and backup; `docs` for README and CLAUDE.md)
- **Files:**
  - `docs/deploy.md`, the Droplet runbook:
    - Install docker compose, create `/opt/newsbot/{data}`, and `chown 10001:10001 data`.
    - Place `config.yaml`, `.env` and `docker-compose.yml`, and set `chmod 600 .env`.
    - GHCR login if the package is private.
    - `docker compose pull && docker compose up -d`.
    - Rollback with `TAG=sha-xxxx docker compose up -d`.
    - Log viewing.
    - Restore from backup.
  - `scripts/backup.sh`: `sqlite3 /opt/newsbot/data/newsbot.db ".backup /opt/newsbot/backups/newsbot-$(date +%F).db"`, then keep the 7 newest (`ls -1t | tail -n +8 | xargs -r rm`). Plus a cron line in the runbook. It requires `apt install sqlite3` on the host.
  - `README.md`: what it is, local dev with `.env.dev` and `config.dev.yaml`, the CLI dry-run, the commands.
  - `CLAUDE.md`: layout, "no SQL outside `store/repo.py`", test commands, the rule to update `docs/design.md` when the design changes, and the preview quality gate after any prompt change.
  - `docs/design.md`: fold in the SPEC-DEV decisions the owner accepted.
- **Verify:** run `scripts/backup.sh` against a local DB and confirm the backup opens (`sqlite3 backup.db "pragma integrity_check"`). The docs agent follows the README from a fresh clone to a successful `--dry-run` with fixtures.

### Step 22: First deploy and launch (`devops`, with the owner present)
- **OWNER CHECKPOINT H: first deploy.**
  1. The owner puts prod secrets and the prod `config.yaml` on the Droplet and invites the **prod** bot to the community guild with the step 14 permissions.
  2. `devops` follows `docs/deploy.md`.
  3. Run `/newsbot preview` in prod, then `/newsbot status`.
  4. Install the backup cron.
- **Verify:**
  - The container is `healthy` and survives `docker compose restart`.
  - The first scheduled digest posts at the configured local time.
  - The next morning a backup file exists.
  - `/newsbot status` shows all sources green or explained.

---

## 3. Data and schema changes

This is greenfield. There is one migration, `newsbot/store/migrations/001_initial.sql`, applied at startup by `store/db.migrate()`, which tracks it in `PRAGMA user_version`. The schema is as in step 3. It differs from spec section 5 in three ways, all pending owner approval:
- `items.topic_key` is replaced by an `item_topics` join table (SPEC-DEV 1).
- The `digests.status` enum gains `pending` and the table gains `updated_at` (SPEC-DEV 2).
- `stories_fts` is an external-content FTS5 table kept in sync by triggers.

Foreign keys: `story_items`, `item_topics` → CASCADE; `stories.is_update_of` → SET NULL. Every connection sets `PRAGMA foreign_keys=ON`.

Future schema changes are new `NNN_*.sql` files; existing ones are never edited.

---

## 4. Risks, ambiguities and spec deviations

### Spec deviations needing owner sign-off (Checkpoint A)

| # | Issue in spec | Plan default |
|---|---|---|
| 1 | `items.url` is UNIQUE and `items.topic_key` is a single column, but section 4.3 keeps an item "for each topic" it matches. Both can't hold. | Drop `items.topic_key` and add `item_topics(item_id, topic_key, uncertain)`. |
| 2 | Section 4.5 saves items and stories first, then posts. If the post fails, the saved items make a retry find "no new items". A crash mid-post leaves nothing that blocks a double post. | Claim the day with a `pending` row. Post. Then save items, stories and the final status in one transaction. A failed post saves nothing (retry recollects). A stale `pending` row after a crash blocks auto catch-up and alerts the admin, who can `run-now` (force). |
| 3 | Keyword filtering drops official posts that don't name the game (e.g. a Palworld Steam post titled "v0.6.2 Patch Notes", or every r/Palworld post). This directly threatens the success criterion "nothing important from official channels missed". | A source with exactly one topic in `topics` is dedicated: all its items are confident matches for that topic. Multi-topic or unscoped sources still need a keyword match. `RawItem` gains a `topics` field. |
| 4 | Items without `published_at` (Brave, some feeds): the lookback filter is undefined for them. | Keep them. URL dedupe prevents repeats. |
| 5 | "The newest `max_items_per_topic`" lets busy subreddits crowd out official items. | Order by trust (official > press > community), then recency, then cap. |
| 6 | The spec passes 3 days of prior headlines but never says what to do with a repeat story that has no new information. | The prompt marks it `relevant: false`; genuine new developments get `update_of_headline`. |
| 7 | Bluesky `searchPosts` without auth: `public.api.bsky.app` has at times returned 403 for unauthenticated search. The spec lists no Bluesky secret. | Optional `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env`. Without them, try unauthenticated and on 401/403 skip with a coverage note (not a health failure). The owner creates an app password if they want Bluesky. |
| 8 | Matching `update_of_headline` back to a story is underspecified (exact? fuzzy?). | Normalized exact match against the prior headlines sent in the prompt; otherwise ignore, as the spec says. Fuzzy matching risks false links. A cleaner later option is to send prior story ids and have the model return `update_of_id`; I didn't adopt it because it changes the spec's schema. |
| 9 | What happens to the fallback headline list in storage is undefined. | Save items (for dedupe) but create no stories for a fallback topic, so that day's fallback items won't appear in `/news`. The alternative, storing them as `reported` stories, mislabels rumors. |
| 10 | **`/news` with options can't coexist with `/news search`.** Discord doesn't allow a command that has subcommands to be invoked on its own. | `/news recent …` and `/news search …`. An alternative is `/news` plus a separate `/search`. |

Also added beyond the spec: the `official` label is enforced in code (downgraded to `reported`), not only in the prompt, and URLs that aren't http(s) are rejected during canonicalization. Both only tighten what the spec intends.

### Technical risks

- **discord.py and APScheduler event loop.** `AsyncIOScheduler` must start inside `setup_hook`, so it binds to the loop discord.py owns. Creating it at import or in `__main__` before `client.run()` binds the wrong loop or none. Pin APScheduler to 3.x.
  - sqlite calls go through `asyncio.to_thread`, so gateway heartbeats aren't blocked.
  - `on_ready` fires on every reconnect, so the catch-up logic uses a once-flag.
  - One `asyncio.Lock` serializes all pipeline runs.
- **FTS5 in `python:3.14-slim`.** The official image links Debian's libsqlite3, which has FTS5 enabled, and the owner's Homebrew Python 3.14 on macOS also has it. The plan doesn't rely on that: a build-time `RUN` check plus the startup `assert_fts5()` make a missing FTS5 fail loudly.
- **Timezone and DST.** `CronTrigger(..., timezone=ZoneInfo(tz))` handles DST for 09:00. The `tzdata` pip package guarantees zone data in the slim image. `run_date` is the local date. A time between 02:00 and 03:00 would be skipped or doubled on DST days; config validation warns if `time` falls there.
- **Brave free tier.** The limits in the brief (about 1 query per second, a monthly cap) have changed before, and Brave's plan and pricing structure has been revised. I can't confirm the current free allowance from the code. The planned use is 3 topics × 2 queries = 6 a day, about 180 a month, so any free tier should cover it; queries are paced at 1.1 s. At signup, the owner confirms that the **News Search** endpoint is included in the chosen plan; if it isn't, switch to `/res/v1/web/search` with `freshness=pd`. Also check Brave's terms on storing results; the plan stores only the URL, title and a snippet of up to 500 characters.
- **Reddit from a DigitalOcean IP.** Reddit often returns 403 or 429 to datacenter IPs and generic user agents. Mitigations are a descriptive User-Agent and `/new/.rss`. A block shows up in source health after 3 failures. If it persists in prod, the owner decides whether to drop Reddit or accept the gap. There is no workaround inside v1 scope, since the Reddit API is out of scope.
- **Blizzard official news may have no RSS.** If step 12 finds none, Diablo IV official coverage comes from Steam announcements (app 2344520) and the official YouTube feed. HTML scraping would need a new collector type, so it is out of scope unless the owner asks.
- **Embed limits.** Handled in step 10: 4096 per description, 6000 per message, 10 embeds per message, and a header of at most 2000 characters. Long URLs use up the budget quickly, so each story is capped at 3 links.
- **Interaction timeouts.** Slash commands must be acknowledged within 3 s. Every handler that runs the pipeline or queries the DB calls `defer()` first. A preview has to finish inside the 15-minute followup window, which it does with 20 s collector timeouts and at most 3 LLM attempts per topic.
- **Double retries in the Anthropic SDK.** The SDK retries twice by default, which on top of our own 3 attempts would mean 9 calls. `max_retries=0` is set on the client.
- **Structured output.** The plan uses forced tool use with a pydantic-derived schema. It's widely supported on Haiku 4.5, and every response is still validated by pydantic. The spec's top-level array is wrapped in `{"stories": [...]}` because tool input must be an object; this is invisible to the rest of the system.
- **The volume is owned by uid 10001.** The host's `./data` must be `chown 10001`, or the first write fails. This is in the runbook.
- **The backup cron needs `sqlite3` on the host.** The spec places it on the host; the runbook installs it. `.backup` is safe with WAL mode.
- **Alias noise.** The alias "Borderlands" also matches Borderlands 3 and Tiny Tina news as confident. The model's `relevant: false` catches most of it. The owner may narrow the alias at Checkpoint B.

---

## 5. Testing strategy

- **Unit tests** (TDD by `implement`, extended by `test-engineer`) cover steps 2–10 as listed in each step. They need no network: HTTP goes through `httpx.MockTransport` with committed fixtures, the LLM is the `LLMClient` stub, and the store uses `tmp_path` SQLite files (not `:memory:`, so WAL is exercised).
- **Integration test** (step 11): fixture sources, stub LLM, temp DB, `PrintPublisher`. It covers the full guard, save, fallback and preview-writes-nothing matrix. Discord itself is not tested automatically, per spec section 10.
- **Bot-layer tests** (steps 14–16) cover only the pure decision functions: catch-up, DST next-fire, run_date, permission check, pager owner check and spend. No gateway mocking; it's brittle and low value.
- **Manual testing:** the step 17 checklist in the test guild, plus the step 18 container healthcheck.
- **Regression gate:** CI runs ruff and pytest on every push. Any prompt change needs an owner-reviewed `/newsbot preview` (spec section 10); this goes in CLAUDE.md.
- **Coverage focus for `test-engineer`:** canonicalization edge cases, embed-limit boundaries, FTS query escaping, the guard state machine, and retry and fallback accounting.

---

## 6. Out of scope

- Everything spec section 1 lists as out of scope: X, the Reddit API, subscriptions and DMs, a dashboard, multiple guilds, and LLM-judge evaluation.
- An HTML-scraping collector (see the Blizzard risk).
- Fuzzy or ID-based update linking (SPEC-DEV 8 alternative).
- Merging http and https URLs during dedupe.
- Automated deploy from CI to the Droplet (the spec has the owner run pull and up).
- Metrics and alerting beyond admin-channel messages.
- Off-host backup copies. The 7 daily backups sit on the same Droplet; DigitalOcean's own backups or a remote sync is a sensible follow-up, not requested.

---

## Owner checkpoints

| ID | When | Owner action |
|---|---|---|
| A | Before or during M1 | Decide SPEC-DEV 1–10; confirm the digest time and timezone |
| B | Step 12 | Approve the seed source list (**blocks launch**) |
| C | Step 13 | Create the Anthropic and Brave keys (and optionally a Bluesky app password) |
| D | Steps 13 and 17 | Review preview quality (first pass, then final approval) |
| E | Step 14 | Create the prod and dev Discord apps and tokens, the private test guild, and invite the dev bot |
| F | Step 19 | Create the GitHub repo; decide whether the GHCR package is public or private |
| G | Step 20 | Triage accepted-risk QA findings |
| H | Step 22 | Put prod secrets and config on the Droplet, invite the prod bot, be present for the first deploy |

## Milestones

| Milestone | Steps | Exit criterion | Rough effort |
|---|---|---|---|
| **M1: headless pipeline** | 0–13 | A live `python -m newsbot.pipeline.run --dry-run` prints a sensible digest from real sources; the full test suite is green | About 2–3 agent-days. The largest pieces are collectors, summarize and format. Source research (step 12) runs in parallel. Owner: about 1–2 h (keys, source review, first quality read). |
| **M2: bot in the test guild** | 14–17 | The step 17 checklist passes; the owner approves preview quality | About 1.5–2 agent-days. Owner: about 1 h (Discord apps and guild, acceptance walkthrough). |
| **M3: deployed** | 18–22 | A healthy container on the Droplet from GHCR; the first scheduled digest posts; backups rotate | About 1 agent-day (devops, docs, QA plus fixes). Owner: about 1–2 h (repo, GHCR, Droplet secrets, first-deploy watch). |

Files referenced: `/Users/someclown/dev/discord-newsbot/docs/design.md` (the spec). All other paths in the plan are to be created under `/Users/someclown/dev/discord-newsbot/`.
agentId: ad1db864706650184 (use SendMessage with to: 'ad1db864706650184', summary: '<5-10 word recap>' to continue this agent)
<usage>subagent_tokens: 50397
tool_uses: 2
duration_ms: 329051</usage>
