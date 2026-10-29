"""Adversarial round on masked links (v3.4.0): try to break the label, the markdown and the cuts.

`tests/test_masked_links.py` pins the happy paths. This file goes after the edges: hosts that
canonicalize oddly, IDNA rules that disagree about the same name, labels at the 40-character
line, duplicate numbering, titles that try to close or open a link, every length path (4096
shedding, "+N more", link shedding, the hard-cut fallbacks), and the promise that nothing else
in `format.py` (or the lounge) moved.

A few tests here are written to fail against the code as built; each says why in its docstring.
"""

from __future__ import annotations

import ast
import importlib.util
import random
import re
import subprocess
import sys
import types
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from newsbot.bot import format as fmt
from newsbot.bot.format import (
    _link_label,
    _link_line,
    _truncate_description,
    discord_len,
    render_code_alerts,
    render_followup_alerts,
    render_guild_digest,
    render_headlines_embed,
    render_item_page,
    render_picked_headlines_embed,
    render_roundup_alerts,
    render_story_page,
)
from newsbot.config import GameCfg
from newsbot.pipeline.summarize import StoryDraft
from newsbot.shift.decide import CodeCandidate
from newsbot.store.models import HeadlineItem, ItemView, StoryView

REPO = Path(__file__).resolve().parent.parent
BASE_COMMIT = "1d51ba8"
LIMIT = 4096
T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
RUN_DATE = date(2026, 10, 29)
PAL = GameCfg(key="palworld", name="Palworld")
IND = GameCfg(key="industry", name="Gaming industry")

# A masked link as the bot writes one, with a permissive URL part (a host can hold odd
# characters, so the URL is "anything up to the first `>)`"), and the shapes a cut leaves.
_ANY_LINK_RE = re.compile(r"\[[a-z0-9.\- ()]+\]\(<.*?>\)", re.DOTALL)
_LABEL_TAIL_RE = re.compile(r"\[[a-z0-9.\- ()]*\]?\(?<?$")


def open_link(text: str) -> bool:
    """True when `text` ends in the middle of a masked link (an unclosed `[` or `(<`)."""
    rest = _ANY_LINK_RE.sub("", text)
    return "](<" in rest or bool(_LABEL_TAIL_RE.search(rest))


def unescaped_link_joints(text: str) -> list[int]:
    """Indexes of every `](` that Discord would read as the joint of a masked link."""
    found = []
    for i in range(len(text) - 1):
        if text[i : i + 2] != "](":
            continue
        slashes = 0
        j = i - 1
        while j >= 0 and text[j] == "\\":
            slashes += 1
            j -= 1
        if slashes % 2 == 0:
            found.append(i)
    return found


def item(title="A headline", *, url="https://www.pcgamer.com/games/", trust="press"):
    return HeadlineItem(
        url=url,
        title=title,
        source_name="Feed",
        trust=trust,
        published_at=T0,
        collected_at=T0,
        uncertain=False,
    )


def draft(urls, headline="Headline", summary="Summary", label="reported"):
    return StoryDraft(
        headline=headline,
        summary=summary,
        label=label,
        item_urls=list(urls),
        update_of_story_id=None,
    )


def story_view(urls, summary="Summary", n=1):
    return StoryView(
        id=n,
        topic_key="palworld",
        headline=f"Headline {n}",
        summary=summary,
        label="reported",
        created_at=T0,
        urls=list(urls),
        is_update_of=None,
    )


def item_view(url, title="A headline", n=1):
    return ItemView(
        id=n,
        url=url,
        title=title,
        excerpt="",
        source_name="Feed",
        trust="press",
        published_at=T0,
        collected_at=T0,
        topic_keys=["palworld"],
    )


def shown_label(url: str) -> str:
    """The text `_link_line` puts on `url` (the label of the URL as it is really shown)."""
    line = _link_line([url])
    match = re.match(r"\[(.*?)\]\(<", line)
    assert match, f"no link drawn for {url!r}: {line!r}"
    return match.group(1)


# --- the label: www, dots, ports, userinfo, case ---


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://www.example.com/x", "example.com"),
        ("https://WWW.EXAMPLE.COM/x", "example.com"),
        ("https://www2.example.com/x", "www2.example.com"),
        ("https://www3.example.com/x", "www3.example.com"),
        ("https://wwwexample.com/x", "wwwexample.com"),
        ("https://www-example.com/x", "www-example.com"),
        ("https://notwww.example.com/x", "notwww.example.com"),
        ("https://example.com./x", "example.com"),
        ("https://example.com../x", "example.com"),
        ("https://WWW.Example.COM.:443/x", "example.com"),
        ("https://example.com:8443/x", "example.com"),
        ("http://example.com:80/x", "example.com"),
        ("https://example.com:0/x", "example.com"),
        ("https://127.0.0.1:8080/x", "127.0.0.1"),
        ("https://a.b.c.d.e.example.com/x", "a.b.c.d.e.example.com"),
        ("https://localhost/x", "localhost"),
        ("https://a/x", "a"),
        ("https://xn--bcher-kva.de/x", "xn--bcher-kva.de"),
        ("https://www.xn--bcher-kva.de/x", "xn--bcher-kva.de"),
        ("https://BÜCHER.de/x", "xn--bcher-kva.de"),
    ],
)
def test_label_after_the_canonical_form_of_the_host(url, label):
    assert shown_label(url) == label


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/x",
        "https://user:secret@example.com/x",
        "https://:@example.com/x",
        "https://@example.com/x",
        "https://example.com@evil.com/x",
        "https://example.com:99999/x",
    ],
)
def test_userinfo_and_bad_ports_never_reach_a_link_or_a_label(url):
    assert _link_line([url]) == ""
    assert fmt._headline_line(item(url=url)) is None


def test_a_headline_with_a_noisy_host_shows_the_clean_label_and_the_canonical_url():
    line = fmt._headline_line(item("t", url="https://www.Example.com./a?utm_source=x#top"))
    assert line == "• t — [example.com](<https://example.com/a>)"


@pytest.mark.parametrize(
    "url",
    [
        "https://exa%6dple.com/x",
        "https://%65xample.com/x",
        "https://example.com%2eevil.com/x",
        "https://example%2Ecom/x",
        "https://exam%00ple.com/x",
    ],
)
def test_a_percent_encoded_host_is_labelled_link(url):
    assert shown_label(url) == "link"


@pytest.mark.parametrize(
    "url",
    [
        "https://[2001:db8::1]/x",
        "https://[::1]:8080/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://foo_bar.example.com/x",
        "https://_dmarc.example.com/x",
        "https://a*b.com/x",
        "https://a`b.com/x",
        "https://a|b.com/x",
        "https://a~b.com/x",
        "https://a\\b.com/x",
        "https://a<b.com/x",
    ],
)
def test_hosts_outside_the_label_alphabet_are_labelled_link(url):
    assert shown_label(url) == "link"


def test_an_ipv6_literal_link_keeps_its_brackets_inside_the_angle_brackets_only():
    assert _link_line(["https://[2001:db8::1]/x"]) == "[link](<https://[2001:db8::1]/x>)"


# --- the label: 40 and 41 characters ---


def test_forty_and_forty_one_character_hosts_with_dots_and_hyphens():
    forty = "a" * 19 + "." + "b" * 10 + "-" + "c" * 9
    assert len(forty) == 40
    assert shown_label(f"https://{forty}/x") == forty
    assert shown_label(f"https://{forty}c/x") == "link"
    assert shown_label(f"https://www.{forty}/x") == forty  # www. does not count toward the 40
    assert shown_label(f"https://www.{forty}c/x") == "link"


def test_the_forty_is_counted_after_idna_so_a_short_unicode_host_can_be_too_long():
    def puny(n):
        return ("日" * n + ".jp").encode("idna").decode("ascii")

    longest_ok = max(n for n in range(1, 40) if len(puny(n)) <= 40)
    assert len(puny(longest_ok + 1)) > 40
    assert shown_label(f"https://{'日' * longest_ok}.jp/x") == puny(longest_ok)
    assert shown_label(f"https://{'日' * (longest_ok + 1)}.jp/x") == "link"


def test_a_forty_character_label_still_numbers_and_stays_whole():
    host = "a" * 28 + ".example.com"
    assert len(host) == 40
    line = _link_line([f"https://{host}/a", f"https://{host}/b"])
    assert line == f"[{host}](<https://{host}/a>) · [{host} (2)](<https://{host}/b>)"


def test_a_63_character_dns_label_is_over_the_line_and_a_64_character_one_is_dropped_whole():
    assert shown_label("https://" + "a" * 63 + ".com/x") == "link"
    assert _link_line(["https://" + "a" * 64 + ".com/x"]) == ""  # canonicalize() refuses it


# --- the label: look-alikes ---


@pytest.mark.parametrize(
    "host",
    [
        "pаypal.com",  # Cyrillic a
        "gοogle.com",  # Greek omicron
        "аррӏе.com",  # a whole Cyrillic word
        "exаmple.co.uk",
        "ｅxample.com.evil.org",  # fullwidth e (becomes a plain "e" under IDNA mapping)
        "😀.com",
        "日本語.jp",
        "mixed-日本語-latin.com",
    ],
)
def test_a_non_ascii_host_is_never_drawn_in_its_own_letters(host):
    label = shown_label(f"https://{host}/x")
    assert label.isascii()
    assert re.fullmatch(r"[a-z0-9.\-]+", label)


@pytest.mark.parametrize(
    ("host", "xn_form"),
    [("pаypal.com", "xn--pypal-4ve.com"), ("аpple.com", "xn--pple-43d.com")],
)
def test_a_mixed_script_look_alike_shows_as_xn_form(host, xn_form):
    assert shown_label(f"https://{host}/x") == xn_form


