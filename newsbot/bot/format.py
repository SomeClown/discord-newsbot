"""Turn a day's stories into `discord.Embed` objects, and pack them under Discord's limits.

This module only builds data; it never talks to the gateway (a plain
`discord.Embed` is just a fancy dict, so nothing here needs a bot token or
a running event loop). That's on purpose: it's the only way to unit-test
"does a 41st story get cut, and does the '+N more' line say the right
number" without standing up a Discord connection, which is a genuinely
bad way to find out you're off by one.

Everything user-facing that started life as scraped text goes through
`esc()` before it reaches an embed. URLs never do -- they go straight into
link targets (`<url>`, which suppresses Discord's preview embed instead of
letting six of them stack up under one story), and only ever come from
`StoryDraft.item_urls`, which `pipeline/summarize.py` has already checked
against what we actually collected. A headline can lie about the news; it
should not get to lie about where you're clicking.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

import discord

from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.store.models import StatusSnapshot, StoryView

_DESCRIPTION_LIMIT = 4096
_TITLE_LIMIT = 256
_MESSAGE_TOTAL_LIMIT = 6000
_MAX_EMBEDS_PER_MESSAGE = 10
_HEADER_LIMIT = 2000
_MAX_LINKS_SHOWN = 3
_MAX_FIELD_VALUE = 1024

_LABEL_ORDER = {"official": 0, "reported": 1, "rumor": 2}
_LABEL_MARKER = {"official": "🟢 OFFICIAL", "reported": "🟡 REPORTED", "rumor": "🔴 RUMOR"}
_UPDATE_MARKER = "🔁 UPDATE"
_LINKY_MENTION_RE = re.compile(r"<(?=[#/])")

# A fixed, arbitrary 6-color palette (Discord's own brand blurple plus five
# others that read fine against dark and light themes). Which topic gets
# which color only needs to be *stable*, not meaningful.
_PALETTE = [0x5865F2, 0x57F287, 0xFEE75C, 0xEB459E, 0xED4245, 0x1ABC9C]


def esc(s: str) -> str:
    """Escape markdown and @mentions, so scraped text can't format itself or ping the server.

    `allowed_mentions=none` on the actual send is the real backstop; this
    is the belt to that suspenders, so a headline never even *renders* as
    a working @everyone in someone's client before the send-time guard
    would have caught it anyway.
    """
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(s))
    # escape_mentions() covers users, roles and @everyone but, per its own
    # docstring, not channels (<#id>) or slash-command links (</name:id>).
    # A zero-width space after the "<" defuses both without visibly changing
    # the text. test-engineer caught this one; I'd assumed "mentions" meant
    # all of them, which is the kind of assumption that ages badly.
    return _LINKY_MENTION_RE.sub("<\u200b", escaped)


def _topic_color(topic_key: str) -> int:
    """A color per topic, stable across restarts.

    Plain `hash()` on a string is salted per-process (`PYTHONHASHSEED`),
    so Borderlands would get a new color every time the container
    restarted. `sha256` doesn't have moods.
    """
    digest = hashlib.sha256(topic_key.encode()).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def _sort_key(story: StoryDraft) -> tuple[int, int]:
    # Official before reported before rumor; within a label, the story
    # backed by more sources comes first. Ties keep dict/list order,
    # which is fine -- nothing here claims to break them meaningfully.
    return (_LABEL_ORDER[story.label], -len(story.item_urls))


def _link_line(urls: list[str]) -> str:
    shown = urls[:_MAX_LINKS_SHOWN]
    # `<url>` (angle brackets) tells Discord "link this, but don't expand
    # it into a preview card" -- without that, three or four stacked link
    # previews turn one story into a wall of thumbnails.
    line = " · ".join(f"<{url}>" for url in shown)
    extra = len(urls) - len(shown)
    if extra > 0:
        line += f" · +{extra} more"
    return line


def _story_block(story: StoryDraft) -> str:
    marker = _UPDATE_MARKER if story.update_of_story_id is not None else _LABEL_MARKER[story.label]
    return "\n".join(
        [f"{marker} · {esc(story.headline)}", esc(story.summary), _link_line(story.item_urls)]
    )


def _truncate_description(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _topic_embed(topic: Topic, stories: list[StoryDraft]) -> discord.Embed:
    color = _topic_color(topic.key)
    title = esc(topic.name)[:_TITLE_LIMIT]
    if not stories:
        return discord.Embed(title=title, description="No new stories today.", color=color)

    ordered = sorted(stories, key=_sort_key)
    blocks = [_story_block(story) for story in ordered]

    # Least important (cut first) is the reverse of display order, i.e.
    # trim from the end of `blocks`. Walk down from "keep everything"
    # until the description plus its cut-count line fits.
    kept = len(blocks)
    while kept > 0:
        cut = len(blocks) - kept
        description = "\n\n".join(blocks[:kept])
        if cut:
            description += f"\n\n+{cut} more, use /news"
        if len(description) <= _DESCRIPTION_LIMIT:
            break
        kept -= 1
    else:
        # Not even the single most important story fits on its own --
        # a pathologically long summary, in practice never (summaries are
        # capped at 400 chars by the schema). Hard-truncate rather than
        # emit an empty embed.
        description = _truncate_description(blocks[0])

    return discord.Embed(title=title, description=description, color=color)


def _fallback_embed(topic: Topic, items: list[TopicItem], note: str | None) -> discord.Embed:
    color = _topic_color(topic.key)
    title = esc(topic.name)[:_TITLE_LIMIT]
    if not items:
        # No items at all makes the *reason* we have no stories moot --
        # "the model failed" and "there was nothing to summarize" look
        # identical to a reader either way.
        return discord.Embed(title=title, description="No new stories today.", color=color)

    lines = [esc(note)] if note else []
    lines.extend(
        f"• {esc(topic_item.item.title)} — <{topic_item.item.url}>" for topic_item in items
    )
    return discord.Embed(
        title=title, description=_truncate_description("\n".join(lines)), color=color
    )


def _pack_messages(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Greedily pack embeds into messages, respecting the 10-embed and 6000-char caps."""
    messages: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_total = 0
    for embed in embeds:
        embed_len = len(embed)
        if current and (
            len(current) >= _MAX_EMBEDS_PER_MESSAGE
            or current_total + embed_len > _MESSAGE_TOTAL_LIMIT
        ):
            messages.append(current)
            current, current_total = [], 0
        current.append(embed)
        current_total += embed_len
    if current:
        messages.append(current)
    return messages


