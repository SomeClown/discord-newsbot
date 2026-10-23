"""Render every SHiFT alert case through the real renderers into a Discord-dark HTML page.

The terminal look (design.md §17) is made of inline-code chips, fences and a block cursor,
and none of that can be judged from a test assertion; somebody has to look at it. This is
how. It builds each case with the real `format.py` functions (no copies of the strings, so
the page can't drift from the bot), runs the result through a small hand-written
Markdown-to-HTML converter, and writes one page. `--screenshots` then photographs the page
with headless Chrome at a desktop width and a phone width, because a good share of the
people reading these alerts are doing it on a phone and I would like to know what that
looks like before they do.

It is an approximation of Discord, not Discord. The converter knows about fences, chips,
bold, mentions, timestamps and `<link>`s, which is everything the bot writes and nothing
else; the fonts, the spacing and the exact grays are my eyeballing of a dark-theme client.
The first live alert is the real test, and the page says so at the top, in case anyone
forgets and starts arguing with a screenshot.
One more caveat: its chip and fence pairing is tidier than Discord's, so a message with stray
backticks can look fine here and still break in a real client.

The clock is fixed (`NOW`), so two runs write the same page byte for byte, and the page
loads nothing from anywhere: no scripts, no fonts, no images. Standard library plus the
bot's own modules, nothing to install.

    python scripts/preview_shift_look.py [--out build/shift-look] [--screenshots]
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import discord

from newsbot.bot.format import (
    cap_shown,
    details_reserve,
    render_alert_edit,
    render_code_alerts,
    render_current_pages,
    render_followup_alerts,
    render_history_pages,
    render_roundup_alerts,
)
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeDetailsRow, CodeView, CurrentCode

ROOT = Path(__file__).resolve().parent.parent

# Pinned so the output is the same every time: the "in 7 days" pills and the details lines
# that count hours all read against this, never against the real clock.
NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
# (file name, window width, window height). The height is just "tall": Chrome screenshots the
# window, not the page, and I'd rather have one long image per width than a pile of slices.
# Too tall leaves a dark band at the bottom; too short cuts the end off. Chrome also hangs
# (it doesn't fail, it hangs) on a window taller than 8,192 px at 2x scale, so that is the
# ceiling. Both pages are near it now (the desktop one is about 7,750 and the phone one about
# 7,700, in windows of 7,900 and 8,000); a new case that pushes either past it means trimming
# cases (the roundup in case 10 was cut to 17 codes to make room for case 12), because the
# height can't simply go up.
SCREENSHOTS = (("desktop.png", 1280, 7900), ("phone.png", 390, 8000))

# Headless Chrome won't lay a page out narrower than this, whatever `--window-size` says: it
# lays out at 500 and then crops the screenshot to the width you asked for, which clips
# the right-hand side of a phone-sized page. So a narrower shot photographs the page through
# an iframe of exactly that width (see `take_screenshots`), and the page's own layout, media
# query and all, sees a real 390 px phone.
MIN_WINDOW_WIDTH = 500
_FRAME = (
    '<!DOCTYPE html><html><body style="margin:0;background:#313338">'
    '<iframe src="{src}" width="{width}" height="{height}" style="border:0;display:block">'
    "</iframe></body></html>\n"
)

# What prod would pass to the packer: the two sources that supply details today.
PROD_RESERVE = details_reserve(["mentalmars", "Borderlands (Bluesky)"])

# A message is its text or an embed; a captioned one is `(caption, message)`.
Message = str | discord.Embed
Item = Message | tuple[str, Message]
Case = tuple[str, list[Item]]

FOOTER_CODES = "Codes from Gearbox and mentalmars.com · redeem at shift.gearboxsoftware.com"
ROLE_PING = "<@&1411111111111111111>"
LONG_URL = (
    "https://example.com/borderlands-4/" + "shift-codes-megathread-with-a-very-long-slug/" * 4
)


# --- the cases ---


def _code(n: int) -> str:
    """A made-up but code-shaped SHiFT code, numbered; the renderers insist on the shape."""
    return f"PRV{n:02d}-{n * 7919 % 100000:05d}-ZZZZ{n % 10}-A1B2C-D3E4F"


def _cand(
    n: int,
    *,
    golden: bool = False,
    source: str = "Gearbox",
    url: str = "https://example.com/post",
    roundup: bool = False,
) -> CodeCandidate:
    return CodeCandidate(
        code=_code(n),
        golden=golden,
        source_name=source,
        item_url=url,
        fresh=True,
        roundup=roundup,
    )


def _row(n: int, **fields) -> CodeDetailsRow:
    return CodeDetailsRow(code=_code(n), clock_start=NOW, updated_at=NOW, **fields)


def _golden_row(n: int, keys: int, expires: date | None, via: str = "mentalmars") -> CodeDetailsRow:
    return _row(
        n,
        reward=f"{keys} Golden Key{'s' if keys != 1 else ''}",
        reward_from=via,
        category="golden",
        category_from=via,
        expires_on=expires,
        expires_from=via if expires else "",
    )


def _alert_texts(alerts) -> list[Item]:
    return [a.content for a in alerts]


def build_cases(now: datetime) -> list[Case]:
    """Every case in the preview, as `(title, messages)`, rendered by the real functions."""
    today = now.date()
    cases: list[Case] = []

    # 1. A new code from an official source, with details, pinging the server.
    c1 = _cand(1, golden=True, source="Gearbox", url="https://x.com/gearbox/status/1")
    d1 = {c1.code: _golden_row(1, 3, today + timedelta(days=7), via="Gearbox")}
    cases.append(
        (
            "1. A new code, pinged, with details",
            _alert_texts(
                render_code_alerts(
                    [c1], ping=True, details=d1, now=now, reserve=PROD_RESERVE, nonce_scope="p"
                )
            ),
        )
    )

    # 2. A community code: no ping, and nothing known about it yet.
    c2 = _cand(2, source="r/Borderlands4", url="https://reddit.example/r/Borderlands4/abc")
    cases.append(
        (
            "2. A community code, unpinged, no details",
            _alert_texts(render_code_alerts([c2], ping=False, now=now, reserve=PROD_RESERVE)),
        )
    )

    # 3. Two golden codes under a role ping (the heading says Golden Key, since all are).
    c3 = [_cand(3, golden=True), _cand(4, golden=True)]
    d3 = {
        c3[0].code: _golden_row(3, 1, today + timedelta(days=1)),
        c3[1].code: _golden_row(4, 5, today + timedelta(days=14)),
    }
    cases.append(
        (
            "3. Two Golden Keys under a role ping",
            _alert_texts(
                render_code_alerts(
                    c3,
                    ping=True,
                    ping_mention=ROLE_PING,
                    details=d3,
                    now=now,
                    reserve=PROD_RESERVE,
                )
            ),
        )
    )

    # 4. A mixed batch: the golden one wears "Golden Key:" in front of its source.
    c4 = [_cand(5, golden=True), _cand(6, source="mentalmars"), _cand(7, source="mentalmars")]
    d4 = {
        c4[0].code: _golden_row(5, 3, today + timedelta(days=5)),
        c4[1].code: _row(
            6,
            reward="Rare Weapon Skin",
            reward_from="mentalmars",
            category="cosmetic",
            category_from="mentalmars",
        ),
    }
    cases.append(
        (
            "4. A mixed batch (Golden Key: in the source line)",
            _alert_texts(
                render_code_alerts(c4, ping=True, details=d4, now=now, reserve=PROD_RESERVE)
            ),
        )
    )

    # 5. Six codes with details is more than one message holds at the prod reserve.
    c5 = [_cand(10 + i, source="mentalmars") for i in range(6)]
    d5 = {
        c.code: _golden_row(10 + i, 1 + i, today + timedelta(days=3 + i)) for i, c in enumerate(c5)
    }
    cases.append(
        (
            "5. A batch that continues (six codes)",
            _alert_texts(
                render_code_alerts(c5, ping=True, details=d5, now=now, reserve=PROD_RESERVE)
            ),
        )
    )

    # 6. A permanent code reads "expires never".
    c6 = _cand(20, source="mentalmars")
    d6 = {
        c6.code: _row(
            20,
            reward="Permanent Shift Code",
            reward_from="mentalmars",
            category="permanent",
            category_from="mentalmars",
        )
    }
    cases.append(
        (
            "6. A permanent code",
            _alert_texts(
                render_code_alerts([c6], ping=False, details=d6, now=now, reserve=PROD_RESERVE)
            ),
        )
    )

    # 7. The worst case the reserve is sized for: a 300-unit reward, via names of 19
    # characters, and a link long enough to be a problem.
    c7 = _cand(21, golden=True, source="Community Source #1", url=LONG_URL)
    reward = cap_shown("3 Golden Keys and a generous pile of cosmetics, " * 8, 300)
    d7 = {
        c7.code: _row(
            21,
            reward=reward,
            reward_from="Community Source #1",
            category="golden",
            category_from="Community Source #1",
            expires_on=date(2026, 10, 30),
            expires_from="Community Source #2",
        )
    }
    cases.append(
        (
            "7. The worst case (300-unit reward, long names, long link)",
            _alert_texts(
                render_code_alerts([c7], ping=True, details=d7, now=now, reserve=PROD_RESERVE)
            ),
        )
    )

    # 8. An edit: the alert went out bare, then a table listed the reward and expiry.
    c8 = [_cand(30, golden=True, source="mentalmars"), _cand(31, source="mentalmars")]
    posted = render_code_alerts(c8, ping=True, now=now, reserve=PROD_RESERVE)[0]
    d8 = {
        c8[0].code: _golden_row(30, 3, today + timedelta(days=6)),
        c8[1].code: _golden_row(31, 1, today + timedelta(days=6)),
    }
    edited = render_alert_edit(
        posted.header, c8, golden_prefix=posted.golden_prefix, details=d8, now=now
    )
    cases.append(
        (
            "8. An edit, before and after",
            [("as posted", posted.content), ("after the details arrive", edited)],
        )
    )

    # 9. An alert posted before v3.2.0 keeps its old header and layout when it's edited.
    c9 = [_cand(40, source="r/Borderlands4")]
    classic_header = "@everyone **New SHiFT code**"
    d9 = {c9[0].code: _golden_row(40, 3, today + timedelta(days=4))}
    cases.append(
        (
            "9. A pre-3.2.0 alert, edited (the mixed fortnight)",
            [
                (
                    "as posted by v3.1",
                    render_alert_edit(
                        classic_header, c9, golden_prefix=False, details=None, now=now
                    ),
                ),
                (
                    "after an edit, still the classic layout",
                    render_alert_edit(classic_header, c9, golden_prefix=False, details=d9, now=now),
                ),
            ],
        )
    )

    # 10. A roundup, long enough to continue.
    c10 = [
        _cand(
            50 + i,
            source="Borderlands Fan Wiki",
            url="https://example.com/wiki/shift",
            roundup=True,
        )
        for i in range(17)
    ]
    d10 = {
        c.code: _golden_row(50 + i, 1 + i % 5, today + timedelta(days=2 + i))
        for i, c in enumerate(c10)
    }
    cases.append(
        (
            "10. A roundup and its continuation",
            _alert_texts(render_roundup_alerts(c10, details=d10, now=now)),
        )
    )

    # 11. A follow-up (a second source confirmed a community code), with details and without.
    code_a, code_b = _code(60), _code(61)
    d11 = {code_a: _golden_row(60, 3, today + timedelta(days=5))}
    cases.append(
        (
            "11. A follow-up, with and without details",
            [
                (
                    "with details",
                    render_followup_alerts(
                        [code_a], ping_mention="@everyone", details=d11, now=now
                    )[0].content,
                ),
                (
                    "without",
                    render_followup_alerts([code_b], ping_mention="@everyone")[0].content,
                ),
            ],
        )
    )

    # 12. A follow-up that continues: one ping, then a "(continued)" message. Worst-case details
    # keep it to a handful of codes; a real burst of 25 plain ones does the same, just taller.
    c12f = [_cand(80 + i, source="Community Source #1") for i in range(5)]
    d12f = {
        c.code: _row(
            80 + i,
            reward=cap_shown("3 Golden Keys and a generous pile of cosmetics, " * 8, 300),
            reward_from="Community Source #1",
            category="golden",
            category_from="Community Source #1",
            expires_on=date(2026, 10, 30),
            expires_from="Community Source #2",
        )
        for i, c in enumerate(c12f)
    }
    cases.append(
        (
            "12. A follow-up that continues (five codes at worst-case details)",
            _alert_texts(
                render_followup_alerts(
                    [c.code for c in c12f], ping_mention="@everyone", details=d12f, now=now
                )
            ),
        )
    )

    # 13. The test alert, which can't pass for a real one.
    c12 = _cand(70, golden=True, source="test")
    cases.append(
        (
            "13. A test alert",
            _alert_texts(
                render_code_alerts([c12], ping=False, test=True, now=now, reserve=PROD_RESERVE)
            ),
        )
    )

    # 14. /shift codes: everything, golden only, and nothing at all.
    current = [
        CurrentCode(
            _code(80), now - timedelta(days=1), _golden_row(80, 3, today + timedelta(days=6))
        ),
        CurrentCode(
            _code(81), now - timedelta(days=2), _golden_row(81, 1, today + timedelta(days=1))
        ),
        CurrentCode(
            _code(82),
            now - timedelta(days=9),
            _row(
                82,
                reward="Rare Weapon Skin",
                reward_from="mentalmars",
                category="cosmetic",
                category_from="mentalmars",
                expires_on=today + timedelta(days=20),
                expires_from="mentalmars",
            ),
        ),
        CurrentCode(
            _code(83),
            now - timedelta(days=60),
            _row(
                83,
                reward="Permanent Shift Code",
                reward_from="mentalmars",
                category="permanent",
                category_from="mentalmars",
                origin="Borderlands 4 launch",
            ),
        ),
        CurrentCode(_code(84), now - timedelta(hours=3)),
    ]
    empty = "No current codes. I'll post new ones in <#1411111111111111112> when they show up."
    cases.append(
        (
            "14. /shift codes (all, type: golden, empty)",
            [
                (
                    "all",
                    render_current_pages(
                        current, kind="all", now=now, footer=FOOTER_CODES, empty=empty
                    )[0],
                ),
                (
                    "type: golden",
                    render_current_pages(
                        current, kind="golden", now=now, footer=FOOTER_CODES, empty=empty
                    )[0],
                ),
                (
                    "empty",
                    render_current_pages([], kind="all", now=now, footer=FOOTER_CODES, empty=empty)[
                        0
                    ],
                ),
            ],
        )
    )

    # 15. /shift history, with each marker it has: expired, from a roundup, old post.
    views = [
        CodeView(
            _code(90),
            now - timedelta(days=1),
            "Gearbox",
            "https://example.com/p/1",
            "posted",
            False,
            _golden_row(90, 3, today + timedelta(days=6), via="Gearbox"),
        ),
        CodeView(
            _code(91),
            now - timedelta(days=5),
            "mentalmars",
            "https://example.com/p/2",
            "posted",
            False,
            _golden_row(91, 1, today - timedelta(days=2)),
        ),
        CodeView(
            _code(92),
            now - timedelta(days=6),
            "Borderlands Fan Wiki",
            "https://example.com/wiki/shift",
            "roundup",
            True,
        ),
        CodeView(
            _code(93),
            now - timedelta(days=11),
            "r/Borderlands4",
            "https://reddit.example/r/old",
            "too_old",
            False,
        ),
    ]
    cases.append(
        (
            "15. /shift history (expired, from a roundup, old post)",
            list(render_history_pages(views, timezone="America/Los_Angeles", now=now)),
        )
    )
    return cases


# --- Discord Markdown to HTML, as little of it as the bot ever writes ---

# One alternation, scanned left to right, so whatever starts first wins and a backslash
# escape (first in the list) eats its character before anything can read it as markup.
_INLINE_RE = re.compile(
    r"\\(?P<esc>[!-/:-@\[-`{-~])"
    r"|`(?P<chip>[^`\n]+)`"
    r"|<t:(?P<stamp>\d{1,12}):R>"
    r"|<(?P<url>https?://[^>\s]+)>"
    r"|<@&(?P<role>\d+)>"
    r"|<#(?P<channel>\d+)>"
    r"|(?P<everyone>@everyone)"
    r"|\*\*(?P<bold>.+?)\*\*"
)
_FENCE_RE = re.compile(r"```[ \t]*\n?(.*?)\n?```", re.DOTALL)
COPY_GLYPH = "⧉"


def _relative(stamp: int) -> str:
    """Discord's "in 6 hours" / "3 days ago" for a Unix timestamp, read against `NOW`.

    Close to what Discord's client does (it rounds the way moment.js does), not exact.
    """
    seconds = int(stamp - NOW.timestamp())
    span = abs(seconds)
    if span < 45:
        text = "a few seconds"
    elif span < 90:
        text = "a minute"
    elif span < 45 * 60:
        text = f"{round(span / 60)} minutes"
    elif span < 90 * 60:
        text = "an hour"
    elif span < 22 * 3600:
        text = f"{round(span / 3600)} hours"
    elif span < 36 * 3600:
        text = "a day"
    elif span < 26 * 86400:
        text = f"{round(span / 86400)} days"
    elif span < 45 * 86400:
        text = "a month"
    elif span < 320 * 86400:
        text = f"{round(span / (30.44 * 86400))} months"
    else:
        text = f"{round(span / (365.25 * 86400))} years"
    return f"in {text}" if seconds >= 0 else f"{text} ago"


def _inline(text: str) -> str:
    """One run of non-fence text as HTML. Every piece of it is `html.escape`d before any markup."""
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "esc":
            out.append(html.escape(value))
        elif kind == "chip":
            out.append(f'<code class="chip">{html.escape(value)}</code>')
        elif kind == "stamp":
            # The pattern lets 12 digits through, which reaches past the year 9999 and past what
            # `datetime` can hold. Discord would draw something; I show the token as typed
            # (escaped) and move on, since no case I build gets anywhere near it.
            try:
                when = datetime.fromtimestamp(int(value), UTC).strftime("%A, %B %d, %Y %H:%M UTC")
            except ValueError, OverflowError, OSError:
                out.append(html.escape(m.group(0)))
            else:
                relative = _relative(int(value))
                out.append(f'<span class="pill time" title="{html.escape(when)}">{relative}</span>')
        elif kind == "url":
            safe = html.escape(value, quote=True)
            out.append(f'<a href="{safe}" rel="noreferrer">{safe}</a>')
        elif kind == "role":
            out.append('<span class="pill mention">@role</span>')
        elif kind == "channel":
            out.append('<span class="pill mention">#channel</span>')
        elif kind == "everyone":
            out.append('<span class="pill mention">@everyone</span>')
        else:
            out.append(f"<strong>{_inline(value)}</strong>")
    out.append(html.escape(text[pos:]))
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """A message's Discord Markdown as HTML: fences, chips, bold, mentions, timestamps, links."""
    out: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        before = text[pos : m.start()]
        # The newline either side of a fence is Discord's own block spacing, not a blank line.
        out.append(_inline(before.removesuffix("\n")))
        out.append(
            f'<pre><span class="copy" aria-hidden="true">{COPY_GLYPH}</span>'
            f"<code>{html.escape(m.group(1))}</code></pre>"
        )
        pos = m.end()
        if text[pos : pos + 1] == "\n":
            pos += 1
    out.append(_inline(text[pos:]))
    return "".join(out)