@pytest.mark.parametrize(
    ("host", "real_host"),
    [
        ("faß.de", "xn--fa-hia.de"),
        ("ß.de", "xn--zca.de"),
        ("ς.com", "xn--3xa.com"),
    ],
)
def test_a_label_names_the_host_the_connection_really_uses_and_not_its_idna_2003_cousin(
    host, real_host
):
    """FAILS AS BUILT. Python's `idna` codec is IDNA 2003: `faß.de` becomes `fass.de`.

    The connection (httpx, browsers) uses IDNA 2008, where `faß.de` is `xn--fa-hia.de`. So
    the label says `fass.de`, a different real site, over a link that goes to `faß.de`: the
    look-alike passing for a real domain, which the brief says must not happen. `netguard.py`
    already documents the same split and uses the 2008 form. Either the 2008 form or `link`
    is acceptable here.
    """
    label = shown_label(f"https://{host}/x")
    assert label in {real_host, "link"}


@pytest.mark.parametrize("host", ["a‍b.com", "a‌b.com"])
def test_a_joiner_in_a_host_does_not_vanish_from_the_label(host):
    """FAILS AS BUILT. IDNA 2003 deletes U+200C and U+200D, so `a<ZWJ>b.com` is labelled `ab.com`.

    IDNA 2008 refuses the name outright, so a client never connects to `ab.com`; the label
    claims a site the link does not go to.
    """
    assert shown_label(f"https://{host}/x") != "ab.com"


def test_label_never_raises_and_always_fits_the_alphabet():
    rng = random.Random(29)  # noqa: S311 (a reproducible sequence, not a secret)
    pieces = [
        "http://", "https://", "www.", "WWW.", "xn--", "a", "B", "z9", "-", "_", ".", "..", ":",
        "@", "/", "[", "]", "::1", "%41", "%zz", " ", "\t", "\n", "\x00", "ß", "ς", "日", "😀",
        "‍", "ﬁ", " ", "\ud800", "é", "‮", "<", ">", "(", ")", "\\", "`",
    ]  # fmt: skip
    for _ in range(4000):
        url = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        label = _link_label(url)
        assert label == "link" or (len(label) <= 40 and re.fullmatch(r"[a-z0-9.\-]+", label)), (
            url,
            label,
        )


def test_every_shown_link_gets_a_label_in_the_alphabet():
    rng = random.Random(30)  # noqa: S311 (a reproducible sequence, not a secret)
    pieces = ["www.", "a", "B", "-", "_", ".", "ß", "日", "é", " ", ">", ")", "%41", "xn--", "9"]
    for _ in range(3000):
        url = "https://" + "".join(rng.choice(pieces) for _ in range(rng.randint(1, 10))) + "/p"
        line = _link_line([url])
        if not line:
            continue
        match = re.match(r"\[([^\]]*)\]\(<", line)
        assert match, (url, line)
        assert re.fullmatch(r"[a-z0-9.\-]+", match.group(1)), (url, line)


# --- duplicate numbering ---


def test_link_fallbacks_are_numbered_among_themselves_and_apart_from_real_labels():
    line = _link_line(
        ["https://[::1]/a", "https://foo_bar.com/b", "https://pcgamer.com/c", "https://[::2]/d"]
    )
    assert line == (
        "[link](<https://[::1]/a>) · [link (2)](<https://foo_bar.com/b>) · "
        "[pcgamer.com](<https://pcgamer.com/c>) · +1 more"
    )


def test_a_real_host_called_link_collides_with_the_fallback_and_is_numbered():
    line = _link_line(["https://link/a", "https://[::1]/b"])
    assert line == "[link](<https://link/a>) · [link (2)](<https://[::1]/b>)"


def test_www_and_bare_spellings_of_one_site_number_as_one_label():
    line = _link_line(
        ["https://www.pcgamer.com/a", "https://PCGAMER.com/b", "https://pcgamer.com./c"]
    )
    assert re.findall(r"\[([^\]]*)\]", line) == [
        "pcgamer.com",
        "pcgamer.com (2)",
        "pcgamer.com (3)",
    ]


def test_a_unicode_host_and_its_xn_spelling_number_as_one_label():
    line = _link_line(["https://bücher.de/a", "https://xn--bcher-kva.de/b"])
    assert re.findall(r"\[([^\]]*)\]", line) == ["xn--bcher-kva.de", "xn--bcher-kva.de (2)"]


def test_a_dropped_invite_between_two_duplicates_takes_no_number():
    line = _link_line(["https://pcgamer.com/a", "https://discord.gg/abc", "https://pcgamer.com/b"])
    assert re.findall(r"\[([^\]]*)\]", line) == ["pcgamer.com", "pcgamer.com (2)"]
    assert "more" not in line


def test_numbers_stop_at_three_links_and_the_hidden_ones_are_just_counted():
    line = _link_line([f"https://pcgamer.com/{i}" for i in range(6)])
    assert re.findall(r"\[([^\]]*)\]", line) == [
        "pcgamer.com",
        "pcgamer.com (2)",
        "pcgamer.com (3)",
    ]
    assert line.endswith(" · +3 more")
    assert "(4)" not in line


def test_the_more_count_leaves_out_invites_and_junk():
    urls = ["https://discord.gg/x", "javascript:x", *[f"https://s{i}.com/p" for i in range(5)]]
    line = _link_line(urls)
    assert line.endswith(" · +2 more")
    assert line.count("](<") == 3


def test_a_label_that_already_looks_numbered_cannot_exist():
    # A host can't hold parentheses, so "x (2)" can't be a host that then collides with the
    # second "x"; the only way to see "(2)" in a label is the numbering itself. (A host with a
    # space in it is not drawn at all, see the odd-host tests below.)
    assert shown_label("https://x.com/a") == "x.com"
    assert _link_line(["https://x(2).com/a"]) == ""  # a paren in a host: not drawn at all
    assert _link_line(["https://x (2).com/a"]) == ""


# --- markdown breakage ---

_HOSTILE_TITLES = [
    "[hi](<https://evil.example/>)",
    "[hi](https://evil.example/)",
    "](<https://evil.example/>)",
    "x](<https://evil.example/>) y",
    "title ending in ]",
    "title ending in (",
    "title ending in [",
    "title ending in \\",
    "title ending in \\\\",
    "title ending in ](",
    "title ending in \\](",
    "\\[x\\](<https://evil.example/>)",
    "\\\\[x](<https://evil.example/>)",
    "\\\\\\](<",
    "[[[](((<<<>>>)))",
    "<https://evil.example/>",
    "<https://evil.example/> [x](<https://evil.example/>)",
    ">)",
    "pcgamer.com](<https://evil.example/>)",
    "[pcgamer.com](<https://evil.example/>)",
    "[pcgamer.com (2)](<https://evil.example/>)",
    "]",
    "(",
    "[",
    "[]",
    "[](",
    "](",
    "](<",
    "](<>)",
    "`](<https://evil.example/>)`",
    "||](<https://evil.example/>)||",
    "**](<https://evil.example/>)**",
    "@everyone](<https://evil.example/>)",
    "[a](<https://e.example/>)\n[b](<https://e.example/>)",
]


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_a_hostile_headline_title_leaves_exactly_the_one_real_link(title):
    line = fmt._headline_line(item(title))
    assert line is not None
    assert line.endswith(" — [pcgamer.com](<https://pcgamer.com/games>)")
    assert "\n" not in line
    joints = unescaped_link_joints(line)
    assert joints == [line.rindex("](<")], line


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_story_text_leaves_exactly_the_real_links(title):
    story = draft(["https://pcgamer.com/a", "https://ign.com/b"], headline=title, summary=title)
    block = fmt._story_block(story)
    real = [m.start() + m.group().index("](") for m in _ANY_LINK_RE.finditer(block)]
    assert len(real) == 2
    assert unescaped_link_joints(block) == real, block
    page = render_story_page(
        [story_view(["https://pcgamer.com/a", "https://ign.com/b"], summary=title)],
        {"palworld": PAL},
        title,
        1,
        1,
    )
    text = str(page.description)
    real = [m.start() + m.group().index("](") for m in _ANY_LINK_RE.finditer(text)]
    assert unescaped_link_joints(text) == real, text


def test_fuzzed_titles_never_open_a_second_link():
    rng = random.Random(31)  # noqa: S311 (a reproducible sequence, not a secret)
    alphabet = [
        *"\\[]()<>*_~|`@# :/.-!&",
        *["](<", "](", "https://", "<https://e.com>", "[a](<https://e.com>)", "@everyone"],
        *["discord.gg/x", "\u200b", "\n", "😀", "<#1>"],
    ]
    link = "[pcgamer.com](<https://pcgamer.com/games>)"
    for _ in range(6000):
        title = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        line = fmt._headline_line(item(title, trust=rng.choice(["official", "press"])))
        assert line is not None and line.endswith(" — " + link), (title, line)
        assert unescaped_link_joints(line) == [len(line) - len(link) + link.index("](")], (
            title,
            line,
        )


def test_a_title_that_is_a_bare_link_is_not_counted_as_one_by_the_link_counter():
    module = _load_link_counts()
    for title in (
        "<https://evil.example/>",
        "[x](<https://evil.example/>)",
        "https://evil.example",
    ):
        line = fmt._headline_line(item(title))
        assert module._LINK_RE.findall(line) == ["https://pcgamer.com/games"], line


