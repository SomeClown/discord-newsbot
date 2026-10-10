"""Read a code's details out of Gearbox's own words, when (and only when) they're unambiguous.

A fan site's table says what a code is worth and when it dies; Gearbox's post
usually just says "here's a code, enjoy". Now and then it says a little more:
"3 Golden Keys, expires October 14". This module reads exactly those two
patterns out of an official post and gives up on anything else (design.md §16,
"Details from Gearbox's own posts"). Giving up is the feature. A wrong expiry
is worse than a blank one, because the blank one gets filled in by mentalmars
and the wrong one outranks mentalmars.

The rules, so nobody has to rediscover them:
- the caller only asks about an official-trust post holding exactly one code
  (`decide.sightings_from_items`); with several codes nobody can say which
  detail belongs to which, and this module can't see the other codes anyway;
- the post can be much longer than the code's own announcement (a Steam patch-notes
  post is a screenful of unrelated prose with one code stuck in it somewhere), so a
  count or an expiry only counts if it's near the code: in the code's own paragraph
  (blank lines split paragraphs) and within about 200 characters either side of it.
  A bonus that "expires October 20" three paragraphs up is the bonus's business;
- a Golden Key count ("3 Golden Keys", "a Golden Key", "five Golden Keys") is
  the reward, and it makes the category golden. It has to be plainly a
  quantity: the number agrees with the noun (one key, several keys), isn't
  glued to other text ("10/14", "2.5", "twenty-five"), doesn't trail a name or
  a date ("Borderlands 4", "Oct 14", "Update 2"), and doesn't modify a noun
  ("a Golden Key code"). Two different counts, or a count per platform ("5 for
  PC and 3 for console"): no count at all;
- an expiry is "expires", "expiring", "valid until", or "valid/redeemable/good
  through" followed by a month-name date ("October 14", "Oct 14, 2026") or a
  full numeric date ("10/14/2026", US order, four-digit year). A bare "through"
  doesn't count ("the event runs through Oct 20" is about the event). Give up,
  and record no expiry at all, on:
  - two different dates, or a date that looks like one and won't parse
    ("10/14", "Feb 30");
  - a negation just before the keyword ("not valid until"), a past tense one ("was
    valid until", "were valid until") or the word "expired" anywhere nearby, or the
    post saying when something starts ("starts", "valid from"), because that date
    is the other end of the window, or the wrong tense of it;
  - a range or an alternative after the date ("Oct 14 - Oct 21", "Oct 14 or
    15");
  - a weekday that doesn't match its date;
  - a date before the post's own Pacific day, year given or not. A code announced
    already dead is a misread, and this is the cheap way to catch the ones where
    "was valid until October 2, 2026" sailed past the tense check;
- a date with no year takes its next occurrence on or after the post's publish
  day (Pacific), unless it fell in the week before that day, which is more
  likely a typo or past tense than a deadline eleven months out. With no
  publish time to go on, it takes `now`'s day; with neither, it's a date we
  can't anchor, so the answer is no expiry (a date with a year is taken as it
  comes, since there's nothing to check it against).

The date grammar is `collectors.shift_table.parse_shift_date`, the same one the
table reader uses; this module only finds the dates in prose and hands each one
over in the table's style. That import happens inside a function, because
`shift_table` pulls in `bot.format`, which pulls in `shift.decide`, which pulls
in this module. I found out about the circle the usual way.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from newsbot.collectors.base import ItemDetails
from newsbot.shift.details import SHIFT_TZ
from newsbot.shift.match import CODE_RE, defuse_codes

_WORD_COUNTS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

# A count must be a plain number or one of the words above, standing in front
# of "Golden Key(s)". The lookbehind refuses a number glued to what comes
# before it ("10/14", "2.5", "twenty-five"), the lookahead refuses "Golden Key
# code" and "Golden Key SHiFT code" (a compound noun, not a quantity), and the
# singular/plural check happens in `_read_count`. Bounded gaps everywhere: this
# runs on whatever a stranger's post contains.
_COUNT_RE = re.compile(
    r"(?<![/.\-\u2013\u2014:,\d])\b"
    r"(?P<n>\d{1,2}|a|an|one|two|three|four|five|six|seven|eight|nine|ten)[ \t\r\n\u00a0]{1,3}"
    r"golden[ \t\r\n\u00a0_-]{0,3}key(?P<s>s)?\b"
    r"(?![ \t\u00a0]{1,3}(?:codes?|shift)\b)",
    re.ASCII | re.IGNORECASE,
)

# "5 for PC and 3 for console": a bare number, "for" or "on", and a platform.
# Counts that differ by platform can't be one reward.
_PLATFORM_SPLIT_RE = re.compile(
    r"\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)[ \t\u00a0]{1,3}"
    r"(?:for|on)[ \t\u00a0]{1,3}(?:the[ \t\u00a0]{1,3})?"
    r"(?:pc|consoles?|xbox|playstation|ps\d?|switch|steam)\b",
    re.ASCII | re.IGNORECASE,
)

# The word before a count, when that word makes the number part of a name or a
# date: "Borderlands 4 Golden Keys", "Oct 14 Golden Keys", "Update 2 ...".
_MONTH_NAMES = (
    "january february march april may june july august september october november december"
).split()
_NAME_WORDS = frozenset(
    {name[:3] for name in _MONTH_NAMES}
    | set(_MONTH_NAMES)
    | {"sept", "borderlands", "update", "patch", "season", "part", "episode", "day", "week"}
)
_PREV_WORD_RE = re.compile(r"([A-Za-z]{1,12})\.?[\s\u00a0]{0,3}\Z", re.ASCII)

# Bare "through" is deliberately absent: "the event runs through Oct 20" is about the
# event, and a wrong expiry is worse than none.
_KEYWORD_RE = re.compile(
    r"\b(?:expires?|expiring|valid\s{1,3}until|(?:valid|redeemable|good)\s{1,3}through)\b",
    re.ASCII | re.IGNORECASE,
)

# "not valid until", "isn't valid until", "won't be valid until": the text just before
# a keyword (up to two words, no punctuation between) says the opposite of an expiry.
_NEGATION_WINDOW = 40
_NEGATED_RE = re.compile(
    r"(?:\bnot|n['\u2019]t|\bwon['\u2019]t[ \t\u00a0]+be)"
    r"(?:[ \t\u00a0]+[A-Za-z']+){0,2}[ \t\u00a0]*\Z",
    re.ASCII | re.IGNORECASE,
)
# "was valid until", "were all valid until": the same shape as the negation, with a past-tense
# verb. The thing being described is over.
_PAST_RE = re.compile(
    r"\b(?:was|were)(?:[ \t\u00a0]+[A-Za-z']+){0,2}[ \t\u00a0]*\Z",
    re.ASCII | re.IGNORECASE,
)
# "expired", anywhere in the window. A post that talks about codes that have expired isn't
# one whose dates this reader can tell apart, and "expires" is not matched by this: `\b` at
# the end of "expire" refuses the "d".
_EXPIRED_RE = re.compile(r"\bexpired\b", re.ASCII | re.IGNORECASE)
# A post that says when something starts has two ends in it, and we only read one.
_STARTS_RE = re.compile(r"\bstarts\b|\bvalid\s{1,3}from\b", re.ASCII | re.IGNORECASE)

# After the keyword: an optional colon and "on", an optional weekday, then the date.
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_LEAD_RE = re.compile(
    r"[\s\u00a0:]{0,4}(?:on[\s\u00a0]{1,3})?"
    r"(?:(?P<wd>(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?)"
    r"\.?,?[\s\u00a0]{1,3})?",
    re.ASCII | re.IGNORECASE,
)
_MONTH_DATE_RE = re.compile(
    r"(?P<word>[A-Za-z]{3,9})\.?[\s\u00a0]{1,3}(?P<day>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    r"(?:,?[\s\u00a0]{1,3}(?P<year>\d{4})(?!\d))?",
    re.ASCII,
)
# What can follow a date to make it half of a range or one of a few options: a connector
# ("or", "and", "to", a dash, a slash) and then another date or a bare day number.
_MORE_DATE_RE = re.compile(
    r"[ \t\u00a0]{0,3}(?:(?:or|and|to|through|thru|until|till)\b|[-\u2013\u2014/&])"
    r"(?:[ \t\u00a0]{1,3}(?:the|maybe|even|perhaps|or|to|through|thru|until|till)\b)*"
    r"[ \t\u00a0]{0,3}(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6}\.?"
    r"[ \t\u00a0]{0,3})?\d",
    re.ASCII | re.IGNORECASE,
)
# Anything shaped like a numeric date, so "10/14" can be recognized and refused
# instead of ignored (ignoring it would let a second, readable date win by default).
_NUMERIC_DATE_RE = re.compile(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d+))?(?![\d/])", re.ASCII)
_SLASHED_RE = re.compile(r"\d{1,2}/\d", re.ASCII)
_FULL_YEAR_RE = re.compile(r"\d{4}", re.ASCII)

# How far past a keyword the date can start. "valid until" plus a weekday plus a
# sentence's worth of padding is still well inside this.
_LOOKAHEAD = 48
# How far past a date to look for the rest of a range.
_AFTER_WINDOW = 40

# How far either side of the code a count or an expiry may sit, and what ends a paragraph.
_CODE_WINDOW = 200
# A post that names its one code more often than this isn't a one-line announcement, and
# reading a window per mention would let it make the reader do real work. It gives up.
_MAX_MENTIONS = 10
# A blank line, or one of the Unicode line and paragraph separators (U+2028, U+2029, U+0085),
# which are somebody's idea of a paragraph break whether or not an editor shares the opinion.
_PARAGRAPH_BREAK_RE = re.compile(r"[ \t\r\f\v\u00a0]*\n[ \t\r\f\v\u00a0]*\n|[\u2028\u2029\u0085]")
# Where a clause ends: sentence punctuation (and any closing quote or bracket) before a space
# or the end, or a line break. A period before a digit ("Oct. 14", "No. 5") doesn't count,
# because the clause it would cut is the very thing the reader is trying not to cut.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"')\]\u201d\u2019]*(?=\s|\Z)|\n")
_DIGIT_AHEAD_RE = re.compile(r"\s*\d")

# A year-less date this many days before the publish day (or fewer) is read as a typo or
# past tense, not as next year's deadline.
_RECENT_PAST_DAYS = 7

# How many years ahead a year-less date will look. Only Feb 29 ever needs more than one.
_YEARLESS_SPAN = 5


class _Unsure(Exception):
    """A date was there, we can't read it, and we'd rather say nothing than guess."""


def _follows_a_name(text: str, start: int) -> bool:
    """Whether the word right before `start` makes a number there part of a name or date."""
    match = _PREV_WORD_RE.search(text[max(0, start - 20) : start])
    return match is not None and match.group(1).lower() in _NAME_WORDS


def _read_count(text: str) -> int | None:
    """The one Golden Key count in `text`, or None (no count, an unclear one, or two)."""
    if _PLATFORM_SPLIT_RE.search(text):
        return None
    counts = set()
    for match in _COUNT_RE.finditer(text):
        if _follows_a_name(text, match.start()):
            continue
        raw = match.group("n").lower()
        count = int(raw) if raw.isdigit() else _WORD_COUNTS[raw]
        # One key, several keys: "3 Golden Key" and "1 Golden Keys" aren't quantities.
        if (count == 1) == (match.group("s") is not None):
            continue
        counts.add(count)
    if len(counts) != 1:
        return None
    (count,) = counts
    return count if count >= 1 else None


def _post_day(published_at: datetime | None, now: datetime | None) -> date | None:
    """The Pacific calendar day a year-less date is measured from, if there is one."""
    moment = published_at or now
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(SHIFT_TZ).date()


def _read_month_date(match: re.Match[str], anchor: date | None) -> date | None:
    """One month-name match as a date; None if it isn't a month; `_Unsure` if it won't parse."""
    from newsbot.collectors.shift_table import parse_shift_date

    word, day = match.group("word"), match.group("day")
    if parse_shift_date(f"{word} 1, 2000") is None:
        return None  # "through Thursday 5": not a date at all, so not our business
    if match.group("year") is not None:
        found = parse_shift_date(f"{word} {day}, {match.group('year')}")
        if found is None:
            raise _Unsure
        return found
    if anchor is None:
        raise _Unsure
    # Last year's date, if it was only days ago, is "this just ended", not a deadline.
    previous = parse_shift_date(f"{word} {day}, {anchor.year - 1}")
    if previous is not None and (anchor - previous).days <= _RECENT_PAST_DAYS:
        raise _Unsure
    for year in range(anchor.year, anchor.year + _YEARLESS_SPAN):
        found = parse_shift_date(f"{word} {day}, {year}")
        if found is None:
            continue
        if found >= anchor:
            return found
        if (anchor - found).days <= _RECENT_PAST_DAYS:
            raise _Unsure
    raise _Unsure


def _read_numeric_date(match: re.Match[str]) -> date:
    """A slash date in US order with a four-digit year, or `_Unsure`."""
    from newsbot.collectors.shift_table import parse_shift_date

    year = match.group("y")
    if year is None or _FULL_YEAR_RE.fullmatch(year) is None:
        raise _Unsure
    # Spelled out as a month name so the one date grammar (and its 2000 to 2100
    # year range) does the checking.
    names = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    month = int(match.group("m"))
    if not 1 <= month <= 12:
        raise _Unsure
    found = parse_shift_date(f"{names[month - 1]} {match.group('d')}, {year}")
    if found is None:
        raise _Unsure
    return found


def _read_expiry(text: str, anchor: date | None) -> date | None:
    """The one expiry date `text` announces, or None (none found, two found, or unsure).

    `text` is the code's window (`_windows`), not the whole post. `anchor` is the post's
    Pacific day; a date before it is a code announced already dead, which is a misread.
    """
    if _STARTS_RE.search(text) or _EXPIRED_RE.search(text):
        return None
    found: set[date] = set()
    try:
        for keyword in _KEYWORD_RE.finditer(text):
            before = text[max(0, keyword.start() - _NEGATION_WINDOW) : keyword.start()]
            if _NEGATED_RE.search(before) or _PAST_RE.search(before):
                raise _Unsure
            tail = text[keyword.end() : keyword.end() + _LOOKAHEAD]
            lead = _LEAD_RE.match(tail)
            skipped = lead.end() if lead else 0
            weekday = lead.group("wd") if lead else None
            tail = tail[skipped:]
            numeric = _NUMERIC_DATE_RE.match(tail)
            if numeric is not None:
                parsed: date | None = _read_numeric_date(numeric)
                date_end = numeric.end()
            elif _SLASHED_RE.match(tail):
                raise _Unsure  # "1/2/3/4" and friends: date-shaped, unreadable
            else:
                month_date = _MONTH_DATE_RE.match(tail)
                if month_date is None:
                    continue
                parsed = _read_month_date(month_date, anchor)
                date_end = month_date.end()
            if parsed is None:
                continue
            if anchor is not None and parsed < anchor:
                raise _Unsure  # dated before the post itself: whatever this is, it isn't a deadline
            if weekday is not None and _WEEKDAYS[weekday[:3].lower()] != parsed.weekday():
                raise _Unsure  # "Tuesday, October 14" when the 14th is a Wednesday
            after = keyword.end() + skipped + date_end
            if text[after : after + 1].isdecimal():
                raise _Unsure  # a digit from another script glued on: not the date it looks like
            if _MORE_DATE_RE.match(text, after, after + _AFTER_WINDOW):
                raise _Unsure  # a range or an alternative; the first date is likely the wrong end
            found.add(parsed)
    except _Unsure:
        return None
    if len(found) != 1:
        return None
    (expiry,) = found
    return expiry


def _clause_edges(text: str, lo: int, hi: int) -> tuple[list[int], list[int]]:
    """Where clauses start and end in `text[lo:hi]`: two lists of positions, each in order.

    A clause ends just after its sentence punctuation (or at a line break) and the next one
    starts right after that. Positions outside `lo..hi` are left out.
    """
    starts: list[int] = []
    ends: list[int] = []
    for match in _SENTENCE_END_RE.finditer(text, lo, min(len(text), hi + 2)):
        if match.group() == "\n":
            end, start = match.start(), match.end()
        else:
            run = match.group()
            if "!" not in run and "?" not in run and _DIGIT_AHEAD_RE.match(text, match.end()):
                continue
            end = start = match.end()
        if lo <= end <= hi:
            ends.append(end)
        if lo <= start <= hi:
            starts.append(start)
    return starts, ends


def _window_for(text: str, first: int, last: int) -> str:
    """The stretch of `text` around the mention at `text[first:last]` that is safe to read.

    The plain window is the mention's paragraph and `_CODE_WINDOW` characters either side.
    A plain cut is a bad idea, though: it lands in the middle of "December 31, 2027" and
    leaves "December 3", or takes "not" off the front of "valid until October 14" and leaves a
    promise nobody made. So each edge that isn't a paragraph edge snaps outward to the nearest
    clause boundary, at most another `_CODE_WINDOW` characters away. An edge with no boundary
    that close is inside some very long clause, and the partial clause at that edge is
    dropped instead: the window pulls in to the boundary nearest the mention, or to the
    mention itself. Giving up is the feature.
    """
    width = _CODE_WINDOW
    # The paragraph edges, looked for no further than a snap can ever reach. An edge nobody
    # found a break for is a limit of the search (unknown), not the start of a paragraph.
    region_lo, region_hi = max(0, first - 2 * width), min(len(text), last + 2 * width)
    floor, floor_known = region_lo, region_lo == 0
    for paragraph_break in _PARAGRAPH_BREAK_RE.finditer(text, region_lo, first):
        floor, floor_known = paragraph_break.end(), True
    ceiling, ceiling_known = region_hi, region_hi == len(text)
    found = _PARAGRAPH_BREAK_RE.search(text, last, region_hi)
    if found is not None:
        ceiling, ceiling_known = found.start(), True

    # The front edge.
    base_lo = max(floor, first - width)
    if base_lo == floor and floor_known:
        start = floor
    else:
        reach = max(floor, base_lo - width)
        starts, _ = _clause_edges(text, reach, first)
        outward = [b for b in starts if b <= base_lo]
        if reach == floor and floor_known:
            outward.append(floor)
        if outward:
            start = max(outward)
        else:
            inward = [b for b in starts if b > base_lo]
            start = min(inward) if inward else first

    # The back edge.
    base_hi = min(ceiling, last + width)
    if base_hi == ceiling and ceiling_known:
        end = ceiling
    else:
        reach = min(ceiling, base_hi + width)
        _, ends = _clause_edges(text, last, reach)
        outward = [e for e in ends if e >= base_hi]
        if reach == ceiling and ceiling_known:
            outward.append(ceiling)
        if outward:
            end = min(outward)
        else:
            inward = [e for e in ends if e < base_hi]
            end = max(inward) if inward else last
    return text[start:end]


def _windows(text: str, code: str) -> list[str]:
    """The stretches of `text` around each place `code` appears (see `_window_for`).

    Only the slice around the code is ever searched, so a very long post costs the same as a
    short one. Empty when the code isn't in the text at all, which the caller treats as "can't
    say anything about it". Also empty for a code named more than `_MAX_MENTIONS` times: a
    post that says the same code eleven times isn't a one-line announcement.
    """
    windows = []
    for match in CODE_RE.finditer(text):
        if match.group(1).upper() != code:
            continue
        if len(windows) == _MAX_MENTIONS:
            return []
        windows.append(_window_for(text, match.start(), match.end()))
    return windows


def _agreed[T](readings: list[T | None]) -> T | None:
    """The one value every window that read something agrees on, or None (nothing, or a clash)."""
    values = {r for r in readings if r is not None}
    if len(values) != 1:
        return None
    (value,) = values
    return value


def read_official_details(
    text: str,
    code: str,
    published_at: datetime | None,
    *,
    now: datetime | None = None,
) -> ItemDetails | None:
    """What an official post says about `code`, or None if it says nothing we're sure of.

    `text` is the post as one string; the caller has already checked that `code`
    is the only code in it. Only the text near the code is read (see `_windows`): a long
    post's other paragraphs are about other things. A found Golden Key count sets the
    reward to "N Golden Key(s)" and the category to golden; a found expiry sets
    `expires_on`. Either part can come back alone. If the code turns up in more than one
    place and the places disagree, that part is dropped. The result is always rank "official".
    """
    windows = _windows(text, code)
    anchor = _post_day(published_at, now)
    count = _agreed([_read_count(w) for w in windows])
    expiry = _agreed([_read_expiry(w, anchor) for w in windows])
    if count is None and expiry is None:
        return None
    reward = ""
    if count is not None:
        # Built from a number, so nothing code-shaped can be in it today. The rule is
        # "no code-shaped text in a reward", and this keeps it true if the reader ever
        # learns to quote.
        reward = defuse_codes(f"{count} Golden Key{'' if count == 1 else 's'}")
    return ItemDetails(
        code=code,
        rank="official",
        reward=reward,
        category="golden" if count is not None else None,
        expires_on=expiry,
    )


__all__ = ["read_official_details"]