def _story_count(
    topic: Topic, summaries: dict[str, TopicSummary], fallback_items: dict[str, list[TopicItem]]
) -> int:
    summary = summaries.get(topic.key)
    if summary is None:
        return 0
    if summary.fallback:
        return len(fallback_items.get(topic.key, []))
    return len(summary.stories)


def _header(
    run_date: date,
    topics: list[Topic],
    summaries: dict[str, TopicSummary],
    fallback_items: dict[str, list[TopicItem]],
    coverage_notes: list[str],
) -> str:
    counts = " · ".join(
        f"{esc(topic.name)}: {_story_count(topic, summaries, fallback_items)}" for topic in topics
    )
    lines = [f"**News digest — {run_date.isoformat()}**", counts]
    if coverage_notes:
        lines.append("\n".join(f"- {esc(note)}" for note in coverage_notes))
    return _truncate_description("\n".join(lines), _HEADER_LIMIT)


@dataclass
class RenderedDigest:
    header: str
    embed_messages: list[list[discord.Embed]]  # each inner list is one Discord message


def render_digest(
    run_date: date,
    topics: list[Topic],
    summaries: dict[str, TopicSummary],
    fallback_items: dict[str, list[TopicItem]],
    coverage_notes: list[str],
) -> RenderedDigest:
    """Render one day's digest: a header plus embeds packed into messages.

    A topic with no `TopicSummary` at all (nothing was collected for it
    today) renders the same empty state as one that got items but no
    stories -- from a reader's chair, "nothing happened" and "we found
    nothing worth a story" look identical, and should.
    """
    embeds = []
    for topic in topics:
        summary = summaries.get(topic.key)
        if summary is not None and summary.fallback:
            embeds.append(_fallback_embed(topic, fallback_items.get(topic.key, []), summary.note))
        else:
            embeds.append(_topic_embed(topic, summary.stories if summary else []))

    header = _header(run_date, topics, summaries, fallback_items, coverage_notes)
    return RenderedDigest(header=header, embed_messages=_pack_messages(embeds))