def _load_link_counts():
    path = REPO / "scripts" / "link_counts.py"
    spec = importlib.util.spec_from_file_location("link_counts_adversarial", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_link_counts_counts_one_per_masked_link_including_fallbacks_and_numbered_ones():
    module = _load_link_counts()
    line = _link_line(
        ["https://[::1]/a", "https://[::2]/b", "https://pcgamer.com/c", "https://x.com/d"]
    )
    assert len(module._LINK_RE.findall(line)) == 3  # the fourth is "+1 more"
    story = fmt._story_block(draft(["https://pcgamer.com/a", "https://pcgamer.com/b"]))
    assert module._LINK_RE.findall(story) == ["https://pcgamer.com/a", "https://pcgamer.com/b"]


def test_link_counts_counts_a_digest_through_the_real_renderers_once_per_link():
    module = _load_link_counts()
    rendered = render_guild_digest(
        RUN_DATE,
        [PAL],
        {"palworld": 5},
        stories_by_game={
            "palworld": [
                draft([f"https://pcgamer.com/{i}" for i in range(5)]),
                draft(["https://ign.com/a"], headline="Other"),
            ]
        },
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
    )
    text = str(rendered.messages[0].embed.description)
    assert len(module._LINK_RE.findall(text)) == 4  # 3 shown (+2 more) and 1


# --- URLs with characters that matter to the markdown ---


@pytest.mark.parametrize(
    "url",
    [
        "https://a.com/x?q=>](<https://evil.com>)&z=[1]",
        "https://a.com/Foo_(bar)",
        "https://a.com/x)(<y",
        "https://a.com/x?a=1&b=<&c=‮",
        "https://a.com/a>b",
        "https://a.com/x|y||z`w",
        "https://a.com/x\\",
        "https://a.com/\u200b",
        "https://a.com/%E2%80%AE",
    ],
)
def test_a_path_or_query_cannot_end_the_angle_brackets_or_the_parentheses_early(url):
    line = _link_line([url])
    match = re.fullmatch(r"\[a\.com\]\(<(https://a\.com/[^<>\s\[\]`|\\]*)>\)", line)
    assert match, line
    assert "](" not in match.group(1)
    assert open_link(line) is False


# --- URLs whose host holds characters canonicalize() lets through ---


_UNDRAWABLE_HOSTS = [
    "exa mple.com",
    "a>b.com",
    "a >b.com",
    "a\u00a0b.com",
    "a\x1fb.com",
    "a\x85b.com",
]


@pytest.mark.parametrize("host", _UNDRAWABLE_HOSTS)
def test_a_url_with_whitespace_or_gt_in_its_host_is_not_drawn_on_any_masked_surface(host):
    odd = f"https://{host}/x"
    assert _link_line([odd]) == ""
    assert fmt._headline_line(item(url=odd)) is None
    assert render_headlines_embed(PAL, [item(url=odd)]) is None
    assert render_picked_headlines_embed(IND, [item(url=odd)]) is None
    block = fmt._story_block(draft([odd]))
    assert "](<" not in block


@pytest.mark.parametrize("host", ["exa mple.com", "a>b.com"])
def test_an_undrawable_url_takes_no_slot_no_number_and_no_more_count(host):
    odd = f"https://{host}/x"
    line = _link_line([odd, "https://pcgamer.com/a", odd, "https://pcgamer.com/b", odd])
    assert (
        line
        == "[pcgamer.com](<https://pcgamer.com/a>) · [pcgamer.com (2)](<https://pcgamer.com/b>)"
    )
    line = _link_line([odd, *[f"https://s{i}.com/p" for i in range(4)], odd])
    assert line.count("](<") == 3 and line.endswith(" · +1 more")


@pytest.mark.parametrize("host", ["exa mple.com", "a>b.com"])
def test_an_undrawable_first_url_does_not_cost_a_story_a_link_that_would_fit(host):
    odd = f"https://{host}/x"
    four = [f"https://site{i}.example.com/{'p' * 315}" for i in range(4)]
    story = draft([odd, *four], summary="s" * 3000)
    # The same story without the odd URL, so the answer is whatever the plain one gives.
    plain = fmt._fitting_story_block(draft(four, summary="s" * 3000))
    assert plain is not None
    assert fmt._fitting_story_block(story) == plain
    assert odd not in plain


@pytest.mark.parametrize("host", ["exa mple.com", "a>b.com"])
def test_the_surfaces_agree_on_which_urls_are_drawn(host):
    odd = f"https://{host}/x"
    ok = "https://pcgamer.com/a"
    story = draft([odd, ok])
    page = render_story_page([story_view([odd, ok])], {"palworld": PAL}, "t", 1, 1)
    digest = fmt._topic_embed(PAL, [story])
    for text in (fmt._story_block(story), str(page.description), str(digest.description)):
        assert text.endswith("[pcgamer.com](<https://pcgamer.com/a>)")
        assert host not in text
    items_page = render_item_page(
        [item_view(odd), item_view(ok)], {"palworld": "Palworld"}, "t", 1, 1
    )
    assert str(items_page.description).count("](<") == 1
    assert (
        render_headlines_embed(PAL, [item("a", url=odd), item("b", url=ok)]).description.count(
            "](<"
        )
        == 1
    )


@pytest.mark.parametrize("host", _UNDRAWABLE_HOSTS)
def test_the_shift_surfaces_still_show_such_a_url_exactly_as_before(old_format, host):
    url = f"https://{host}/post"
    cands = [_cand("AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", url)]
    new = [m.content for m in render_code_alerts(cands, ping=False)]
    old = [m.content for m in old_format.render_code_alerts(cands, ping=False)]
    assert new == old


@pytest.mark.parametrize("host", ["a`b.com", "a|b.com", "a\\b.com"])
def test_odd_hosts_that_the_cut_pattern_does_know_are_cut_whole_or_not_at_all(host):
    link = _link_line([f"https://{host}/x"])
    text = f"Headline\nSummary\n{link}"
    for limit in range(1, discord_len(text)):
        assert open_link(_truncate_description(text, limit)) is False, limit


# --- cuts: _truncate_description ---


def _story_like_text(count=4, label_host="pcgamer.com"):
    blocks = []
    for i in range(count):
        urls = [
            f"https://{label_host}/{i}/a",
            f"https://{label_host}/{i}/b",
            "https://[2001:db8::1]/c",
            "https://extra.example.com/d",
        ]
        blocks.append(
            fmt._story_block(draft(urls, headline=f"Headline {i}", summary="Summary text " * 4))
        )
    return "\n\n".join(blocks)


@pytest.mark.parametrize(
    "label_host", ["pcgamer.com", "a" * 28 + ".example.com", "a", "xn--bcher-kva.de"]
)
def test_the_hard_cut_never_ends_inside_a_link_at_any_limit(label_host):
    text = _story_like_text(label_host=label_host)
    spans = [(m.start(), m.end()) for m in _ANY_LINK_RE.finditer(text)]
    assert len(spans) >= 12
    for limit in range(1, discord_len(text) + 1):
        cut = _truncate_description(text, limit)
        assert discord_len(cut) <= limit, limit
        assert open_link(cut) is False, (limit, cut[-60:])
        if cut != text:
            assert cut.endswith("…")
            body = cut[:-1]
            assert text.startswith(body), limit
            assert not any(start < len(body) < end for start, end in spans), limit


def test_the_hard_cut_on_an_astral_title_next_to_a_link_still_counts_utf16():
    text = "😀" * 40 + "\n" + _link_line(["https://pcgamer.com/a", "https://ign.com/b"])
    for limit in range(1, discord_len(text)):
        cut = _truncate_description(text, limit)
        assert discord_len(cut) <= limit, limit
        assert open_link(cut) is False, limit
        cut.encode("utf-16")  # a lone surrogate would raise


def test_the_hard_cut_with_the_link_at_the_very_start_leaves_only_the_ellipsis():
    text = _link_line(["https://pcgamer.com/a"]) + " tail " * 10
    cut = _truncate_description(text, 10)
    assert cut == "…"


def test_the_hard_cut_with_a_limit_of_zero_does_not_crash_or_leave_a_bracket():
    text = _link_line(["https://pcgamer.com/a"])
    assert _truncate_description(text, 0) in {"", "…"}


def test_a_text_exactly_at_the_limit_is_returned_unchanged_and_one_over_is_cut_cleanly():
    text = "Summary\n" + _link_line(["https://pcgamer.com/aaaa", "https://ign.com/bbbb"])
    assert _truncate_description(text, discord_len(text)) == text
    over = _truncate_description(text, discord_len(text) - 1)
    assert over != text
    assert open_link(over) is False
    assert discord_len(over) <= discord_len(text) - 1


def test_a_cut_at_the_edge_of_a_link_leaves_no_dangling_separator():
    """FAILS AS BUILT (cosmetic). The docstring promises "no line ends on" a middle dot or dash.

    That holds only when the cut lands inside a link. If it lands in the ` · ` before a link, or
    right before the `[`, the separator stays: `[a.com](<https://a.com/x>) ·…`. The build log
    accepts losing a separator; this is the opposite, a separator kept at the end of a post.
    """
    text = "Head\nSumm\n[a.com](<https://a.com/x>) · [b.com](<https://b.com/yyyyyyy>) · +2 more"
    for limit in range(1, discord_len(text)):
        cut = _truncate_description(text, limit)
        assert not re.search(r"[·—]\s*…$", cut), (limit, cut[-40:])


# --- cuts through the renderers ---


def test_topic_embed_hard_cut_sweep_never_leaves_half_a_link():
    for pad in range(0, 360, 3):
        story = draft(
            [f"https://site{j}.example.com/{'p' * 140}" for j in range(3)],
            summary="s" * (3700 + pad),
        )
        text = str(fmt._topic_embed(PAL, [story]).description)
        assert discord_len(text) <= LIMIT, pad
        assert open_link(text) is False, (pad, text[-80:])


def test_story_page_cut_sweep_never_leaves_half_a_link():
    for count in range(1, 14):
        for summary in (40, 400):
            stories = [
                story_view(
                    [f"https://site{count}-{j}.example.com/{'p' * 110}" for j in range(3)],
                    summary="s" * summary,
                    n=k,
                )
                for k in range(count)
            ]
            text = str(render_story_page(stories, {"palworld": PAL}, "t", 1, 1).description)
            assert discord_len(text) <= LIMIT, (count, summary)
            assert open_link(text) is False, (count, summary, text[-80:])


def test_item_page_cut_sweep_never_leaves_half_a_link():
    for count in range(2, 14):
        for title_len in (60, 299):
            items = [
                item_view(f"https://site{k}.example.com/{'p' * 150}", title="t" * title_len, n=k)
                for k in range(count)
            ]
            text = str(render_item_page(items, {"palworld": "Palworld"}, "t", 1, 1).description)
            assert discord_len(text) <= LIMIT, (count, title_len)
            assert open_link(text) is False, (count, title_len, text[-80:])


def test_a_free_digest_with_a_huge_note_cuts_before_the_link_or_drops_the_line():
    for note_len in range(3500, 4200, 5):
        embed = render_headlines_embed(PAL, [item("One")], note="n" * note_len)
        text = str(embed.description)
        assert discord_len(text) <= LIMIT, note_len
        assert open_link(text) is False, (note_len, text[-60:])


def test_a_comped_fallback_with_a_note_at_the_edge_never_shows_half_a_link():
    items = [item(f"Headline {i}", url=f"https://s{i}.example.com/{'p' * 200}") for i in range(30)]
    for note_len in range(3000, 4090, 11):
        embed = render_headlines_embed(PAL, items, note="n" * note_len)
        text = str(embed.description)
        assert discord_len(text) <= LIMIT, note_len
        assert open_link(text) is False, (note_len, text[-60:])


# --- shedding: free lists and "+N more" ---


def _reference_kept(lines, sep="\n"):
    """The most parts that fit under 4096 with their own "+N more" line, written out by hand."""
    for kept in range(len(lines), 0, -1):
        cut = len(lines) - kept
        text = sep.join(lines[:kept])
        if cut:
            text += f"\n\n+{cut} more, use /news"
        if discord_len(text) <= LIMIT:
            return kept
    return 0


def test_free_list_more_count_is_exact_at_every_size_around_the_limit():
    crossings = set()
    for others in (200, 230, 260):
        for pad in range(0, 300, 3):
            items = [item("headline 000 " + "w" * pad, url="https://s0.example.com/0")]
            items += [
                item(f"headline {i:03d} " + "w" * others, url=f"https://s{i}.example.com/{i}")
                for i in range(1, 25)
            ]
            items = [
                replace(it, published_at=T0 - timedelta(hours=n)) for n, it in enumerate(items)
            ]
            lines = [fmt._headline_line(it) for it in sorted(items, key=fmt._headline_sort_key)]
            want = _reference_kept(lines)
            text = str(render_headlines_embed(PAL, items).description)
            assert discord_len(text) <= LIMIT
            shown = [ln for ln in text.split("\n") if ln.startswith("• ")]
            assert len(shown) == want, (others, pad)
            assert text.endswith(f"\n\n+{len(lines) - want} more, use /news"), (others, pad)
            crossings.add(want)
    assert len(crossings) > 1  # the sweep really crossed a shedding boundary


def test_free_list_with_a_note_counts_only_the_item_lines_as_more():
    items = [
        item(f"headline {i:03d} " + "w" * 240, url=f"https://s{i}.example.com/{i}", trust="press")
        for i in range(40)
    ]
    items = [replace(it, published_at=T0 - timedelta(hours=n)) for n, it in enumerate(items)]
    embed = render_headlines_embed(PAL, items, note="Summary unavailable; showing headlines.")
    text = str(embed.description)
    shown = [ln for ln in text.split("\n") if ln.startswith("• ")]
    assert text.startswith("Summary unavailable")
    assert text.endswith(f"+{40 - len(shown)} more, use /news")
    assert discord_len(text) <= LIMIT


def test_an_all_dropped_list_makes_no_embed_even_with_masked_links_in_play():
    junk = [item(url=u) for u in ("https://discord.gg/abc", "javascript:x", "not a url")]
    assert render_headlines_embed(PAL, junk) is None
    assert render_headlines_embed(PAL, junk, note="Summary unavailable") is None
    assert render_picked_headlines_embed(IND, junk) is None


def test_the_comped_digest_more_count_is_exact_around_the_limit():
    crossings = set()
    for others in (300, 340, 380):
        for pad in range(1, 400, 3):
            built = [
                draft(
                    [f"https://site{i}-{j}.example.com/{'p' * 70}" for j in range(3)],
                    headline=f"Headline {i}",
                    summary="s" * (pad if i == 0 else others),
                )
                for i in range(12)
            ]
            blocks = [fmt._story_block(s) for s in sorted(built, key=fmt._sort_key)]
            want = _reference_kept(blocks, "\n\n")
            got = str(fmt._topic_embed(PAL, built).description)
            assert discord_len(got) <= LIMIT
            assert got.count("REPORTED \u00b7") == want, (others, pad)
            assert got.endswith(f"+{len(blocks) - want} more, use /news"), (others, pad)
            assert open_link(got) is False
            crossings.add(want)
    assert len(crossings) > 1


# --- link shedding in _fitting_story_block ---


def test_link_shedding_keeps_as_many_whole_links_as_fit_across_the_threshold():
    kept_counts = set()
    for pad in range(0, 900, 7):
        urls = [f"https://site{j}.example.com/{'p' * 1100}" for j in range(3)]
        story = draft(urls, summary="s" * (400 + pad))
        block = fmt._fitting_story_block(story)
        if block is None:
            kept_counts.add(None)
            continue
        assert discord_len(block) <= LIMIT, pad
        assert open_link(block) is False, pad
        links = block.count("](<")
        kept_counts.add(links)
        if links < 3:
            more = fmt._story_block(replace(story, item_urls=urls[: links + 1])).rstrip("\n")
            assert discord_len(more) > LIMIT, (pad, links)
        assert not block.endswith("\n")
    assert {3, 2} <= kept_counts or {3, 2, 1} <= kept_counts  # it did shed, and it kept some


def test_link_shedding_counts_only_links_that_will_show_and_keeps_masked_labels_right():
    urls = [
        "https://discord.gg/zzz",
        "https://www.pcgamer.com/a",
        "https://pcgamer.com/b" + "p" * 1800,
    ]
    story = draft(urls, summary="s" * 2600)
    block = fmt._fitting_story_block(story)
    assert block is not None and discord_len(block) <= LIMIT
    assert "discord.gg" not in block
    assert re.findall(r"\[([^\]]*)\]\(<", block)[0] == "pcgamer.com"


def test_a_story_over_the_limit_even_without_links_is_skipped_not_cut():
    story = draft(["https://pcgamer.com/a"], summary="s" * 4200)
    assert fmt._fitting_story_block(story) is None
    rendered = render_guild_digest(
        RUN_DATE,
        [IND],
        {"industry": 5},
        stories_by_game={"industry": [story]},
        items_by_game={},
        notes_by_game={},
        coverage_notes=[],
        capped={"industry": 5},
    )
    assert not rendered.messages


def test_a_linkless_block_after_shedding_has_no_trailing_separator_or_newline():
    urls = [f"https://site{j}.example.com/{'p' * 2000}" for j in range(3)]
    block = fmt._fitting_story_block(draft(urls, summary="s" * 400))
    assert block is not None
    assert not block.endswith(("\n", " ", "·"))


# --- headline caps with the new form ---


def test_the_headline_url_cap_is_exact_at_1000_and_1001_with_a_label_in_front():
    prefix = "https://pcgamer.com/"
    kept = fmt._headline_line(item(url=prefix + "a" * (1000 - len(prefix))))
    dropped = fmt._headline_line(item(url=prefix + "a" * (1001 - len(prefix))))
    assert kept is not None and dropped is None
    assert kept.endswith(">)")


def test_the_cap_counts_the_url_as_shown_not_as_given():
    # A long utm string is stripped, so this is a short URL and must be kept.
    url = "https://pcgamer.com/a?utm_source=" + "x" * 2000
    line = fmt._headline_line(item(url=url))
    assert line is not None and line.endswith("[pcgamer.com](<https://pcgamer.com/a>)")


def test_the_worst_headline_line_stays_under_the_documented_1700_for_every_title_shape():
    host = "a" * 28 + ".example.com"
    head = f"https://{host}/"
    url = head + "a" * (1000 - len(head))
    shapes = ["https://" * 37, "@" * 300, "*" * 300, "<#1>" * 75, "\\" * 300, "😀" * 150,
              "[](" * 100, "_https://" * 33, "](<" * 100]  # fmt: skip
    for title in shapes:
        line = fmt._headline_line(item(title, url=url, trust="official"))
        assert line is not None
        assert discord_len(line) < 1700, (title[:12], discord_len(line))


def test_the_industry_picks_embed_sheds_whole_lines_at_every_size_and_keeps_every_link_closed():
    for pad in range(0, 300, 5):
        picks = [
            item("w" * (250 + pad % 50), url=f"https://s{i}.example.com/{'p' * 700}")
            for i in range(5)
        ]
        picks[-1] = item("w" * pad, url="https://last.example.com/x")
        text = str(render_picked_headlines_embed(IND, picks).description)
        assert discord_len(text) <= LIMIT
        assert "more, use /news" not in text
        for ln in text.split("\n"):
            assert re.fullmatch(r"• .* — \[[a-z0-9.\-]+\]\(<[^>\s]+>\)", ln), ln


# --- invite links stay dropped, everywhere ---

_INVITES = [
    "https://discord.gg/abcdef",
    "https://DISCORD.GG/abcdef",
    "https://www.discord.gg/abcdef",
    "https://discord.com/invite/abcdef",
    "https://discordapp.com/invite/abcdef",
    "https://canary.discord.com/invite/abcdef",
    "https://discord.gift/abcdef",
    "https://discord.gg./abcdef",
    "https://discord.gg:443/abcdef",
]


@pytest.mark.parametrize("url", _INVITES)
def test_an_invite_is_dropped_from_every_masked_surface(url):
    assert _link_line([url]) == ""
    assert fmt._headline_line(item(url=url)) is None
    block = fmt._story_block(draft([url, "https://pcgamer.com/a"]))
    assert "discord" not in block.lower() and block.endswith(
        "[pcgamer.com](<https://pcgamer.com/a>)"
    )
    page = render_story_page(
        [story_view([url, "https://pcgamer.com/a"])], {"palworld": PAL}, "t", 1, 1
    )
    assert "discord" not in str(page.description).lower()
    items_page = render_item_page(
        [item_view(url), item_view("https://pcgamer.com/x")], {}, "t", 1, 1
    )
    assert "discord" not in str(items_page.description).lower()


def test_a_story_with_only_invites_ends_without_a_link_line_at_all():
    block = fmt._fitting_story_block(draft(["https://discord.gg/a"], summary="s" * 4000))
    assert block is not None and discord_len(block) <= LIMIT
    assert "](<" not in block


# --- the surfaces that must not change ---

_CHANGED = {"_link_line", "_headline_line", "_truncate_description", "_fitting_story_block"}
_NEW = {
    "_LABEL_RE",
    "_MAX_LABEL",
    "_FALLBACK_LABEL",
    "_link_label",
    "_masked_link",
    "_masked_shown_link",
    "_MASKED_LINK_RE",
    "_DANGLING_SEPARATOR_RE",
    "import:import httpx",
    "import:from newsbot.netguard import host_name",
    "import:from urllib.parse import urlsplit",
}


def _git_show(relpath: str) -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "show", f"{BASE_COMMIT}:{relpath}"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        pytest.skip(f"git or commit {BASE_COMMIT} is not available here")


def _without_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _top_level(source: str) -> dict[str, str]:
    tree = _without_docstrings(ast.parse(source))
    out = {}
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = ",".join(ast.unparse(t) for t in targets)
            out[names] = ast.dump(node)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            out[f"import:{ast.unparse(node)}"] = ast.dump(node)
        else:
            out[f"stmt{index}"] = ast.dump(node)
    return out


def test_every_top_level_definition_in_format_py_is_identical_except_the_four_that_changed():
    old = _top_level(_git_show("newsbot/bot/format.py"))
    new = _top_level((REPO / "newsbot" / "bot" / "format.py").read_text())
    changed = {name for name in old.keys() & new.keys() if old[name] != new[name]}
    assert changed == _CHANGED
    assert set(old) - set(new) == set()
    assert set(new) - set(old) == _NEW


@pytest.mark.parametrize(
    "relpath",
    [
        "newsbot/lounge/quotes.py",
        "newsbot/discord_links.py",
        "newsbot/pipeline/normalize.py",
        "newsbot/pipeline/summarize.py",
        "newsbot/pipeline/prompts.py",
        "newsbot/pipeline/industry.py",
        "newsbot/bot/terminal.py",
    ],
)
def test_the_modules_the_brief_says_to_leave_alone_are_untouched(relpath):
    assert _top_level((REPO / relpath).read_text()) == _top_level(_git_show(relpath))


@pytest.fixture(scope="module")
def old_format():
    source = _git_show("newsbot/bot/format.py")
    name = "_newsbot_format_masked_adversarial_base"
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


_ALERT_URLS = [
    "https://www.Example.com/post?id=1",
    "https://bücher.de/post",
    "https://example.com:8443/post",
    "https://[2001:db8::1]/post",
    "https://a_b.example.com/post",
    "https://discord.gg/abcdef",
    "https://exa mple.com/post",
    "https://a>b.com/post",
    "https://" + "a" * 60 + ".example.com/post",
    "https://example.com/" + "p" * 300,
]


def _cand(code, url, **kwargs):
    return CodeCandidate(
        code=code, golden=False, source_name="Gearbox", item_url=url, fresh=True, **kwargs
    )


@pytest.mark.parametrize("url", _ALERT_URLS)
def test_shift_alerts_keep_their_bare_links_for_every_kind_of_url(old_format, url):
    code_a, code_b = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
    cands = [_cand(code_a, url), _cand(code_b, url)]
    for ping in (True, False):
        new = [m.content for m in render_code_alerts(cands, ping=ping)]
        old = [m.content for m in old_format.render_code_alerts(cands, ping=ping)]
        assert new == old
        assert "](<" not in "".join(new)
    roundup = [_cand(code_a, url, roundup=True), _cand(code_b, url, roundup=True)]
    assert [m.content for m in render_roundup_alerts(roundup)] == [
        m.content for m in old_format.render_roundup_alerts(roundup)
    ]
    assert [
        m.content for m in render_followup_alerts([code_a, code_b], ping_mention="@everyone")
    ] == [
        m.content
        for m in old_format.render_followup_alerts([code_a, code_b], ping_mention="@everyone")
    ]


def test_with_no_links_a_story_block_and_a_headline_digest_differ_from_the_old_ones_only_in_links(
    old_format,
):
    story = draft([], headline="H", summary="S")
    assert fmt._story_block(story) == old_format._story_block(story)
    assert _link_line([]) == old_format._link_line([]) == ""
    assert (
        _link_line(["https://discord.gg/x"])
        == old_format._link_line(["https://discord.gg/x"])
        == ""
    )
    assert _link_line(["https://a.com/1"] * 0 + ["javascript:x"]) == ""


def test_the_more_count_of_old_and_new_link_lines_agree_for_any_mix(old_format):
    rng = random.Random(32)  # noqa: S311 (a reproducible sequence, not a secret)
    pool = [
        "https://pcgamer.com/a", "https://ign.com/b", "https://discord.gg/x", "javascript:x",
        "https://[::1]/z", "https://www.pcgamer.com/c", "not a url", "https://bücher.de/q",
    ]  # fmt: skip
    for _ in range(500):
        urls = [rng.choice(pool) for _ in range(rng.randint(0, 9))]
        new = _link_line(urls)
        old = old_format._link_line(urls)
        assert new.count(" · ") == old.count(" · ")
        assert re.findall(r"\+\d+ more", new) == re.findall(r"\+\d+ more", old)
        assert re.findall(r"<(https?://[^>\s]+)>", new) == re.findall(r"<(https?://[^>\s]+)>", old)


# =====================================================================================
# Round 2: the fix itself (label via netguard.host_name, _masked_shown_link, the loose
# cut pattern, and the always-strip separator rule)
# =====================================================================================

import time  # noqa: E402

import httpx  # noqa: E402

from newsbot.netguard import host_name  # noqa: E402


def connection_host(shown_url: str) -> str:
    """The host the connection would use for `shown_url`, in the shape the label has."""
    return host_name(httpx.URL(shown_url)).lower().removeprefix("www.").rstrip(".")


# --- the label is the host the connection uses ---


@pytest.mark.parametrize(
    ("host", "label"),
    [
        ("faß.de", "xn--fa-hia.de"),
        ("ß.de", "xn--zca.de"),
        ("ς.com", "xn--3xa.com"),
        ("ΑΣ.gr", "xn--mxa0b.gr"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("BÜCHER.DE", "xn--bcher-kva.de"),
        ("日本語.jp", "xn--wgv71a119e.jp"),
        ("www.日本語.jp", "xn--wgv71a119e.jp"),
        ("pаypal.com", "xn--pypal-4ve.com"),
        ("a--b.com", "a--b.com"),
        ("0-0.com", "0-0.com"),
        ("1.2.3.4", "1.2.3.4"),
        ("a.b", "a.b"),
        ("x", "x"),
        ("9", "9"),
    ],
)
def test_the_label_is_the_idna_2008_host(host, label):
    assert shown_label(f"https://{host}/x") == label


@pytest.mark.parametrize(
    "host",
    [
        "a‍b.com",
        "a‌b.com",
        "a​b.com",
        "a­b.com",
        "ａｐｐｌｅ.com",  # fullwidth: IDNA 2008 without mapping refuses it
        "exa%6dple.com",
        "%65xample.com",
        "xn--.com",
        "xn--a-.com",
        "-a.com",
        "a-.com",
        "a.-b.com",
        "a-.b.com",
        "…",
        "a_b.com",
    ],
)
def test_hosts_the_connection_would_refuse_or_that_start_or_end_badly_are_labelled_link(host):
    assert shown_label(f"https://{host}/x") == "link"


def test_the_label_agrees_with_the_idna_library_on_a_list_of_names():
    idna = pytest.importorskip("idna")
    for host in [
        "bücher.de",
        "日本語.jp",
        "faß.de",
        "ς.com",
        "ΑΣ.gr",
        "пример.испытание",
        "例え.テスト",
    ]:
        want = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        label = shown_label(f"https://{host}/x")
        assert label in {want, "link"}, (host, label, want)
        if len(want) <= 40:
            assert label == want, host


def test_the_label_is_never_a_host_the_connection_would_not_use():
    rng = random.Random(33)  # noqa: S311 (a reproducible sequence, not a secret)
    pieces = ["www.", "a", "B", "-", "_", ".", "ß", "ς", "日", "é", "%41", "xn--", "9", "‍", "ａ"]
    checked = 0
    for _ in range(4000):
        url = "https://" + "".join(rng.choice(pieces) for _ in range(rng.randint(1, 9))) + "/p"
        shown = fmt._masked_shown_link(url)
        if shown is None:
            continue
        label = _link_label(shown)
        if label == "link":
            continue
        checked += 1
        assert label == connection_host(shown), (url, shown, label)
        assert label.isascii()
    assert checked > 200  # the fuzz really produced labels


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1",
        "http://a b/",
        "http://\ud800.com/",
        "http://" + "a" * 300 + ".com/",
        "http://" + ".".join(["a" * 63] * 5) + "/",
        "",
        "//",
        "http://\x00/",
        "http://a:99999/",
        "http://a:b/",
        "not a url",
        "https://[2001:db8::1]:80/x",
        "https://" + "日" * 80 + ".jp/",
    ],
)
def test_whatever_httpx_raises_on_becomes_the_plain_word(url):
    assert _link_label(url) in {"link"} or re.fullmatch(r"[a-z0-9.\-]+", _link_label(url))


