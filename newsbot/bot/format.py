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
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from newsbot.bot import terminal
from newsbot.config import GameInfo, Topic
from newsbot.discord_links import is_discord_invite
from newsbot.pipeline.normalize import canonicalize
from newsbot.pipeline.summarize import StoryDraft, estimate_spend_usd
from newsbot.shift.decide import CodeCandidate, group_roundups
from newsbot.shift.details import SHIFT_TZ, end_of_day, expired_by_expiry
from newsbot.shift.match import is_code
from newsbot.store.models import (
    CodeDetailsRow,
    CodeView,
    CurrentCode,
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

_LABEL_ORDER = {"official": 0, "reported": 1, "rumor": 2}
_LABEL_MARKER = {"official": "🟢 OFFICIAL", "reported": "🟡 REPORTED", "rumor": "🔴 RUMOR"}
_UPDATE_MARKER = "🔁 UPDATE"
# "<" followed by a channel link (<#), a command link (</), or one of Discord's other
# angle-bracket tokens: a custom emoji (<:name:id>, <a:name:id>), a timestamp (<t:...>,
# which can pose as a real date) or guild navigation (<id:browse>). `esc()` puts a
# zero-width space after the "<" so none of them is ever live.
_LINKY_MENTION_RE = re.compile(r"<(?=[#/:]|a:|t:|id:)", re.IGNORECASE)
# Belt-and-suspenders behind summarize.py's postprocess(), which is
# supposed to have already stripped any `scheme://...` token out of
# headline/summary text before it ever reaches this module. This is the
# second layer, for whatever text reaches an embed some other way (or
# whatever the first layer's regex didn't quite cover): a zero-width
# space right after the scheme's colon defuses "https://" into
# "https:/\u200b/", which reads identically to a person and autolinks to
# nobody. No `\b` on purpose: an underscore counts as a word character, so
# "_https://evil.example" slipped past it and Discord linked the URL right after
# the escaped underscore. The lookbehind starts a match only at the front of a
# run of scheme characters (so a million letters in a row are scanned once, not
# once per letter), and anything that isn't a scheme character, "_" included,
# may come before it.
_URL_SCHEME_RE = re.compile(r"(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]*):(?=//)", re.IGNORECASE)
# Discord makes invite links clickable even without a scheme, because of
# course it does. Same rule as above: no `\b`, so "_discord.gg/abc" is defused too.
_BARE_INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/", re.IGNORECASE)

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
    escaped = _LINKY_MENTION_RE.sub("<\u200b", escaped)
    escaped = _URL_SCHEME_RE.sub(lambda m: f"{m.group(1)}:\u200b", escaped)
    return _BARE_INVITE_RE.sub(lambda m: f"{m.group(1)}\u200b/", escaped)


def esc(s: str) -> str:
    """Escape markdown and @mentions, so scraped text can't format itself or ping the server.

    `allowed_mentions=none` on the actual send is the real backstop; this
    is the belt to that suspenders, so a headline never even *renders* as
    a working @everyone in someone's client before the send-time guard
    would have caught it anyway.
    """
    # `ignore_links=False` on purpose: the default leaves anything URL-shaped alone, markdown
    # included, so a reward of "see https://x.y/`" kept a raw backtick that could close a chip
    # early, and three of them could open a fence. A scheme gets defused a few lines down, so
    # no live link is lost by escaping what's inside one.
    #
    # That still isn't the whole story: `escape_markdown` treats a markdown link, "[a](b)", as
    # one token and puts a backslash on its "[" only, leaving everything inside raw, so
    # "[a](b`)" kept a live backtick. The "](" is what makes it a link, so I cut the text there,
    # escape the pieces on their own (without it the link pattern has nothing to match), and
    # stitch them back with the "]" escaped, plus the "[" in every piece that had a "](" after
    # it (as before, so a masked link still reads as text). Same words on screen, and no link
    # left to hide in.
    pieces = [discord.utils.escape_markdown(p, ignore_links=False) for p in s.split("](")]
    pieces[:-1] = [p.replace("[", "\\[") for p in pieces[:-1]]
    escaped = discord.utils.escape_mentions("\\](".join(pieces))
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


def cap_shown(text: str, limit: int) -> str:
    """Trim `text` so that its escaped form is at most `limit` UTF-16 units, adding "…" if it cuts.

    The limit is measured on what Discord is actually sent: `esc()` can nearly
    double a string full of markdown or mentions, and a reward that's 300
    characters raw and 590 shown is exactly the kind of thing a limit is
    supposed to catch. `esc()` isn't strictly additive (it looks ahead for
    "://", for one), so the cut is a binary search followed by a check on the
    real candidate, and the loop at the end is there for the day the search's
    monotonic assumption turns out to be wrong. Text that already fits comes
    back untouched.
    """
    if discord_len(esc(text)) <= limit:
        return text

    def candidate(n: int) -> str:
        return text[:n].rstrip() + "…"

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if discord_len(esc(candidate(mid))) <= limit:
            lo = mid
        else:
            hi = mid - 1
    while lo and discord_len(esc(candidate(lo))) > limit:
        lo -= 1
    return candidate(lo)


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


def _shown_link(url: str) -> str | None:
    """`_safe_link`, or None if the page is a Discord invite: the link a reader may be shown.

    A hijacked feed can point an item at `discord.gg/...` and still carry a real code, and
    items stored before v3.3.1 can still be in the store. The code (or the headline) and
    its source name go out as always; only the link is left off, because printing it would
    be a live invite. Shorter than with a link, so the edit reserve never notices. The
    check runs on the URL as given and on its canonical form, in case canonicalizing
    ever turns one into the other.
    """
    safe = _safe_link(url)
    if safe is None or is_discord_invite(url) or is_discord_invite(safe):
        return None
    return safe


def _link_line(urls: list[str]) -> str:
    safe_urls = [safe for url in urls if (safe := _shown_link(url)) is not None]
    shown = safe_urls[:_MAX_LINKS_SHOWN]
    # `<url>` (angle brackets) tells Discord "link this, but don't expand
    # it into a preview card": without that, three or four stacked link
    # previews turn one story into a wall of thumbnails.
    line = " · ".join(f"<{url}>" for url in shown)
    extra = len(safe_urls) - len(shown)
    if extra > 0:
        line += f" · +{extra} more"
    return line


def _one_line(text: str) -> str:
    """The model's headline or summary with every whitespace run (line breaks too) as one space.

    Both are written by a model that has read a feed excerpt, and an excerpt can
    talk it into a summary with a blank line and a `🟢 OFFICIAL · ...` of its own,
    which renders as a second story block wearing a marker nobody earned. The
    label was already downgraded; the line break was the loophole. Stored text is
    left alone (this runs at render time), and a normal one-line summary comes out
    byte for byte as it went in. `split()` with no argument breaks on every kind of
    whitespace, the same trick `_headline_line` plays on titles.
    """
    return " ".join(text.split())


# The bot's own label markers, and the look-alikes of them, as a model (or a feed title) could
# write them with no help from a newline. Invisible characters in front are allowed, because
# they don't stop a line looking like it starts with the marker: any format character
# (category Cf: zero-width, bidi controls, the soft hyphen) plus two that Unicode files under
# other categories but render as nothing (U+034F, U+180E).


def _invisible(char: str) -> bool:
    return unicodedata.category(char) == "Cf" or char in "\u034f\u180e"


# A look-alike is any colored dot or square, check mark or loop arrow a reader would take for
# the bot's own marker (the bot's four are in the list: 1F7E2, 1F7E1, 1F534 and 1F501).
_LOOKALIKE_EMOJI = (
    "[\U0001f534\U0001f535\U0001f7e0-\U0001f7eb\u26aa\u26ab\u2b1b\u2b1c"
    "\u2705\u2611\u2714\U0001f501-\U0001f504]"
)
_MARKER_EMOJI = (
    "[" + "".join(m.split(" ")[0] for m in (*_LABEL_MARKER.values(), _UPDATE_MARKER)) + "]"
)
# What `_fold` writes in place of a combining mark. A private-use character, so it can't be
# mistaken for anything a reader typed ("_" could) and no pattern but the ones below knows it.
_MARK = "\ue000"
# What can ride on an emoji and still be part of it: the variation selector, the five
# skin-tone swatches and combining marks. (The zero-width joiner is invisible, so `_fold` has
# dropped it already.)
_EMOJI_TAIL = f"[\ufe0f\U0001f3fb-\U0001f3ff{_MARK}]*"
# Matched against the folded (NFKC, upper case) text, where a combining mark has become
# `_MARK`, so the word must end there: "UPDATES" is not "UPDATE", and neither is "UPDATE"
# plus an accent on its last letter. A separator counts only when whitespace or the end of the text
# follows it, which keeps "Update -5% off" and "Official:the patch" as the ordinary text they
# are. Several look-alike emoji in a row (or one with spaces after it) are one lead-in.
_LABEL_WORD = rf"(?:OFFICIAL|REPORTED|RUMOR|UPDATE)(?![\w{_MARK}])"
_SEPARATOR = "[·:|\\-\u2013\u2014](?=\\s|$)"
_TAIL = rf"\s*(?:{_SEPARATOR}\s*)?"
# Tried in this order at each step. The three are separate patterns, and the run of bot emoji is
# its own pattern, because one pattern that tries "a run of emoji, then a word" and falls back
# to "one emoji" re-reads a long run once per emoji it takes off.
_LEADING_LABEL_RES = (
    re.compile(rf"\s*(?:{_LOOKALIKE_EMOJI}{_EMOJI_TAIL}\s*)+{_LABEL_WORD}{_TAIL}"),
    re.compile(rf"\s*{_LABEL_WORD}\s*{_SEPARATOR}{_TAIL}"),
    re.compile(rf"\s*(?:{_MARKER_EMOJI}{_EMOJI_TAIL}{_TAIL})+"),
)
# The bot's markers exactly as they render, wherever they sit: its emoji (with whatever rides
# on it), one space, the word that goes with that emoji, and the " ·" after it if there is
# one. This pattern only says whether `_drop_exact_markers` has any work to look for (one of
# the bot's emoji; a space and a word won't do, since an invisible can sit between them); the
# scan decides.
_EXACT_MARKER_HINT_RE = re.compile(_MARKER_EMOJI)
_EXACT_MARKER_WORDS = tuple(
    (word, emoji)
    for emoji, word in (marker.split(" ") for marker in (*_LABEL_MARKER.values(), _UPDATE_MARKER))
)
_MARK_CATEGORIES = ("Mn", "Mc", "Me")


def _is_mark(char: str) -> bool:
    """A combining mark (not the variation selector, which the patterns want as it is)."""
    return char != "\ufe0f" and unicodedata.category(char) in _MARK_CATEGORIES


def _rides_on_emoji(char: str) -> bool:
    """True for what can follow an emoji and still be part of it (see `_EMOJI_TAIL`)."""
    return char == "\ufe0f" or "\U0001f3fb" <= char <= "\U0001f3ff" or _is_mark(char)


def _takes_selector(char: str) -> bool:
    """True for what U+FE0F can turn into an emoji: a symbol, or a keycap's digit, # or *."""
    return unicodedata.category(char).startswith("S") or char in "0123456789#*"


def _hidden(text: str) -> list[bool]:
    """For each character of `text`, whether it draws as nothing.

    That's `_invisible`, plus a variation selector (U+FE0F) with no emoji to ride on. The
    selector asks the character in front of it to draw as an emoji; after a letter, a space,
    an invisible or the start of the line there's nobody to ask, so it draws as nothing and
    hides a label word exactly as well as a zero-width space does. Riding means right after an
    emoji, or after something that rides on one (green circle, skin tone, selector is one emoji
    wearing two riders). The test for "an emoji" is any symbol, which is generous: a selector
    on a math sign counts as riding and stays where it is, which is how it was before.
    """
    flags: list[bool] = []
    on_emoji = False
    for char in text:
        if char == "\ufe0f":
            flags.append(not on_emoji)
        elif _invisible(char):
            flags.append(True)
            on_emoji = False
        else:
            flags.append(False)
            if not _rides_on_emoji(char):
                on_emoji = _takes_selector(char)
    return flags


def _fold(text: str) -> tuple[str, list[int]]:
    """`text` folded for label matching, plus where each folded character came from.

    NFKC turns full-width and mathematical-bold letters into plain ones, and upper-casing
    makes the match case-blind. Both can change a character's length ("ﬃ" becomes three
    letters), so the text is folded one character at a time and `origin[i]` is the index in
    `text` that folded character `i` came from (with one more entry at the end for "all of
    it"). A cut found in the folded copy is then made in the original.

    Two more things happen here so the patterns can stay simple. Invisible characters are
    left out of the folded copy (a zero-width space between the emoji and the word hides
    nothing from a reader, so it shouldn't hide anything from us; `_hidden` says which, and
    that includes a variation selector with no emoji under it), and a combining mark becomes
    `_MARK` so that a word ending in an accented letter doesn't look like it ends early. A
    variation selector riding on an emoji is a mark too, but the patterns want it as it is.
    (A real private-use character `_MARK` in the text becomes U+FFFD, so it can't pose as one.)
    """
    folded: list[str] = []
    origin: list[int] = []
    hidden = _hidden(text)
    for index, char in enumerate(text):
        if hidden[index]:
            continue
        if char == _MARK:
            piece = "\ufffd"
        elif _is_mark(char):
            piece = _MARK
        else:
            piece = unicodedata.normalize("NFKC", char).upper()
        folded.append(piece)
        origin.extend([index] * len(piece))
    origin.append(len(text))
    return "".join(folded), origin


def _strip_leading_labels(text: str) -> str:
    """`text` with leading label markers and look-alikes taken off, as often as they repeat."""
    folded, origin = _fold(text)
    cursor = 0
    while True:
        for pattern in _LEADING_LABEL_RES:
            if match := pattern.match(folded, cursor):
                cursor = match.end()
                break
        else:
            break
    return text[origin[cursor] :] if cursor else text


def _drop_exact_markers(text: str) -> str:
    """`text` (single spaces only) without the bot's exact markers, wherever they sit.

    Each marker comes out and leaves one space, and runs of spaces collapse, which can make
    the pieces on either side into a marker of their own ("<green> <yellow> REPORTED
    OFFICIAL" is two). Doing that in rounds with a regex re-reads the whole string every
    round, and a title built to nest them took a minute and a half. This reads the text once,
    left to right, keeping what it has so far on a stack: when the end of the stack becomes a
    whole marker, pop it. Whether a marker is whole depends only on what follows its word (no
    letter or digit, and then the dot if there is one), and nothing later can change that, so
    the answer is the same as the rounds'.

    Characters that draw as nothing (`_hidden`) don't count when deciding whether something
    is a marker, the same deal the leading strip gets from `_fold`: a zero-width joiner after
    the emoji or a stray selector in front of the word doesn't make "<green> OFFICIAL" any
    less of a marker to a reader. `shown` keeps where the drawn characters sit on the stack,
    and the matching only looks at those. A marker that comes out takes the hidden characters
    touching it on either side with it, so nothing is left in the gap selecting nothing.
    """
    hidden = _hidden(text)
    n = len(text)

    def past_hidden(at: int) -> int:
        while at < n and hidden[at]:
            at += 1
        return at

    out: list[str] = []
    shown: list[int] = []
    i = 0
    while i < n:
        char = text[i]
        hide = hidden[i]
        i += 1
        if char == " ":
            if out and out[-1] != " ":
                shown.append(len(out))
                out.append(" ")
            continue
        out.append(char)
        if hide:
            continue
        shown.append(len(out) - 1)
        for word, emoji in _EXACT_MARKER_WORDS:
            size = len(word)
            if char != word[-1] or len(shown) < size + 2 or out[shown[-size - 1]] != " ":
                continue
            if "".join(out[at] for at in shown[-size:]) != word:
                continue
            start = len(shown) - size - 2
            while start >= 0 and _rides_on_emoji(out[shown[start]]):
                start -= 1
            if start < 0 or out[shown[start]] != emoji:
                continue
            after = past_hidden(i)
            following = text[after] if after < n else ""
            if following and (following.isalnum() or following == "_" or _is_mark(following)):
                break
            if following == "·":
                after = past_hidden(after + 1)
            elif following == " ":
                dot = past_hidden(after + 1)
                if text.startswith("·", dot):
                    after = past_hidden(dot + 1)
            i = after
            del out[shown[start - 1] + 1 if start else 0 :]
            del shown[start:]
            if out and out[-1] != " ":
                shown.append(len(out))
                out.append(" ")
            break
    while out and out[-1] == " ":
        out.pop()
    return "".join(out)


def _without_labels(text: str) -> str:
    """`text` (already on one line) with the bot's label markers, and look-alikes, taken off.

    Collapsing line breaks stops model-written or scraped text from starting a new line, but
    one that simply opens with `🟢 OFFICIAL · Valve buys ...` sits right under (or in place
    of) a real marker and reads as one, newline or not. The real marker is only ever the one
    the renderer puts in front, so any other is removed here:

    - from the front, as often as it repeats: one or more look-alike emoji (with any skin
      tone, variation selector or combining mark riding on them) and a label word, a label
      word with a separator (`UPDATE:`, `official -`), or one of the bot's four emoji alone,
      matched after NFKC folding and without regard to case, plus the separator and spaces
      left behind. Invisible characters anywhere in that run are skipped. A bare "Official
      patch notes" is ordinary text and stays, and so is a label word whose last letter
      carries an accent;
    - from anywhere, the bot's exact markers (`🟢 OFFICIAL`, with or without the ` ·` or `·`
      after it, and with the same things riding on the emoji), which is what a model that
      has read our output tends to produce. Invisible characters inside one are skipped
      here too. A label word on its own mid-line stays.

    "Invisible" includes a variation selector that isn't riding on an emoji (see `_hidden`),
    and a marker that comes out, from the front or the middle, takes the invisible characters
    touching it along with it.

    Text with none of these comes back byte for byte. If stripping would leave nothing, the
    original stays: a blank line reads as a bug.
    """
    kept = text
    while True:
        before = kept
        kept = _strip_leading_labels(kept)
        if _EXACT_MARKER_HINT_RE.search(kept):
            single = " ".join(kept.split())
            reduced = _drop_exact_markers(single)
            if reduced != single:
                kept = reduced
        if kept == before:
            break
    return kept or text


def _ai_line(text: str) -> str:
    """`_one_line`, then with the bot's label markers and look-alikes of them taken off.

    See `_without_labels` for exactly what goes. Ordinary text comes out as `_one_line`
    leaves it.
    """
    return _without_labels(_one_line(text))


_LIST_START_RE = re.compile(r"^([ \t]*[0-9]+)\.(?=\s|$)", re.MULTILINE)


def escape_list_start(text: str) -> str:
    """`text` with a backslash before the dot of any line that starts like a numbered list.

    Discord draws "2026. The year ..." as item 2026 of an ordered list, and
    `escape_markdown` only knows about the bullets. Call this on a line that begins with
    scraped or AI text, after `esc()`; it doesn't belong inside `esc()` itself, which runs
    mid-line in a lot of places. Digits are ASCII only, the dot must be followed by
    whitespace or the end of the line ("1.Valve" and "10) x" are plain text), and only the
    first run on each line is touched. Spaces before the number are allowed, since Discord
    allows them too.
    """
    return _LIST_START_RE.sub(r"\1\\.", text)


def _story_block(story: StoryDraft) -> str:
    marker = _UPDATE_MARKER if story.update_of_story_id is not None else _LABEL_MARKER[story.label]
    return "\n".join(
        [
            f"{marker} · {esc(_ai_line(story.headline))}",
            escape_list_start(esc(_ai_line(story.summary))),
            _link_line(story.item_urls),
        ]
    )


def _truncate_description(text: str, limit: int = _DESCRIPTION_LIMIT) -> str:
    """`text` cut to `limit` units with a "…", never leaving a lone backslash in front of it.

    A cut can land right after a backslash `esc()` put in front of something. Left at the end
    it would escape the "…", so an odd run of them loses one. (`_truncate_utf16` itself leaves
    that alone: a source name's cut is pinned the old way, and I'd rather not move it.)
    """
    cut = _truncate_utf16(text, limit, suffix="…")
    if cut == text:
        return cut
    head = cut.removesuffix("…")
    if (len(head) - len(head.rstrip("\\"))) % 2:
        head = head[:-1]
    return head + "…"


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
                    f"{marker} · {esc(topic_name)} · {esc(_ai_line(story.headline))}",
                    escape_list_start(esc(_ai_line(story.summary))),
                    _link_line(story.urls),
                ]
            )
        )
    embed.description = _truncate_description("\n\n".join(blocks))
    embed.set_footer(text=f"Page {page} of {pages}")
    return embed