def render_story_page(
    stories: list[StoryView],
    topics_by_key: dict[str, Topic],
    title: str,
    page: int,
    pages: int,
) -> discord.Embed:
    """Render one page of `/news recent` or `/news search` results."""
    embed = discord.Embed(title=esc(title)[:_TITLE_LIMIT], color=_PALETTE[0])
    if not stories:
        embed.description = "No stories found."
        return embed

    blocks = []
    for story in stories:
        topic = topics_by_key.get(story.topic_key)
        topic_name = topic.name if topic else story.topic_key
        marker = _UPDATE_MARKER if story.is_update_of is not None else _LABEL_MARKER[story.label]
        blocks.append(
            "\n".join(
                [
                    f"{marker} · {esc(topic_name)} · {esc(story.headline)}",
                    esc(story.summary),
                    _link_line(story.urls),
                ]
            )
        )
    embed.description = _truncate_description("\n\n".join(blocks))
    embed.set_footer(text=f"Page {page} of {pages}")
    return embed


def render_status(snap: StatusSnapshot, spend_usd: float) -> discord.Embed:
    """Render `/newsbot status`: last run, source health, and the running spend estimate."""
    embed = discord.Embed(title="newsbot status", color=_PALETTE[0])

    if snap.last_digest is not None:
        embed.add_field(
            name="Last digest",
            value=esc(f"{snap.last_digest.run_date.isoformat()} — {snap.last_digest.status}"),
            inline=False,
        )
    else:
        embed.add_field(name="Last digest", value="none yet", inline=False)

    embed.add_field(name="Items (24h)", value=str(snap.items_last_24h))
    embed.add_field(name="Stories (24h)", value=str(snap.stories_last_24h))
    embed.add_field(name="Est. spend this month", value=f"${spend_usd:.2f}")

    # One line per source in the description, not one field per source.
    # Discord caps an embed at 25 fields, and the first real config had 24
    # sources plus four summary fields; you can guess how that went.
    healthy = sum(1 for s in snap.source_health if s.consecutive_failures == 0)
    embed.add_field(name="Sources healthy", value=f"{healthy} of {len(snap.source_health)}")

    problems = sorted(
        (s for s in snap.source_health if s.consecutive_failures > 0),
        key=lambda s: (-s.consecutive_failures, s.source_name.casefold()),
    )
    if problems:
        lines = ["**Sources with recent failures**"]
        for source in problems:
            flag = "⚠️ " if source.consecutive_failures >= 3 else ""
            error = f": {source.last_error.splitlines()[0][:120]}" if source.last_error else ""
            lines.append(esc(f"{flag}{source.source_name} ({source.consecutive_failures}){error}"))
        embed.description = _truncate_description("\n".join(lines))
    else:
        embed.description = "Every source answered on its last run."

    return embed


def to_text(r: RenderedDigest) -> str:
    """Render a `RenderedDigest` as plain text, for the CLI's `PrintPublisher`.

    Nobody's Discord client is involved in `--dry-run`, so the embeds'
    structure (title, description, footer) gets flattened into something
    readable on a terminal instead.
    """
    parts = [r.header]
    for message in r.embed_messages:
        for embed in message:
            parts.append(f"\n--- {embed.title or ''} ---")
            if embed.description:
                parts.append(str(embed.description))
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value}")
            if embed.footer and embed.footer.text:
                parts.append(f"({embed.footer.text})")
    return "\n".join(parts)


__all__ = [
    "RenderedDigest",
    "esc",
    "render_digest",
    "render_status",
    "render_story_page",
    "to_text",
]