def test_the_label_function_only_swallows_what_it_means_to():
    # Not a str at all is a caller bug and should still be loud.
    with pytest.raises((TypeError, AttributeError)):
        _link_label(None)  # type: ignore[arg-type]


# --- _masked_shown_link, and the three surfaces agreeing ---


def test_masked_shown_link_is_shown_link_minus_odd_hosts_with_parentheses_encoded():
    rng = random.Random(34)  # noqa: S311 (a reproducible sequence, not a secret)
    pieces = ["www.", "a", "-", "_", ".", " ", ">", "<", ")", "(", "\x1f", "\x85", "ß", "日", "%41"]
    path_pieces = ["p", "(", ")", "%28", "%29", "/", "_(x)", ")(", "q"]
    seen_none = seen_same = seen_encoded = 0
    for _ in range(4000):
        host = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
        path = "".join(rng.choice(path_pieces) for _ in range(rng.randint(0, 6)))
        url = f"https://{host}/{path}"
        shown = fmt._shown_link(url)
        masked_shown = fmt._masked_shown_link(url)
        if shown is None:
            assert masked_shown is None
            continue
        netloc = shown.split("/", 3)[2]
        if re.search(r"[\s>]", shown) or re.search(r"[()]", netloc):
            assert masked_shown is None, url
            seen_none += 1
            continue
        front = "https://" + netloc
        assert masked_shown == front + shown[len(front) :].replace("(", "%28").replace(")", "%29")
        assert "(" not in masked_shown and ")" not in masked_shown
        seen_same += 1
        seen_encoded += masked_shown != shown
    assert seen_none > 50 and seen_same > 50 and seen_encoded > 20