# --- SHiFT code details (design.md §16) ---
#
# A code's reward and expiry come from places that range from Gearbox's own post to
# whatever a fan site's table says today, and then they get printed under a code in a
# message Discord will refuse to take if it's one unit over 2,000. The deal this
# section makes: the details line has a worst case that can be computed up front
# (`details_reserve`), every alert reserves that much room per code when it's packed,
# and so a later edit, with whatever the details turned out to be, still fits. I did
# the arithmetic twice. The first time I forgot that markdown escaping can double a
# string, which is why the 300-character cap is measured on what's shown.

# The most a reward or origin can be once escaped (the collector and the official
# reader cut to this; `cap_shown` enforces it again below so a hostile row can't
# stretch a reservation that was sized in good faith).
_DETAIL_SHOWN_MAX = 300
_VIA_NAME_MAX = 100
# The longest expiry value an alert can carry: "Sep 30, 2026" (12), then the live timestamp,
# " (<t:" and ":R>)" around up to 11 digits. A timestamp today is 10 digits (it stays 10 until
# the year 2286); the 11th is room I'm keeping in case I'm wrong about that. The `expires` label
# is counted separately, in `details_reserve`.
_STAMP_OPEN, _STAMP_CLOSE = " (<t:", ":R>)"
_STAMP_DIGITS_MAX = 11
_EXPIRY_VALUE_MAX = len("Sep 30, 2026") + len(_STAMP_OPEN) + _STAMP_DIGITS_MAX + len(_STAMP_CLOSE)
_DETAIL_SEP = " · "
_DETAIL_AND = " and "
# The label chips that lead each part of the line, each with the space that follows it.
_REWARD_LEAD = terminal.label(terminal.REWARD) + " "
_EXPIRES_LEAD = terminal.label(terminal.EXPIRES) + " "
_VIA_LEAD = terminal.label(terminal.VIA) + " "
_SRC_LEAD = terminal.label(terminal.SRC) + " "
_FIRST_SEEN_LEAD = terminal.label(terminal.FIRST_SEEN) + " "
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Bidirectional overrides and isolates (U+202A to U+202E, U+2066 to U+2069). A reward
# that opens one without closing it can make the rest of the line read backwards, the `via`
# names included. Only these: the zero-width joiner holds emoji sequences together, and
# the other invisible characters are somebody else's problem.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")


