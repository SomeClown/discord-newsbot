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
from newsbot.pipeline.normalize import canonicalize
from newsbot.pipeline.summarize import StoryDraft, TopicSummary
from newsbot.shift.decide import CodeCandidate
from newsbot.shift.match import is_code
from newsbot.store.models import StatusSnapshot, StoryView

_DESCRIPTION_LIMIT = 4096
_TITLE_LIMIT = 256
_MESSAGE_TOTAL_LIMIT = 6000
_MAX_EMBEDS_PER_MESSAGE = 10
_HEADER_LIMIT = 2000
_MAX_LINKS_SHOWN = 3
_MAX_FIELD_VALUE = 1024
# Discord's plain-message content cap (as opposed to an embed's much bigger
# limits above) -- code alerts are plain messages, not embeds, since a code
# is meant to be select-and-copy-able, and an embed's description puts a
# faint background behind text that makes triple-clicking to select it a
# worse experience than it needs to be.
_ALERT_CONTENT_LIMIT = 2000
_CONTINUATION_HEADER = "**(continued)**"

_LABEL_ORDER = {"official": 0, "reported": 1, "rumor": 2}
_LABEL_MARKER = {"official": "🟢 OFFICIAL", "reported": "🟡 REPORTED", "rumor": "🔴 RUMOR"}
_UPDATE_MARKER = "🔁 UPDATE"
_LINKY_MENTION_RE = re.compile(r"<(?=[#/])")
# Belt-and-suspenders behind summarize.py's postprocess(), which is
# supposed to have already stripped any `scheme://...` token out of
# headline/summary text before it ever reaches this module. This is the
# second layer, for whatever text reaches an embed some other way (or
# whatever the first layer's regex didn't quite cover): a zero-width
# space right after the scheme's colon defuses "https://" into
# "https:/​/", which reads identically to a person and autolinks to
# nobody.
_URL_SCHEME_RE = re.compile(r"\b([a-z][a-z0-9+.\-]*):(?=//)", re.IGNORECASE)
# Discord makes invite links clickable even without a scheme, because of
# course it does.
_BARE_INVITE_RE = re.compile(r"\b(discord\.gg|discord(?:app)?\.com/invite)/", re.IGNORECASE)

# A fixed, arbitrary 6-color palette (Discord's own brand blurple plus five
# others that read fine against dark and light themes). Which topic gets
# which color only needs to be *stable*, not meaningful.
_PALETTE = [0x5865F2, 0x57F287, 0xFEE75C, 0xEB459E, 0xED4245, 0x1ABC9C]


def discord_len(s: str) -> int:
    """Length of `s` in UTF-16 code units, matching how Discord measures its own limits.

    A Python `str` counts codepoints (`len("🤖") == 1`), but Discord's
    documented limits (title, description, field, embed-total, message)
    are UTF-16 code units -- and most emoji, plus a good chunk of CJK
    extension characters, live outside the Basic Multilingual Plane, which
    makes them *two* UTF-16 units apiece. A digest full of emoji reactions
    to a patch note could look comfortably under 4096 by `len()` and still
    bounce off Discord's real limit. `encode("utf-16-le")` is two bytes
    per unit, hence the `// 2`.
    """
    return len(s.encode("utf-16-le")) // 2


def _truncate_utf16(text: str, limit: int, *, suffix: str = "") -> str:
    """Truncate `text` to at most `limit` UTF-16 units, keeping `suffix` (if any) intact.

    Walks codepoint by codepoint rather than slicing raw UTF-16 units, so
    an astral character (2 units) is always kept whole or dropped whole --
    a raw unit-based slice could otherwise cut a surrogate pair in half
    and leave a lone surrogate sitting at the end of the string, which is
    exactly the kind of thing that turns into a mojibake diamond in
    whoever's client renders it.
    """
    if discord_len(text) <= limit:
        return text
    budget = max(limit - discord_len(suffix), 0)
    kept: list[str] = []
    total = 0
    for ch in text:
        ch_len = discord_len(ch)
        if total + ch_len > budget:
            break
        kept.append(ch)
        total += ch_len
    return "".join(kept).rstrip() + suffix


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
    escaped = _LINKY_MENTION_RE.sub("<\u200b", escaped)
    # Same idea, aimed at a URL scheme instead of a mention: a zero-width
    # space right after the colon stops "https://evil.example" from ever
    # being a live "scheme://" token, without changing how it reads.
    escaped = _URL_SCHEME_RE.sub(lambda m: f"{m.group(1)}:\u200b", escaped)
    return _BARE_INVITE_RE.sub(lambda m: f"{m.group(1)}\u200b/", escaped)


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


