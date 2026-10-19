"""Task 6 must not change a byte of what the old call sites render (plan task 6, design 16).

`fixtures/format_pre_details_snapshot.txt` is `newsbot/bot/format.py` exactly as
`git show HEAD:newsbot/bot/format.py` printed it before task 6 (the task 5 commit,
858fb5b), checked in so the comparison survives the task 6 commit and doesn't need git at
test time. It is loaded as a throwaway module, the same trick the v2.2.0 and v3.0.5
repo snapshots play. Every call below runs the old renderer and the new one with the
arguments an existing call site passes (no details, no clock, no reserve) and requires
identical output: content, codes, ping flag, nonce and ping prefix.

The one deliberate difference is out of reach of a real config and is not compared: the
old packer hard-truncated a source name too long to fit a message, and the new one raises
(plan \u00a73.4, "the truncation step is gone"). Config caps names at 100 characters, so the
two agree on everything a config can produce.

Invisible and bidi characters are `\\uXXXX` escapes on purpose; this file stays ASCII.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import newsbot.bot.format as new
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import CodeView

SNAPSHOT = Path(__file__).parent / "fixtures" / "format_pre_details_snapshot.txt"
ROLE20 = "<@&" + "1" * 20 + ">"
PINGS = [
    (False, None),
    (True, None),
    (True, "@everyone"),
    (True, "@here"),
    (True, ROLE20),
]
NAMES = ["Gearbox Blog", "*" * 100, "\U0001f600" * 100, "e\u0301" * 50, "@everyone" * 11]
URLS = [
    "https://e.com/a",
    "https://e.com/" + "a/" * 900,  # sheds its link when it has to
    "https://e.com/a?x=%22%3C&y=\u00e9",
    "",
]
FIELDS = ("content", "codes", "ping", "nonce", "ping_prefix")


@pytest.fixture(scope="module")
def old() -> Iterator[types.ModuleType]:
    """`bot/format.py` as it was before task 6."""
    module = types.ModuleType("newsbot_format_pre_details")
    sys.modules[module.__name__] = module  # dataclasses looks its own module up here
    try:
        exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(module.__name__, None)


def _code(i: int) -> str:
    return f"{i:04d}A-BCDEF-GHJKL-MNPQR-STUVW"


def _shape(alerts) -> list[tuple]:
    return [tuple(getattr(a, f) for f in FIELDS) for a in alerts]


def _golden(pattern: str, i: int) -> bool:
    return {"none": False, "all": True, "alternate": i % 2 == 0, "first": i == 0}[pattern]


def test_the_snapshot_really_is_the_old_module(old):
    assert not hasattr(old, "details_line")
    assert not hasattr(old, "render_alert_edit")
    assert "header" not in {f.name for f in dataclasses.fields(old.RenderedAlert)}


# --- code alerts ---


@pytest.mark.parametrize("name", NAMES, ids=["plain", "stars", "emoji", "combining", "mentions"])
@pytest.mark.parametrize("url", URLS, ids=["short", "long", "encoded", "none"])
def test_code_alerts_with_default_arguments_are_byte_identical(old, name, url):
    checked = 0
    for n in (1, 3, 10, 25, 60):
        for pattern in ("none", "all", "alternate", "first"):
            batch = [
                CodeCandidate(
                    code=_code(i),
                    golden=_golden(pattern, i),
                    source_name=name,
                    item_url=url,
                    fresh=True,
                )
                for i in range(n)
            ]
            # Both nonce scopes get covered by alternating them across the combinations.
            combos = [(p, t) for p in PINGS for t in (False, True)]
            for k, ((ping, mention), test) in enumerate(combos):
                scope = ("", "g1|c2")[k % 2]
                kwargs = dict(ping=ping, ping_mention=mention, test=test, nonce_scope=scope)
                assert _shape(new.render_code_alerts(batch, **kwargs)) == _shape(
                    old.render_code_alerts(batch, **kwargs)
                ), (n, pattern, kwargs)
                checked += 1
    assert checked == 5 * 4 * len(PINGS) * 2


def test_passing_the_new_arguments_explicitly_as_their_defaults_changes_nothing(old):
    batch = [
        CodeCandidate(
            code=_code(i),
            golden=i % 3 == 0,
            source_name="Gearbox Blog",
            item_url="https://e.com/a",
            fresh=True,
        )
        for i in range(30)
    ]
    expected = _shape(old.render_code_alerts(batch, ping=True, ping_mention=ROLE20))
    for extra in (
        dict(details=None, now=None, reserve=0),
        dict(details={}, now=None, reserve=0),
        dict(details={}, now=datetime(2026, 10, 9, tzinfo=UTC), reserve=0),
    ):
        got = new.render_code_alerts(batch, ping=True, ping_mention=ROLE20, **extra)
        assert _shape(got) == expected


def test_errors_are_the_same_errors(old):
    bad = CodeCandidate(code="nope", golden=False, source_name="s", item_url="", fresh=True)
    with pytest.raises(ValueError, match="not a SHiFT code"):
        old.render_code_alerts([bad], ping=True)
    with pytest.raises(ValueError, match="not a SHiFT code"):
        new.render_code_alerts([bad], ping=True)
    assert new.render_code_alerts([], ping=True) == old.render_code_alerts([], ping=True) == []


# --- roundups ---


@pytest.mark.parametrize("name", NAMES, ids=["plain", "stars", "emoji", "combining", "mentions"])
@pytest.mark.parametrize(
    "url", ["https://e.com/post", "https://e.com/" + "a" * 1700, ""], ids=["short", "huge", "none"]
)
def test_roundups_with_default_arguments_are_byte_identical(old, name, url):
    for n in (1, 5, 40, 90):
        for groups in (1, 3):
            cands = [
                CodeCandidate(
                    code=_code(i),
                    golden=False,
                    source_name=name if i % groups == 0 else f"Other {i % groups}",
                    item_url=url if i % groups == 0 else f"https://e.com/g{i % groups}",
                    fresh=True,
                    roundup=True,
                )
                for i in range(n)
            ]
            for scope in ("", "g1|c2"):
                assert _shape(new.render_roundup_alerts(cands, nonce_scope=scope)) == _shape(
                    old.render_roundup_alerts(cands, nonce_scope=scope)
                ), (n, groups, scope)


# --- follow-ups ---


@pytest.mark.parametrize("mention", ["@everyone", "@here", ROLE20])
def test_followups_with_default_arguments_are_byte_identical(old, mention):
    for n in range(1, 21):
        codes = [_code(i) for i in range(n)]
        for scope in ("", "g1|c2"):
            assert _shape(
                [new.render_followup_alert(codes, ping_mention=mention, nonce_scope=scope)]
            ) == (
                _shape([old.render_followup_alert(codes, ping_mention=mention, nonce_scope=scope)])
            )


def test_followup_errors_are_the_same_errors(old):
    for fn in (old.render_followup_alert, new.render_followup_alert):
        with pytest.raises(ValueError, match="1 to 20"):
            fn([], ping_mention="@everyone")
        with pytest.raises(ValueError, match="1 to 20"):
            fn([_code(i) for i in range(21)], ping_mention="@everyone")
        with pytest.raises(ValueError, match="not a SHiFT code"):
            fn(["nope"], ping_mention="@everyone")


# --- the old /shift codes page ---


def _view(i: int, **kwargs) -> CodeView:
    defaults = dict(
        code=_code(i),
        first_seen_at=datetime(2026, 9, 25, 20, 0, tzinfo=UTC),
        source_name="Gearbox Blog",
        item_url="https://e.com/a",
        status="posted",
        from_roundup=False,
    )
    defaults.update(kwargs)
    return CodeView(**defaults)


# The old `/shift codes` page renderer is gone (`/shift history` draws the same blocks). What it
# drew is kept as fixed strings, taken from the snapshot's own `render_code_page` before it was
# deleted from the live module, so the blocks can't drift without a test noticing.
_BLOCK = "```\n{code}\n```\nFirst seen {day} · {rest}"
_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_URL = "<https://e.com/a>"


def _history(views, tz):
    embeds = new.render_history_pages(
        views, timezone=tz, now=datetime(2026, 9, 26, 12, 0, tzinfo=UTC), title="/shift history"
    )
    return "\n\n".join(e.description for e in embeds)


@pytest.mark.parametrize(
    ("tz", "day"), [("America/Los_Angeles", "2026-09-25"), ("Pacific/Kiritimati", "2026-09-26")]
)
def test_history_draws_the_old_code_page_blocks_byte_for_byte(tz, day):
    def block(i, rest):
        return _BLOCK.format(code=_code(i), day=day, rest=rest)

    base = f"Gearbox Blog · {_URL}"
    assert _history([], tz) == "No codes seen in that window."
    assert _history([_view(0)], tz) == block(0, base)
    markers = [
        _view(0),
        _view(1, status="too_old"),
        _view(2, status="seeded"),
        _view(3, from_roundup=True),
    ]
    assert _history(markers, tz) == "\n\n".join(
        [
            block(0, base),
            block(1, f"{base} · (old post)"),
            block(2, f"{base} · (already around when alerts started)"),
            block(3, f"{base} · (from a roundup)"),
        ]
    )
    stars = [_view(0, source_name="*" * 100), _view(1, item_url="javascript:x")]
    assert _history(stars, tz) == "\n\n".join(
        [
            block(0, "\\*" * 49 + "\\" + _ELLIPSIS + f" · {_URL}"),
            block(1, "Gearbox Blog"),
        ]
    )
    emoji = [_view(0, source_name="\N{GRINNING FACE}" * 100, item_url="")]
    assert _history(emoji, tz) == block(0, "\N{GRINNING FACE}" * 49 + _ELLIPSIS)


# --- helpers every other renderer leans on ---


def test_the_shared_text_helpers_are_unchanged(old):
    samples = [
        "",
        "plain",
        "@everyone <@&123456789012345678> <#1> </a:1> https://x.example discord.gg/x",
        "*_~`|>#-# [x](y)",
        "\U0001f600" * 50,
        "e\u0301" * 50,
        "\u202e\u2066 mixed \u200d",
    ]
    for text in samples:
        assert new.esc(text) == old.esc(text)
        assert new.discord_len(text) == old.discord_len(text)
        assert new._truncate_utf16(text, 7, suffix="\u2026") == old._truncate_utf16(
            text, 7, suffix="\u2026"
        )


# --- the contract of everything that was public ---


# Gone on purpose, with the `/shift codes` it drew (its blocks live on in `/shift history`, and
# the fixed strings above hold them).
RETIRED = {"render_code_page"}


def test_every_old_public_name_is_still_exported(old):
    assert set(old.__all__) - RETIRED <= set(new.__all__)
    assert not RETIRED & set(new.__all__)


def test_every_old_public_function_still_accepts_every_old_call(old):
    for name in sorted(set(old.__all__) - RETIRED):
        before, after = getattr(old, name), getattr(new, name)
        if not inspect.isfunction(before):
            continue
        old_params = inspect.signature(before).parameters
        new_params = inspect.signature(after).parameters
        for pname, param in old_params.items():
            assert pname in new_params, (name, pname)
            assert new_params[pname].kind == param.kind, (name, pname)
            assert new_params[pname].default == param.default, (name, pname)
        assert list(old_params) == list(new_params)[: len(old_params)], name
        for pname in set(new_params) - set(old_params):
            assert new_params[pname].default is not inspect.Parameter.empty, (name, pname)


def test_rendered_alert_only_gained_trailing_fields_with_defaults(old):
    before = dataclasses.fields(old.RenderedAlert)
    after = dataclasses.fields(new.RenderedAlert)
    assert [f.name for f in after][: len(before)] == [f.name for f in before]
    for f in before:
        assert new.RenderedAlert.__dataclass_fields__[f.name].default == f.default
    added = after[len(before) :]
    assert {f.name for f in added} == {"header", "golden_prefix"}
    assert all(f.default is not dataclasses.MISSING for f in added)


def test_old_style_construction_and_equality_of_rendered_alerts_still_work(old):
    a = new.RenderedAlert(content="c", codes=["x"], ping=False, nonce="n")
    assert (a.header, a.golden_prefix, a.ping_prefix) == ("", False, "")