def test_link_line_headline_line_and_fitting_block_agree_on_every_url():
    pool = [
        "https://pcgamer.com/a",
        "https://exa mple.com/x",
        "https://a>b.com/x",
        "https://discord.gg/abc",
        "https://[2001:db8::1]/x",
        "https://b\u00fccher.de/y",
        "javascript:x",
        "https://a\u00a0b.com/x",
        "https://ign.com/z",
    ]
    # Room for exactly three links of this size, so a fourth "usable" one would show.
    four = [f"https://site{i}.example.com/{'p' * 315}" for i in range(4)]
    plain = fmt._fitting_story_block(draft(four, summary="s" * 3000))
    assert plain is not None and plain.count("](<") < 4
    for url in pool:
        drawn = _link_line([url]) != ""
        assert drawn == (fmt._headline_line(item(url=url)) is not None), url
        assert drawn == (fmt._masked_shown_link(url) is not None), url
        block = fmt._fitting_story_block(draft([url, *four], summary="s" * 3000))
        assert block is not None
        # An undrawn URL changes nothing. A drawn one takes the first slot, and shedding then
        # takes links from the end, so the block never has fewer links than the plain one.
        if not drawn:
            assert block == plain, url
        else:
            assert plain.count("](<") <= block.count("](<") <= 3, url