def _safe_link(url: str) -> str | None:
    """Re-run `url` through `canonicalize()` immediately before it's wrapped in `<...>`.

    Every URL that reaches here should already be canonical -- item_urls
    via `pipeline.summarize.postprocess`, fallback items via
    `pipeline.normalize.normalize()` at collection time -- but "should
    already be" is exactly the kind of assumption that's cheap to just
    re-check right where it matters. `canonicalize()` percent-encodes any
    angle bracket, square bracket, quote, backtick or space in the path,
    which is what stops a poisoned URL from closing this markup's
    `<...>` autolink early and growing a fake
    `[text](url)` link right after it. Returns None (and the caller drops
    the link) on the rare case a URL doesn't survive re-canonicalizing at
    all, rather than ever emitting one unescaped.
    """
    return canonicalize(url)


def _link_line(urls: list[str]) -> str:
    safe_urls = [safe for url in urls if (safe := _safe_link(url)) is not None]
    shown = safe_urls[:_MAX_LINKS_SHOWN]
    # `<url>` (angle brackets) tells Discord "link this, but don't expand
    # it into a preview card" -- without that, three or four stacked link
    # previews turn one story into a wall of thumbnails.
    line = " · ".join(f"<{url}>" for url in shown)
    extra = len(safe_urls) - len(shown)
    if extra > 0:
        line += f" · +{extra} more"
    return line


def _story_block(story: StoryDraft) -> str:
    marker = _UPDATE_MARKER if story.update_of_story_id is not None else _LABEL_MARKER[story.label]
    return "\n".join(
        [f"{marker} · {esc(story.headline)}", esc(story.summary), _link_line(story.item_urls)]
    )


