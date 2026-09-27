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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import discord

from newsbot.collectors.base import CollectorResult
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.normalize import canonicalize
from newsbot.pipeline.summarize import StoryDraft, TopicSummary, estimate_spend_usd
from newsbot.shift.decide import CodeCandidate, group_roundups
from newsbot.shift.match import is_code
from newsbot.store.models import AlertStatus, StatusSnapshot, StoryView, Usage

_DESCRIPTION_LIMIT = 4096
_TITLE_LIMIT = 256
_MAX_LINKS_SHOWN = 3
_MAX_FIELD_VALUE = 1024
# design.md §13, D1: a coverage note ("Brave search skipped: quota exceeded")
# now rides along in the footer of every topic embed that actually posts,
# since there's no shared header message left for it to live in. Capped well
# under an embed footer's real 2048-unit limit -- a footer is meant to be a
# quiet aside, not a second description.
_COVERAGE_FOOTER_LIMIT = 512
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
    # By the time this is called, render_digest has already decided this
    # topic has something to say (design.md §13: a topic with nothing posts
    # nothing, rather than an embed reading "No new stories today.") -- this
    # still handles an empty list defensively, since a caller outside
    # render_digest (a future one, or a test) shouldn't get a crash instead
    # of a sane-looking embed for the case that's genuinely rare now.
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


def _story_count(
    topic: Topic, summaries: dict[str, TopicSummary], fallback_items: dict[str, list[TopicItem]]
) -> int:
    summary = summaries.get(topic.key)
    if summary is None:
        return 0
    if summary.fallback:
        return len(fallback_items.get(topic.key, []))
    return len(summary.stories)


def _coverage_footer(coverage_notes: list[str]) -> str | None:
    """The footer text every posted topic embed carries today's coverage notes in (D1).

    v1 put these in the header, under a shared message every topic's
    embeds rode along with; v2 has no header left, so each embed that
    actually posts gets its own copy in the footer instead -- a reader
    looking at just the Palworld channel still gets to know Brave search
    was skipped today, without needing a digest-wide message that no
    longer exists to tell them.
    """
    if not coverage_notes:
        return None
    text = "Reduced coverage today: " + "; ".join(coverage_notes)
    return _truncate_utf16(esc(text), _COVERAGE_FOOTER_LIMIT, suffix="…")


@dataclass
class TopicMessage:
    """One topic's own digest post: one embed, to one channel (design.md §13).

    v1 packed every topic's embed under a shared header message in one
    channel; v2 gives each game its own channel and drops the header and
    the discussion thread entirely, so there's no longer anything to pack
    -- one topic, one embed, one message, one channel.
    """

    topic_key: str
    topic_name: str
    channel_id: int
    embed: discord.Embed


@dataclass
class RenderedDigest:
    run_date: date
    messages: list[TopicMessage]
    coverage_notes: list[str]


def render_digest(
    run_date: date,
    topics: list[Topic],
    summaries: dict[str, TopicSummary],
    fallback_items: dict[str, list[TopicItem]],
    coverage_notes: list[str],
) -> RenderedDigest:
    """Render one day's digest: one `TopicMessage` per topic that actually has something to say.

    A topic with no `TopicSummary` at all (nothing was collected for it
    today), an empty stories list, or a fallback with no items to list
    gets no message at all -- design.md §13's "nothing posted for a game
    with no news" (owner decision A). Topics post in config order,
    matching the order `topics` was handed in.
    """
    footer = _coverage_footer(coverage_notes)
    messages = []
    for topic in topics:
        summary = summaries.get(topic.key)
        if summary is not None and summary.fallback:
            items = fallback_items.get(topic.key, [])
            if not items:
                continue
            embed = _fallback_embed(topic, items, summary.note)
        else:
            stories = summary.stories if summary else []
            if not stories:
                continue
            embed = _topic_embed(topic, stories)
        if footer:
            embed.set_footer(text=footer)
        messages.append(
            TopicMessage(
                topic_key=topic.key, topic_name=topic.name, channel_id=topic.channel_id, embed=embed
            )
        )
    return RenderedDigest(run_date=run_date, messages=messages, coverage_notes=coverage_notes)


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