def _aware(moment: datetime) -> datetime:
    """`moment` as an aware datetime; a naive one is taken to be UTC, like the store's."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _detail_text(text: str) -> str:
    """Scraped reward or origin text, ready to escape: bidi controls gone, still at most 300 shown.

    Whitespace of every kind, newlines included, collapses to single spaces, so a reward can't
    start its own list, quote or line ("\n1. x" is just "1. x" now). That only ever shrinks
    the text, so the reserve and both layouts' budgets are safe.

    The strip comes before the escaping on purpose (a bidi character wedged inside
    "@everyone" would otherwise hide it from `esc`, and then vanish). The cap is applied
    again afterwards because `esc` isn't strictly additive, so removing a character can
    in principle make a neighbor escape. As far as I can tell that never grows the
    string; the property test throws bidi-laden rewards at it to keep me honest.
    """
    return cap_shown(" ".join(_BIDI_RE.sub("", text).split()), _DETAIL_SHOWN_MAX)


def _short_date(d: date, *, with_year: bool = True) -> str:
    year = f", {d.year}" if with_year else ""
    return f"{_MONTHS[d.month - 1]} {d.day}{year}"


def _expiry_value(
    row: CodeDetailsRow | None, now: datetime, *, with_days: bool = False
) -> tuple[str, str] | None:
    """How a code's expiry reads at `now`, as `(label word, value)`, or None when nothing is known.

    A permanent code is `expires` "never" (whatever date a stray table row left on it). A
    known expiry is a Pacific calendar day that lasts until the next midnight there:
    on that day it's "in 5 hours", the day before it's "tomorrow", otherwise a date. After
    it, the label swaps to `expired` and the value is the date. `with_days` is
    `/shift codes`' flavor of the date: no year unless it's another year, plus "(in 5 days)".
    """
    if row is None:
        return None
    if row.category == "permanent":
        return terminal.EXPIRES, "never"
    if row.expires_on is None:
        return None
    now = _aware(now)
    today = now.astimezone(SHIFT_TZ).date()
    due = row.expires_on
    if due < today:
        return terminal.EXPIRED, _short_date(due)
    if due == today:
        hours = int((end_of_day(due) - now).total_seconds() // 3600)
        if hours < 1:
            return terminal.EXPIRES, "in under an hour"
        return terminal.EXPIRES, f"in {hours} hour{'s' if hours != 1 else ''}"
    if due == today + timedelta(days=1):
        return terminal.EXPIRES, "tomorrow"
    if with_days:
        shown = _short_date(due, with_year=due.year != today.year)
        return terminal.EXPIRES, f"{shown} (in {(due - today).days} days)"
    return terminal.EXPIRES, _short_date(due)


def _live_expiry_value(row: CodeDetailsRow | None) -> str | None:
    """A code's expiry as a posted message says it: the date, then a live Discord timestamp.

    `Oct 14, 2026 (<t:1792047600:R>)`, for the `expires` label to sit in front of. The date is
    the Pacific calendar day the sources gave; the timestamp is the instant that day ends
    (`end_of_day`, Unix seconds), and Discord keeps its "in 6 hours" current for every reader,
    in their own language, long after the message was written. That is why a posted alert
    never says "in 6 hours" itself: it would be wrong by lunchtime. A permanent code is
    "never".

    The token is built here, from an integer the bot computed, and is never passed through
    `esc()` (which defuses `<t:` on purpose, so scraped text can't forge one). A date so
    far out that its timestamp wouldn't fit the reserve's 11 digits (only a hand-edited row
    gets there; the readers stop at 2100) shows as the date alone.
    """
    if row is None:
        return None
    if row.category == "permanent":
        return "never"
    if row.expires_on is None:
        return None
    value = _short_date(row.expires_on)
    stamp = int(end_of_day(row.expires_on).timestamp())
    if 0 < stamp < 10**_STAMP_DIGITS_MAX:
        value += f"{_STAMP_OPEN}{stamp}{_STAMP_CLOSE}"
    return value


def _via_name(name: str) -> str:
    """A source name as the `via` part shows it: 100 characters at most, cut with a "…" if longer.

    Config and `clean_observation` both hold names to 100 characters, so this only ever
    trims a row stored under an older, longer name (or edited by hand). It's here because
    the alternative is an entry bigger than a page. The cut counts characters, not shown
    units, on purpose: a 100-character name full of markdown is legal, draws whole, and is
    what the reserve is sized for (200 units, escapes and all).
    """
    if len(name) <= _VIA_NAME_MAX:
        return name
    return name[: _VIA_NAME_MAX - 1].rstrip() + "…"


def details_line(
    details: CodeDetailsRow | None,
    *,
    now: datetime,
    show_expiry: bool = True,
    relative: bool = False,
) -> str | None:
    """The line under a code.

    For example `` `reward` 3 Golden Keys · `expires` Oct 14, 2026 (<t:1792047600:R>) · `via`
    mentalmars `` (the labels are chips; see `terminal`).

    None when there's neither a reward nor an expiry to show, so an alert looks as it
    always did until details exist. `via` names whoever supplied what's shown (both
    names, in reward-then-expiry order, when they differ); a permanent code's "expiry"
    credits whoever set its category. Everything scraped goes through `esc()`, and nothing
    scraped ever goes inside a chip; the timestamp is the bot's own and goes in after it
    (see `_live_expiry_value`).

    This is the persisted wording: alerts, edits, follow-ups and roundups all stay in a
    channel for good, so their expiry is a date plus a timestamp Discord keeps current.
    `relative=True` is for a page drawn fresh on every call (`/shift history`): its expiry
    reads "in 6 hours" or "tomorrow" against `now` (Pacific time, see `_expiry_value`), and
    a date that has passed swaps the label to `expired`. `show_expiry=False` leaves the
    expiry (and its credit) out; history uses it for a code whose marker already says
    "expired", so the page says it once.
    """
    if details is None:
        return None
    reward = _detail_text(details.reward)
    expiry: tuple[str, str] | None
    if not show_expiry:
        expiry = None
    elif relative:
        expiry = _expiry_value(details, now)
    else:
        value = _live_expiry_value(details)
        expiry = None if value is None else (terminal.EXPIRES, value)
    if not reward and expiry is None:
        return None
    parts: list[str] = []
    sources: list[str] = []
    if reward:
        parts.append(f"{_REWARD_LEAD}{esc(reward)}")
        sources.append(details.reward_from)
    if expiry is not None:
        word, value = expiry
        parts.append(f"{terminal.label(word)} {value}")
        sources.append(
            details.category_from if details.category == "permanent" else details.expires_from
        )
    names = list(dict.fromkeys(name for name in sources if name))
    if names:
        parts.append(_VIA_LEAD + _DETAIL_AND.join(esc(_via_name(name)) for name in names))
    return _DETAIL_SEP.join(parts)


def details_reserve(names: Iterable[str]) -> int:
    """The most room (UTF-16 units) one code's details line can ever take, newline included.

    `names` is every source name that can supply details (see plan §3.4). The line at its
    worst is a 300-unit reward, the longest expiry value (a date and an 11-digit live
    timestamp), and `via` two of the longest names, each part behind its label chip. That
    comes to `369 + 2L` for names of `L` units: 1 + 9 + 300 + 3 + 10 + 32 + 3 + 6 + L + 5 + L,
    which is the newline, `` `reward` ``, the reward, a separator, `` `expires` ``, the value,
    a separator, `` `via` ``, a name, " and ", and the other name. The alert packer sets this
    much aside per code whether or not the code has details yet, which is what lets an
    edit land later without the message outgrowing Discord's cap. A test pins
    `details_line` under this for hostile input.
    """
    # A name longer than 100 characters is never drawn whole (`_via_name`), so the reserve
    # sizes it at 100 and a stored legacy monster can't inflate every alert's slot.
    longest = max((discord_len(esc(name[:_VIA_NAME_MAX])) for name in names), default=0)
    return (
        1  # the newline that puts the line under its code
        + len(_REWARD_LEAD)
        + _DETAIL_SHOWN_MAX
        + len(_DETAIL_SEP)
        + len(_EXPIRES_LEAD)
        + _EXPIRY_VALUE_MAX
        + len(_DETAIL_SEP)
        + len(_VIA_LEAD)
        + longest
        + len(_DETAIL_AND)
        + longest
    )


# --- the v3.1 ("classic") alert layout, kept for edits of pre-3.2.0 alerts ---
#
# DEPRECATED: delete in the first release more than 14 days after v3.2.0 is deployed;
# nothing can edit a pre-3.2.0 alert after that.
#
# An alert is editable for 14 days, so for two weeks after v3.2.0 goes out there are
# messages in channels that were packed in the v3.1 layout, and an edit has to put them
# back the way they were: the 3.1 header, "details via", no `src` chip. Re-rendering one
# in the new look would be a bigger message than the one the packer sized, and a message
# one unit over 2,000 is a message Discord refuses. So the three pieces that make up that
# layout live on here as frozen copies of v3.1.4's, with their own string constants, so
# the shared ones can be restyled without touching a word of this. `render_alert_edit`
# decides which layout a message gets from its stored header (see its docstring). Nothing
# else calls these, and nothing here should ever be "improved". It is a museum exhibit.

_CLASSIC_DETAIL_SEP = " · "
_CLASSIC_DETAIL_VIA = " · details via "
_CLASSIC_DETAIL_AND = " and "
_CLASSIC_EXPIRES = "expires "
_CLASSIC_PERMANENT = "permanent"
_CLASSIC_STAMP_OPEN, _CLASSIC_STAMP_CLOSE = " (<t:", ":R>)"
_CLASSIC_STAMP_DIGITS_MAX = 11
_CLASSIC_GOLDEN_PREFIX = "Golden Key: "
_CLASSIC_LINK_SEP = " · "
_CLASSIC_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _classic_live_expiry(row: CodeDetailsRow | None) -> str | None:
    """v3.1's persisted expiry: the date, then a live Discord timestamp ("permanent" if it is).

    Frozen copy of v3.1.4's `_live_expiry`; see the section note above.
    """
    if row is None:
        return None
    if row.category == "permanent":
        return _CLASSIC_PERMANENT
    if row.expires_on is None:
        return None
    due = row.expires_on
    phrase = f"{_CLASSIC_EXPIRES}{_CLASSIC_MONTHS[due.month - 1]} {due.day}, {due.year}"
    stamp = int(end_of_day(due).timestamp())
    if 0 < stamp < 10**_CLASSIC_STAMP_DIGITS_MAX:
        phrase += f"{_CLASSIC_STAMP_OPEN}{stamp}{_CLASSIC_STAMP_CLOSE}"
    return phrase


def _classic_details_line(details: CodeDetailsRow | None) -> str | None:
    """v3.1's details line, as `details_line(..., relative=False, show_expiry=True)` made it.

    For example `3 Golden Keys · expires Oct 14, 2026 (<t:1792047600:R>) · details via mentalmars`.
    None when there's neither a reward nor an expiry to show. Frozen copy of v3.1.4's.
    """
    if details is None:
        return None
    reward = _detail_text(details.reward)
    expiry = _classic_live_expiry(details)
    if not reward and expiry is None:
        return None
    parts: list[str] = []
    sources: list[str] = []
    if reward:
        parts.append(esc(reward))
        sources.append(details.reward_from)
    if expiry is not None:
        parts.append(expiry)
        sources.append(
            details.category_from if details.category == "permanent" else details.expires_from
        )
    line = _CLASSIC_DETAIL_SEP.join(parts)
    names = list(dict.fromkeys(name for name in sources if name))
    if names:
        line += _CLASSIC_DETAIL_VIA + _CLASSIC_DETAIL_AND.join(
            esc(_via_name(name)) for name in names
        )
    return line


def _classic_alert_block(
    candidate: CodeCandidate,
    *,
    golden_prefix: bool,
    link: bool = True,
    line: str | None = None,
) -> str:
    """v3.1's entry for one code: fence, details line if any, then its source and link.

    Frozen copy of v3.1.4's `_alert_block`; `link=False` is still the one thing it sheds.
    """
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    prefix = _CLASSIC_GOLDEN_PREFIX if golden_prefix and candidate.golden else ""
    safe_url = _shown_link(candidate.item_url) if link else None
    shown_link = f"{_CLASSIC_LINK_SEP}<{safe_url}>" if safe_url else ""
    lines = [f"```\n{candidate.code}\n```"]
    if line:
        lines.append(line)
    lines.append(f"{prefix}{esc(candidate.source_name)}{shown_link}")
    return "\n".join(lines)


# --- /shift codes (design.md §13) ---

_CODE_PAGE_MAX_SOURCE = 100
_CODE_PAGE_EMPTY = "No codes seen in that window."


def _code_marker(view: CodeView, now: datetime | None = None) -> str | None:
    """The D5 marker for one code, or None for a code that just... posted normally.

    Checked in this order on purpose: `from_roundup` wins over `status`
    (a roundup code that overflowed the cap is `status='roundup'`, not
    `'posted'`, but it's still "from a roundup" to a member reading this,
    not some fourth unexplained state): see `record_silent_codes`'s own
    docstring for why `from_roundup` is a separate column instead of being
    derived from `status`. `/shift history` passes `now`, and a code whose
    known expiry has passed is "expired" before anything else (design.md §16): the
    most useful thing to know about a dead code is that it's dead.
    """
    if now is not None and expired_by_expiry(view.details, now):
        return "expired"
    if view.from_roundup:
        return "from a roundup"
    if view.status == "too_old":
        return "old post"
    if view.status == "seeded":
        return "already around when alerts started"
    return None


def _code_page_block(
    view: CodeView, timezone: str, *, max_len: int | None = None, now: datetime | None = None
) -> str:
    """One `/shift history` entry: the fence, the details line, `src`, then `first seen`.

    The marker (`expired`, `from a roundup`, ...) is plain text after the date, not a chip:
    chips are for the fixed labels and the marker is a value. `max_len` is the room the
    whole entry may take; past it the link goes first, then the source name is cut.
    """
    code_block = f"```\n{view.code}\n```"
    first_seen = view.first_seen_at.astimezone(ZoneInfo(timezone)).date().isoformat()
    marker = _code_marker(view, now)
    marker_suffix = f"{_DETAIL_SEP}{marker}" if marker else ""
    seen_line = f"\n{_FIRST_SEEN_LEAD}{first_seen}{marker_suffix}"
    # `/shift history` (it passes `now`) also shows the details line right under the fence.
    # It never sheds: it's capped, and it's the point.
    line = (
        details_line(view.details, now=now, show_expiry=marker != "expired", relative=True)
        if now is not None
        else None
    )
    head = f"{code_block}\n{line}" if line else code_block
    source = _truncate_utf16(esc(view.source_name), _CODE_PAGE_MAX_SOURCE, suffix="…")
    safe_url = _shown_link(view.item_url)
    link = f"{_DETAIL_SEP}<{safe_url}>" if safe_url else ""
    block = f"{head}\n{_SRC_LEAD}{source}{link}{seen_line}"
    if max_len is None or discord_len(block) <= max_len:
        return block

    # Same shedding order as `_alert_block`: the link goes first (a
    # collected URL is the part most likely to be long and least likely
    # to be missed; the code and its source are the point).
    block = f"{head}\n{_SRC_LEAD}{source}{seen_line}"
    if discord_len(block) <= max_len:
        return block

    # Still too long: hard-truncate the source name. The fenced code
    # block never shrinks; a partial code would be actively wrong, and
    # cutting mid-fence would unbalance every block after it.
    fixed_len = discord_len(head) + 1 + discord_len(_SRC_LEAD) + discord_len(seen_line)
    name_budget = max(max_len - fixed_len, 0)
    truncated_name = _truncate_utf16(esc(view.source_name), name_budget, suffix="…")
    return f"{head}\n{_SRC_LEAD}{truncated_name}{seen_line}"


# --- /shift codes and /shift history pages (design.md §16) ---
#
# Pages fill by size, not by count: an embed description holds 4,096 units, a code's
# entry is rarely more than a hundred and can be several hundred when a reward is long,
# and "eight per page" was only ever a polite guess. Entries never split across pages
# (an entry is at most about 700 units, so one always fits), and a group that runs onto
# the next page gets its heading again with "(continued)", so nobody has to scroll
# back up to find out whether they're reading Cosmetics or Permanent.

_CURRENT_GROUPS = (
    ("golden", terminal.GROUP_GOLDEN_KEYS),
    ("cosmetic", terminal.GROUP_COSMETICS),
    ("permanent", terminal.GROUP_PERMANENT),
    ("other", terminal.GROUP_OTHER),
)
_CURRENT_KINDS = ("all", *(key for key, _ in _CURRENT_GROUPS if key != "other"))
_HISTORY_FOOTER_NOTE = "Where the expiry is unknown, older codes may have stopped working."


def _pack_sections(sections: list[tuple[str | None, list[str]]]) -> list[str]:
    """Greedily fill descriptions from `(heading, entries)` sections; entries never split.

    Entries are joined by a blank line; a section's heading rides on its first entry on
    each page (as `[heading (continued)]` on every page but the first). A section with no heading
    is just entries. An entry that can't fit a page by itself raises ValueError; the
    caps make that impossible, and a loud failure beats a silently shortened reward.
    """
    pages: list[str] = []
    parts: list[str] = []
    used = 0
    for heading, entries in sections:
        on_page = False  # whether this section's heading is already on the current page
        started = False  # whether this section has put an entry anywhere yet
        for entry in entries:
            while True:
                label = None
                if heading is not None and not on_page:
                    label = terminal.heading(heading, continued=started)
                block = f"{label}\n{entry}" if label else entry
                cost = discord_len(block) + (2 if parts else 0)
                if used + cost <= _DESCRIPTION_LIMIT:
                    break
                if not parts:
                    raise ValueError(f"a /shift entry is {cost} UTF-16 units; a page holds less")
                pages.append("\n\n".join(parts))
                parts, used, on_page = [], 0, False
            parts.append(block)
            used += cost
            on_page = started = True
    if parts:
        pages.append("\n\n".join(parts))
    return pages


def _page_embeds(
    title: str, descriptions: list[str], footer: str, empty: str
) -> list[discord.Embed]:
    """One embed per description, titled and footed "Page i of n"; or one with `empty`.

    Titles are fixed constants from `terminal`, never user text, and so they go in as they
    are. That is on purpose: `esc()` would turn the title's "> " into "\\> " and the
    prompt would print its own backslash. (The truncation stays; it costs nothing and
    a title that ever grew past Discord's cap should still be a title.) Every page is
    green, the empty one included.
    """
    safe_title = _truncate_utf16(title, _TITLE_LIMIT)
    descriptions = descriptions or [empty]
    embeds = []
    for i, description in enumerate(descriptions, start=1):
        embed = discord.Embed(title=safe_title, color=terminal.SHIFT_GREEN, description=description)
        embed.set_footer(text=f"{footer} · Page {i} of {len(descriptions)}")
        embeds.append(embed)
    return embeds


def _current_group(code: CurrentCode) -> str:
    category = code.details.category if code.details else None
    return category if category in ("golden", "cosmetic", "permanent") else "other"


def _current_newest(code: CurrentCode) -> datetime:
    """When a code counts as new: its "Added" day (Pacific midnight), else its first sighting."""
    added = code.details.added_on if code.details else None
    if added is not None:
        return datetime.combine(added, time.min, tzinfo=SHIFT_TZ)
    return _aware(code.first_seen_at)


def _current_entry(code: CurrentCode, now: datetime) -> str:
    """The code in its fence, then `reward` and `expires` (and a permanent code's origin) under it.

    A permanent code reads `expires` never, with its origin, when it has one, as plain
    escaped text after it. A code with no known expiry or no reward says "unknown" in its
    place. Only the labels are chips; the reward and the origin are scraped text.
    """
    d = code.details
    reward = _detail_text(d.reward) if d else ""
    expiry = _expiry_value(d, now, with_days=True)
    word, value = expiry if expiry is not None else (terminal.EXPIRES, "unknown")
    line = f"{_REWARD_LEAD}{esc(reward) if reward else 'unknown'}"
    line += f"{_DETAIL_SEP}{terminal.label(word)} {value}"
    if d is not None and d.category == "permanent":
        origin = _detail_text(d.origin)
        if origin:
            line += f"{_DETAIL_SEP}{esc(origin)}"
    return f"```\n{code.code}\n```\n{line}"


def render_current_pages(
    entries: Sequence[CurrentCode], *, kind: str, now: datetime, footer: str, empty: str
) -> list[discord.Embed]:
    """`/shift codes`: the codes that should still work, grouped, newest first, paged by size.

    Groups run golden keys, cosmetics, permanent, other (a code with no details, or
    whose reward is none of the first three, is Other), and `kind` ("all", "golden",
    "cosmetic" or "permanent") keeps one of them. Inside a group the newest comes first:
    by the table's "Added" day when it has one, else the bot's first sighting. `footer`
    is the sources line; "Page i of n" is added here. `empty` is the whole page when
    nothing is current. Deciding what counts as current is the store's business
    (`repo.current_codes`); this only draws what it's given.
    """
    if kind not in _CURRENT_KINDS:
        raise ValueError(f"unknown /shift codes type {kind!r}")
    sections: list[tuple[str | None, list[str]]] = []
    for key, heading in _CURRENT_GROUPS:
        if kind not in ("all", key):
            continue
        members = sorted(
            (c for c in entries if _current_group(c) == key), key=_current_newest, reverse=True
        )
        if members:
            sections.append((heading, [_current_entry(c, now) for c in members]))
    return _page_embeds(terminal.PAGE_TITLES_CURRENT[kind], _pack_sections(sections), footer, empty)


def render_history_pages(
    views: Sequence[CodeView], *, timezone: str, now: datetime
) -> list[discord.Embed]:
    """`/shift history`: every code seen in the window, as given (newest first), paged by size.

    The old dated list, now with the fence, a details line, `src` and `first seen` under each
    code, and an "expired" marker for a code whose known expiry has passed. The title is
    always the history one; there's no way to hand it another. An entry too big for a page on its
    own (a very long collected URL, most likely) sheds its link like an alert does.
    """
    blocks = [_code_page_block(v, timezone, max_len=_DESCRIPTION_LIMIT, now=now) for v in views]
    return _page_embeds(
        terminal.PAGE_TITLE_HISTORY,
        _pack_sections([(None, blocks)]),
        _HISTORY_FOOTER_NOTE,
        _CODE_PAGE_EMPTY,
    )


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
    # The exact first paragraph of `content` (ping prefix included), or the continuation
    # header. An edit re-renders the blocks under it and keeps this as it went out. Empty
    # for a follow-up: its prompt line is part of the content, and nothing ever edits it.
    header: str = ""
    # Whether this message's batch was mixed golden and plain, so its golden entries wear
    # "Golden Key: ". An edit needs it to rebuild the same blocks; it can't be derived
    # from the message's own codes (a continuation may hold only the golden ones).
    golden_prefix: bool = False


def _alert_header(candidates: list[CodeCandidate], *, plural: bool, test: bool = False) -> str:
    """The prompt chip that heads a new-code alert: `> new SHiFT codes ▌` and its kin.

    "Golden Key" when every code in the batch is golden, "SHiFT" otherwise; the plural
    follows the batch, because the header has to be fixed before packing (it's part of the
    budget). A test alert says `[test]` so it can't pass for a real one.
    """
    kind = "Golden Key" if all(c.golden for c in candidates) else "SHiFT"
    suffix = "s" if plural else ""
    return terminal.prompt(f"{'[test] ' if test else ''}new {kind} code{suffix}")


def _continuation_header(*, test: bool = False) -> str:
    """The header on every message after a batch's first: never pinged, `[test]` if the batch is."""
    return terminal.prompt(f"{'[test] ' if test else ''}new SHiFT codes (continued)")


def _alert_block(
    candidate: CodeCandidate,
    *,
    golden_prefix: bool,
    link: bool = True,
    line: str | None = None,
) -> str:
    """One code's entry: its fence, the details line if there is one, then `src` and its link.

    The details line sits right under the fence (design.md §16). `link=False` is the
    one thing an entry sheds when it's too big: the code is the point, the source is
    what a reader checks before trusting it, and the link is mostly redundant with
    "go redeem it". The source name never shrinks any more (config caps it at 100
    characters, so there is nothing left to shrink).
    """
    # Checked, not just trusted: this is the one place a code goes out to
    # Discord, and a code that somehow isn't shaped like a code (a bug
    # upstream, not a real scenario) is worth a loud failure here rather
    # than a quietly wrong alert. A plain `assert` strips out under `-O`;
    # this doesn't get to.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    prefix = "Golden Key: " if golden_prefix and candidate.golden else ""
    safe_url = _shown_link(candidate.item_url) if link else None
    shown_link = f" · <{safe_url}>" if safe_url else ""
    lines = [f"```\n{candidate.code}\n```"]
    if line:
        lines.append(line)
    lines.append(f"{_SRC_LEAD}{prefix}{esc(candidate.source_name)}{shown_link}")
    return "\n".join(lines)


def _line_for(
    code: str, details: Mapping[str, CodeDetailsRow] | None, now: datetime | None
) -> str | None:
    """The details line for `code` out of a `details` mapping, or None if it has none."""
    if not details:
        return None
    if now is None:
        raise ValueError("rendering details needs a clock (now)")
    return details_line(details.get(code), now=now)


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
    details: Mapping[str, CodeDetailsRow] | None = None,
    now: datetime | None = None,
    reserve: int = 0,
) -> list[RenderedAlert]:
    """Render a batch of new SHiFT codes into one or more alert messages.

    One ping covers the whole batch (A4): only the first message's header
    carries `@everyone` (and only if `ping` is true to begin with); every
    continuation starts with `_continuation_header` instead and never
    pings, no matter how many messages the batch spills into. Mixed
    golden/non-golden batches (A3) keep the plain "new SHiFT code(s)"
    title but prefix each golden entry with "Golden Key:" so it doesn't
    read as an ordinary code. Headers are prompt chips (`terminal.prompt`);
    the shapes are in design.md §17.

    `ping_mention` is the text a ping starts with: `"@everyone"` (the
    default, which is all v2 ever used) or a role mention like `"<@&123>"`
    for a server that picked a role. It only matters when `ping` is true.

    `nonce_scope` salts every message's nonce (the fan-out passes the server
    and channel ids). Without it, two servers getting the same batch would
    send the same nonce, and Discord may hand the second one the first one's
    message back instead of posting. Same scope, same nonces: a retry to the
    same channel still reuses its own.

    `details` (code -> its stored details) puts a details line under each code that
    has something to say, read against `now` (required when `details` is given).
    `reserve` is `details_reserve(...)`: every code is packed as if its line were that
    big, whether or not it has one yet, so an edit that adds or lengthens a line later
    re-renders into the same message and still fits (design.md §16). The reserve is
    a floor, not a promise I'm willing to rely on: a line that somehow outgrows it is
    packed at its real size instead, and anything that still can't fit a message on its
    own sheds its link and then raises.
    """
    if not candidates:
        return []

    ping_prefix = f"{ping_mention or '@everyone'} " if ping else ""
    mixed = any(c.golden for c in candidates) and not all(c.golden for c in candidates)
    first_header = ping_prefix + _alert_header(candidates, plural=len(candidates) > 1, test=test)
    continuation = _continuation_header(test=test)

    # The most room any one entry can ever count on: alone in its own
    # message, under whichever header is longer, plus the "\n\n" joining the header to the
    # block. Which one that is depends on the batch: a ping makes the first header longer,
    # and with no ping the continuation's "(continued)" usually wins. So take the max.
    solo_budget = (
        _ALERT_CONTENT_LIMIT - max(discord_len(first_header), discord_len(continuation)) - 2
    )
    # Each entry is (code, content, slot). The content carries the real details line;
    # the slot is what packing counts: the block without any line, plus the reserve.
    entries: list[tuple[str, str, int]] = []
    for c in candidates:
        line = _line_for(c.code, details, now)
        line_cost = discord_len(line) + 1 if line else 0
        room = max(reserve, line_cost)
        for with_link in (True, False):
            slot = discord_len(_alert_block(c, golden_prefix=mixed, link=with_link)) + room
            if slot <= solo_budget:
                break
        else:
            # The link was the only thing that could go. If the rest still doesn't fit
            # alone in a message, no amount of packing will help it.
            raise ValueError(
                f"alert entry for {c.code!r} can't fit in one message "
                f"({slot} UTF-16 units of {solo_budget}), even without its link"
            )
        entries.append(
            (c.code, _alert_block(c, golden_prefix=mixed, link=with_link, line=line), slot)
        )

    # Greedily pack entries into batches under Discord's 2000-char message
    # cap, counting each batch's own header (the first and the continuation differ in
    # length, and which is longer depends on the ping) plus a "\n\n" joiner per entry.
    # Every entry is already guaranteed to fit `solo_budget` on its own
    # (see above), so this loop only ever has to decide when to start a
    # *new* batch, never split one entry across two.
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_len = 0
    for code, block, slot in entries:
        header_len = discord_len(first_header if not batches else continuation)
        block_len = slot + 2  # "\n\n" joining it to the header/prior block
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
        header = first_header if i == 0 else continuation
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
                header=header,
                golden_prefix=mixed,
            )
        )
    return rendered


