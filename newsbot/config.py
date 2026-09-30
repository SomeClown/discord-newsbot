"""Load and validate config.yaml and the environment.

Everything the bot needs to run correctly lives in two places: `config.yaml`
(topics, sources, timing: the stuff an owner tweaks without touching code)
and the environment (tokens: the stuff that should never end up in a
committed file or a log line). This module is the only place that reads
either one. If the config is wrong, we'd rather the process refuse to start
with a message that says exactly what's wrong than limp along half-configured
and drop stories from a topic nobody noticed was misspelled (ask me how I
know).

Two shapes of `config.yaml` load here (design.md §15). The v3 shape is a
global `catalog:` of games; the v2 shape is per-server `topics:` and
`sources:`. If there's no `catalog:`, the catalog is *derived* from the v2
keys, so the config already running in production keeps loading and the bot
behaves exactly as it did. If there are both, the catalog wins and the old
keys are only read by the one-time import (`LegacySetup`), like a moving box
nobody has unpacked yet.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import yaml
from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.lounge.welcome import ALLOWED_PLACEHOLDERS, unknown_placeholders, worst_case_length
from newsbot.text import shown_url

Trust = Literal["official", "press", "community"]

# `\Z`, not `$`: in Python, `$` also matches just before a trailing newline,
# so "08:00\n" (one YAML block scalar away) used to pass as a time. The
# test-engineer found that one; I'd written `$` out of pure habit.
_TOPIC_KEY_RE = re.compile(r"^[a-z0-9_]+\Z")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\Z")
# Discord allows 25 choices per slash-command option, and /news recent spends
# one of them on "All". Hence 24, not the round number I first wrote.
_MAX_TOPICS = 24
# v3 catalog cap: one Discord select menu holds 25 options, and there's no "All" in it.
_MAX_CATALOG_GAMES = 25


class ConfigError(Exception):
    """Raised when config.yaml or the environment fails validation.

    The message lists every failure found, not just the first one; nobody
    wants to fix a typo, restart, and discover the *next* typo one at a time.
    """


def _reject_bool_channel_id(v: object) -> object:
    """A `mode="before"` guard shared by every `channel_id` field.

    Pydantic's lax int coercion happily turns a quoted numeric string or a
    whole-number float into an int (both genuinely useful for a
    hand-edited YAML file), but `bool` is *also* an `int` subclass in
    Python, so `channel_id: true` was silently loading as `1` instead of
    failing config validation, which is a considerably more confusing way
    to find out than a startup error (test-engineer caught it before a
    typo like that ever got the chance to). `strict=True` would close this
    the blunt way, but it also closes the string/float coercions this
    module's own tests pin as intentional: rejecting bool specifically,
    ahead of pydantic's normal int coercion, is the one check that catches
    the typo without taking those away.
    """
    if isinstance(v, bool):
        raise ValueError("channel_id must be an int, not a bool")
    return v


def _reject_bool_guild_id(v: object) -> object:
    """The guild-id twin of `_reject_bool_channel_id`, with a message that says "guild".

    (I reused the channel one at first, so a bad `home_guild_id` complained
    about `channel_id`. Nobody deserves that at startup.)
    """
    if isinstance(v, bool):
        raise ValueError("must be a number, not true or false")
    return v


def _reject_blank_name(v: str) -> str:
    if not v.strip():
        raise ValueError("can't be blank")
    return v


def _check_search_queries(v: list[str]) -> list[str]:
    if any(not q.strip() for q in v):
        raise ValueError("search_queries entries must be non-empty strings")
    return v


class GameInfo(Protocol):
    """What the filter, the prompts and web search need to know about a game.

    v2's `Topic` and v3's `GameCfg` both satisfy this, which is the whole
    reason it exists: the pipeline doesn't care which config shape a game
    came from, and I'd rather not teach it to. Read-only properties, so a
    frozen or plain model both fit.
    """

    @property
    def key(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def aliases(self) -> list[str]: ...
    @property
    def entities(self) -> list[str]: ...
    @property
    def search_queries(self) -> list[str]: ...
    @property
    def match_name(self) -> bool: ...


class Topic(BaseModel):
    key: str
    name: str
    # v2.0 (design.md §13): each game posts its own digest to its own
    # channel now, so there's no longer a shared fallback to inherit this
    # from; every topic has to name one.
    channel_id: int = Field(gt=0)
    aliases: list[str] = []
    entities: list[str] = []
    # Per-topic overrides for the web_search collector. Empty means "use the
    # source's global query_templates"; the two schemas coexist because most
    # topics are happy with "{name} news" and one weird topic never is.
    search_queries: list[str] = []

    @property
    def match_name(self) -> bool:
        """Always true for a v2 topic; only catalog games can opt out (see `GameInfo`)."""
        return True

    @field_validator("channel_id", mode="before")
    @classmethod
    def _validate_channel_id_not_bool(cls, v: object) -> object:
        return _reject_bool_channel_id(v)

    @field_validator("name")
    @classmethod
    def _validate_name_not_blank(cls, v: str) -> str:
        return _reject_blank_name(v)

    @field_validator("aliases", "entities")
    @classmethod
    def _drop_blank_terms(cls, v: list[str], info: ValidationInfo) -> list[str]:
        """Drop blank aliases and entities, with a warning, instead of refusing to load.

        v2.2 never checked these, so a config with `aliases: [""]` loaded and
        ran; refusing it now would turn an upgrade into an outage over a
        stray dash in a YAML list. A blank term compiles to a regex that
        matches between any two punctuation marks, so it can't stay, and
        dropping it is the closest thing to what the owner meant.
        """
        kept = [t for t in v if t.strip()]
        if len(kept) != len(v):
            logging.getLogger(__name__).warning(
                "topic %r: dropping %d blank %s entries (a blank term matches nearly everything)",
                info.data.get("key", "?"),
                len(v) - len(kept),
                info.field_name,
            )
        return kept

    @field_validator("search_queries")
    @classmethod
    def _validate_search_queries(cls, v: list[str]) -> list[str]:
        return _check_search_queries(v)


class DigestCfg(BaseModel):
    # v2.0 (design.md §13): the combined digest channel is gone; each
    # topic posts to its own Topic.channel_id instead. A leftover
    # digest.channel_id in an old v1 config.yaml is caught by
    # load_config's raw-YAML pre-check, before pydantic ever gets a
    # chance to just silently ignore the unrecognized field.
    time: str
    timezone: str
    lookback_hours: int = 24
    max_items_per_topic: int = 60
    # design.md §6: a plain-text status message to the admin channel after
    # every POST run that actually posts (ok or partial), so "the digest
    # went out fine" doesn't require anyone to go look. Only takes effect
    # when `admin_channel_id` is set; true by default because a run report
    # is meant to be the normal, boring case, not something an owner has to
    # discover and opt into.
    report_to_admin: bool = True
    # Self-host plan task 2: what SYSTEM_PROMPT calls the things `topics`
    # are ("the video games {games}"). Defaults to "video games" so an
    # owner who never touches this keeps the exact prompt this bot has
    # always shipped with, byte for byte; a fork tracking, say, tabletop
    # RPG news instead can say so without editing prompts.py. Any change
    # here still needs an owner-reviewed `/newsbot preview` before merge
    # (CLAUDE.md), same as any other prompt edit.
    subject: str = "video games"

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


class SharedRssSource(RssSource, extra="forbid"):
    """An RSS source that belongs to no one game (design.md §15, decision D1)."""

    games: list[str] | None = None


class SharedSteamSource(SteamSource, extra="forbid"):
    games: list[str] | None = None


class SharedBlueskySource(BlueskySource, extra="forbid"):
    games: list[str] | None = None


# `web_search` is in this union only so a stray one parses far enough for
# load_config to say where it belongs, instead of pydantic's union-tag error.
SharedSourceCfg = Annotated[
    SharedRssSource | SharedSteamSource | SharedBlueskySource | WebSearchSource,
    Field(discriminator="type"),
]

_SHARED_BY_TYPE: dict[str, type[RssSource | SteamSource | BlueskySource]] = {
    "rss": SharedRssSource,
    "steam_news": SharedSteamSource,
    "bluesky_search": SharedBlueskySource,
}


class GameCfg(BaseModel, extra="forbid"):
    """One catalog game (v3): a `Topic` without a channel, plus its own sources.

    Channels are per server now, so they live in the database and this has
    none. `match_name: false` is for names that are also ordinary words
    (Rust, Destiny, Apex): the aliases do the matching instead.
    """

    key: str
    name: str
    aliases: list[str] = []
    entities: list[str] = []
    search_queries: list[str] = []
    match_name: bool = True
    # A source listed here belongs to this game, so it never says `topics`
    # (load_config rejects one that does).
    sources: list[Source] = []

    @model_validator(mode="before")
    @classmethod
    def _let_the_channel_id_precheck_speak(cls, data: object) -> object:
        # `channel_id` is a mistake with its own, better message (load_config's
        # raw pre-check: "use /newsbot follow"). Left to extra="forbid" it would
        # be reported twice, and would stop every cross-check from running.
        if isinstance(data, dict) and "channel_id" in data:
            return {k: v for k, v in data.items() if k != "channel_id"}
        return data

    @field_validator("name")
    @classmethod
    def _validate_name_not_blank(cls, v: str) -> str:
        return _reject_blank_name(v)

    @field_validator("aliases", "entities")
    @classmethod
    def _validate_terms_not_blank(cls, v: list[str], info: ValidationInfo) -> list[str]:
        # Unlike a v2 topic, nothing old depends on a blank term here, so it's
        # an error rather than a shrug. It would match nearly every headline.
        if any(not t.strip() for t in v):
            raise ValueError(f"{info.field_name} can't have blank entries")
        return v

    @field_validator("search_queries")
    @classmethod
    def _validate_search_queries(cls, v: list[str]) -> list[str]:
        return _check_search_queries(v)


class WebSearchCfg(BaseModel, extra="forbid"):
    """Brave News search, once a day, for comped servers' games (design.md §15).

    Brave has always been one global source, so it gets one global block
    instead of a slot in every game's source list.
    """

    name: str = "Brave Search"
    queries_per_game: int = Field(2, ge=1)
    query_templates: list[str] = ["{name} news", "{name} update OR patch OR leak"]
    trust: Trust = "press"


class ShiftCfg(BaseModel, extra="forbid"):
    """Global SHiFT detection. Per-server channel and ping live in the database."""

    # Empty means every game, same as v2's alerts.topics.
    games: list[str] = []
    max_item_age_hours: int = Field(48, ge=1, le=720)
    # Per server, per that server's local day; 0 means codes post but never ping.
    max_pings_per_day: int = Field(3, ge=0)
    ping_trust: list[Trust] = ["official", "press"]
    max_codes_per_item: int = Field(5, ge=1)
    allow_test_command: bool = False


class CollectionCfg(BaseModel, extra="forbid"):
    interval_minutes: int = Field(60, ge=15, le=1440)
    lookback_hours: int = Field(24, ge=1)
    max_items_per_game: int = Field(60, ge=1)


class AiCfg(BaseModel, extra="forbid"):
    # What SYSTEM_PROMPT calls the games ("the video games ..."); it was
    # digest.subject in v2 and the prompt stays byte-identical by default.
    subject: str = "video games"


class OwnerReportCfg(BaseModel, extra="forbid"):
    """When the owner's daily all-servers summary goes out."""

    time: str = "21:00"
    timezone: str = "America/Los_Angeles"

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"owner_report.time {v!r} is not HH:MM (24-hour)")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"owner_report.timezone {v!r} is not a known IANA zone") from exc
        return v