def test_an_undrawn_url_never_hides_a_drawn_one_in_fitting_story_block():
    four = [f"https://site{i}.example.com/{'p' * 315}" for i in range(4)]
    plain = fmt._fitting_story_block(draft(four, summary="s" * 3000))
    for odd in (
        "https://exa mple.com/x",
        "https://a>b.com/x",
        "https://discord.gg/abc",
        "javascript:x",
    ):
        assert fmt._fitting_story_block(draft([odd, *four], summary="s" * 3000)) == plain, odd


# --- the loose cut pattern ---


def _spans_of_real_links(text: str, labels: list[str]) -> list[tuple[int, int]]:
    spans = []
    for label in labels:
        start = 0
        while (i := text.find(f"[{label}](<", start)) != -1:
            end = text.index(">)", i) + 2
            spans.append((i, end))
            start = end
    return sorted(spans)


def check_cut(text, spans, limit):
    """Cut `text` at `limit` and check what must hold for any title: whole links, prefix only."""
    cut = _truncate_description(text, limit)
    assert discord_len(cut) <= limit, limit
    assert cut.endswith("…"), limit
    body = cut[:-1]
    assert text.startswith(body), (limit, cut)
    assert not any(start < len(body) < end for start, end in spans), (limit, cut)
    return body


def test_a_stray_bracket_in_a_title_swallows_at_most_the_title_text_before_the_link():
    title = "y" * 100 + " [stray z"
    line = fmt._headline_line(item(title))
    assert line is not None
    text = line + "\n\n" + fmt._headline_line(item("second", url="https://ign.com/x"))
    spans = _spans_of_real_links(text, ["pcgamer.com", "ign.com"])
    stray = text.index("[stray")
    first_link = spans[0][0]
    assert first_link - stray < 61
    lost_most = 0
    for limit in range(1, discord_len(text)):
        body = check_cut(text, spans, limit)
        if stray < limit <= spans[0][1]:
            # Anywhere from the stray "[" through the real link, the cut backs up to the "[".
            assert len(body) <= max(stray, limit - 1), limit
            lost_most = max(lost_most, limit - 1 - len(body))
    # What it costs: the title text from the stray "[" on, plus the " — " (about 60 characters).
    assert lost_most <= (spans[0][1] - stray) + 1


def test_a_stray_bracket_more_than_sixty_characters_away_swallows_nothing():
    title = "[stray " + "y" * 120
    line = fmt._headline_line(item(title))
    text = line + "\n\n" + fmt._headline_line(item("second", url="https://ign.com/x"))
    spans = _spans_of_real_links(text, ["pcgamer.com", "ign.com"])
    inside = spans[0][0] + 5
    cut = _truncate_description(text, inside + 1)
    # Backs up to just before the real link, not to the stray "[" far behind it.
    assert cut == text[: spans[0][0]].rstrip(" —") + "…"
    assert "[stray" in cut


@pytest.mark.parametrize(
    "title",
    [
        "abc [stray words",
        "[start of title and then more words",
        "x" * 200 + " [stray",
        "a [b] c [d",
        "[[[[[[ many",
        "tail bracket [",
        "[a\\] b [c",
    ],
)
def test_stray_brackets_never_make_a_cut_leave_half_a_link_or_cut_a_link_wrongly(title):
    one = fmt._headline_line(item(title))
    two = fmt._headline_line(item(title + " again", url="https://ign.com/x", trust="official"))
    text = f"**Palworld**\n{one}\n\n**Other**\n{two}"
    spans = _spans_of_real_links(text, ["pcgamer.com", "ign.com"])
    assert len(spans) == 2
    for limit in range(1, discord_len(text)):
        body = check_cut(text, spans, limit)
        # Every real link that is in the output is whole, and none is split from its label.
        for start, end in spans:
            if end <= len(body):
                assert body[start:end] == text[start:end]


def test_stray_bracket_fuzz_never_leaves_half_a_link():
    rng = random.Random(36)  # noqa: S311 (a reproducible sequence, not a secret)
    alphabet = ["[", "]", "(", ")", "<", ">", "\\", " ", "x", "y", "](", "](<", "·", "—", "**", "_"]
    for _ in range(300):
        titles = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 50))) for _ in range(3)
        ]
        lines = [
            fmt._headline_line(item(t, url=f"https://s{i}.example.com/p"))
            for i, t in enumerate(titles)
        ]
        text = "\n".join(lines)
        spans = _spans_of_real_links(text, [f"s{i}.example.com" for i in range(3)])
        assert len(spans) == 3
        for limit in rng.sample(range(1, discord_len(text)), 25):
            check_cut(text, spans, limit)


def test_an_escaped_look_alike_in_a_title_is_still_not_a_link_to_the_cut():
    line = fmt._headline_line(item("\\[x\\](<https://e.example/>) tail"))
    assert line is not None
    limit = line.index("e.example") + 3
    cut = _truncate_description(line, limit)
    # A cut inside the look-alike keeps everything up to the cut, because it is just text.
    assert cut == line[: limit - 1] + "\u2026"
    assert "[pcgamer.com]" not in cut


def test_a_backslash_run_before_a_stray_bracket_hides_it_from_the_cut_and_nothing_else():
    for slashes in ("\\", "\\\\", "\\\\\\"):
        line = fmt._headline_line(item(f"word{slashes}[stray"))
        spans = _spans_of_real_links(line, ["pcgamer.com"])
        for limit in range(1, discord_len(line)):
            check_cut(line, spans, limit)


def test_the_cut_pattern_is_not_slow_on_hostile_text():
    cases = [
        "[" * 3000 + "a" * 1000,
        "[a](<" * 800,
        "x" + " " * 3500 + "y" * 1000,
        "x " + "· " * 1800 + "y" * 1000,
        "[" + "a" * 59 + "](<" + "b" * 3500,
        ("[" + "a" * 59 + "](<" + "b" * 40 + "\n") * 80,
    ]
    for text in cases:
        started = time.perf_counter()
        cut = _truncate_description(text, 3000)
        assert time.perf_counter() - started < 2.0, text[:20]
        assert discord_len(cut) <= 3000


# --- the always-strip separator rule ---


def test_no_cut_of_a_link_line_ever_ends_on_a_separator():
    text = (
        "T — [a.com](<https://a.com/x>) · [b.com](<https://b.com/yyyy>) · "
        "[link](<https://c/z>) · +2 more"
    )
    for limit in range(1, discord_len(text)):
        cut = _truncate_description(text, limit)
        assert not re.search(r"[·—]\s*…$", cut), (limit, cut)
        assert text.startswith(cut[:-1]), limit
        assert open_link(cut) is False, limit


def test_no_cut_of_a_headline_list_ends_on_a_dash_or_leaves_a_headline_without_its_link():
    text = "\n\n".join(
        fmt._headline_line(item(f"Headline {i}", url=f"https://s{i}.example.com/p"))
        for i in range(5)
    )
    for limit in range(1, discord_len(text)):
        cut = _truncate_description(text, limit)
        assert not re.search(r"[·—]\s*…$", cut), (limit, cut)
        assert open_link(cut) is False, limit