def render_alert_edit(
    header: str,
    candidates: list[CodeCandidate],
    *,
    golden_prefix: bool,
    details: Mapping[str, CodeDetailsRow] | None,
    now: datetime,
) -> str:
    """The new content for an alert message that's being edited in place (design.md §16).

    `header` is the first paragraph as it was stored when the message went out (ping
    prefix included, or not, if Discord made us strip it), and `candidates` are the
    message's own codes in the order they were packed. The blocks are the ones
    `render_code_alerts` made, with whatever the details say today. They fit because
    the original packing reserved room for the worst line; if the config has since gained
    a longer source name and they somehow don't, every link goes, and if that still
    isn't enough this raises ValueError for the caller to log. The header's ping prefix goes
    back out as it went in: the edit pass sends the message's stored ping choice with it
    (`mentions_for`), but only while the header still starts with that ping's own text, so an
    edit repeats a mention the message already had and never adds one.

    The stored header is also the layout marker (design.md §17). A header in the terminal look
    (`terminal.is_terminal_header`) is edited in the current layout. Anything else is a v3.1
    alert, edited in the frozen classic layout it was packed for, and "anything else" includes a
    header I don't recognize at all. Unknown means classic because classic is the smaller of the
    two: a classic render fits a message packed for either layout, and the reverse isn't true.
    """
    if terminal.is_terminal_header(header):
        block = _alert_block

        def line_for(code: str) -> str | None:
            return _line_for(code, details, now)
    else:
        block = _classic_alert_block

        def line_for(code: str) -> str | None:
            return _classic_details_line(details.get(code)) if details else None

    for with_link in (True, False):
        blocks = [
            block(
                c,
                golden_prefix=golden_prefix,
                link=with_link,
                line=line_for(c.code),
            )
            for c in candidates
        ]
        content = "\n\n".join([header, *blocks])
        if discord_len(content) <= _ALERT_CONTENT_LIMIT:
            return content
    raise ValueError(
        f"edited alert content exceeds {_ALERT_CONTENT_LIMIT} UTF-16 units "
        f"({discord_len(content)}), even without links; codes: {[c.code for c in candidates]}"
    )