def _alerts_field_value(alerts: AlertStatus) -> str:
    """The `/newsbot status` "SHiFT alerts" field's value (plan step 10).

    `disabled` when the config block's off; otherwise the last sweep's
    time and summary (or "no sweep yet" before the first one has run),
    how many codes have ever posted, and today's ping spend against the
    cap -- with `(seeding)` appended while the marker's still unset, since
    "0 codes alerted, pings 0 of 3" reads very differently depending on
    whether that's "nothing's happened yet" or "we're deliberately
    staying quiet on purpose" (A1).
    """
    if not alerts.enabled:
        return "disabled"
    if alerts.last_sweep_at is not None:
        sweep_part = f"last sweep {alerts.last_sweep_at.isoformat()}"
        if alerts.last_sweep_summary:
            sweep_part += f" · {esc(alerts.last_sweep_summary)}"
    else:
        sweep_part = "no sweep yet"
    value = (
        f"{sweep_part} · {alerts.codes_alerted} codes alerted · "
        f"pings today {alerts.pings_today} of {alerts.max_pings}"
    )
    if not alerts.seeded:
        value += " (seeding)"
    if alerts.test_command_enabled:
        value += " · test command ENABLED"
    return _truncate_utf16(value, _MAX_FIELD_VALUE, suffix="…")


def render_status(
    snap: StatusSnapshot, spend_usd: float, alerts: AlertStatus | None = None
) -> discord.Embed:
    """Render `/newsbot status`: last run, source health, and the running spend estimate.

    `alerts` is optional so every existing caller (and every test that
    predates the SHiFT alert sweep) keeps seeing exactly the same embed --
    the "SHiFT alerts" field only appears when a caller actually has an
    `AlertStatus` to show, which `/newsbot status`'s handler always does
    in practice (design.md §12 wants this field shown even when the
    feature is off, so it computes one regardless of `cfg.alerts.enabled`).
    """
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

    if alerts is not None:
        embed.add_field(name="SHiFT alerts", value=_alerts_field_value(alerts), inline=False)

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
    # A deterministic id for this message, passed to `channel.send(nonce=...)`
    # (plan §1) so a retried send after a `PublishError` -- our own client
    # gave up waiting for a response, not necessarily proof the message
    # never landed -- can't turn into a second `@everyone` in the channel.
    # Discord dedupes two sends sharing a nonce within its own short
    # window; deriving it from `codes` and this message's position in the
    # batch (not from wall-clock time or a random value) is what makes a
    # retry of *this* message reuse the *same* nonce instead of minting a
    # fresh one that Discord has never seen before.
    nonce: str = ""


def _alert_title(candidates: list[CodeCandidate], *, plural: bool) -> str:
    suffix = "s" if plural else ""
    if all(c.golden for c in candidates):
        return f"New Golden Key code{suffix}"
    return f"New SHiFT code{suffix}"