class AlertsCfg(BaseModel, extra="forbid"):
    """Settings for the SHiFT code alert sweep (design.md §12).

    Absent entirely, `enabled` defaults to False; an owner who never
    touches this block never gets an unannounced `@everyone` pinger
    bolted onto their digest bot. `config.example.yaml` ships it
    commented with `true`, so turning it on is a deliberate uncomment,
    not a surprise default.
    """

    enabled: bool = False
    # v2.0 (design.md §13): SHiFT alerts move off the shared digest channel
    # onto their own: required once `enabled` is true (checked in
    # load_config, where the friendly message lives), optional otherwise
    # so a disabled block doesn't need a channel it'll never post to.
    channel_id: int | None = Field(None, gt=0)
    interval_minutes: int = Field(60, ge=15, le=1440)
    max_item_age_hours: int = Field(48, ge=1, le=720)
    # 0 disables pinging entirely without disabling the sweep; codes
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
    # posts; codes aren't gatekept by trust, only the @everyone ping is.
    # A batch pings only if at least one code queued to post came from a
    # source whose trust is in this list.
    ping_trust: list[Trust] = ["official", "press"]
    # QA item 7 (owner decision, 2026-09-25): an item naming more than
    # this many distinct codes is a roundup/megathread, not a genuine
    # single-code announcement; its sightings don't count toward
    # alerting (shift/decide.py's aggregate/sightings_from_items).
    max_codes_per_item: int = Field(5, ge=1)

    @field_validator("channel_id", mode="before")
    @classmethod
    def _validate_channel_id_not_bool(cls, v: object) -> object:
        return _reject_bool_channel_id(v)