def _roundup_header(
    source_name: str, item_url: str, count: int, *, max_len: int | None = None
) -> str:
    """The first line of a roundup: `` `> SHiFT roundup ▌` 7 codes from Some Site · <link> ``.

    `count` is how many codes this group is rendering (the claimed ones, after a partial
    claim), not how many land in this one message; the header is fixed before packing.
    """
    codes_text = f"{count} code{'' if count == 1 else 's'}"
    lead = f"{terminal.prompt(terminal.ROUNDUP_HEADER)} {codes_text} from "
    safe_url = _shown_link(item_url)
    link = f" · <{safe_url}>" if safe_url else ""
    header = f"{lead}{esc(source_name)}{link}"
    if max_len is None or discord_len(header) <= max_len:
        return header

    # Same shedding order as `_alert_block`: the link is the first thing to go (it's
    # redundant with "click the code" anyway), and if a hostile or just very long source
    # name still doesn't fit, hard-truncate it. The chip and the count never shrink; the
    # chip is what tells a reader this code didn't come with a ping.
    header = f"{lead}{esc(source_name)}"
    if discord_len(header) <= max_len:
        return header

    name_budget = max(max_len - discord_len(lead), 0)
    return f"{lead}{_truncate_utf16(esc(source_name), name_budget, suffix='…')}"


