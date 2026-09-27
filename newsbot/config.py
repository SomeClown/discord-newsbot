"""Load and validate config.yaml and the environment.

Everything the bot needs to run correctly lives in two places: `config.yaml`
(topics, sources, timing: the stuff an owner tweaks without touching code)
and the environment (tokens: the stuff that should never end up in a
committed file or a log line). This module is the only place that reads
either one. If the config is wrong, we'd rather the process refuse to start
with a message that says exactly what's wrong than limp along half-configured
and drop stories from a topic nobody noticed was misspelled (ask me how I
know).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import yaml
from pydantic import BaseModel, Field, HttpUrl, SecretStr, ValidationError, field_validator

Trust = Literal["official", "press", "community"]

_TOPIC_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Discord allows 25 choices per slash-command option, and /news recent spends
# one of them on "All". Hence 24, not the round number I first wrote.
_MAX_TOPICS = 24


class ConfigError(Exception):
    """Raised when config.yaml or the environment fails validation.

    The message lists every failure found, not just the first one; nobody
    wants to fix a typo, restart, and discover the *next* typo one at a time.
    """


def _reject_bool_channel_id(v: object) -> object:
    """A `mode="before"` guard shared by every `channel_id` field.

    Pydantic's lax int coercion happily turns a quoted numeric string or a
    whole-number float into an int -- both genuinely useful for a
    hand-edited YAML file -- but `bool` is *also* an `int` subclass in
    Python, so `channel_id: true` was silently loading as `1` instead of
    failing config validation, which is a considerably more confusing way
    to find out than a startup error (test-engineer caught it before a
    typo like that ever got the chance to). `strict=True` would close this
    the blunt way, but it also closes the string/float coercions this
    module's own tests pin as intentional -- rejecting bool specifically,
    ahead of pydantic's normal int coercion, is the one check that catches
    the typo without taking those away.
    """
    if isinstance(v, bool):
        raise ValueError("channel_id must be an int, not a bool")
    return v


class Topic(BaseModel):
    key: str
    name: str
    # v2.0 (design.md §13): each game posts its own digest to its own
    # channel now, so there's no longer a shared fallback to inherit this
    # from -- every topic has to name one.
    channel_id: int = Field(gt=0)
    aliases: list[str] = []
    entities: list[str] = []
    # Per-topic overrides for the web_search collector. Empty means "use the
    # source's global query_templates"; the two schemas coexist because most
    # topics are happy with "{name} news" and one weird topic never is.
    search_queries: list[str] = []

    @field_validator("channel_id", mode="before")
    @classmethod
    def _validate_channel_id_not_bool(cls, v: object) -> object:
        return _reject_bool_channel_id(v)

    @field_validator("search_queries")
    @classmethod
    def _validate_search_queries(cls, v: list[str]) -> list[str]:
        if any(not q.strip() for q in v):
            raise ValueError("search_queries entries must be non-empty strings")
        return v


class DigestCfg(BaseModel):
    # v2.0 (design.md §13): the combined digest channel is going away in
    # favor of a channel per topic, but the field stays optional -- not
    # gone -- until step 4 actually removes it; nothing reads it as
    # optional in the meantime, since the topics that still need a
    # channel to post to now get one from Topic.channel_id instead.
    channel_id: int | None = None
    time: str
    timezone: str
    lookback_hours: int = 24
    max_items_per_topic: int = 60
    # design.md §6: a plain-text status message to the admin channel after
    # every POST run that actually posts (ok or partial) -- so "the digest
    # went out fine" doesn't require anyone to go look. Only takes effect
    # when `admin_channel_id` is set; true by default because a run report
    # is meant to be the normal, boring case, not something an owner has to
    # discover and opt into.
    report_to_admin: bool = True

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"digest.time {v!r} is not HH:MM (24-hour)")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"digest.timezone {v!r} is not a known IANA zone") from exc
        return v


class RssSource(BaseModel):
    type: Literal["rss"]
    name: str
    url: HttpUrl
    topics: list[str] | None = None
    trust: Trust


class SteamSource(BaseModel):
    type: Literal["steam_news"]
    name: str
    app_id: int
    topics: list[str] | None = None
    trust: Trust


class BlueskySource(BaseModel):
    type: Literal["bluesky_search"]
    name: str | None = None
    query: str
    topics: list[str] | None = None
    trust: Trust


class WebSearchSource(BaseModel):
    type: Literal["web_search"]
    name: str = "Brave Search"
    queries_per_topic: int = 2
    query_templates: list[str] = ["{name} news", "{name} update OR patch OR leak"]
    trust: Trust


Source = Annotated[
    RssSource | SteamSource | BlueskySource | WebSearchSource, Field(discriminator="type")
]


class AlertsCfg(BaseModel, extra="forbid"):
    """Settings for the SHiFT code alert sweep (design.md §12).

    Absent entirely, `enabled` defaults to False -- an owner who never
    touches this block never gets an unannounced `@everyone` pinger
    bolted onto their digest bot. `config.example.yaml` ships it
    commented with `true`, so turning it on is a deliberate uncomment,
    not a surprise default.
    """

    enabled: bool = False
    # v2.0 (design.md §13): SHiFT alerts move off the shared digest channel
    # onto their own -- required once `enabled` is true (checked in
    # load_config, where the friendly message lives), optional otherwise
    # so a disabled block doesn't need a channel it'll never post to.
    channel_id: int | None = Field(None, gt=0)
    interval_minutes: int = Field(60, ge=15, le=1440)
    max_item_age_hours: int = Field(48, ge=1, le=720)
    # 0 disables pinging entirely without disabling the sweep -- codes
    # still get recorded and posted, just never with @everyone attached.
    max_pings_per_day: int = Field(3, ge=0)
    allow_test_command: bool = False
    # A6 (owner decision, 2026-09-25): scope the sweep to specific topics --
    # a Diablo IV patch note has never once contained a Borderlands SHiFT
    # code, and pinging the whole server for every game's codes when the
    # owner only cares about one is a worse default than the sweep quietly
    # doing nothing most topics ever need. Empty means "every topic",
    # checked against `cfg.topics` keys at load time (see load_config).
    topics: list[str] = []
    # QA item 7 option A (owner decision, 2026-09-25): trust-gated pings.
    # A community-only code (a Reddit thread guessing a code, say) still
    # posts -- codes aren't gatekept by trust, only the @everyone ping is.
    # A batch pings only if at least one code queued to post came from a
    # source whose trust is in this list.
    ping_trust: list[Trust] = ["official", "press"]
    # QA item 7 (owner decision, 2026-09-25): an item naming more than
    # this many distinct codes is a roundup/megathread, not a genuine
    # single-code announcement -- its sightings don't count toward
    # alerting (shift/decide.py's aggregate/sightings_from_items).
    max_codes_per_item: int = Field(5, ge=1)

    @field_validator("channel_id", mode="before")
    @classmethod
    def _validate_channel_id_not_bool(cls, v: object) -> object:
        return _reject_bool_channel_id(v)


class AppConfig(BaseModel):
    guild_id: int
    admin_channel_id: int | None = None
    admin_permission: str = "manage_guild"
    digest: DigestCfg
    topics: list[Topic]
    sources: list[Source]
    alerts: AlertsCfg = AlertsCfg()


class Secrets(BaseModel):
    discord_token: SecretStr | None
    anthropic_api_key: SecretStr
    brave_api_key: SecretStr | None
    bluesky_handle: str | None
    bluesky_app_password: SecretStr | None


def _bluesky_default_name(query: str) -> str:
    return f"Bluesky: {query}"


def _format_pydantic_error(error: dict, raw: dict) -> str:
    """Turn one pydantic error dict into a line for `ConfigError`.

    Almost every error just gets pydantic's own `msg` prefixed with its
    dotted location, same as before v2.0. The one exception: a missing
    `topics[i].channel_id` gets a message naming the topic by key instead
    of just its index, since "topics.2.channel_id: Field required" makes
    an owner go count list entries by hand before they even know which
    game they forgot.
    """
    loc = error["loc"]
    if (
        len(loc) == 3
        and loc[0] == "topics"
        and loc[2] == "channel_id"
        and error["type"] == "missing"
    ):
        idx = loc[1]
        raw_topics = raw.get("topics") or []
        key = (
            raw_topics[idx].get("key", "?")
            if isinstance(idx, int) and idx < len(raw_topics)
            else "?"
        )
        return (
            f"topics[{idx}] ({key}): channel_id is required -- each game "
            "posts to its own channel as of v2.0"
        )
    return f"{'.'.join(str(p) for p in loc)}: {error['msg']}"


def load_config(path: str | Path) -> AppConfig:
    """Load, validate, and cross-check `config.yaml`.

    Raises `ConfigError` with every problem found, rather than pydantic's
    default one-shot exception, so a first run doesn't turn into a game of
    whack-a-mole against a stack trace.
    """
    raw = yaml.safe_load(Path(path).read_text()) or {}

    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [_format_pydantic_error(e, raw) for e in exc.errors()]
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors)) from exc

    errors: list[str] = []

    # admin_permission must name a real discord.Permissions flag, checked
    # against VALID_FLAGS (the actual name -> bit mapping), not hasattr()
    # against the class -- discord.Permissions also has real attributes
    # like `value` (a property) and `all`/`none` (classmethods) that
    # hasattr() would happily call a "flag" too, which is how "value"
    # used to sail through config validation as an admin_permission that
    # then had no bit to check anyone's permissions against.
    if cfg.admin_permission not in discord.Permissions.VALID_FLAGS:
        errors.append(
            f"admin_permission {cfg.admin_permission!r} is not a discord.Permissions flag"
        )

    # Topic keys: unique, within Discord's choice limit, and shaped like an
    # identifier so they're safe to use as SQLite values and slash-command
    # choice values alike.
    if len(cfg.topics) > _MAX_TOPICS:
        errors.append(
            f"{len(cfg.topics)} topics exceeds the limit of {_MAX_TOPICS} "
            '(Discord allows 25 choices; one is "All")'
        )
    seen_keys: set[str] = set()
    for topic in cfg.topics:
        if not _TOPIC_KEY_RE.match(topic.key):
            errors.append(f"topic key {topic.key!r} must match ^[a-z0-9_]+$")
        if topic.key in seen_keys:
            errors.append(f"duplicate topic key {topic.key!r}")
        seen_keys.add(topic.key)

    known_keys = seen_keys

    for topic_key in cfg.alerts.topics:
        if topic_key not in known_keys:
            errors.append(f"alerts.topics references unknown topic {topic_key!r}")

    if cfg.alerts.enabled and cfg.alerts.channel_id is None:
        errors.append(
            "alerts.channel_id is required when alerts.enabled is true "
            "(v2.0: SHiFT alerts post to their own channel)"
        )

    if cfg.alerts.allow_test_command and not cfg.alerts.enabled:
        # A config that turns on the test command but not the feature it
        # tests is almost certainly a copy-paste mistake, not intent -- it
        # used to surface as a bare RuntimeError the first time someone
        # ran /newsbot test-alert, long after config load had already
        # said everything looked fine.
        errors.append(
            "alerts.allow_test_command is true but alerts.enabled is false -- "
            "/newsbot test-alert has nothing to test with alerts disabled"
        )

    # Fill in Bluesky default names before the uniqueness check, since
    # source_health.source_name is the primary key: two sources silently
    # sharing a name would silently share health tracking too.
    resolved_sources = []
    for source in cfg.sources:
        if isinstance(source, BlueskySource) and source.name is None:
            source = source.model_copy(update={"name": _bluesky_default_name(source.query)})
        resolved_sources.append(source)
    cfg = cfg.model_copy(update={"sources": resolved_sources})

    seen_names: set[str] = set()
    filtered_sources = []
    for source in cfg.sources:
        for topic_key in getattr(source, "topics", None) or []:
            if topic_key not in known_keys:
                errors.append(f"source {source.name!r} references unknown topic {topic_key!r}")
        if source.name in seen_names:
            errors.append(f"duplicate source name {source.name!r}")
        seen_names.add(source.name)

        if isinstance(source, WebSearchSource) and not os.environ.get("BRAVE_API_KEY"):
            # This isn't a hard failure: web search is one of four collector
            # types, and refusing to start over a missing optional key would
            # be a worse outcome than just running without it.
            logging.getLogger(__name__).warning(
                "web_search source configured but BRAVE_API_KEY is not set; disabling it"
            )
            continue
        filtered_sources.append(source)
    cfg = cfg.model_copy(update={"sources": filtered_sources})

    if errors:
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors))

    if cfg.alerts.allow_test_command:
        # /newsbot test-alert lets anyone with admin_permission post a fake
        # SHiFT code alert on demand -- exactly what the private test guild
        # needs and exactly what a production config should never carry,
        # so a startup log line is the one place this gets said out loud.
        logging.getLogger(__name__).warning(
            "alerts.allow_test_command is true; /newsbot test-alert will be registered"
        )

    return cfg


def configured_source_names(cfg: AppConfig) -> set[str]:
    """The `source_name` every currently-configured source records health under.

    This has to match `build_collectors` exactly -- every `Collector` sets
    `self.name = source.name`, and `cfg.sources` already has Bluesky's
    default name filled in by the time `load_config` returns it (see
    above), so this is just "read `.name` off what's configured" with one
    place to fix if a fifth source type ever shows up and someone forgets.
    Used to filter `/newsbot status` down to sources that still exist,
    instead of every source that ever recorded health (see the IGN
    incident in CLAUDE.md).
    """
    return {source.name for source in cfg.sources}


def load_secrets(env: Mapping[str, str] = os.environ, *, require_discord: bool = True) -> Secrets:
    """Load secrets from the environment (never from config.yaml).

    `require_discord=False` lets the CLI pipeline runner (which never talks
    to Discord) skip demanding a token it will never use.
    """
    discord_token = env.get("DISCORD_TOKEN")
    anthropic_key = env.get("ANTHROPIC_API_KEY")

    errors: list[str] = []
    if require_discord and not discord_token:
        errors.append("DISCORD_TOKEN is not set")
    if not anthropic_key:
        errors.append("ANTHROPIC_API_KEY is not set")
    if errors:
        raise ConfigError("Invalid secrets:\n" + "\n".join(f"  - {e}" for e in errors))

    return Secrets(
        discord_token=discord_token,
        anthropic_api_key=anthropic_key,
        brave_api_key=env.get("BRAVE_API_KEY") or None,
        # People copy their handle from the profile page, "@" and all. Bluesky's
        # login wants it bare, and says so with an unhelpful 400.
        bluesky_handle=(env.get("BLUESKY_HANDLE") or "").strip().lstrip("@") or None,
        bluesky_app_password=env.get("BLUESKY_APP_PASSWORD") or None,
    )
