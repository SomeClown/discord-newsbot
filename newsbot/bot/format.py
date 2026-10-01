"""Turn a day's stories into `discord.Embed` objects, and pack them under Discord's limits.

This module only builds data; it never talks to the gateway (a plain
`discord.Embed` is just a fancy dict, so nothing here needs a bot token or
a running event loop). That's on purpose: it's the only way to unit-test
"does a 41st story get cut, and does the '+N more' line say the right
number" without standing up a Discord connection, which is a genuinely
bad way to find out you're off by one.

Everything user-facing that started life as scraped text goes through
`esc()` before it reaches an embed. URLs never do: they go straight into
link targets (`<url>`, which suppresses Discord's preview embed instead of
letting six of them stack up under one story), and only ever come from
`StoryDraft.item_urls`, which `pipeline/summarize.py` has already checked
against what we actually collected. A headline can lie about the news; it
should not get to lie about where you're clicking.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import discord

from newsbot.config import GameInfo, Topic
from newsbot.pipeline.normalize import canonicalize
from newsbot.pipeline.summarize import StoryDraft
from newsbot.shift.decide import CodeCandidate, group_roundups
from newsbot.shift.match import is_code
from newsbot.store.models import (
    CodeView,
    HeadlineItem,
    ItemView,
    Notice,
    SourceHealthRow,
    StoryView,
)
from newsbot.text import plain_line, shown_url

_DESCRIPTION_LIMIT = 4096
_TITLE_LIMIT = 256
_MAX_LINKS_SHOWN = 3
_MAX_FIELD_VALUE = 1024
# design.md §13, D1: a coverage note ("Brave search skipped: quota exceeded")
# now rides along in the footer of every topic embed that actually posts,
# since there's no shared header message left for it to live in. Capped well
# under an embed footer's real 2048-unit limit: a footer is meant to be a
# quiet aside, not a second description.
_COVERAGE_FOOTER_LIMIT = 512
# Discord's plain-message content cap (as opposed to an embed's much bigger
# limits above): code alerts are plain messages, not embeds, since a code
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
    are UTF-16 code units, and most emoji, plus a good chunk of CJK
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
    an astral character (2 units) is always kept whole or dropped whole:
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


def _defuse_mentions_and_links(s: str) -> str:
    """Same mention/channel-link/invite/url-scheme defusing as `esc`, minus markdown escaping.

    For text headed into an embed *footer*: Discord doesn't render
    markdown there at all, so `escape_markdown`'s backslashes would just
    show up as literal backslashes instead of escaping anything: this
    keeps the actual safety (no live @everyone, no live link) without
    adding punctuation nobody asked for.
    """
    escaped = discord.utils.escape_mentions(s)
    escaped = _LINKY_MENTION_RE.sub("<​", escaped)
    escaped = _URL_SCHEME_RE.sub(lambda m: f"{m.group(1)}:​", escaped)
    return _BARE_INVITE_RE.sub(lambda m: f"{m.group(1)}​/", escaped)


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
    # which is fine: nothing here claims to break them meaningfully.
    return (_LABEL_ORDER[story.label], -len(story.item_urls))


def _safe_link(url: str) -> str | None:
    """Re-run `url` through `canonicalize()` immediately before it's wrapped in `<...>`.

    Every URL that reaches here should already be canonical (item_urls
    via `pipeline.summarize.postprocess`, fallback items via
    `pipeline.normalize.normalize()` at collection time), but "should
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
    # it into a preview card": without that, three or four stacked link
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


def _topic_embed(topic: GameInfo, stories: list[StoryDraft]) -> discord.Embed:
    # By the time this is called, render_guild_digest has already decided this
    # topic has something to say (design.md §13: a topic with nothing posts
    # nothing, rather than an embed reading "No new stories today."): this
    # still handles an empty list defensively, since a caller outside
    # render_guild_digest (a future one, or a test) shouldn't get a crash instead
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
        # Not even the single most important story fits on its own:
        # a pathologically long summary, in practice never (summaries are
        # capped at 400 chars by the schema). Hard-truncate rather than
        # emit an empty embed.
        description = _truncate_description(blocks[0])

    return discord.Embed(title=title, description=description, color=color)


def _coverage_footer(coverage_notes: list[str]) -> str | None:
    """The footer text every posted topic embed carries today's coverage notes in (D1).

    v1 put these in the header, under a shared message every topic's
    embeds rode along with; v2 has no header left, so each embed that
    actually posts gets its own copy in the footer instead: a reader
    looking at just the Palworld channel still gets to know Brave search
    was skipped today, without needing a digest-wide message that no
    longer exists to tell them.
    """
    if not coverage_notes:
        return None
    text = "Reduced coverage today: " + "; ".join(coverage_notes)
    return _truncate_utf16(_defuse_mentions_and_links(text), _COVERAGE_FOOTER_LIMIT, suffix="…")