def _roundup_code_block(candidate: CodeCandidate, line: str | None = None) -> str:
    # No source/link per entry (the header already carries the one
    # source and link every code in this group shares) and no golden-key
    # prefix (design.md §13 doesn't ask for one here): just the code
    # itself, select-and-copy-able the same way a normal alert's code is,
    # and its details line when the bot happens to know one.
    if not is_code(candidate.code):
        raise ValueError(f"not a SHiFT code: {candidate.code!r}")
    block = f"```\n{candidate.code}\n```"
    return f"{block}\n{line}" if line else block


def render_roundup_alerts(
    candidates: list[CodeCandidate],
    *,
    nonce_scope: str = "",
    details: Mapping[str, CodeDetailsRow] | None = None,
    now: datetime | None = None,
) -> list[RenderedAlert]:
    """Render fresh roundup-only codes into unpinged `> SHiFT roundup ▌` messages (design.md §13).

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
    the "(continued)" roundup prompt, and no group's codes ever share a message with
    another group's (a header names one specific roundup post; mixing two
    posts' codes under one header would misattribute them).

    `nonce_scope` salts the nonces per server and channel; see `render_code_alerts`.

    Roundup codes show a details line if `details` has one (read against `now`), sized
    by what it really is. There is deliberately no reservation here: nothing ever edits
    a roundup (design.md §16: their details live in `/shift codes`), so there is no
    later, bigger line to leave room for.
    """
    if not candidates:
        return []

    rendered: list[RenderedAlert] = []
    for group in group_roundups(candidates):
        entries = [(c.code, _roundup_code_block(c, _line_for(c.code, details, now))) for c in group]
        # Every code block is short and fixed-shape (a 29-character code
        # in a fenced block), so it's always the header, not the block,
        # that's at risk of busting the cap (a ~2000-char URL, a hostile
        # source name). Give `_roundup_header` a budget that guarantees it
        # fits alongside this group's largest block before measuring anything
        # else, instead of discovering the overflow after the fact. Largest, not
        # first: a later code can carry a details line (hundreds of units) that the
        # first one doesn't, and any block may land under the header.
        largest_block_len = max(discord_len(block) for _, block in entries) + 2  # "\n\n" joiner
        continuation = terminal.prompt(terminal.ROUNDUP_CONTINUATION)
        first_header = _roundup_header(
            group[0].source_name,
            group[0].item_url,
            len(group),
            max_len=_ALERT_CONTENT_LIMIT - largest_block_len,
        )
        solo_budget = (
            _ALERT_CONTENT_LIMIT - max(discord_len(first_header), discord_len(continuation)) - 2
        )
        # A second check, not the mechanism that keeps things under budget:
        # `_roundup_header`'s own shrinking already guarantees the largest block fits
        # under `first_header`; this still catches a code block busting the
        # *continuation* header's budget, which never shrinks.
        for code, block in entries:
            if discord_len(block) > solo_budget:
                raise ValueError(f"roundup code block for {code!r} exceeds the per-message budget")

        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_len = 0
        for code, block in entries:
            header_len = discord_len(first_header if not batches else continuation)
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
            header = first_header if i == 0 else continuation
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
                RenderedAlert(
                    content=content, codes=batch_codes, ping=False, nonce=nonce, header=header
                )
            )
    return rendered


# One follow-up message names at most this many codes; a bigger burst spills into
# "(continued)" messages under the same ping. 20 keeps any one message from turning
# into a wall (a fenced code is three lines tall now).
FOLLOWUP_MAX_CODES = 20


def _followup_entry(
    code: str, details: Mapping[str, CodeDetailsRow] | None, now: datetime | None
) -> str:
    """One follow-up entry: the code in its fence, plus its details line if it has one."""
    entry = f"```\n{code}\n```"
    line = _line_for(code, details, now)
    return f"{entry}\n{line}" if line else entry