class WelcomeCfg(BaseModel, extra="forbid"):
    """The lounge welcome (design.md §14). Off unless someone turns it on."""

    enabled: bool = False
    # Required once `enabled` is true, and checked (placeholders, length) in
    # load_config even while disabled, so a typo can't sit quietly until the
    # day someone flips the switch.
    message: str = ""


_SOURCE_KINDS = ("wikiquote", "file", "url")


class QuoteSourceCfg(BaseModel, frozen=True):
    """One entry in `lounge.daily_quote.sources`: a kind and a value.

    `key` is the source's identity in the database and the cache: it depends
    only on the source itself, never on its position in the list, so
    reordering or editing the neighbors leaves everyone's no-repeat deck
    alone. A `file` value is already an absolute path by the time
    `load_config` hands this back.
    """

    kind: Literal["wikiquote", "file", "url"]
    value: str

    @property
    def key(self) -> str:
        if self.kind == "wikiquote":
            # MediaWiki treats underscores as spaces and the first letter as
            # case-insensitive, so "oscar_wilde" and "Oscar Wilde" are one page.
            title = " ".join(self.value.replace("_", " ").split())
            return "wikiquote:" + title[:1].upper() + title[1:]
        return f"{self.kind}:{self.value.strip()}"


class DailyQuoteCfg(BaseModel, extra="forbid"):
    """The daily quote (design.md §14). Off unless someone turns it on."""

    enabled: bool = False
    time: str = "08:00"
    # None means "use the built-in list"; load_config swaps in the real list,
    # so nothing downstream ever sees None. An empty list is an error there.
    sources: list[QuoteSourceCfg] | None = None

    @field_validator("time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"lounge.daily_quote.time {v!r} is not HH:MM (24-hour)")
        return v

    @field_validator("sources", mode="before")
    @classmethod
    def _parse_sources(cls, v: object) -> object:
        """Turn each one-key mapping (`wikiquote: Oscar Wilde`) into a `QuoteSourceCfg`."""
        if not isinstance(v, list):
            return v
        problems: list[str] = []
        parsed: list[object] = []
        for i, entry in enumerate(v):
            where = f"lounge.daily_quote.sources[{i}]"
            if isinstance(entry, QuoteSourceCfg):
                parsed.append(entry)
                continue
            if not isinstance(entry, dict) or len(entry) != 1:
                message = f"{where} must have exactly one of wikiquote, file or url"
                if isinstance(entry, dict) and entry:
                    message += f" (found: {', '.join(str(k) for k in entry)})"
                problems.append(message)
                continue
            ((kind, value),) = entry.items()
            if kind not in _SOURCE_KINDS:
                problems.append(f"{where} has an unknown key {kind!r} (use wikiquote, file or url)")
            elif not isinstance(value, str) or not value.strip():
                problems.append(f"{where}.{kind} must be a non-empty string")
            else:
                parsed.append(QuoteSourceCfg(kind=kind, value=value.strip()))
        if problems:
            raise ValueError("; ".join(problems))
        return parsed


class LoungeCfg(BaseModel, extra="forbid"):
    """The optional `lounge:` block: welcomes and a daily quote, one channel."""

    # Required once either feature is enabled (checked in load_config).
    channel_id: int | None = Field(None, gt=0)
    welcome: WelcomeCfg = WelcomeCfg()
    daily_quote: DailyQuoteCfg = DailyQuoteCfg()

    @field_validator("channel_id", mode="before")
    @classmethod
    def _validate_channel_id_not_bool(cls, v: object) -> object:
        return _reject_bool_channel_id(v)


class LegacySetup(BaseModel):
    """The one-time import's view of the v2 keys: one server's whole setup.

    Built whenever `guild_id` is present, whichever shape the rest of the
    file is in. After the import has run, none of this is read again.
    """

    guild_id: int
    admin_channel_id: int | None
    digest_time: str
    timezone: str
    games: list[tuple[str, int]]
    shift_enabled: bool
    shift_channel_id: int | None
    shift_ping: Literal["everyone", "none"]
    lounge: LoungeCfg | None
    alerts_max_pings: int


class AppConfig(BaseModel):
    # Global settings (v3).
    home_guild_id: int | None = Field(None, gt=0)
    admin_channel_id: int | None = None
    admin_permission: str = "manage_guild"
    command_guild_ids: list[int] = []
    comped_guild_ids: list[int] = []
    owner_report: OwnerReportCfg = OwnerReportCfg()
    collection: CollectionCfg = CollectionCfg()
    ai: AiCfg = AiCfg()
    run_report: bool = True
    web_search: WebSearchCfg | None = None
    shift: ShiftCfg = ShiftCfg()
    shared_sources: list[SharedSourceCfg] = []
    catalog: list[GameCfg] = []
    legacy: LegacySetup | None = None
    # The v2 fields. Optional now, because a v3 config has none of them; they
    # stay populated for old-shape configs so the running v2 path keeps
    # working until the cutover deletes them.
    guild_id: int | None = Field(None, gt=0)
    digest: DigestCfg | None = None
    topics: list[Topic] = []
    sources: list[Source] = []
    alerts: AlertsCfg = AlertsCfg()
    lounge: LoungeCfg = LoungeCfg()

    # `guild_id` is v2's key and gets the same checks as `home_guild_id`, since
    # it's copied into it. v2.2 would have failed at runtime on a non-positive
    # one anyway; this just says so at startup.
    @field_validator("home_guild_id", "guild_id", mode="before")
    @classmethod
    def _validate_guild_id_not_bool(cls, v: object) -> object:
        return _reject_bool_guild_id(v)

    @field_validator("command_guild_ids", "comped_guild_ids", mode="before")
    @classmethod
    def _validate_guild_id_list(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                _reject_bool_guild_id(item)
        return v

    @field_validator("command_guild_ids", "comped_guild_ids")
    @classmethod
    def _validate_guild_ids_positive(cls, v: list[int]) -> list[int]:
        if any(i <= 0 for i in v):
            raise ValueError("guild ids must be positive integers")
        return v


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
    message = error["msg"]
    # The lounge validators write their own complete messages, path included.
    # Left alone they'd come out as "lounge.daily_quote.time: Value error,
    # lounge.daily_quote.time '99:99' is not HH:MM", which says the path twice
    # and the words "Value error" once too often.
    if error["type"] == "value_error" and message.startswith(
        ("Value error, lounge.", "Value error, owner_report.")
    ):
        return message.removeprefix("Value error, ")
    if error["type"] == "value_error":
        message = message.removeprefix("Value error, ")
    elif error["type"] == "extra_forbidden":
        where = ".".join(str(p) for p in loc[:-1]) or "the top level"
        message += f" (unknown key {str(loc[-1])!r} in {where}; check the spelling)"
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
            f"topics[{idx}] ({key}): channel_id is required "
            "(each game posts to its own channel as of v2.0)"
        )
    return f"{'.'.join(str(p) for p in loc)}: {message}"


def _source_problem(i: int, src: QuoteSourceCfg) -> str | None:
    """What's wrong with one quote source's value, or None if it's fine."""
    where = f"lounge.daily_quote.sources[{i}].{src.kind}"
    if src.kind == "wikiquote":
        v = src.value
        if v.lower().startswith("http"):
            return (
                f'{where} {v!r} isn\'t a page title (use the title, like "Oscar Wilde", not a URL)'
            )
        if len(v) > 255:
            # No echo: 256 characters of it wouldn't help anyone find the entry.
            return f"{where} is {len(v)} characters long; a Wikiquote page title can be 255 at most"
        if any(c in v for c in "#<>[]{}|"):
            return (
                f"{where} {v!r} has a character a Wikiquote page title can't have "
                "(one of # < > [ ] { } |)"
            )
    elif src.kind == "url":
        # Whatever we echo goes through shown_url: a raw link to a private gist
        # carries its secret in the userinfo or query, and a config error is
        # exactly what gets pasted into a chat when asking for help.
        shown = shown_url(src.value)
        try:
            parts = urlsplit(src.value)
            scheme, host = parts.scheme.lower(), parts.hostname
        except ValueError:
            return f"{where} {shown!r} isn't an https:// address"
        if scheme == "http":
            return f"{where} {shown!r} uses plain http; only https:// addresses are allowed"
        if scheme != "https" or not host:
            return f"{where} {shown!r} isn't an https:// address"
    return None


def _check_lounge(cfg: AppConfig, config_path: Path, errors: list[str]) -> AppConfig:
    """Cross-check the `lounge:` block, appending to `errors`; return `cfg` with sources filled in.

    Pydantic has already checked each field on its own. This is the part that
    needs two fields at once (a channel for an enabled feature) or the config
    file's location (relative `file:` paths). Shapes are checked even while a
    feature is disabled, for the same reason the welcome text is: a typo
    shouldn't get to wait for the day somebody flips the switch.
    """
    lounge = cfg.lounge
    welcome, quote = lounge.welcome, lounge.daily_quote

    if (welcome.enabled or quote.enabled) and lounge.channel_id is None:
        errors.append(
            "lounge.channel_id is required when lounge.welcome.enabled "
            "or lounge.daily_quote.enabled is true"
        )

    if welcome.enabled and not welcome.message.strip():
        errors.append("lounge.welcome.message is required when lounge.welcome.enabled is true")
    elif welcome.message.strip():
        for name in unknown_placeholders(welcome.message):
            allowed = " or ".join("{" + p + "}" for p in sorted(ALLOWED_PLACEHOLDERS))
            errors.append(
                f"lounge.welcome.message has an unknown placeholder {{{name}}} (use {allowed})"
            )
        length = worst_case_length(welcome.message)
        if length > 2000:
            errors.append(
                f"lounge.welcome.message is {length} characters with the longest possible "
                "mention and server name filled in; Discord's limit is 2000"
            )

    sources = quote.sources
    if sources is None:
        sources = [QuoteSourceCfg(kind="wikiquote", value=t) for t in DEFAULT_WIKIQUOTE_PAGES]
    elif not sources:
        errors.append(
            "lounge.daily_quote.sources is empty; remove it to use the built-in list, "
            "or set lounge.daily_quote.enabled to false"
        )
    else:
        resolved: list[QuoteSourceCfg] = []
        seen: dict[str, int] = {}
        for i, src in enumerate(sources):
            problem = _source_problem(i, src)
            if problem:
                errors.append(problem)
            if src.kind == "file":
                # absolute(), not resolve(): "relative to the config file's
                # directory" should mean that, symlinks and all.
                path = Path(src.value)
                if not path.is_absolute():
                    path = config_path.absolute().parent / path
                src = src.model_copy(update={"value": str(path)})
            if src.key in seen:
                errors.append(f"lounge.daily_quote.sources[{i}] repeats sources[{seen[src.key]}]")
            else:
                seen[src.key] = i
            resolved.append(src)
        sources = resolved

    quote = quote.model_copy(update={"sources": sources})
    return cfg.model_copy(update={"lounge": lounge.model_copy(update={"daily_quote": quote})})


_LEGACY_KEYS = ("guild_id", "digest", "topics", "sources", "alerts", "lounge")


def _with_default_name(source):
    """Fill in a Bluesky source's default name (see the uniqueness check in load_config)."""
    if isinstance(source, BlueskySource) and source.name is None:
        return source.model_copy(update={"name": _bluesky_default_name(source.query)})
    return source


def _shape_problems(raw: dict) -> list[str]:
    """Problems with which keys are present at all, judged from the raw YAML.

    A v3 config has `catalog:`; a v2 config has `topics:` and `sources:` (and
    the per-server keys around them). Neither is its own error, since "field
    required" for four fields at once doesn't tell anyone which shape they
    were going for. A v2 config missing just one of its own keys still gets
    pydantic's familiar wording, so nobody has to relearn what a typo looks like.
    """
    if "catalog" in raw:
        return []
    if "topics" not in raw and "sources" not in raw:
        return ["config needs a catalog: (v3), or the v2 topics: and sources: to import from"]
    return [
        f"{key}: Field required"
        for key in ("guild_id", "digest", "topics", "sources")
        if key not in raw
    ]


def _catalog_raw_problems(raw: dict) -> list[str]:
    """`catalog[i].channel_id`: not a model field, so a plain BaseModel would quietly ignore it."""
    problems = []
    catalog = raw.get("catalog")
    if isinstance(catalog, list):
        for i, entry in enumerate(catalog):
            if isinstance(entry, dict) and "channel_id" in entry:
                problems.append(
                    f"catalog[{i}].channel_id: channels are per server now; use /newsbot follow"
                )
    return problems


def _at_least_one(value: int, where: str) -> int:
    """Clamp a v2 number up to 1 (with a warning) for a v3 field that needs >= 1.

    v2.2 took 0 or a negative number for these without complaint, and what it
    did with them was quietly post nothing: `lookback_hours: 0` means
    "nothing is recent", `max_items_per_topic: 0` caps every topic to an
    empty list, and `queries_per_topic: 0` slices the query list to nothing.
    v3 can't express "collect nothing" (and shouldn't want to), so 1 is the
    nearest value that means something, and the owner gets told.
    """
    if value >= 1:
        return value
    logging.getLogger(__name__).warning(
        "%s is %d, which v2 accepted but which only ever made the bot collect nothing; "
        "using 1 instead",
        where,
        value,
    )
    return 1


def _derive_catalog(
    topics: Sequence[Topic], sources: Sequence[Source]
) -> tuple[list[GameCfg], list[SharedSourceCfg], WebSearchCfg | None]:
    """Regroup v2 `topics` plus `sources` into catalog, shared sources and web search.

    A source naming exactly one known topic belongs to that game. Anything
    else (no topics, or several) is shared, keeping the restriction as
    `games`. The first `web_search` source becomes the global block.
    """
    games = {t.key: GameCfg(**t.model_dump(exclude={"channel_id"})) for t in topics}
    shared: list[SharedSourceCfg] = []
    web_search: WebSearchCfg | None = None
    for source in sources:
        if isinstance(source, WebSearchSource):
            if web_search is None:
                web_search = WebSearchCfg(
                    name=source.name,
                    queries_per_game=_at_least_one(source.queries_per_topic, "queries_per_topic"),
                    query_templates=source.query_templates,
                    trust=source.trust,
                )
            else:
                logging.getLogger(__name__).warning(
                    "more than one web_search source configured; v3 has one global block, "
                    "so %r is not used",
                    source.name,
                )
            continue
        scope = source.topics or []
        if len(scope) == 1 and scope[0] in games:
            games[scope[0]].sources.append(source.model_copy(update={"topics": None}))
        else:
            fields = source.model_dump(exclude={"topics"})
            shared.append(_SHARED_BY_TYPE[source.type](**fields, games=scope or None))
    return list(games.values()), shared, web_search


def _v3_problems(cfg: AppConfig) -> list[str]:
    """Cross-checks on a v3 catalog: keys, sources, and everything that names a game."""
    errors: list[str] = []
    if len(cfg.catalog) > _MAX_CATALOG_GAMES:
        errors.append(
            f"catalog has {len(cfg.catalog)} games; the limit is {_MAX_CATALOG_GAMES} "
            "(a Discord select menu holds 25 options)"
        )
    keys: set[str] = set()
    for i, game in enumerate(cfg.catalog):
        if not _TOPIC_KEY_RE.match(game.key):
            errors.append(f"catalog[{i}].key {game.key!r} must match ^[a-z0-9_]+$")
        if game.key in keys:
            errors.append(f"duplicate game key {game.key!r}")
        keys.add(game.key)
        if not game.match_name and not game.aliases:
            errors.append(
                f"match_name is false for {game.key} but it has no aliases, "
                "so nothing would ever match"
            )
        for j, source in enumerate(game.sources):
            where = f"catalog[{i}] ({game.key}).sources[{j}]"
            if isinstance(source, WebSearchSource):
                errors.append(f"{where}: web_search goes in the top-level web_search: block")
            elif source.topics is not None:
                errors.append(
                    f"{where} ({source.name}): remove topics; "
                    "a source listed under a game belongs to that game"
                )

    for j, shared in enumerate(cfg.shared_sources):
        if isinstance(shared, WebSearchSource):
            errors.append(
                f"shared_sources[{j}]: web_search goes in the top-level web_search: block"
            )
            continue
        if shared.topics is not None:
            errors.append(f"shared_sources[{j}] ({shared.name}): use games:, not topics:")
        for g in shared.games or []:
            if g not in keys:
                errors.append(f"shared_sources[{j}] ({shared.name}) names unknown game {g!r}")

    for g in cfg.shift.games:
        if g not in keys:
            errors.append(f"shift.games names unknown game {g!r}")

    # source_health is keyed by name, so the check spans every place a source can live.
    names = [s.name for game in cfg.catalog for s in game.sources]
    names += [s.name for s in cfg.shared_sources]
    if cfg.web_search is not None:
        names.append(cfg.web_search.name)
    seen: set[str] = set()
    for name in names:
        if name in seen:
            errors.append(f"duplicate source name {name!r}")
        seen.add(name)

    for i, topic in enumerate(cfg.topics):
        if topic.key not in keys:
            errors.append(
                f"topics[{i}] ({topic.key}) isn't in catalog; "
                "the v2 import needs every old game in the catalog"
            )
    return errors


def _build_legacy(cfg: AppConfig, guild_id: int, digest: DigestCfg, raw: dict) -> LegacySetup:
    alerts = cfg.alerts
    return LegacySetup(
        guild_id=guild_id,
        admin_channel_id=cfg.admin_channel_id,
        digest_time=digest.time,
        timezone=digest.timezone,
        games=[(t.key, t.channel_id) for t in cfg.topics],
        shift_enabled=alerts.enabled,
        shift_channel_id=alerts.channel_id,
        shift_ping="everyone" if alerts.max_pings_per_day > 0 else "none",
        # Present in the file at all, even disabled: the import decides what
        # a disabled block is worth, not the loader.
        lounge=cfg.lounge if "lounge" in raw else None,
        alerts_max_pings=alerts.max_pings_per_day,
    )


def _yaml_kind(value: object) -> str:
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "plain text"
    return f"a bare {type(value).__name__} value"


def load_config(path: str | Path) -> AppConfig:
    """Load, validate, and cross-check `config.yaml`.

    Raises `ConfigError` with every problem found, rather than pydantic's
    default one-shot exception, so a first run doesn't turn into a game of
    whack-a-mole against a stack trace.
    """
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            "Invalid config:\n  - the top level of the file must be a set of "
            f"`key: value` settings, not {_yaml_kind(raw)}"
        )
    log = logging.getLogger(__name__)

    # A pre-check against the raw YAML, not a pydantic field: DigestCfg no
    # longer declares channel_id at all, and a plain BaseModel silently
    # ignores fields it doesn't recognize: an old v1 config.yaml that
    # still sets digest.channel_id would otherwise load "successfully"
    # with that value quietly going nowhere, which is a worse outcome
    # than the field simply not existing. Collected ahead of pydantic's
    # own errors so it folds into the same combined ConfigError either way.
    pre_errors: list[str] = []
    digest_raw = raw.get("digest")
    if isinstance(digest_raw, dict) and "channel_id" in digest_raw:
        pre_errors.append(
            "digest.channel_id was removed in v2.0: move it to a channel_id "
            "on each topic (topics[].channel_id); there is no fallback"
        )
    pre_errors += _shape_problems(raw)
    pre_errors += _catalog_raw_problems(raw)

    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as exc:
        errors = pre_errors + [_format_pydantic_error(e, raw) for e in exc.errors()]
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors)) from exc

    # Unknown top-level keys stay legal (v2.2 and hybrid files carry keys this
    # loader doesn't read), so they can't be errors; but `comped_guild_id`
    # quietly dropping a server's premium tier is exactly the kind of thing a
    # log line is for. `legacy` is ours, not something a file gets to set.
    unknown_top = sorted(str(k) for k in raw if k not in AppConfig.model_fields or k == "legacy")
    if unknown_top:
        log.warning(
            "ignoring unknown top-level keys in config (typos?): %s", ", ".join(unknown_top)
        )

    errors: list[str] = list(pre_errors)
    is_v3 = "catalog" in raw
    explicit = cfg.model_fields_set

    # admin_permission must name a real discord.Permissions flag, checked
    # against VALID_FLAGS (the actual name -> bit mapping), not hasattr()
    # against the class: discord.Permissions also has real attributes
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

    # The keys alerts.topics may name: the catalog's when there is one (a
    # derived catalog has exactly the topics' keys, so it's the same set).
    known_keys = {g.key for g in cfg.catalog} if is_v3 else seen_keys

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
        # tests is almost certainly a copy-paste mistake, not intent; it
        # used to surface as a bare RuntimeError the first time someone
        # ran /newsbot test-alert, long after config load had already
        # said everything looked fine.
        errors.append(
            "alerts.allow_test_command is true but alerts.enabled is false: "
            "/newsbot test-alert has nothing to test with alerts disabled"
        )

    # Fill in Bluesky default names before the uniqueness check, since
    # source_health.source_name is the primary key: two sources silently
    # sharing a name would silently share health tracking too.
    cfg = cfg.model_copy(
        update={
            "sources": [_with_default_name(s) for s in cfg.sources],
            "catalog": [
                g.model_copy(update={"sources": [_with_default_name(s) for s in g.sources]})
                for g in cfg.catalog
            ],
            "shared_sources": [_with_default_name(s) for s in cfg.shared_sources],
        }
    )

    if is_v3:
        errors += _v3_problems(cfg)
    else:
        # Derive mode: no catalog, so build one from the v2 keys, before the
        # web_search filter below throws the Brave source away for lack of a
        # key. Blocks the file spells out itself win over the derived ones.
        games, shared, web_search = _derive_catalog(cfg.topics, cfg.sources)
        if "shared_sources" in raw:
            errors.append("shared_sources needs a catalog: (the v2 shape derives its own)")
        update: dict[str, object] = {"catalog": games, "shared_sources": shared}
        if cfg.digest is not None:
            derived = {
                "collection": CollectionCfg(
                    interval_minutes=cfg.alerts.interval_minutes,
                    lookback_hours=_at_least_one(
                        cfg.digest.lookback_hours, "digest.lookback_hours"
                    ),
                    max_items_per_game=_at_least_one(
                        cfg.digest.max_items_per_topic, "digest.max_items_per_topic"
                    ),
                ),
                "ai": AiCfg(subject=cfg.digest.subject),
                "run_report": cfg.digest.report_to_admin,
                "shift": ShiftCfg(
                    games=cfg.alerts.topics,
                    max_item_age_hours=cfg.alerts.max_item_age_hours,
                    max_pings_per_day=cfg.alerts.max_pings_per_day,
                    ping_trust=cfg.alerts.ping_trust,
                    max_codes_per_item=cfg.alerts.max_codes_per_item,
                    allow_test_command=cfg.alerts.allow_test_command,
                ),
                "web_search": web_search,
            }
            update.update({k: v for k, v in derived.items() if k not in explicit})
        cfg = cfg.model_copy(update=update)
        # v3's own cross-check only runs for a catalog:, but an explicit shift:
        # block in a v2-shaped file can name a ghost game just as easily, and
        # the sweep would then watch nothing without a word. The derived shift
        # is skipped: its games are alerts.topics, already checked above.
        if "shift" in explicit:
            for g in cfg.shift.games:
                if g not in known_keys:
                    errors.append(f"shift.games names unknown game {g!r}")

    warned_brave = False
    seen_names: set[str] = set()
    filtered_sources = []
    for source in cfg.sources:
        if not is_v3:
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
            log.warning("web_search source configured but BRAVE_API_KEY is not set; disabling it")
            warned_brave = True
            continue
        filtered_sources.append(source)
    cfg = cfg.model_copy(update={"sources": filtered_sources})

    if is_v3 and cfg.web_search is not None and not warned_brave:
        if not os.environ.get("BRAVE_API_KEY"):
            log.warning("web_search configured but BRAVE_API_KEY is not set; disabling it")

    cfg = _check_lounge(cfg, Path(path), errors)

    # D9: the home guild defaults to the old single guild. A prod config sets
    # home_guild_id itself at rollout; that's a config value, not a code default.
    if cfg.guild_id is not None:
        if cfg.home_guild_id is None:
            cfg = cfg.model_copy(update={"home_guild_id": cfg.guild_id})
        if cfg.digest is None:
            errors.append("guild_id is set but digest: is missing; the v2 import needs both")
        else:
            cfg = cfg.model_copy(
                update={"legacy": _build_legacy(cfg, cfg.guild_id, cfg.digest, raw)}
            )

    if errors:
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors))

    if is_v3:
        old_keys = [k for k in _LEGACY_KEYS if k in raw]
        if old_keys:
            log.info(
                "catalog: is present, so these v2 keys are only read by the one-time "
                "import; once it has run they can be deleted from config.yaml: %s",
                ", ".join(old_keys),
            )

    if cfg.alerts.allow_test_command:
        # /newsbot test-alert lets anyone with admin_permission post a fake
        # SHiFT code alert on demand: exactly what the private test guild
        # needs and exactly what a production config should never carry,
        # so a startup log line is the one place this gets said out loud.
        log.warning("alerts.allow_test_command is true; /newsbot test-alert will be registered")

    return cfg