def _truncate_description(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    return _truncate_utf16(text, limit, suffix="…")


def _topic_embed(topic: Topic, stories: list[StoryDraft]) -> discord.Embed:
    color = _topic_color(topic.key)
    title = _truncate_utf16(esc(topic.name), _TITLE_LIMIT)
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
        if discord_len(description) <= _DESCRIPTION_LIMIT:
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
    title = _truncate_utf16(esc(topic.name), _TITLE_LIMIT)
    if not items:
        # No items at all makes the *reason* we have no stories moot --
        # "the model failed" and "there was nothing to summarize" look
        # identical to a reader either way.
        return discord.Embed(title=title, description="No new stories today.", color=color)

    lines = [esc(note)] if note else []
    for topic_item in items:
        safe_url = _safe_link(topic_item.item.url)
        if safe_url is None:
            continue
        lines.append(f"• {esc(topic_item.item.title)} — <{safe_url}>")
    return discord.Embed(
        title=title, description=_truncate_description("\n".join(lines)), color=color
    )


def _embed_len(embed: discord.Embed) -> int:
    """`discord.Embed.__len__`, but counted in UTF-16 units instead of codepoints.

    Mirrors discord.py's own `__len__` (title + description + every
    field's name and value + footer text + author name) field for field,
    since that total -- not any individual piece -- is what
    `_MESSAGE_TOTAL_LIMIT` (Discord's 6000-per-message cap) is measured
    against.
    """
    total = discord_len(embed.title or "") + discord_len(embed.description or "")
    for field in embed.fields:
        total += discord_len(field.name or "") + discord_len(field.value or "")
    if embed.footer and embed.footer.text:
        total += discord_len(embed.footer.text)
    if embed.author and embed.author.name:
        total += discord_len(embed.author.name)
    return total


def _pack_messages(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Greedily pack embeds into messages, respecting the 10-embed and 6000-char caps."""
    messages: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_total = 0
    for embed in embeds:
        embed_len = _embed_len(embed)
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
    embed = discord.Embed(title=_truncate_utf16(esc(title), _TITLE_LIMIT), color=_PALETTE[0])
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
            value=_truncate_utf16(
                esc(f"{snap.last_digest.run_date.isoformat()} — {snap.last_digest.status}"),
                _MAX_FIELD_VALUE,
            ),
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
    healthy = sum(1 for s in snap.source_health if s.consecutive_failures == 0 and not s.never_run)
    never_run = sum(1 for s in snap.source_health if s.never_run)
    healthy_value = f"{healthy} of {len(snap.source_health)}"
    if never_run:
        healthy_value += f" ({never_run} not run yet)"
    embed.add_field(name="Sources healthy", value=healthy_value)

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


@dataclass
class RenderedAlert:
    """One Discord message's worth of a SHiFT code alert batch.

    `codes` is that message's own slice of the codes it announces -- when a
    batch splits across several messages (see `render_code_alerts`),
    `shift/sweep.py` needs to know which codes to mark `posted` against
    which message id, and a code that landed in message 2 shouldn't get
    stamped with message 1's id just because it was easier to track one
    running total.
    """

    content: str
    codes: list[str]
    ping: bool


def _alert_title(candidates: list[CodeCandidate], *, plural: bool) -> str:
    suffix = "s" if plural else ""
    if all(c.golden for c in candidates):
        return f"New Golden Key code{suffix}"
    return f"New SHiFT code{suffix}"


def _alert_block(candidate: CodeCandidate, *, golden_prefix: bool) -> str:
    # Checked, not just trusted: this is the one place a code goes out to
    # Discord, and a code that somehow isn't shaped like a code (a bug
    # upstream, not a real scenario) is worth a loud failure here rather
    # than a quietly wrong alert. A plain `assert` strips out under `-O`;
    # this doesn't get to.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    prefix = "Golden Key: " if golden_prefix and candidate.golden else ""
    safe_url = _safe_link(candidate.item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    return f"```\n{candidate.code}\n```\n{prefix}{esc(candidate.source_name)}{link}"


def render_code_alerts(
    candidates: list[CodeCandidate], *, ping: bool, test: bool = False
) -> list[RenderedAlert]:
    """Render a batch of new SHiFT codes into one or more alert messages.

    One ping covers the whole batch (A4): only the first message's header
    carries `@everyone` (and only if `ping` is true to begin with); every
    continuation starts with `_CONTINUATION_HEADER` instead and never
    pings, no matter how many messages the batch spills into. Mixed
    golden/non-golden batches (A3) keep the plain "New SHiFT code(s)"
    title but prefix each golden entry with "Golden Key:" so it doesn't
    read as an ordinary code.
    """
    if not candidates:
        return []

    mixed = any(c.golden for c in candidates) and not all(c.golden for c in candidates)
    title = _alert_title(candidates, plural=len(candidates) > 1)
    test_prefix = "[TEST] " if test else ""
    first_header = ("@everyone " if ping else "") + f"**{test_prefix}{title}**"

    entries = [(c.code, _alert_block(c, golden_prefix=mixed)) for c in candidates]

    # Greedily pack entries into batches under Discord's 2000-char message
    # cap, counting each batch's own header (the first batch's is longer,
    # thanks to "@everyone " and the title) plus a "\n\n" joiner per entry.
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_len = 0
    for code, block in entries:
        header_len = discord_len(first_header if not batches else _CONTINUATION_HEADER)
        block_len = discord_len(block) + 2  # "\n\n" joining it to the header/prior block
        if current and header_len + current_len + block_len > _ALERT_CONTENT_LIMIT:
            batches.append(current)
            current = []
            current_len = 0
        current.append((code, block))
        current_len += block_len
    if current:
        batches.append(current)

    rendered = []
    for i, batch in enumerate(batches):
        header = first_header if i == 0 else _CONTINUATION_HEADER
        content = "\n\n".join([header, *(block for _, block in batch)])
        rendered.append(
            RenderedAlert(content=content, codes=[code for code, _ in batch], ping=ping and i == 0)
        )
    return rendered


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
    "RenderedAlert",
    "RenderedDigest",
    "esc",
    "render_code_alerts",
    "render_digest",
    "render_status",
    "render_story_page",
    "to_text",
]