def render_followup_alerts(
    codes: list[str],
    *,
    ping_mention: str,
    nonce_scope: str = "",
    details: Mapping[str, CodeDetailsRow] | None = None,
    now: datetime | None = None,
) -> list[RenderedAlert]:
    """The "confirmed by a second source" follow-up (plan D14): one ping, then a message per batch.

    A community code that already posted unpinged just got a second,
    independent source. This is the nudge: the ping and a prompt line, then each
    code alone in its fence so it copies cleanly (the original alert has the same
    block; this one repeats it so nobody has to scroll). `ping_mention` is the text a
    server's ping choice turns into (`@everyone` or a role mention), and the
    allowed-mentions that make it actually notify come from `mentions_for` at send
    time, as for every other alert.

    Codes are packed in order, greedily, by their real rendered size: a message takes
    codes until the next one would pass `FOLLOWUP_MAX_CODES` or 2,000 UTF-16 units. Only
    the first message pings. Every later one wears the fixed continuation chip instead
    and never pings, the way an alert batch spills over (`render_code_alerts`). A
    burst that doesn't fit one message is therefore still one claim and one unit of
    the day's ping budget, which is the whole reason this isn't capped at one message.

    A code with a details line (`details`, read against `now`) gets it right under
    its fence, as the details stand right now. There's no `src` line: the follow-up
    exists because of a second source, and the `via` part of the details line names who
    supplied the facts. Nothing edits a follow-up, so it isn't given a reserve, and its
    `header` stays empty (the prompt line is part of the content).

    Each message's nonce is salted with a `followup|` marker as well as the server
    scope, so Discord can't mistake it for the original alert's send, and with the
    message's position, so message 2 can't be mistaken for message 1. A retry of the
    same message reuses its own.

    Returns `[]` for no codes, or when not even the first code fits a pinged message
    (that takes a ping mention close to 2,000 units; the caller treats it as the bug it
    is). A later code that can't fit a message of its own is the same bug one message
    on, and raises.
    """
    for code in codes:
        if not is_code(code):
            raise ValueError(f"not a SHiFT code: {code!r}")
    if not codes:
        return []
    ping_prefix = f"{ping_mention} "
    first_header = f"{ping_prefix}{terminal.prompt(terminal.FOLLOWUP_HEADER)}"
    continuation = terminal.prompt(terminal.FOLLOWUP_CONTINUATION)

    # Each batch is a list of (code, entry). A message is its header plus "\n\n" and an
    # entry per code, and UTF-16 lengths add, so the running total is exact.
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_len = 0
    for code in codes:
        entry = _followup_entry(code, details, now)
        cost = discord_len(entry) + 2
        if current and (
            len(current) >= FOLLOWUP_MAX_CODES or current_len + cost > _ALERT_CONTENT_LIMIT
        ):
            batches.append(current)
            current = []
        if not current:
            current_len = discord_len(first_header if not batches else continuation)
            if current_len + cost > _ALERT_CONTENT_LIMIT:
                if not batches:
                    return []
                raise ValueError(f"follow-up entry for {code!r} can't fit in a message of its own")
        current.append((code, entry))
        current_len += cost
    batches.append(current)

    rendered = []
    for i, batch in enumerate(batches):
        header = first_header if i == 0 else continuation
        content = "\n\n".join([header, *(entry for _, entry in batch)])
        # A second check behind the packing loop above, as in `render_code_alerts`: a
        # message that snuck past the cap would be rejected by Discord, and a code lost
        # that way is a code nobody notices is gone. Louder than an `assert`, which
        # `-O` strips.
        if discord_len(content) > _ALERT_CONTENT_LIMIT:
            raise ValueError(
                f"rendered follow-up exceeds {_ALERT_CONTENT_LIMIT} UTF-16 units "
                f"({discord_len(content)}); codes: {[code for code, _ in batch]}"
            )
        batch_codes = [code for code, _ in batch]
        nonce = hashlib.sha256(
            f"{_nonce_salt(nonce_scope)}followup|{'|'.join(batch_codes)}|{i}".encode()
        ).hexdigest()[:25]
        rendered.append(
            RenderedAlert(
                content=content,
                codes=batch_codes,
                ping=i == 0,
                nonce=nonce,
                ping_prefix=ping_prefix if i == 0 else "",
            )
        )
    return rendered


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
    safe_url = _shown_link(item.url)
    if safe_url is None or discord_len(safe_url) > _MAX_HEADLINE_URL:
        return None
    # Titles come from feeds and social posts, and a Bluesky post has line
    # breaks. Left alone, one could start its own line in this list and pose as
    # an official headline, or as the renderer's own "+N more". split() with no
    # argument breaks on every kind of whitespace, so this flattens all of it.
    # The same goes for a title that dresses itself in our label: a community post called
    # "🟢 OFFICIAL · free codes" would sit in this list looking like the real thing, so
    # anything that reads as one of our markers comes off before the cut and the escape.
    title = _truncate_utf16(
        _without_labels(" ".join(item.title.split())), _MAX_HEADLINE_TITLE, suffix="…"
    )
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


# --- The industry post (design.md §18) ---
#
# "Gaming industry" isn't a game, so its post follows its own rules: the stories (or
# the source titles, on a free server) stay in the order they were handed in, at most
# a handful, and there is no "+N more, use /news" line, because /news has nothing to
# add for it. What doesn't fit is shed from the bottom and nobody is told. The
# renderers above stay as they were; these sit beside them.


def _cut(parts: Sequence[str], cap: int | None) -> list[str]:
    """The first `cap` of `parts`; below 1 means none (`[:-1]` would keep all but one)."""
    return list(parts) if cap is None else list(parts[: max(cap, 0)])


def _shed_to_fit(parts: Sequence[str], sep: str, head: str | None = None) -> str | None:
    """Join `parts` (under an optional `head`), dropping whole parts from the bottom until it fits.

    Never says how many were dropped, and never cuts a part in the middle: half a
    line is how a link ends up dead. A part that is over the limit by itself is
    skipped; if the `head` is what keeps the first part from fitting, the head goes
    instead. `None` when nothing is left to show.
    """
    fits = [part for part in parts if discord_len(part) <= _DESCRIPTION_LIMIT]
    for top in ([head] if head else [], []):
        for kept in range(len(fits), 0, -1):
            description = sep.join([*top, *fits[:kept]])
            if discord_len(description) <= _DESCRIPTION_LIMIT:
                return description
    return None


def _lines_embed(
    game: GameInfo, lines: Sequence[str], head: str | None = None
) -> discord.Embed | None:
    """One line per row under `game`'s title, shed from the bottom past 4096 units.

    `head` (a note, such as "Summary unavailable") goes on top, escaped. Returns `None`
    when there are no lines: a game with nothing posts nothing.
    """
    description = _shed_to_fit(lines, "\n", esc(head) if head else None)
    if description is None:
        return None
    return discord.Embed(
        title=_truncate_utf16(esc(game.name), _TITLE_LIMIT),
        description=description,
        color=_topic_color(game.key),
    )


def _fitting_story_block(story: StoryDraft) -> str | None:
    """`_story_block`, or the same story with fewer links if the block alone is too long.

    Story links have no length cap (headline URLs do), so a few long ones can push a
    block past the limit. Whole links go from the end until it fits; a story that is
    still too long with no links at all is skipped. Cutting the block mid-text would
    leave an unclosed `<https://...`.
    """
    block = _story_block(story)
    if discord_len(block) <= _DESCRIPTION_LIMIT:
        return block
    # Count only the links that will really show, or an unusable first URL costs a
    # usable one its place.
    usable = [url for url in story.item_urls if _shown_link(url) is not None]
    for shown in range(min(len(usable), _MAX_LINKS_SHOWN), -1, -1):
        block = _story_block(replace(story, item_urls=usable[:shown])).rstrip("\n")
        if discord_len(block) <= _DESCRIPTION_LIMIT:
            return block
    return None


def _ranked_embed(
    game: GameInfo, stories: Sequence[StoryDraft], cap: int | None = None
) -> discord.Embed | None:
    """The stories as `_topic_embed` writes them, but in the order given and with no "+N more".

    The model ranked these by significance, so `_sort_key` (official first) stays out
    of it. UPDATE and label markers are kept. A story that can't fit even without its
    links is skipped before the cut to `cap`. Returns `None` for no stories.
    """
    blocks = [block for story in stories if (block := _fitting_story_block(story))]
    description = _shed_to_fit(_cut(blocks, cap), "\n\n")
    if description is None:
        return None
    return discord.Embed(
        title=_truncate_utf16(esc(game.name), _TITLE_LIMIT),
        description=description,
        color=_topic_color(game.key),
    )


def render_picked_headlines_embed(
    game: GameInfo, items: Sequence[HeadlineItem], cap: int | None = None
) -> discord.Embed | None:
    """A free server's industry post: the model's picks as source headlines, in its order.

    Each pick is a `_headline_line` (source title and URL, `🟢 OFFICIAL` on official
    items only); nothing the model wrote reaches it. Picks without a usable URL are
    dropped first, then the rest are cut to `cap` if there is one. Returns `None` when
    nothing is left.
    """
    lines = [line for item in items if (line := _headline_line(item))]
    return _lines_embed(game, _cut(lines, cap))


def render_capped_headlines_embed(
    game: GameInfo, items: Sequence[HeadlineItem], note: str | None, cap: int
) -> discord.Embed | None:
    """The headline list, sorted as `render_headlines_embed` sorts it, cut to `cap` lines.

    This is the industry post's fallback when there's no pick to show. `note` goes on
    top; there is no "+N more".
    """
    lines = [
        line for item in sorted(items, key=_headline_sort_key) if (line := _headline_line(item))
    ]
    return _lines_embed(game, _cut(lines, cap), note)


def render_guild_digest(
    run_date: date,
    games: Sequence[GameInfo],
    channels: Mapping[str, int],
    *,
    stories_by_game: Mapping[str, list[StoryDraft]],
    items_by_game: Mapping[str, Sequence[HeadlineItem]],
    notes_by_game: Mapping[str, str | None],
    coverage_notes: list[str],
    picks_by_game: Mapping[str, Sequence[HeadlineItem]] | None = None,
    capped: Mapping[str, int] | None = None,
) -> RenderedDigest:
    """One server's digest: a `TopicMessage` for each followed game that has something to say.

    `games` are the server's followed games in catalog order and `channels` maps each
    game key to its channel. A game whose key is in `stories_by_game` has a stored
    summary (comped): its stories are rendered as v2's embed, and an empty list means
    the summary found nothing, so nothing posts. Every other game gets its headlines
    from `items_by_game`, with `notes_by_game[key]` on top if there is one. A game with
    nothing to show is left out.

    The industry post uses the two optional mappings (design.md §18). A key in
    `capped` is cut to that many entries and never gets a "+N more": with stories, they
    render in the order given (`_ranked_embed`, no official-first sort). A key in
    `picks_by_game` renders those headlines in the order given, cut to its cap if it
    has one; an empty pick list posts nothing and never falls back to `items_by_game`.
    A capped key with neither gets the usual headlines, sorted and cut
    (`render_capped_headlines_embed`). Both mappings default to `None`, which is every
    other game's behavior, unchanged.
    """
    footer = _coverage_footer(coverage_notes)
    picks_by_game = picks_by_game or {}
    capped = capped or {}
    messages = []
    for game in games:
        if game.key in stories_by_game:
            stories = stories_by_game[game.key]
            if game.key in capped:
                embed = _ranked_embed(game, stories, capped[game.key])
            else:
                embed = _topic_embed(game, stories) if stories else None
        elif game.key in picks_by_game:
            embed = render_picked_headlines_embed(
                game, picks_by_game[game.key], capped.get(game.key)
            )
        elif game.key in capped:
            embed = render_capped_headlines_embed(
                game,
                items_by_game.get(game.key, []),
                notes_by_game.get(game.key),
                capped[game.key],
            )
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


# The Sources line names one failing source and says how many more there are, since a
# server's admin can do nothing about the others anyway; the owner's report has the lot.
_REPORT_ERROR_SNIPPET = 150


@dataclass(frozen=True)
class ReportSources:
    """The health of the distinct sources behind one server's games, as of report time.

    `total` counts each source once, however many games it feeds. `failing` is the
    sources with `consecutive_failures > 0`, worst first, as `(name, last error)`;
    `unchecked` is how many have no health row yet (never collected, which isn't the
    same as being fine). Same arithmetic as `/newsbot status`'s per-game clause.
    """

    total: int
    unchecked: int
    failing: Sequence[tuple[str, str | None]]