@dataclass
class TopicMessage:
    """One topic's own digest post: one embed, to one channel (design.md §13).

    v1 packed every topic's embed under a shared header message in one
    channel; v2 gives each game its own channel and drops the header and
    the discussion thread entirely, so there's no longer anything to pack:
    one topic, one embed, one message, one channel.
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


# --- /shift codes (design.md §13) ---

_CODE_PAGE_MAX_SOURCE = 100
_CODE_PAGE_FOOTER_NOTE = "I don't know when codes expire; older ones may have stopped working."
_CODE_PAGE_EMPTY = "No codes seen in that window."


def _code_marker(view: CodeView) -> str | None:
    """The D5 marker for one code, or None for a code that just... posted normally.

    Checked in this order on purpose: `from_roundup` wins over `status`
    (a roundup code that overflowed the cap is `status='roundup'`, not
    `'posted'`, but it's still "from a roundup" to a member reading this,
    not some fourth unexplained state): see `record_silent_codes`'s own
    docstring for why `from_roundup` is a separate column instead of being
    derived from `status`.
    """
    if view.from_roundup:
        return "from a roundup"
    if view.status == "too_old":
        return "old post"
    if view.status == "seeded":
        return "already around when alerts started"
    return None


def _code_page_block(view: CodeView, timezone: str, *, max_len: int | None = None) -> str:
    code_block = f"```\n{view.code}\n```"
    first_seen = view.first_seen_at.astimezone(ZoneInfo(timezone)).date().isoformat()
    seen_prefix = f"First seen {first_seen} · "
    marker = _code_marker(view)
    marker_suffix = f" · ({marker})" if marker else ""
    source = _truncate_utf16(esc(view.source_name), _CODE_PAGE_MAX_SOURCE, suffix="…")
    safe_url = _safe_link(view.item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    block = f"{code_block}\n{seen_prefix}{source}{link}{marker_suffix}"
    if max_len is None or discord_len(block) <= max_len:
        return block

    # Same shedding order as `_alert_block`: the link goes first (a
    # collected URL is the part most likely to be long and least likely
    # to be missed; the code and its source are the point).
    block = f"{code_block}\n{seen_prefix}{source}{marker_suffix}"
    if discord_len(block) <= max_len:
        return block

    # Still too long: hard-truncate the source name. The fenced code
    # block never shrinks; a partial code would be actively wrong, and
    # cutting mid-fence would unbalance every block after it.
    fixed_len = discord_len(code_block) + 1 + discord_len(seen_prefix) + discord_len(marker_suffix)
    name_budget = max(max_len - fixed_len, 0)
    truncated_name = _truncate_utf16(esc(view.source_name), name_budget, suffix="…")
    return f"{code_block}\n{seen_prefix}{truncated_name}{marker_suffix}"


def render_code_page(
    codes: list[CodeView], *, title: str, page: int, pages: int, timezone: str
) -> discord.Embed:
    """Render one page of `/shift codes`: every known code, newest-first, in copyable blocks.

    `timezone` is `cfg.digest.timezone`: the same local calendar the
    daily digest and the ping cap already reason in, so "first seen" reads
    against the clock a member already expects everything else in this
    bot to use, not a UTC date nobody configured. `_code_marker` is what
    turns `from_roundup`/`status` into D5's three markers; a plain
    `'posted'`, non-roundup code gets none, since "posted normally" isn't
    something a reader needs flagged.

    Every entry gets an equal share of the description budget up front
    (QA follow-up: hard-truncating the whole joined description used to
    silently drop entries near the end of a page, and could cut a fenced
    code block in half). `_code_page_block`'s own shrinking (drop the
    link, then truncate the source) only kicks in for an entry that
    actually needs it; a normal-length one is untouched.
    """
    embed = discord.Embed(title=_truncate_utf16(esc(title), _TITLE_LIMIT), color=_PALETTE[0])
    if not codes:
        embed.description = _CODE_PAGE_EMPTY
    else:
        joiner_overhead = 2 * (len(codes) - 1)  # "\n\n" between entries
        per_entry_budget = max((_DESCRIPTION_LIMIT - joiner_overhead) // len(codes), 0)
        blocks = [_code_page_block(c, timezone, max_len=per_entry_budget) for c in codes]
        description = "\n\n".join(blocks)
        # Belt-and-suspenders, same spirit as `render_code_alerts`' own
        # loud failure: nothing should reach here over the cap, since
        # every block above was built to fit its own share of it.
        if discord_len(description) > _DESCRIPTION_LIMIT:
            raise ValueError(
                f"rendered code page exceeds {_DESCRIPTION_LIMIT} UTF-16 units "
                f"({discord_len(description)})"
            )
        embed.description = description
    embed.set_footer(text=f"Page {page} of {pages} · {_CODE_PAGE_FOOTER_NOTE}")
    return embed


@dataclass
class RenderedAlert:
    """One Discord message's worth of a SHiFT code alert batch.

    `codes` is that message's own slice of the codes it announces: when a
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
    # (plan §1) so a retried send after a `PublishError` (our own client
    # gave up waiting for a response, not necessarily proof the message
    # never landed) can't turn into a second `@everyone` in the channel.
    # Discord dedupes two sends sharing a nonce within its own short
    # window; deriving it from `codes` and this message's position in the
    # batch (not from wall-clock time or a random value) is what makes a
    # retry of *this* message reuse the *same* nonce instead of minting a
    # fresh one that Discord has never seen before.
    nonce: str = ""
    # The exact text a ping put in front of the header ("@everyone " or
    # "<@&123> "), empty when nothing pinged. `shift/sweep.py`'s retry strips
    # precisely this, so a role ping comes off as cleanly as @everyone does
    # (a retry must never risk a second live ping of either kind).
    ping_prefix: str = ""


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
    # name plus a long collected URL, most likely): shed the link
    # first. The code itself is the whole point of the alert; the source
    # name is the next thing worth keeping (it's what a reader checks
    # against before trusting a code); the link is the part most likely
    # to already be redundant with "click the code, go to shift.gearbox
    # website" and the first thing worth losing.
    block = f"{code_block}\n{prefix}{esc(candidate.source_name)}"
    if discord_len(block) <= max_len:
        return block

    # Still too long: hard-truncate the source name itself. The fenced
    # code block (```\n<code>\n```) and any golden-key prefix are fixed
    # and never truncated: a partial code would be actively wrong, not
    # just abbreviated.
    fixed_len = discord_len(code_block) + 1 + discord_len(prefix)  # +1 for the joining "\n"
    name_budget = max(max_len - fixed_len, 0)
    truncated_name = _truncate_utf16(esc(candidate.source_name), name_budget, suffix="…")
    return f"{code_block}\n{prefix}{truncated_name}"