def test_stripping_a_separator_never_eats_a_closing_parenthesis_or_a_word():
    text = "Head\n[a.com](<https://a.com/x>) · [b.com](<https://b.com/yyyy>)"
    first_end = text.index(">)") + 2
    for limit in range(first_end + 2, discord_len(text)):
        cut = _truncate_description(text, limit)
        if len(cut[:-1]) >= first_end:
            assert cut.startswith("Head\n[a.com](<https://a.com/x>)"), limit


@pytest.mark.parametrize(
    ("text", "limit", "want"),
    [
        ("aaaa bbbb · cccc", 11, "aaaa bbbb…"),
        ("aaaa bbbb — cccc", 11, "aaaa bbbb…"),
        ("aaaa bbbb · — · cccc", 16, "aaaa bbbb…"),
        ("aaaa bbbb\n· cccc", 11, "aaaa bbbb…"),
        ("• 🟢 OFFICIAL · Title here", 15, "• 🟢 OFFICIAL…"),
    ],
)
def test_a_trailing_separator_goes_with_the_cut(text, limit, want):
    assert _truncate_description(text, limit) == want


@pytest.mark.parametrize(
    ("text", "limit", "want"),
    [
        (
            "well-known fact·ory",
            17,
            "well-known fact·…",
        ),  # no space before it: a word, not a separator
        ("a—b dash here too", 4, "a—b…"),
        ("x ·y z", 5, "x ·y…"),  # the cut is after the "y", so the dot is not trailing
    ],
)
def test_text_that_only_resembles_a_separator_is_kept(text, limit, want):
    assert _truncate_description(text, limit) == want


def test_text_that_fits_is_returned_even_if_it_ends_on_a_separator():
    assert _truncate_description("Done ·", 100) == "Done ·"
    assert _truncate_description("Done —", discord_len("Done —")) == "Done —"


def test_everything_stripped_leaves_only_the_ellipsis():
    assert _truncate_description(" · · · tail", 8) == "…"


# =====================================================================================
# Round 3: parentheses (qa fix batch 1): a parser-shaped check and the encoding
# =====================================================================================

# The link rule of simple-markdown (the library Discord's markdown comes from), ported to
# Python: a label, then `(`, then an optional `<`, then a target that ends at the first `)`
# that is not inside a `(...)` pair, whether the angle brackets are there or not.
_SM_LINK = re.compile(
    r"\[((?:\[[^\]]*\]|[^\[\]]|\](?=[^\[]*\]))*)\]"
    r"\(\s*<?((?:\([^)]*\)|[^\s\\]|\\.)*?)>?(?:\s+['\"]([\s\S]*?)['\"])?\s*\)"
)
# The autolink rule: a bare `<scheme:/...>`.
_SM_AUTOLINK = re.compile(r"<[^: >]+:/[^ >]+>")


def sm_parse(text: str) -> tuple[list[tuple[str, str]], str]:
    """Links as simple-markdown would read them: ([(label, target)], everything left over).

    Backslash pairs are inert and dropped from the leftover; autolinks count as links.
    """
    links: list[tuple[str, str]] = []
    left: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            i += 2
            continue
        if char == "[" and (m := _SM_LINK.match(text, i)):
            links.append((m.group(1), m.group(2)))
            i = m.end()
            continue
        if char == "<" and (m := _SM_AUTOLINK.match(text, i)):
            links.append(("", m.group(0)[1:-1]))
            i = m.end()
            continue
        left.append(char)
        i += 1
    return links, "".join(left)


_LIVE_LEFTOVER_RE = re.compile(r"https?://|[*_~|`\[\]<>]")


def test_the_ported_parser_sees_the_bug_the_encoding_fixes():
    raw = "[x](<https://blog.example/codes)https://discord.gg/abcdef>)"
    links, left = sm_parse(raw)
    assert links == [("x", "https://blog.example/codes")]
    assert "https://discord.gg/abcdef" in left  # the live invite the fix exists to prevent
    wiki = sm_parse("[x](<https://en.wikipedia.org/wiki/Foo_(bar)>)")
    assert wiki == ([("x", "https://en.wikipedia.org/wiki/Foo_(bar)")], "")
    assert sm_parse("[x](<https://a.com/x(y>)")[0] == [("x", "https://a.com/x(y")]


_PAREN_URLS = [
    "https://blog.example/codes)https://discord.gg/abcdef",
    "https://a.com/x)**bold**",
    "https://a.com/x)__under__ and ~~strike~~ and ||spoiler|| and `code`",
    "https://a.com/x(unbalanced",
    "https://a.com/x((y",
    "https://a.com/x)(y",
    "https://a.com/x)(<https://evil.example>)",
    "https://a.com/x)[evil](https://evil.example)",
    "https://en.wikipedia.org/wiki/Foo_(bar)",
    "https://en.wikipedia.org/wiki/Foo_(bar)_(baz)",
    "https://en.wikipedia.org/wiki/Foo_((x))",
    "https://en.wikipedia.org/wiki/Foo_(bar))",
    "https://a.com/p?q=)&r=(",
    "https://a.com/p?q=a)b&c=(d",
    "https://a.com/x%29y%28z",
    "https://a.com/x%2529y%2528z",
    "https://a.com/x#frag)https://discord.gg/abc",
    "https://a.com/path with space)tail",
    "https://a.com/x\\)y",
    "https://a.com/x)\\",
    "https://bücher.de/x)(y",
    "https://[2001:db8::1]/x)y",
    "https://a）b.com/x",  # fullwidth parenthesis in the host: not an ASCII one
    "https://a＞b.com/x",  # fullwidth ">"
    "https://a.com/)(",
]
_DROPPED_PAREN_URLS = [
    "https://a)b.com/x",
    "https://a(b.com/x",
    "https://a.com)/x",
    "https://a.com(x)/p",
    "https://a)https://discord.gg/abc.com/x",
]


def parse_one_line(line: str, expected: list[str]) -> None:
    links, left = sm_parse(line)
    assert [target for _, target in links] == expected, line
    assert not _LIVE_LEFTOVER_RE.search(left), (line, left)


def drawn_targets(urls: list[str]) -> list[str]:
    """What the masked surfaces should link to: each URL as `_masked_shown_link` writes it."""
    shown = [u for url in urls if (u := fmt._masked_shown_link(url)) is not None]
    return shown[:3]


def check_all_surfaces(urls: list[str]) -> None:
    want = drawn_targets(urls)
    more = max(len([url for url in urls if fmt._masked_shown_link(url)]) - 3, 0)
    # The link line, and the story block that carries it.
    line = _link_line(urls)
    parse_one_line(line.removesuffix(f" · +{more} more") if more else line, want)
    block = fmt._story_block(draft(urls, headline="Plain headline", summary="Plain summary text."))
    parse_one_line(
        block.split("\n", 2)[2].removesuffix(f" · +{more} more")
        if more
        else block.split("\n", 2)[2],
        want,
    )
    top = block.split("\n", 2)[:2]
    assert not _LIVE_LEFTOVER_RE.search("\n".join(top)), top
    # The comped /news page.
    page = render_story_page(
        [story_view(urls, summary="Plain summary.")], {"palworld": PAL}, "t", 1, 1
    )
    last = str(page.description).split("\n")[-1]
    parse_one_line(last.removesuffix(f" · +{more} more") if more else last, want)
    # One headline per URL, and the free /news page.
    for url in urls:
        wanted = drawn_targets([url])
        headline = fmt._headline_line(item("Plain title", url=url))
        encoded_len = discord_len(wanted[0]) if wanted else 0
        if not wanted or encoded_len > 1000:
            assert headline is None, url
            continue
        assert headline is not None
        parse_one_line(headline, wanted)
        items_page = render_item_page(
            [item_view(url, "Plain title")], {"palworld": "Palworld"}, "t", 1, 1
        )
        body = str(items_page.description).split("\n", 1)[1]
        parse_one_line(body, wanted)


@pytest.mark.parametrize("url", _PAREN_URLS)
def test_a_url_with_parentheses_parses_as_one_link_with_nothing_live_left_over(url):
    assert fmt._masked_shown_link(url) is not None, url
    check_all_surfaces([url])
    check_all_surfaces([url, "https://pcgamer.com/a", url])  # numbered duplicates around it


@pytest.mark.parametrize("url", _DROPPED_PAREN_URLS)
def test_a_url_with_a_parenthesis_in_its_host_is_not_drawn_anywhere(url):
    assert fmt._masked_shown_link(url) is None
    assert _link_line([url]) == ""
    assert fmt._headline_line(item(url=url)) is None
    check_all_surfaces([url, "https://pcgamer.com/a"])


def test_parentheses_in_the_path_are_all_encoded_and_the_target_is_the_whole_url():
    shown = fmt._masked_shown_link("https://en.wikipedia.org/wiki/Foo_(bar)_(baz)")
    assert shown == "https://en.wikipedia.org/wiki/Foo_%28bar%29_%28baz%29"
    line = _link_line(["https://en.wikipedia.org/wiki/Foo_(bar)_(baz)"])
    assert line == f"[en.wikipedia.org](<{shown}>)"


def test_three_links_with_parentheses_and_a_plus_more_tail_parse_as_three_links():
    urls = [f"https://s{i}.example.com/p)x(y" for i in range(5)]
    line = _link_line(urls)
    links, left = sm_parse(line)
    assert [t for _, t in links] == drawn_targets(urls)
    assert left == " ·  ·  · +2 more"