@dataclass(frozen=True)
class ReportClaude:
    """What a comped digest's summaries cost and when they were made.

    `cost_usd` is `None` when no row had token counts (a row from before they were
    recorded reads as zero, which is "unknown", not "free"). `prepared_at` has one
    entry per stored summary the digest showed; `shared` means another server could
    have used the same rows, and the cost is the rows' whole cost either way, not a split.
    """

    cost_usd: float | None
    prepared_at: Sequence[datetime]
    shared: bool


def _report_cost_str(spend_usd: float) -> str:
    # "<$0.01" for anything that'd otherwise round to "$0.00": a summary that costs
    # half a cent still cost something, and "$0.00" reads as free, which it isn't.
    if spend_usd < 0.01:
        return "<$0.01"
    return f"~${spend_usd:.2f}"


def report_claude_from_rows(
    rows: Sequence[tuple[int, int, datetime | None]], shared: bool
) -> ReportClaude | None:
    """Fold stored summary rows `(input tokens, output tokens, written at)` into a `ReportClaude`.

    `None` for no rows at all. A row with no tokens at all contributes no cost.
    """
    if not rows:
        return None
    priced = [(i, o) for i, o, _ in rows if i or o]
    cost = sum(estimate_spend_usd(i, o) for i, o in priced) if priced else None
    return ReportClaude(cost, [at for _, _, at in rows if at is not None], shared)


def _report_prepared_text(prepared_at: Sequence[datetime], timezone: str) -> str | None:
    """The "summaries prepared at 08:30" text in the server's clock, or None with nothing to say."""
    if not prepared_at:
        return None
    try:
        zone = ZoneInfo(timezone)
        suffix = ""
    except ZoneInfoNotFoundError, ValueError, OSError:
        # A report that can't name the time zone still beats no report.
        zone, suffix = UTC, " UTC"
    local = sorted(
        (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(zone) for at in prepared_at
    )
    first, last = local[0].strftime("%H:%M"), local[-1].strftime("%H:%M")
    when = first if first == last else f"{first} to {last}"
    noun = "summary" if len(prepared_at) == 1 else "summaries"
    return f"{noun} prepared at {when}{suffix}"


def _report_claude_line(claude: ReportClaude, timezone: str, took: str) -> str:
    head = _report_cost_str(claude.cost_usd) if claude.cost_usd is not None else ""
    if claude.shared:
        head = f"{head}, shared" if head else "shared"
    prepared = _report_prepared_text(claude.prepared_at, timezone)
    if head and prepared:
        return f"Claude: {head} ({prepared}) · took {took}"
    if head or prepared:
        return f"Claude: {head or prepared} · took {took}"
    return f"Took {took}"


def _report_sources_line(sources: ReportSources, *, with_detail: bool) -> str | None:
    total = sources.total
    if total <= 0:
        return None
    if sources.unchecked >= total:
        return f"Sources: {total} {'source' if total == 1 else 'sources'} not checked yet"
    ok = total - sources.unchecked - len(sources.failing)
    line = f"Sources: {ok} of {total} ok"
    if sources.unchecked:
        line += f", {sources.unchecked} not checked yet"
    if not with_detail or not sources.failing:
        return line
    name, error = sources.failing[0]
    first = next((ln for ln in (error or "").splitlines() if ln.strip()), "")
    # Cut to one line, then shorten each URL, then cap, then escape: the escaping goes
    # last so a backslash it adds can't be cut in half, and so a `[text](url)` in an
    # error can't come out as a live link. The URL itself stays readable.
    text = plain_line(_redact_urls(plain_line(first, 400)), _REPORT_ERROR_SNIPPET)
    # Markdown's `[text](target)` needs the two brackets to touch; a zero-width space
    # between them leaves the text readable and the link dead.
    text = text.replace("](", "]\u200b(")
    parts = [f"{esc(plain_line(name, 60))}: {esc(text) if text else 'unknown error'}"]
    extra = len(sources.failing) - 1
    if extra > 0:
        parts.append(f"+{extra} more")
    return f"{line} ({'; '.join(parts)})"


def _count_noun(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _report_stories_label(story_total: int, headline_total: int, comped: bool) -> str:
    """The count's label: "8 stories", "8 items" (a free server's headlines), or both."""
    if not comped:
        return _count_noun(story_total + headline_total, "item", "items")
    if story_total and headline_total:
        return (
            f"{_count_noun(story_total, 'story', 'stories')}, "
            f"{_count_noun(headline_total, 'headline', 'headlines')}"
        )
    if headline_total:
        return _count_noun(headline_total, "headline", "headlines")
    return _count_noun(story_total, "story", "stories")


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
    story_games: Collection[str] | None = None,
    sources: ReportSources | None = None,
    claude: ReportClaude | None = None,
    timezone: str = "UTC",
) -> str:
    """The per-server run report, for that server's admin channel (plan §3.5): one plain message.

    A per-game count with a `[jump]` link for each game that posted, any skipped game
    with its reason, the health of the sources behind its games, and how long the
    digest took. `story_games` is `None` for a free server (its counts are headlines,
    "items"); for a comped one it names the games whose count is AI stories, and the
    rest are headline fallbacks. `claude` (comped only, when summaries were used) puts
    the summaries' cost and prepare time on the last line; without it that line is just
    "Took Xs". `sources` is `None` to leave the Sources line out. Kept under
    `_ALERT_CONTENT_LIMIT` by shedding the notes, then the failing source's detail,
    then the reasons, then the links, then a flat truncation.
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
        story_total = sum(counts.get(g.key, 0) for g in games if g.key in (story_games or ()))
        headline_total = sum(counts.get(g.key, 0) for g in games) - story_total
        label = _report_stories_label(story_total, headline_total, story_games is not None)
        return f"{label}: " + " · ".join(parts)

    def skipped_line(with_reasons: bool) -> str | None:
        if not skipped:
            return None
        names = {g.key: g.name for g in games}
        parts = [
            f"{esc(names.get(key, key))}"
            + (f" ({esc(friendly_skip_reason(reason))})" if with_reasons else "")
            for key, reason in skipped.items()
        ]
        return "Skipped: " + "; ".join(parts)

    header_line = (
        f"{_REPORT_HEADER_EMOJI[status]} **{_REPORT_HEADER_VERB[status]}** · "
        f"{_report_date(run_date)} ({run_kind})"
    )
    took = _report_duration_str(duration)
    took_line = _report_claude_line(claude, timezone, took) if claude else f"Took {took}"
    notes_line = "Notes: " + esc("; ".join(notes)) if status == "partial" and notes else None

    def build(with_links: bool, with_reasons: bool, with_notes: bool, with_detail: bool) -> str:
        lines = [
            header_line,
            stories_line(with_links),
            _report_sources_line(sources, with_detail=with_detail) if sources else None,
            skipped_line(with_reasons),
        ]
        if with_notes:
            lines.append(notes_line)
        lines.append(took_line)
        return "\n".join(line for line in lines if line)

    for with_links, with_reasons, with_notes, with_detail in (
        (True, True, True, True),
        (True, True, False, True),
        (True, True, False, False),
        (True, False, False, False),
        (False, False, False, False),
    ):
        content = build(with_links, with_reasons, with_notes, with_detail)
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


# What a skipped game's reason says in a server's run report, by the same categories as
# above. The raw error stays in the logs and in the stored notes; a server's admin gets
# a sentence, not "404 Not Found (error code: 10003)". Anything else is trimmed raw text.
_SKIP_REASON_TEXT = {
    "missing permissions": "I'm missing permissions there",
    "channel gone": "its channel was deleted",
    "rate limited": "Discord rate limited me",
    "timed out": "Discord took too long to answer",
}
_SKIP_OTHER_MAX = 60


def friendly_skip_reason(raw: str) -> str:
    """A plain-English reason for a skipped game, from its raw error. Not escaped."""
    text = raw.casefold()
    for reason, pattern in _REASON_PATTERNS:
        if pattern.search(text) and reason in _SKIP_REASON_TEXT:
            return _SKIP_REASON_TEXT[reason]
    return plain_line(raw, _SKIP_OTHER_MAX) or "unknown error"


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


def _spend_amount(usd: float) -> str:
    """Dollars as the owner's report shows them: "$0.00" for none, "under $0.01", else "$X.XX"."""
    if usd <= 0:
        return "$0.00"
    if usd < 0.01:
        return "under $0.01"
    return f"${usd:.2f}"


def render_owner_report(
    outcomes: Sequence[DigestOutcome],
    failing: Sequence[SourceHealthRow],
    *,
    spend_24h_usd: float,
    spend_month_usd: float,
    month_name: str,
) -> str:
    """The owner's daily report: the summary line, the Claude spend, then every failing source (D8).

    The second line is the spend estimate: the last 24 hours, and the month so far
    (`month_name` is the owner's local month, in English). It is always there; a copy with no
    summaries yet shows "$0.00" twice. Both figures are dollars, already estimated from token
    counts by the caller, so this stays a pure function of what it's handed. The 24-hour amount
    gets an "about" once it's a cent or more; below that it reads "under $0.01".

    Capped at Discord's message limit; the source list is cut at 10 with a
    "+N more" line so the first two lines at the top always survive. Every URL
    in an error goes through `shown_url` first: httpx puts the whole address
    in its messages, credentials and token query strings included.
    """
    day = _spend_amount(spend_24h_usd)
    if spend_24h_usd >= 0.01:
        day = f"about {day}"
    lines = [
        f"newsbot daily: {render_digest_summary_line(outcomes)}",
        f"Claude spend: {day} in the last 24 hours, "
        f"{_spend_amount(spend_month_usd)} so far in {month_name} (estimate)",
    ]
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
    "ReportClaude",
    "ReportSources",
    "RenderedAlert",
    "RenderedDigest",
    "TopicMessage",
    "details_line",
    "details_reserve",
    "esc",
    "escape_list_start",
    "outcome_from_digest",
    "render_alert_edit",
    "render_code_alerts",
    "render_current_pages",
    "render_digest_summary_line",
    "render_followup_alerts",
    "render_guild_digest",
    "friendly_skip_reason",
    "render_guild_run_report",
    "render_guild_overview",
    "render_guild_status",
    "render_headlines_embed",
    "render_history_pages",
    "render_item_page",
    "render_owner_report",
    "render_roundup_alerts",
    "render_story_page",
    "report_claude_from_rows",
    "to_text",
]