def _nonce_salt(scope: str) -> str:
    """The text a nonce hash starts with: nothing for no scope, else the scope and a bar."""
    return f"{scope}|" if scope else ""


def render_code_alerts(
    candidates: list[CodeCandidate],
    *,
    ping: bool,
    ping_mention: str | None = None,
    test: bool = False,
    nonce_scope: str = "",
) -> list[RenderedAlert]:
    """Render a batch of new SHiFT codes into one or more alert messages.

    One ping covers the whole batch (A4): only the first message's header
    carries `@everyone` (and only if `ping` is true to begin with); every
    continuation starts with `_CONTINUATION_HEADER` instead and never
    pings, no matter how many messages the batch spills into. Mixed
    golden/non-golden batches (A3) keep the plain "New SHiFT code(s)"
    title but prefix each golden entry with "Golden Key:" so it doesn't
    read as an ordinary code.

    `ping_mention` is the text a ping starts with: `"@everyone"` (the
    default, which is all v2 ever used) or a role mention like `"<@&123>"`
    for a server that picked a role. It only matters when `ping` is true.

    `nonce_scope` salts every message's nonce (the fan-out passes the server
    and channel ids). Without it, two servers getting the same batch would
    send the same nonce, and Discord may hand the second one the first one's
    message back instead of posting. Same scope, same nonces: a retry to the
    same channel still reuses its own.
    """
    if not candidates:
        return []

    ping_prefix = f"{ping_mention or '@everyone'} " if ping else ""
    mixed = any(c.golden for c in candidates) and not all(c.golden for c in candidates)
    title = _alert_title(candidates, plural=len(candidates) > 1)
    test_prefix = "[TEST] " if test else ""
    first_header = ping_prefix + f"**{test_prefix}{title}**"

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
        # nobody would notice was lost: worth a loud failure instead of
        # a plain `assert`, which strips out under `-O`.
        if discord_len(content) > _ALERT_CONTENT_LIMIT:
            raise ValueError(
                f"rendered alert content exceeds {_ALERT_CONTENT_LIMIT} UTF-16 units "
                f"({discord_len(content)}); codes: {[code for code, _ in batch]}"
            )
        batch_codes = [code for code, _ in batch]
        nonce = hashlib.sha256(
            f"{_nonce_salt(nonce_scope)}{'|'.join(batch_codes)}|{i}".encode()
        ).hexdigest()[:25]
        rendered.append(
            RenderedAlert(
                content=content,
                codes=batch_codes,
                ping=ping and i == 0,
                nonce=nonce,
                ping_prefix=ping_prefix if i == 0 else "",
            )
        )
    return rendered


_ROUNDUP_HEADER_PREFIX = "**SHiFT codes from a roundup**"