def _alert_block(
    candidate: CodeCandidate, *, golden_prefix: bool, max_len: int | None = None
) -> str:
    # Checked, not just trusted: this is the one place a code goes out to
    # Discord, and a code that somehow isn't shaped like a code (a bug
    # upstream, not a real scenario) is worth a loud failure here rather
    # than a quietly wrong alert. A plain `assert` strips out under `-O`;
    # this doesn't get to.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    prefix = "Golden Key: " if golden_prefix and candidate.golden else ""
    code_block = f"```\n{candidate.code}\n```"
    safe_url = _safe_link(candidate.item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    block = f"{code_block}\n{prefix}{esc(candidate.source_name)}{link}"
    if max_len is None or discord_len(block) <= max_len:
        return block

    # Long enough that even a message holding this one entry alone would
    # bust Discord's 2000-unit cap (a hostile or just very long source
    # name plus a long collected URL, most likely) -- shed the link
    # first. The code itself is the whole point of the alert; the source
    # name is the next thing worth keeping (it's what a reader checks
    # against before trusting a code); the link is the part most likely
    # to already be redundant with "click the code, go to shift.gearbox
    # website" and the first thing worth losing.
    block = f"{code_block}\n{prefix}{esc(candidate.source_name)}"
    if discord_len(block) <= max_len:
        return block

    # Still too long -- hard-truncate the source name itself. The fenced
    # code block (```\n<code>\n```) and any golden-key prefix are fixed
    # and never truncated: a partial code would be actively wrong, not
    # just abbreviated.
    fixed_len = discord_len(code_block) + 1 + discord_len(prefix)  # +1 for the joining "\n"
    name_budget = max(max_len - fixed_len, 0)
    truncated_name = _truncate_utf16(esc(candidate.source_name), name_budget, suffix="…")
    return f"{code_block}\n{prefix}{truncated_name}"


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

    # The most room any one entry can ever count on: alone in its own
    # message, under whichever header is longer (always the first
    # message's, thanks to "@everyone " and the title, but the max is
    # cheap insurance against that assumption changing later) plus the
    # "\n\n" joining the header to the block.
    solo_budget = (
        _ALERT_CONTENT_LIMIT - max(discord_len(first_header), discord_len(_CONTINUATION_HEADER)) - 2
    )
    entries = []
    for c in candidates:
        block = _alert_block(c, golden_prefix=mixed)
        if discord_len(block) > solo_budget:
            block = _alert_block(c, golden_prefix=mixed, max_len=solo_budget)
        entries.append((c.code, block))

    # Greedily pack entries into batches under Discord's 2000-char message
    # cap, counting each batch's own header (the first batch's is longer,
    # thanks to "@everyone " and the title) plus a "\n\n" joiner per entry.
    # Every entry is already guaranteed to fit `solo_budget` on its own
    # (see above), so this loop only ever has to decide when to start a
    # *new* batch, never split one entry across two.
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
        # Belt-and-suspenders on the packing loop above: nothing should
        # ever reach here over the cap, but a RenderedAlert that snuck
        # past it would be silently rejected by Discord, losing a code
        # nobody would notice was lost -- worth a loud failure instead of
        # a plain `assert`, which strips out under `-O`.
        if discord_len(content) > _ALERT_CONTENT_LIMIT:
            raise ValueError(
                f"rendered alert content exceeds {_ALERT_CONTENT_LIMIT} UTF-16 units "
                f"({discord_len(content)}); codes: {[code for code, _ in batch]}"
            )
        batch_codes = [code for code, _ in batch]
        nonce = hashlib.sha256(f"{'|'.join(batch_codes)}|{i}".encode()).hexdigest()[:25]
        rendered.append(
            RenderedAlert(content=content, codes=batch_codes, ping=ping and i == 0, nonce=nonce)
        )
    return rendered


_ROUNDUP_HEADER_PREFIX = "**SHiFT codes from a roundup**"


def _roundup_header(source_name: str, item_url: str) -> str:
    safe_url = _safe_link(item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    return f"{_ROUNDUP_HEADER_PREFIX} · {esc(source_name)}{link}"


def _roundup_code_block(candidate: CodeCandidate) -> str:
    # No source/link per entry (the header already carries the one
    # source and link every code in this group shares) and no golden-key
    # prefix (design.md §13 doesn't ask for one here) -- just the code
    # itself, select-and-copy-able the same way a normal alert's code is.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    return f"```\n{candidate.code}\n```"


def render_roundup_alerts(candidates: list[CodeCandidate]) -> list[RenderedAlert]:
    """Render fresh roundup-only codes into unpinged "from a roundup" messages (design.md §13).

    v1 recorded every roundup-only code silently, forever; v2.0 posts the
    fresh ones instead (`shift/decide.py`'s `AlertPlan.roundup_to_post`),
    just without a ping and headed differently -- these are still "we're
    not confident enough in this to wake anyone up for it" codes, they're
    just not invisible anymore. `ping` is always `False` here (never
    `True`, not even conditionally); this function is never the place a
    future edit could accidentally reintroduce a second `@everyone` path.

    `group_roundups` splits `candidates` by the post they came from
    (`source_name`, `item_url`) -- design.md §13's "two roundup items ->
    two headers" -- and each group renders independently, packing its own
    codes into one or more messages under Discord's 2000-unit cap exactly
    like `render_code_alerts` does for a normal batch: the first message
    of a group carries that group's header, any continuation uses
    `_CONTINUATION_HEADER`, and no group's codes ever share a message with
    another group's (a header names one specific roundup post; mixing two
    posts' codes under one header would misattribute them).
    """
    if not candidates:
        return []

    rendered: list[RenderedAlert] = []
    for group in group_roundups(candidates):
        first_header = _roundup_header(group[0].source_name, group[0].item_url)
        solo_budget = (
            _ALERT_CONTENT_LIMIT
            - max(discord_len(first_header), discord_len(_CONTINUATION_HEADER))
            - 2
        )
        entries = [(c.code, _roundup_code_block(c)) for c in group]
        # Every code block is short and fixed-shape (a 29-character code
        # in a fenced block) -- nowhere near solo_budget in practice, but
        # this is the same loud failure `render_code_alerts` has for the
        # same "shouldn't be reachable, but 'shouldn't' isn't 'can't'"
        # reason.
        for code, block in entries:
            if discord_len(block) > solo_budget:
                raise ValueError(f"roundup code block for {code!r} exceeds the per-message budget")

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

        for i, batch in enumerate(batches):
            header = first_header if i == 0 else _CONTINUATION_HEADER
            content = "\n\n".join([header, *(block for _, block in batch)])
            if discord_len(content) > _ALERT_CONTENT_LIMIT:
                raise ValueError(
                    f"rendered roundup alert content exceeds {_ALERT_CONTENT_LIMIT} "
                    f"UTF-16 units ({discord_len(content)})"
                )
            batch_codes = [code for code, _ in batch]
            # Plan §5: sha256("roundup|" + codes + index) -- distinct from
            # render_code_alerts' own nonce scheme (no "roundup|" prefix)
            # so a normal and a roundup message for the same code (which
            # can't actually happen, once-per-code, but nonces are cheap
            # insurance) could never collide.
            nonce = hashlib.sha256(f"roundup|{'|'.join(batch_codes)}|{i}".encode()).hexdigest()[:25]
            rendered.append(
                RenderedAlert(content=content, codes=batch_codes, ping=False, nonce=nonce)
            )
    return rendered


# --- Admin-channel run reports (design.md §6, §8) ---
#
# One plain-text message to the admin channel after every POST run that
# actually posts -- scheduled, catch-up, or /newsbot run-now. Failed and
# skipped runs keep their existing detailed alerts (pipeline/run.py) and
# get no report; a preview never reports at all. Plain text, not an embed,
# for the same reason the header is plain text: nobody needs a colored
# sidebar to read "it worked."

_REPORT_HEADER_EMOJI = {"ok": "✅", "partial": "⚠️"}
_REPORT_HEADER_VERB = {"ok": "Digest posted", "partial": "Digest posted with gaps"}
# How many failed/skipped sources get spelled out by name before the report
# just says "+N more" -- three was picked as "enough to see a pattern
# (every Reddit source timed out) without the report turning into its own
# source_health dump."
_MAX_REPORT_SOURCES_SHOWN = 3
# Each failed/skipped source's own first-error-line snippet, so one source
# with a paragraph-long traceback in `error` can't eat the whole report.
_MAX_REPORT_ERROR_SNIPPET = 60


def _report_date(run_date: date) -> str:
    # "Sat Sep 27", not strftime's platform-dependent %-d/%e for the
    # unpadded day -- Linux and macOS both accept %-d, but there's no
    # reason to bet a plain-text status line on a glibc quirk.
    return f"{run_date.strftime('%a %b')} {run_date.day}"


def _report_jump_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _report_story_counts_line(
    topics: list[Topic],
    summaries: dict[str, TopicSummary],
    fallback_items: dict[str, list[TopicItem]],
    guild_id: int,
    posted_by_topic: Mapping[str, int],
    *,
    with_links: bool = True,
) -> str:
    """The run report's "N stories: ..." line, with a `[jump]` link per topic that posted.

    design.md §13: each game now has its own channel and its own message,
    so the one jump link v1's header carried becomes one *per topic* --
    `posted_by_topic` (topic_key -> the message id `DiscordPublisher.publish`
    actually got back) is empty for a topic that had nothing to post, and
    `with_links=False` is `render_run_report`'s own shedding step when the
    whole report doesn't fit under the cap otherwise.
    """
    parts = []
    total = 0
    for topic in topics:
        count = _story_count(topic, summaries, fallback_items)
        total += count
        part = f"{esc(topic.name)} {count}"
        message_id = posted_by_topic.get(topic.key) if with_links else None
        if message_id is not None:
            link = _report_jump_link(guild_id, topic.channel_id, message_id)
            part += f" [jump](<{link}>)"
        parts.append(part)
    return f"{total} stories: " + " · ".join(parts)


def _report_sources_line(results: list[CollectorResult]) -> str:
    total = len(results)
    bad = [(r.source_name, r.error or r.skipped or "") for r in results if r.error or r.skipped]
    ok = total - len(bad)
    line = f"Sources: {ok} of {total} ok"
    if not bad:
        return line

    shown = bad[:_MAX_REPORT_SOURCES_SHOWN]
    parts = []
    for name, message in shown:
        first_line = message.splitlines()[0] if message else "unknown error"
        snippet = _truncate_utf16(esc(first_line), _MAX_REPORT_ERROR_SNIPPET, suffix="…")
        parts.append(f"{esc(name)}: {snippet}")
    extra = len(bad) - len(shown)
    if extra > 0:
        parts.append(f"+{extra} more")
    return line + " (" + "; ".join(parts) + ")"


def _report_cost_str(spend_usd: float) -> str:
    # "<$0.01" for anything that'd otherwise round to "$0.00" -- a
    # summarization call that costs half a cent still cost something, and
    # "$0.00" reads as free, which it isn't.
    if spend_usd < 0.01:
        return "<$0.01"
    return f"~${spend_usd:.2f}"


def _report_duration_str(duration: timedelta) -> str:
    total_seconds = max(int(round(duration.total_seconds())), 0)
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _report_cost_line(usage: Usage, duration: timedelta) -> str:
    spend = estimate_spend_usd(usage.input_tokens, usage.output_tokens)
    return f"Claude: {_report_cost_str(spend)} · took {_report_duration_str(duration)}"


def render_run_report(
    *,
    status: Literal["ok", "partial"],
    run_date: date,
    run_kind: Literal["scheduled", "catch-up", "run-now"],
    topics: list[Topic],
    summaries: dict[str, TopicSummary],
    fallback_items: dict[str, list[TopicItem]],
    results: list[CollectorResult],
    usage: Usage,
    duration: timedelta,
    notes: list[str],
    guild_id: int,
    posted_by_topic: Mapping[str, int],
) -> str:
    """Render the admin-channel run report (design.md §6, §13): one plain-text message.

    `results` is *this run's* collector results, not the cumulative
    `source_health` table -- an admin reading this wants to know what just
    happened, not the all-time record. `notes` is the same coverage-note
    list `render_digest`'s footer and `save_run`'s `error_notes` already
    use; it only shows up here as a "Notes: ..." line when `status` is
    `partial` and there's actually something to say. `posted_by_topic` is
    `DiscordPublisher.publish`'s own return value: topic key -> the message
    id that topic's embed actually landed with, missing for any topic that
    had nothing to post -- that's what lets the stories line's `[jump]`
    links point at the right message in the right channel per game
    (design.md §13), instead of v1's one link to a header that no longer
    exists.

    Kept under `_ALERT_CONTENT_LIMIT` (Discord's plain-message cap, the
    same 2000 UTF-16 units the SHiFT alert messages respect) by shedding
    detail in priority order if it doesn't fit: the notes line first, then
    the per-source failure detail, then the per-topic jump links, then --
    a case that shouldn't be reachable given how short every other line
    is -- a flat truncation of the whole thing.
    """
    header_line = (
        f"{_REPORT_HEADER_EMOJI[status]} **{_REPORT_HEADER_VERB[status]}** · "
        f"{_report_date(run_date)} ({run_kind})"
    )
    story_line = _report_story_counts_line(
        topics, summaries, fallback_items, guild_id, posted_by_topic
    )
    sources_line = _report_sources_line(results)
    cost_line = _report_cost_line(usage, duration)

    notes_line = None
    if status == "partial" and notes:
        notes_line = "Notes: " + esc("; ".join(notes))

    lines = [header_line, story_line, sources_line]
    if notes_line:
        lines.append(notes_line)
    lines.append(cost_line)
    content = "\n".join(lines)
    if discord_len(content) <= _ALERT_CONTENT_LIMIT:
        return content

    if notes_line:
        content = "\n".join([header_line, story_line, sources_line, cost_line])
        if discord_len(content) <= _ALERT_CONTENT_LIMIT:
            return content

    bare_sources_line = sources_line.split(" (", 1)[0]
    content = "\n".join([header_line, story_line, bare_sources_line, cost_line])
    if discord_len(content) <= _ALERT_CONTENT_LIMIT:
        return content

    story_line_no_links = _report_story_counts_line(
        topics, summaries, fallback_items, guild_id, posted_by_topic, with_links=False
    )
    content = "\n".join([header_line, story_line_no_links, bare_sources_line, cost_line])
    if discord_len(content) <= _ALERT_CONTENT_LIMIT:
        return content

    return _truncate_utf16(content, _ALERT_CONTENT_LIMIT, suffix="…")


def to_text(r: RenderedDigest) -> str:
    """Render a `RenderedDigest` as plain text, for the CLI's `PrintPublisher`.

    Nobody's Discord client is involved in `--dry-run`, so each topic's
    embed gets flattened into something readable on a terminal instead,
    headed by which channel it would have gone to (there's no header
    message left to print ahead of it).
    """
    if not r.messages:
        return "Nothing would post today: no game has news."
    parts = []
    for message in r.messages:
        embed = message.embed
        parts.append(f"--- #{message.channel_id} · {embed.title or ''} ---")
        if embed.description:
            parts.append(str(embed.description))
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        if embed.footer and embed.footer.text:
            parts.append(f"({embed.footer.text})")
        parts.append("")
    return "\n".join(parts).rstrip()


__all__ = [
    "RenderedAlert",
    "RenderedDigest",
    "TopicMessage",
    "esc",
    "render_code_alerts",
    "render_digest",
    "render_roundup_alerts",
    "render_run_report",
    "render_status",
    "render_story_page",
    "to_text",
]