def test_fuzzed_urls_parse_as_exactly_the_links_drawn_with_nothing_live_left():
    rng = random.Random(37)  # noqa: S311 (a reproducible sequence, not a secret)
    hosts = ["a.com", "www.b.org", "bücher.de", "[2001:db8::1]", "a\\b.com", "x.com\\", "a%29b.com",
             "a）b.com", "a＞b.com", "a)b.com", "a(b.com", "日本語.jp"]  # fmt: skip
    pieces = ["(", ")", "<", ">", "[", "]", "*", "_", "~", "|", "`", "\\", " ", "'", '"',
              "https://", "discord.gg/abc", "%28", "%2528", "x", "/", "?", "=", "&", "#",
              "**bold**", "))", "((", "(\\)", "​", "‮", "😀", "é"]  # fmt: skip
    drawn = 0
    for _ in range(4000):
        urls = [
            "https://"
            + rng.choice(hosts)
            + "/"
            + "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
            for _ in range(rng.randint(1, 4))
        ]
        want = drawn_targets(urls)
        more = max(len([url for url in urls if fmt._masked_shown_link(url)]) - 3, 0)
        line = _link_line(urls)
        links, left = sm_parse(line.removesuffix(f" · +{more} more") if more else line)
        assert [t for _, t in links] == want, (urls, line)
        assert not _LIVE_LEFTOVER_RE.search(left), (urls, line, left)
        drawn += len(want)
        for url in urls:
            headline = fmt._headline_line(item("Plain title", url=url))
            wanted = drawn_targets([url])
            if headline is None:
                assert not wanted or discord_len(wanted[0]) > 1000
                continue
            parse_one_line(headline, wanted)
    assert drawn > 2000


def test_hostile_titles_next_to_parenthesised_urls_still_leave_only_the_real_links():
    for title in _HOSTILE_TITLES:
        for url in _PAREN_URLS[:12]:
            headline = fmt._headline_line(item(title, url=url))
            assert headline is not None
            links, left = sm_parse(headline)
            assert [t for _, t in links] == drawn_targets([url]), (title, url)
            assert not re.search(r"https?://", left), (title, url, left)


# --- trying to break the encoding ---


def test_an_already_encoded_paren_is_left_alone_and_never_encoded_twice():
    for raw, want in [
        ("https://a.com/x%28y%29", "https://a.com/x%28y%29"),
        ("https://a.com/x%2528y%2529", "https://a.com/x%2528y%2529"),
        ("https://a.com/x%2528", "https://a.com/x%2528"),
        ("https://a.com/x%252528", "https://a.com/x%252528"),
        ("https://a.com/x(y", "https://a.com/x%28y"),
    ]:
        assert fmt._masked_shown_link(raw) == want, raw
    # Encoding is idempotent on its own output: the shown URL goes back through unchanged.
    for raw in ("https://a.com/x(y)", "https://a.com/(((", "https://a.com/x)(y?q=(1)"):
        once = fmt._masked_shown_link(raw)
        assert fmt._masked_shown_link(once) == once


def test_a_paren_and_its_percent_encoding_draw_the_same_link():
    a = _link_line(["https://en.wikipedia.org/wiki/Foo_(bar)"])
    b = _link_line(["https://en.wikipedia.org/wiki/Foo_%28bar%29"])
    c = _link_line(["https://en.wikipedia.org/wiki/Foo_%28bar%29".lower()])
    assert a == b
    assert c.count("](<") == 1


def test_the_percent_form_of_a_host_paren_is_a_link_label_and_not_a_paren():
    # %29 in a host is not an ASCII ")" to the parser, and the label rule sends it to "link".
    line = _link_line(["https://a%29b.com/x"])
    assert line == "[link](<https://a%29b.com/x>)"
    parse_one_line(line, ["https://a%29b.com/x"])


@pytest.mark.parametrize("host", ["a（b.com", "a）b.com", "a＞b.com", "a﹚b.com"])
def test_look_alike_parentheses_in_a_host_are_inert_to_the_parser(host):
    url = f"https://{host}/x"
    line = _link_line([url])
    if line:
        parse_one_line(line, [fmt._masked_shown_link(url)])
        assert _link_label(fmt._masked_shown_link(url)) == "link"


def test_the_headline_cap_counts_the_encoded_url_exactly():
    prefix = "https://pcgamer.com/"
    # 20 + 3 * n + m units once every paren is "%28": land exactly on 1000 and 1001.
    for n in (1, 10, 100, 300):
        m = 1000 - len(prefix) - 3 * n
        kept = fmt._headline_line(item(url=prefix + "(" * n + "a" * m))
        dropped = fmt._headline_line(item(url=prefix + "(" * n + "a" * (m + 1)))
        assert kept is not None, n
        assert dropped is None, n
        assert discord_len(re.search(r"\(<(.*)>\)$", kept).group(1)) == 1000
    # A raw URL well under 1000 that grows past it once encoded is dropped.
    assert fmt._headline_line(item(url=prefix + "(" * 340)) is None
    assert fmt._headline_line(item(url=prefix + "(" * 326)) is not None  # 20 + 978


def test_the_headline_cap_still_counts_plain_urls_the_old_way():
    prefix = "https://pcgamer.com/"
    assert fmt._headline_line(item(url=prefix + "a" * (1000 - len(prefix)))) is not None
    assert fmt._headline_line(item(url=prefix + "a" * (1001 - len(prefix)))) is None


def test_the_item_page_applies_the_same_cap_to_the_encoded_url():
    prefix = "https://pcgamer.com/"
    n = 330
    page = render_item_page(
        [item_view(prefix + "(" * n), item_view(prefix + "(" * 300, "kept")],
        {"palworld": "Palworld"},
        "t",
        1,
        1,
    )
    text = str(page.description)
    assert text.count("](<") == 1 and "kept" in text


def test_the_worst_headline_line_with_an_encoded_url_stays_under_1700():
    host = "a" * 28 + ".example.com"
    head = f"https://{host}/"
    n = 100
    url = head + "(" * n + "a" * (1000 - len(head) - 3 * n)
    for title in ("*" * 300, "\\" * 300, "https://" * 37):
        line = fmt._headline_line(item(title, url=url, trust="official"))
        assert line is not None
        assert discord_len(line) < 1700, (title[:6], discord_len(line))


def test_a_story_block_counts_the_encoded_links_when_it_decides_what_fits():
    # Parentheses cost two more units each once encoded: raw this block fits with three links,
    # encoded it must shed one, and whatever it keeps must be whole and under the limit.
    def url(i):
        return f"https://site{i}.example.com/" + "(" * 200 + "p" * 600

    story = draft([url(i) for i in range(3)], summary="s" * 1400)
    raw_len = sum(discord_len(url(i)) for i in range(3))
    assert raw_len < 2600
    block = fmt._fitting_story_block(story)
    assert block is not None
    assert discord_len(block) <= LIMIT
    assert open_link(block) is False
    assert block.count("](<") == 2  # three encoded links would be over 4096
    assert "(" not in "".join(re.findall(r"\(<([^>]*)>\)", block))


def test_the_comped_digest_and_pages_count_encoded_lengths_too():
    urls = [f"https://site{i}.example.com/" + "(" * 150 + "p" * 100 for i in range(3)]
    stories = [draft(urls, headline=f"H{n}", summary="s" * 380) for n in range(8)]
    text = str(fmt._topic_embed(PAL, stories).description)
    assert discord_len(text) <= LIMIT and open_link(text) is False
    kept = text.count("REPORTED")
    assert text.endswith(f"+{8 - kept} more, use /news") if kept < 8 else True
    views = [story_view(urls, summary="s" * 300, n=n) for n in range(10)]
    page = str(render_story_page(views, {"palworld": PAL}, "t", 1, 1).description)
    assert discord_len(page) <= LIMIT and open_link(page) is False


def test_the_three_surfaces_agree_on_which_paren_urls_are_drawn_and_what_they_point_at():
    pool = _PAREN_URLS + _DROPPED_PAREN_URLS + ["https://pcgamer.com/a", "https://discord.gg/abc"]
    for url in pool:
        want = drawn_targets([url])
        line = _link_line([url])
        headline = fmt._headline_line(item(url=url))
        assert bool(line) == bool(want), url
        assert (headline is not None) == bool(want), url
        if want:
            assert sm_parse(line)[0][0][1] == want[0] == sm_parse(headline)[0][0][1], url
        # The story block sheds a link from the end when it is too long; with one link and a
        # summary that leaves room, the same URL is kept as the same target.
        block = fmt._fitting_story_block(draft([url], summary="s" * 200))
        assert block is not None
        got = [t for _, t in sm_parse(block.split("\n", 2)[2])[0]]
        assert got == want, url


def test_an_undrawn_url_still_never_costs_a_drawn_one_its_place_with_parens_in_play():
    four = [f"https://site{i}.example.com/{'p' * 315}" for i in range(4)]
    plain = fmt._fitting_story_block(draft(four, summary="s" * 3000))
    for odd in ("https://a)b.com/x", "https://a(b.com/x", "https://a.com(x)/p"):
        assert fmt._fitting_story_block(draft([odd, *four], summary="s" * 3000)) == plain, odd


# --- the SHiFT surfaces and the lounge still do not see any of this ---


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://blog.example/codes)https://discord.gg/abcdef",
        "https://a.com/x)**bold**",
        "https://a.com/x(unbalanced",
        "https://a)b.com/post",
        "https://a(b.com/post",
        "https://a.com/x%2528y",
    ],
)
def test_shift_surfaces_keep_parentheses_exactly_as_before(old_format, url):
    code_a, code_b = "AAAA1-AAAAA-AAAAA-AAAAA-AAAAA", "BBBB2-BBBBB-BBBBB-BBBBB-BBBBB"
    cands = [_cand(code_a, url), _cand(code_b, url)]
    for ping in (True, False):
        new = [m.content for m in render_code_alerts(cands, ping=ping)]
        old = [m.content for m in old_format.render_code_alerts(cands, ping=ping)]
        assert new == old
        assert "%28" not in "".join(new) or "%28" in url
    roundup = [_cand(code_a, url, roundup=True), _cand(code_b, url, roundup=True)]
    assert [m.content for m in render_roundup_alerts(roundup)] == [
        m.content for m in old_format.render_roundup_alerts(roundup)
    ]
    assert fmt._shown_link(url) == old_format._shown_link(url)