def _roundup_header(source_name: str, item_url: str, *, max_len: int | None = None) -> str:
    safe_url = _safe_link(item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    header = f"{_ROUNDUP_HEADER_PREFIX} · {esc(source_name)}{link}"
    if max_len is None or discord_len(header) <= max_len:
        return header

    # Same shedding order as `_alert_block`: the link is the first thing
    # to go (it's redundant with "click the code" anyway), and if a
    # hostile or just very long source name still doesn't fit, hard-
    # truncate it. The `_ROUNDUP_HEADER_PREFIX` itself never shrinks;
    # it's what tells a reader this code didn't come with a ping.
    header = f"{_ROUNDUP_HEADER_PREFIX} · {esc(source_name)}"
    if discord_len(header) <= max_len:
        return header

    fixed_len = discord_len(_ROUNDUP_HEADER_PREFIX) + 3  # " · " joining prefix to the name
    name_budget = max(max_len - fixed_len, 0)
    truncated_name = _truncate_utf16(esc(source_name), name_budget, suffix="…")
    return f"{_ROUNDUP_HEADER_PREFIX} · {truncated_name}"


def _roundup_code_block(candidate: CodeCandidate) -> str:
    # No source/link per entry (the header already carries the one
    # source and link every code in this group shares) and no golden-key
    # prefix (design.md §13 doesn't ask for one here): just the code
    # itself, select-and-copy-able the same way a normal alert's code is.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    return f"```\n{candidate.code}\n```"


def render_roundup_alerts(
    candidates: list[CodeCandidate], *, nonce_scope: str = ""
) -> list[RenderedAlert]:
    """Render fresh roundup-only codes into unpinged "from a roundup" messages (design.md §13).

    v1 recorded every roundup-only code silently, forever; v2.0 posts the
    fresh ones instead (`shift/decide.py`'s `AlertPlan.roundup_to_post`),
    just without a ping and headed differently: these are still "we're
    not confident enough in this to wake anyone up for it" codes, they're
    just not invisible anymore. `ping` is always `False` here (never
    `True`, not even conditionally); this function is never the place a
    future edit could accidentally reintroduce a second `@everyone` path.

    `group_roundups` splits `candidates` by the post they came from
    (`source_name`, `item_url`), matching design.md §13's "two roundup
    items -> two headers", and each group renders independently, packing its own
    codes into one or more messages under Discord's 2000-unit cap exactly
    like `render_code_alerts` does for a normal batch: the first message
    of a group carries that group's header, any continuation uses
    `_CONTINUATION_HEADER`, and no group's codes ever share a message with
    another group's (a header names one specific roundup post; mixing two
    posts' codes under one header would misattribute them).

    `nonce_scope` salts the nonces per server and channel; see `render_code_alerts`.
    """
    if not candidates:
        return []

    rendered: list[RenderedAlert] = []
    for group in group_roundups(candidates):
        entries = [(c.code, _roundup_code_block(c)) for c in group]
        # Every code block is short and fixed-shape (a 29-character code
        # in a fenced block), so it's always the header, not the block,
        # that's at risk of busting the cap (a ~2000-char URL, a hostile
        # source name). Give `_roundup_header` a budget that guarantees it
        # fits alongside this group's first block before measuring anything
        # else, instead of discovering the overflow after the fact.
        first_block_len = discord_len(entries[0][1]) + 2  # "\n\n" joining header to block
        first_header = _roundup_header(
            group[0].source_name,
            group[0].item_url,
            max_len=_ALERT_CONTENT_LIMIT - first_block_len,
        )
        solo_budget = (
            _ALERT_CONTENT_LIMIT
            - max(discord_len(first_header), discord_len(_CONTINUATION_HEADER))
            - 2
        )
        # This is now a belt-and-suspenders check, not the mechanism that
        # keeps things under budget: `_roundup_header`'s own shrinking
        # already guarantees the first block fits under `first_header`;
        # this still catches a code block busting the *continuation*
        # header's budget, which never shrinks.
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
            # Plan §5: sha256("roundup|" + codes + index): distinct from
            # render_code_alerts' own nonce scheme (no "roundup|" prefix)
            # so a normal and a roundup message for the same code (which
            # can't actually happen, once-per-code, but nonces are cheap
            # insurance) could never collide.
            nonce = hashlib.sha256(
                f"{_nonce_salt(nonce_scope)}roundup|{'|'.join(batch_codes)}|{i}".encode()
            ).hexdigest()[:25]
            rendered.append(
                RenderedAlert(content=content, codes=batch_codes, ping=False, nonce=nonce)
            )
    return rendered


# One follow-up message names at most this many codes; the rest wait for the
# next pass. A confirmation burst that big has never happened, and 20 keeps
# the whole message a short line instead of a wall.
FOLLOWUP_MAX_CODES = 20


def render_followup_alert(
    codes: list[str], *, ping_mention: str, nonce_scope: str = ""
) -> RenderedAlert:
    """The "confirmed by a second source" follow-up (plan D14): one short, pinged message.

    A community code that already posted unpinged just got a second,
    independent source. This is the nudge: the ping first, then the codes in
    backticks (the original alert already has the copy-able block; this one
    only needs to say which). `ping_mention` is the text a server's ping choice
    turns into (`@everyone` or a role mention), and the allowed-mentions that
    make it actually notify come from `mentions_for` at send time, as for every
    other alert. The nonce is salted with a `followup|` marker as well as the
    server scope, so Discord can't mistake this for the original alert's send.
    """
    if not codes or len(codes) > FOLLOWUP_MAX_CODES:
        raise ValueError(f"a follow-up names 1 to {FOLLOWUP_MAX_CODES} codes, got {len(codes)}")
    for code in codes:
        if not is_code(code):
            raise ValueError(f"not a SHiFT code: {code!r}")
    ping_prefix = f"{ping_mention} "
    named = ", ".join(f"`{code}`" for code in codes)
    nonce = hashlib.sha256(
        f"{_nonce_salt(nonce_scope)}followup|{'|'.join(codes)}".encode()
    ).hexdigest()[:25]
    return RenderedAlert(
        content=f"{ping_prefix}Confirmed by a second source: {named}",
        codes=list(codes),
        ping=True,
        nonce=nonce,
        ping_prefix=ping_prefix,
    )


# --- Admin-channel run reports (design.md §6, §8) ---
#
# One plain-text message to the admin channel after every POST run that
# actually posts: scheduled, catch-up, or /newsbot run-now. Failed and
# skipped runs keep their existing detailed alerts (pipeline/run.py) and
# get no report; a preview never reports at all. Plain text, not an embed,
# for the same reason the header is plain text: nobody needs a colored
# sidebar to read "it worked."

_HEADLINE_TRUST_RANK = {"official": 0, "press": 1, "community": 2}
_OFFICIAL_MARKER = "🟢 OFFICIAL"


def _headline_sort_key(item: HeadlineItem) -> tuple[int, int, float]:
    # design.md §15, owner decision D4: official, then press, then community;
    # within a trust level, confident matches before entity-only ones; then
    # newest first. (The published time if the source gave one, else when we
    # collected it: an undated item is as new as the day we found it.)
    moment = item.published_at or item.collected_at
    return (
        _HEADLINE_TRUST_RANK.get(item.trust, len(_HEADLINE_TRUST_RANK)),
        int(item.uncertain),
        -moment.timestamp(),
    )


# One headline is one line, and one line is allowed only so much of the embed. A
# title is flattened and cut to `_MAX_HEADLINE_TITLE` UTF-16 units (before
# escaping, which can nearly double it); a URL longer than `_MAX_HEADLINE_URL` is
# dropped rather than cut, since half a URL is a link to somewhere else. (The URL
# cap is generous on purpose: a non-ASCII path is percent-encoded at up to twelve
# characters per character.) With both caps one line tops out under 1,700 units, so
# no single item can crowd the other headlines, or the "+N more" line, out of 4096.
_MAX_HEADLINE_TITLE = 300
_MAX_HEADLINE_URL = 1000


def _headline_line(item: HeadlineItem) -> str | None:
    safe_url = _safe_link(item.url)
    if safe_url is None or discord_len(safe_url) > _MAX_HEADLINE_URL:
        return None
    # Titles come from feeds and social posts, and a Bluesky post has line
    # breaks. Left alone, one could start its own line in this list and pose as
    # an official headline, or as the renderer's own "+N more". split() with no
    # argument breaks on every kind of whitespace, so this flattens all of it.
    title = _truncate_utf16(" ".join(item.title.split()), _MAX_HEADLINE_TITLE, suffix="…")
    marker = f"{_OFFICIAL_MARKER} · " if item.trust == "official" else ""
    return f"• {marker}{esc(title)} — <{safe_url}>"


def render_headlines_embed(
    game: GameInfo, items: Sequence[HeadlineItem], note: str | None = None
) -> discord.Embed | None:
    """One game's headline list, for a free server (and for a comped one's fallback).

    Ordered by D4 (see `_headline_sort_key`), with a `🟢 OFFICIAL` marker on official
    items only; a community item gets no "rumor" label, since a headline list makes no
    claim about whether it's true. Titles are flattened to one line and capped, then go
    through `esc()`; URLs go through `_safe_link` and a URL that's too long is dropped
    like an unusable one (see `_headline_line`). When the list doesn't fit under the
    4096-unit description limit, whole lines are shed from the bottom and the last line
    says `+N more, use /news`. Returns `None` when there's nothing to list (no items, or
    none with a usable URL): a game with nothing posts nothing (design.md §13), so the
    caller skips it rather than posting "No new stories today."

    `note` (the comped fallback's "Summary unavailable" line) goes on top and isn't
    counted in the "+N more".
    """
    lines = [
        line for item in sorted(items, key=_headline_sort_key) if (line := _headline_line(item))
    ]
    if not lines:
        return None
    head = [esc(note)] if note else []
    kept = len(lines)
    while kept > 0:
        cut = len(lines) - kept
        description = "\n".join([*head, *lines[:kept]])
        if cut:
            description += f"\n\n+{cut} more, use /news"
        if discord_len(description) <= _DESCRIPTION_LIMIT:
            break
        kept -= 1
    else:
        # One line longer than the whole embed is allowed to be: a title the
        # size of a short story. Hard-truncate rather than post nothing.
        description = _truncate_description("\n".join([*head, lines[0]]))
    return discord.Embed(
        title=_truncate_utf16(esc(game.name), _TITLE_LIMIT),
        description=description,
        color=_topic_color(game.key),
    )


def render_guild_digest(
    run_date: date,
    games: Sequence[GameInfo],
    channels: Mapping[str, int],
    *,
    stories_by_game: Mapping[str, list[StoryDraft]],
    items_by_game: Mapping[str, Sequence[HeadlineItem]],
    notes_by_game: Mapping[str, str | None],
    coverage_notes: list[str],
) -> RenderedDigest:
    """One server's digest: a `TopicMessage` for each followed game that has something to say.

    `games` are the server's followed games in catalog order and `channels` maps each
    game key to its channel. A game whose key is in `stories_by_game` has a stored
    summary (comped): its stories are rendered as v2's embed, and an empty list means
    the summary found nothing, so nothing posts. Every other game gets its headlines
    from `items_by_game`, with `notes_by_game[key]` on top if there is one. A game with
    nothing to show is left out.
    """
    footer = _coverage_footer(coverage_notes)
    messages = []
    for game in games:
        if game.key in stories_by_game:
            stories = stories_by_game[game.key]
            embed = _topic_embed(game, stories) if stories else None
        else:
            embed = render_headlines_embed(
                game, items_by_game.get(game.key, []), notes_by_game.get(game.key)
            )
        if embed is None:
            continue
        if footer:
            embed.set_footer(text=footer)
        messages.append(
            TopicMessage(
                topic_key=game.key,
                topic_name=game.name,
                channel_id=channels[game.key],
                embed=embed,
            )
        )
    return RenderedDigest(run_date=run_date, messages=messages, coverage_notes=coverage_notes)


_REPORT_HEADER_EMOJI = {"ok": "✅", "partial": "⚠️"}
_REPORT_HEADER_VERB = {"ok": "Digest posted", "partial": "Digest posted with gaps"}


def _report_date(run_date: date) -> str:
    # "Sat Sep 27", not strftime's platform-dependent %-d/%e for the
    # unpadded day: Linux and macOS both accept %-d, but there's no
    # reason to bet a plain-text status line on a glibc quirk.
    return f"{run_date.strftime('%a %b')} {run_date.day}"


def _report_jump_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _report_duration_str(duration: timedelta) -> str:
    total_seconds = max(int(round(duration.total_seconds())), 0)
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def render_guild_run_report(
    *,
    status: Literal["ok", "partial"],
    run_date: date,
    run_kind: Literal["scheduled", "catch-up", "run-now"],
    games: Sequence[GameInfo],
    counts: Mapping[str, int],
    channels: Mapping[str, int],
    posted_by_game: Mapping[str, int],
    skipped: Mapping[str, str],
    duration: timedelta,
    notes: list[str],
    guild_id: int,
) -> str:
    """The per-server run report, for that server's admin channel (plan §3.5): one plain message.

    Like the owner's report, minus everything a server has no business seeing: no
    source health (that's the owner's) and no spend. It has a per-game count with a
    `[jump]` link for each game that posted, any skipped game with its reason, and how
    long the digest took. Kept under `_ALERT_CONTENT_LIMIT` by shedding the reasons,
    then the links, then a flat truncation.
    """

    def stories_line(with_links: bool) -> str:
        parts = []
        for game in games:
            part = f"{esc(game.name)} {counts.get(game.key, 0)}"
            message_id = posted_by_game.get(game.key) if with_links else None
            if message_id is not None:
                link = _report_jump_link(guild_id, channels[game.key], message_id)
                part += f" [jump](<{link}>)"
            parts.append(part)
        return f"{sum(counts.get(g.key, 0) for g in games)} items: " + " · ".join(parts)

    def skipped_line(with_reasons: bool) -> str | None:
        if not skipped:
            return None
        names = {g.key: g.name for g in games}
        parts = [
            f"{esc(names.get(key, key))}" + (f" ({esc(reason)})" if with_reasons else "")
            for key, reason in skipped.items()
        ]
        return "Skipped: " + "; ".join(parts)

    header_line = (
        f"{_REPORT_HEADER_EMOJI[status]} **{_REPORT_HEADER_VERB[status]}** · "
        f"{_report_date(run_date)} ({run_kind})"
    )
    took_line = f"Took {_report_duration_str(duration)}"
    notes_line = "Notes: " + esc("; ".join(notes)) if status == "partial" and notes else None

    def build(with_links: bool, with_reasons: bool, with_notes: bool) -> str:
        lines = [header_line, stories_line(with_links), skipped_line(with_reasons)]
        if with_notes:
            lines.append(notes_line)
        lines.append(took_line)
        return "\n".join(line for line in lines if line)

    for with_links, with_reasons, with_notes in (
        (True, True, True),
        (True, True, False),
        (True, False, False),
        (False, False, False),
    ):
        content = build(with_links, with_reasons, with_notes)
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


# --- Owner report and server status (design.md §15, plan task 8) ---

_MAX_FAILING_SOURCES_SHOWN = 10


@dataclass(frozen=True)
class DigestOutcome:
    """One server's digest for the day, boiled down for the owner's one-liner.

    `reason` is a short category for a failure ("missing permissions",
    "Claude error"), not the raw error; it's ignored when `posted` is True.
    """

    posted: bool
    reason: str = ""


# What a failed digest's notes look like is whatever the exception said, so the
# categories are a handful of pattern checks, most specific first. Anything
# unrecognized is "other", which is honest. The status codes and error codes
# are matched on word boundaries: a channel id is eighteen digits, and about one
# in twenty of them contains a 403 or a 404 without having any opinion about HTTP.
_REASON_PATTERNS = (
    ("missing permissions", re.compile(r"\b403\b|forbidden|missing permissions|\b50013\b")),
    ("channel gone", re.compile(r"\b404\b|unknown channel|not found|\b10003\b")),
    ("rate limited", re.compile(r"\b429\b|rate limit")),
    ("timed out", re.compile(r"timed out|timeout")),
    ("couldn't build", re.compile(r"build failed")),
)


def outcome_from_digest(status: str, notes: str | None) -> DigestOutcome:
    """One digest row, as the owner's report counts it.

    `ok` and `partial` posted (a `partial` posted with something degraded or
    skipped, which is the server's business and its notices', not the owner's
    one-liner). `pending` means a run was interrupted and never finished.
    Everything else is a failure, filed under the first reason category the
    notes match.
    """
    if status in ("ok", "partial"):
        return DigestOutcome(True)
    if status == "pending":
        return DigestOutcome(False, "interrupted")
    text = (notes or "").casefold()
    for reason, pattern in _REASON_PATTERNS:
        if pattern.search(text):
            return DigestOutcome(False, reason)
    return DigestOutcome(False, "other")


# Five reasons of at most 40 characters each, plus counts and the lead-in,
# comes to roughly 300 characters: still a one-liner.
_MAX_SUMMARY_REASONS = 5


def render_digest_summary_line(outcomes: Sequence[DigestOutcome]) -> str:
    """The owner's daily one-liner.

    For example: `digests posted to 37 of 38 servers; 1 failed (missing permissions)`.

    Failures are grouped by reason; with more than one reason each gets its
    count, most common first ("3 failed (2 missing permissions, 1 Claude
    error)"). Only the top few reasons by count are shown, then "+N more
    reasons", so the one-liner stays one line. No digests due at all reads
    as such instead of "0 of 0".
    """
    total = len(outcomes)
    if total == 0:
        return "no server digests were due today"
    posted = sum(1 for o in outcomes if o.posted)
    noun = "server" if total == 1 else "servers"
    line = f"digests posted to {posted} of {total} {noun}"
    failed = [o for o in outcomes if not o.posted]
    if not failed:
        return line
    counts: dict[str, int] = {}
    for o in failed:
        reason = esc(plain_line(o.reason, 40)) or "unknown reason"
        counts[reason] = counts.get(reason, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1:
        detail = ranked[0][0]
    else:
        shown = ", ".join(f"{n} {reason}" for reason, n in ranked[:_MAX_SUMMARY_REASONS])
        more = len(ranked) - _MAX_SUMMARY_REASONS
        detail = f"{shown}, +{more} more reasons" if more > 0 else shown
    return f"{line}; {len(failed)} failed ({detail})"


_URL_IN_TEXT = re.compile(r"https?://\S+")


def _redact_urls(text: str) -> str:
    """`text` with each http(s) URL cut down to scheme, host and path (see `shown_url`).

    Trailing quotes and brackets belong to the sentence, not the address
    (httpx wraps the URL in single quotes), so they're kept outside it.
    """

    def one(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = len(url) - len(url.rstrip("'\")>]}.,;"))
        shown = shown_url(url[: len(url) - tail] if tail else url)
        return shown + (url[len(url) - tail :] if tail else "")

    return _URL_IN_TEXT.sub(one, text)


def render_owner_report(
    outcomes: Sequence[DigestOutcome], failing: Sequence[SourceHealthRow]
) -> str:
    """The owner's daily report: the summary line, then every source that's currently failing (D8).

    Capped at Discord's message limit; the source list is cut at 10 with a
    "+N more" line so the summary line at the top always survives. Every URL
    in an error goes through `shown_url` first: httpx puts the whole address
    in its messages, credentials and token query strings included.
    """
    lines = [f"newsbot daily: {render_digest_summary_line(outcomes)}"]
    if not failing:
        lines.append("All sources are healthy.")
    else:
        lines.append(f"{len(failing)} failing source{'s' if len(failing) != 1 else ''}:")
        for row in failing[:_MAX_FAILING_SOURCES_SHOWN]:
            error = (
                f": {esc(_redact_urls(plain_line(row.last_error, 120)))}" if row.last_error else ""
            )
            lines.append(
                f"- {esc(plain_line(row.source_name, 60))} "
                f"({row.consecutive_failures} in a row){error}"
            )
        extra = len(failing) - _MAX_FAILING_SOURCES_SHOWN
        if extra > 0:
            lines.append(f"+{extra} more")
    return _truncate_utf16("\n".join(lines), _ALERT_CONTENT_LIMIT, suffix="…")


def render_guild_status(notices: Sequence[Notice], *, limit: int = 5) -> str:
    """A server's recent problem notes for `/newsbot status`, newest first."""
    if not notices:
        return "No problems recorded lately."
    lines = [
        f"- {n.created_at:%Y-%m-%d %H:%M} UTC: {plain_line(n.text, 300)}" for n in notices[:limit]
    ]
    return _truncate_utf16("\n".join(lines), _MAX_FIELD_VALUE, suffix="…")


def render_item_page(
    items: Sequence[ItemView],
    names_by_key: Mapping[str, str],
    title: str,
    page: int,
    pages: int,
) -> discord.Embed:
    """One page of a free server's `/news recent` or `/news search`: stored item headlines.

    Free servers get headlines, not stories (owner decision D2), so this is
    `_headline_line`'s one-line format (official marker, flattened title, `<url>`) under
    a bold game name. An item whose URL won't survive `_safe_link` is dropped, same as in
    the digest. `ItemView.topic_keys` is already limited to games this server follows.
    """
    embed = discord.Embed(title=_truncate_utf16(esc(title), _TITLE_LIMIT), color=_PALETTE[0])
    blocks = []
    for item in items:
        line = _headline_line(item)
        if line is None:
            continue
        names = ", ".join(names_by_key.get(key, key) for key in item.topic_keys)
        blocks.append(f"**{esc(names)}**\n{line}" if names else line)
    if not blocks:
        embed.description = "No headlines found."
        return embed
    embed.description = _truncate_description("\n\n".join(blocks))
    embed.set_footer(text=f"Page {page} of {pages}")
    return embed


def render_guild_overview(
    *,
    tier: str,
    schedule_line: str,
    digest_lines: Sequence[str],
    game_lines: Sequence[str],
    shift_line: str,
    notices: Sequence[Notice],
    lounge_lines: Sequence[str] | None = None,
) -> discord.Embed:
    """`/newsbot status` for one server: its own settings, last digest, games and notices.

    `lounge_lines` is None for a server with no lounge, and then the section isn't shown.

    Callers pass lines that are already safe (their own `esc()`ed names, `<#id>` channel
    mentions, jump links); every field is cut to Discord's field limit regardless. There's
    no spend in here on purpose: money is the owner's business, not a server's.
    """
    embed = discord.Embed(title="newsbot status", color=_PALETTE[0])

    def field(name: str, lines: Sequence[str]) -> None:
        text = "\n".join(lines) or "Nothing yet."
        embed.add_field(
            name=name, value=_truncate_utf16(text, _MAX_FIELD_VALUE, suffix="…"), inline=False
        )

    field("Server", [f"Tier: {tier}", schedule_line])
    field("Last digest", digest_lines)
    field("Games", game_lines)
    field("SHiFT codes", [shift_line])
    if lounge_lines is not None:
        field("Lounge", lounge_lines)
    field("Recent notices", [render_guild_status(notices)])
    return embed


__all__ = [
    "FOLLOWUP_MAX_CODES",
    "DigestOutcome",
    "RenderedAlert",
    "RenderedDigest",
    "TopicMessage",
    "esc",
    "outcome_from_digest",
    "render_code_alerts",
    "render_code_page",
    "render_digest_summary_line",
    "render_followup_alert",
    "render_guild_digest",
    "render_guild_run_report",
    "render_guild_overview",
    "render_guild_status",
    "render_headlines_embed",
    "render_item_page",
    "render_owner_report",
    "render_roundup_alerts",
    "render_story_page",
    "to_text",
]