# --- the page ---

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: #313338; color: #dbdee1;
  font: 16px/1.375 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
}
main { max-width: 760px; margin: 0 auto; }
.note {
  margin: 0 0 20px; padding: 8px 12px; background: #2b2d31; border-left: 4px solid #f0b232;
  color: #b5bac1; font-size: 14px;
}
h2 { margin: 28px 0 8px; font-size: 14px; color: #f2f3f5; text-transform: none; }
.caption { margin: 12px 0 2px; font-size: 12px; color: #949ba4; }
.msg { margin: 4px 0 8px; padding: 2px 0; overflow-wrap: anywhere; }
.who { font-size: 16px; font-weight: 500; color: #f2f3f5; }
.app {
  margin-left: 6px; padding: 0 4px; border-radius: 3px; background: #5865f2; color: #fff;
  font-size: 10px; vertical-align: 2px;
}
.body { white-space: pre-wrap; }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; }
.chip {
  padding: 0 4px; border-radius: 4px; background: #2b2d31; border: 1px solid #1e1f22;
  font-size: 85%; white-space: pre-wrap;
}
pre {
  position: relative; margin: 6px 0; padding: 7px 36px 7px 11px; background: #2b2d31;
  border: 1px solid #1e1f22; border-radius: 4px; font-size: 14px; white-space: pre-wrap;
  overflow-wrap: anywhere;
}
pre code { font-size: inherit; }
.copy { position: absolute; top: 4px; right: 8px; color: #949ba4; font-size: 15px; }
.pill { padding: 0 2px; border-radius: 3px; }
.mention { background: rgba(88, 101, 242, 0.3); color: #c9cdfb; font-weight: 500; }
.time { background: rgba(78, 80, 88, 0.6); }
a { color: #00a8fc; text-decoration: none; }
.embed {
  max-width: 520px; margin-top: 4px; padding: 8px 16px 16px 12px; background: #2b2d31;
  border-left: 4px solid #3fe37f; border-radius: 4px; overflow-wrap: anywhere;
}
.embed .chip { background: #232428; }
.embed pre { background: #232428; }
.etitle { margin-top: 8px; font-weight: 600; color: #f2f3f5; }
.edesc { margin-top: 8px; font-size: 14px; white-space: pre-wrap; }
.efoot { margin-top: 8px; font-size: 12px; color: #b5bac1; }
@media (max-width: 600px) {
  body { padding: 8px; font-size: 15px; line-height: 1.3; }
  h2 { margin: 18px 0 4px; }
  .msg { margin: 2px 0 4px; }
  pre { margin: 3px 0; padding: 4px 30px 4px 8px; font-size: 13px; }
  .embed { padding: 4px 10px 10px 8px; }
}
"""

_NOTE = (
    "This is an approximation of Discord, not Discord: the fonts, spacing and grays are "
    "guesses, and the real thing may differ."
)


def _message_html(message: Message) -> str:
    if isinstance(message, discord.Embed):
        color = message.color.value if message.color is not None else 0x4F545C
        parts = [f'<div class="embed" style="border-left-color: #{color:06x}">']
        if message.title:
            parts.append(f'<div class="etitle">{html.escape(message.title)}</div>')
        if message.description:
            parts.append(f'<div class="edesc">{markdown_to_html(message.description)}</div>')
        if message.footer.text:
            parts.append(f'<div class="efoot">{html.escape(message.footer.text)}</div>')
        parts.append("</div>")
        body = "".join(parts)
    else:
        body = f'<div class="body">{markdown_to_html(message)}</div>'
    return (
        f'<div class="msg"><div class="who">newsbot<span class="app">APP</span></div>{body}</div>'
    )


def to_html(cases: list[Case]) -> str:
    """The whole preview page for `cases`: a title, the approximation note, then each case."""
    sections: list[str] = []
    for title, items in cases:
        sections.append(f"<section><h2>{html.escape(title)}</h2>")
        for item in items:
            if isinstance(item, tuple):
                caption, message = item
                sections.append(f'<div class="caption">{html.escape(caption)}</div>')
            else:
                message = item
            sections.append(_message_html(message))
        sections.append("</section>")
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>SHiFT alert look</title>"
        f"<style>{_CSS}</style></head><body><main>"
        f'<p class="note">{html.escape(_NOTE)}</p>'
        f"{''.join(sections)}</main></body></html>\n"
    )


# --- the command line ---


def take_screenshots(page: Path, out: Path) -> list[Path]:
    """Photograph `page` with headless Chrome at each width in `SCREENSHOTS`.

    Exits with a plain message, not a traceback, if Chrome isn't where the Mac puts it or
    refuses to run. Local only: nothing here belongs in CI, and the smoke test never gets
    this far.
    """
    if not CHROME.exists():
        raise SystemExit(f"Chrome isn't at {CHROME}; --screenshots needs it (or skip the flag).")
    written: list[Path] = []
    # A scratch profile, because headless Chrome pointed at the default one hands the job to
    # whatever Chrome is already open and then waits forever for it. I found that out by
    # waiting. It is also why this shouldn't touch your real profile.
    with tempfile.TemporaryDirectory(prefix="shift-look-chrome-") as profile:
        for name, width, height in SCREENSHOTS:
            target = out / name
            shot_of = page
            if width < MIN_WINDOW_WIDTH:
                shot_of = out / f"{target.stem}-frame.html"
                shot_of.write_text(
                    _FRAME.format(src=page.name, width=width, height=height), encoding="utf-8"
                )
            try:
                # The argv is a fixed list: the Chrome path above, flags written in this file,
                # and paths we built ourselves. Nothing a stranger typed gets near it.
                subprocess.run(  # noqa: S603
                    [
                        str(CHROME),
                        "--headless=new",
                        "--hide-scrollbars",
                        "--force-device-scale-factor=2",
                        f"--user-data-dir={profile}",
                        f"--window-size={width},{height}",
                        f"--screenshot={target}",
                        shot_of.resolve().as_uri(),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = getattr(exc, "stderr", b"") or b""
                raise SystemExit(
                    f"Chrome failed on {name}: {exc}\n{detail.decode(errors='replace')[-500:]}"
                ) from exc
            written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview the SHiFT alert look in a Discord-dark page."
    )
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "shift-look")
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="also write desktop.png and phone.png (needs Chrome)",
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    page = args.out / "index.html"
    page.write_text(to_html(build_cases(NOW)), encoding="utf-8")
    print(f"wrote {page}")
    if args.screenshots:
        for shot in take_screenshots(page, args.out):
            print(f"wrote {shot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
