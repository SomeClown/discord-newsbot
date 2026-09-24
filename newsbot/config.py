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
_MAX_TOPICS = 25  # Discord's slash-command choice limit.


class ConfigError(Exception):
    """Raised when config.yaml or the environment fails validation.

    The message lists every failure found, not just the first one; nobody
    wants to fix a typo, restart, and discover the *next* typo one at a time.
    """


class Topic(BaseModel):
    key: str
    name: str
    aliases: list[str] = []
    entities: list[str] = []
    # Per-topic overrides for the web_search collector. Empty means "use the
    # source's global query_templates"; the two schemas coexist because most
    # topics are happy with "{name} news" and one weird topic never is.
    search_queries: list[str] = []

    @field_validator("search_queries")
    @classmethod
    def _validate_search_queries(cls, v: list[str]) -> list[str]:
        if any(not q.strip() for q in v):
            raise ValueError("search_queries entries must be non-empty strings")
        return v


class DigestCfg(BaseModel):
    channel_id: int
    time: str
    timezone: str
    lookback_hours: int = 24
    max_items_per_topic: int = 60

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


class AppConfig(BaseModel):
    guild_id: int
    admin_channel_id: int | None = None
    admin_permission: str = "manage_guild"
    digest: DigestCfg
    topics: list[Topic]
    sources: list[Source]


class Secrets(BaseModel):
    discord_token: SecretStr | None
    anthropic_api_key: SecretStr
    brave_api_key: SecretStr | None
    bluesky_handle: str | None
    bluesky_app_password: SecretStr | None


def _bluesky_default_name(query: str) -> str:
    return f"Bluesky: {query}"


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
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors)) from exc

    errors: list[str] = []

    # admin_permission must name a real discord.Permissions flag, checked
    # against the class itself (importing discord for this is free; we
    # already depend on it for everything else).
    if not hasattr(discord.Permissions, cfg.admin_permission):
        errors.append(
            f"admin_permission {cfg.admin_permission!r} is not a discord.Permissions flag"
        )

    # Topic keys: unique, within Discord's choice limit, and shaped like an
    # identifier so they're safe to use as SQLite values and slash-command
    # choice values alike.
    if len(cfg.topics) > _MAX_TOPICS:
        errors.append(f"{len(cfg.topics)} topics exceeds the Discord choice limit of {_MAX_TOPICS}")
    seen_keys: set[str] = set()
    for topic in cfg.topics:
        if not _TOPIC_KEY_RE.match(topic.key):
            errors.append(f"topic key {topic.key!r} must match ^[a-z0-9_]+$")
        if topic.key in seen_keys:
            errors.append(f"duplicate topic key {topic.key!r}")
        seen_keys.add(topic.key)

    known_keys = seen_keys

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

    return cfg


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