def configured_source_names(cfg: AppConfig) -> set[str]:
    """The `source_name` every currently-configured source records health under.

    This has to match the collectors exactly: every `Collector` sets
    `self.name = source.name`, and the sources here already have Bluesky's
    default name filled in by the time `load_config` returns them (see
    above), so this is just "read `.name` off what's configured" with one
    place to fix if a fifth source type ever shows up and someone forgets.
    Covers the v2 `sources` list plus the catalog's and the shared sources
    (a derived catalog repeats the v2 names, which a set doesn't mind). Web
    search counts only while `BRAVE_API_KEY` is set, same as it always has.
    Used to filter `/newsbot status` down to sources that still exist,
    instead of every source that ever recorded health (see the IGN
    incident in CLAUDE.md).
    """
    names = {source.name for source in cfg.sources}
    names |= {source.name for game in cfg.catalog for source in game.sources}
    names |= {source.name for source in cfg.shared_sources}
    if cfg.web_search is not None and os.environ.get("BRAVE_API_KEY"):
        names.add(cfg.web_search.name)
    return names


def count_configured_web_search_sources(path: str | Path) -> int:
    """How many `web_search` sources `path` names, before `load_config` gets a chance to drop any.

    `load_config` silently disables a `web_search` source (with its own
    startup warning) when `BRAVE_API_KEY` isn't set, which is the right
    call at boot but means `AppConfig.sources` can no longer answer "was
    web_search ever configured at all": exactly the question
    `--check-sources` needs answered, to tell "not configured" apart from
    "configured, but skipped for lack of a key". Re-reads the raw YAML
    rather than reusing `load_config`'s own parse, since that parse is
    already past the point where the answer got thrown away.
    """
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        return 0
    sources = raw.get("sources") or []
    return sum(1 for s in sources if isinstance(s, dict) and s.get("type") == "web_search")


def load_check_sources_secrets(env: Mapping[str, str] = os.environ) -> Secrets:
    """Just enough of `Secrets` for `--check-sources`: never reads ANTHROPIC_API_KEY/DISCORD_TOKEN.

    `--check-sources` runs the real collectors to sanity-check
    `config.yaml` before anything talks to Claude or Discord, so it has
    no business demanding either credential. `Secrets` still needs a
    value for `anthropic_api_key` (it isn't optional), so this gives it
    an obvious placeholder instead of reading the environment for one --
    the point isn't that the value is empty, it's that this function
    never even looks.
    """
    return Secrets(
        discord_token=None,
        anthropic_api_key=SecretStr("unused (--check-sources never calls Claude)"),
        brave_api_key=env.get("BRAVE_API_KEY") or None,
        bluesky_handle=(env.get("BLUESKY_HANDLE") or "").strip().lstrip("@") or None,
        bluesky_app_password=env.get("BLUESKY_APP_PASSWORD") or None,
    )


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
