"""Adversarial round for `scripts/preview_shift_look.py`, the local preview of the terminal look.

It's a dev tool, so this stays small. The questions: can hostile message text ever come out of
the converter as live HTML, does the page stay deterministic and load nothing, and does
`--screenshots` stay behind its flag, fail politely and never touch the owner's real Chrome
profile. Nothing here starts Chrome (an autouse fixture refuses every subprocess) and nothing
looks at pixels.
"""

from __future__ import annotations

import html
import importlib.util
import os
import random
import re
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

import discord
import pytest


def _load_script():
    path = Path(__file__).parent.parent / "scripts" / "preview_shift_look.py"
    spec = importlib.util.spec_from_file_location("preview_shift_look_adv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load_script()


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"the preview script ran a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)


# --- a parser that says what the browser would see ---

# The only tags and attributes the converter and the page shell are allowed to produce.
CONTENT_TAGS = {"pre", "code", "span", "a", "strong"}
CONTENT_ATTRS = {
    "pre": set(),
    "code": {"class"},
    "span": {"class", "title", "aria-hidden"},
    "a": {"href", "rel"},
    "strong": set(),
}


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags(markup: str):
    parser = _Tags()
    parser.feed(markup)
    parser.close()
    return parser.tags


def assert_only_our_markup(markup: str) -> None:
    for tag, attrs in _tags(markup):
        assert tag in CONTENT_TAGS, f"live <{tag}> in {markup!r}"
        assert set(attrs) <= CONTENT_ATTRS[tag], f"unexpected attributes {attrs} in {markup!r}"
        if tag == "a":
            assert attrs["href"].startswith(("https://", "http://")), markup


HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '"><script>alert(1)</script>',
    "'><svg/onload=alert(1)>",
    '<a href="javascript:alert(1)">click</a>',
    "[click](javascript:alert(1))",
    "<javascript:alert(1)>",
    "<data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==>",
    '<https://evil.example/"onmouseover="alert(1)>',
    '<https://evil.example/"><script>alert(1)</script>>',
    "<https://evil.example/'onfocus='alert(1)>",
    "<HTTPS://EVIL.EXAMPLE/x>",
    "<https://evil.example/\u2028x>",
    "`<script>alert(1)</script>`",
    '`"><img src=x onerror=alert(1)>`',
    "```\n<script>alert(1)</script>\n```",
    '```"><img src=x onerror=alert(1)>```',
    "**<script>alert(1)</script>**",
    "**`<b onclick=x>`**",
    "\\<script>alert(1)</script>",
    "\\`<script>alert(1)</script>\\`",
    "<t:5:R><script>",
    '<t:5:R"><script>',
    "<@&1><script>alert(1)</script>",
    '<@&1"onclick="x>',
    "</pre><script>alert(1)</script>",
    "</code></pre><img src=x onerror=alert(1)>",
    "<!--",
    "-->",
    "<![CDATA[x]]>",
    "&lt;script&gt;",
    "&#60;script&#62;alert(1)",
    "<style>*{display:none}</style>",
    "<iframe src=//evil.example></iframe>",
    "<meta http-equiv=refresh content='0;url=//evil.example'>",
    "<link rel=stylesheet href=//evil.example/x.css>",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_never_becomes_live_markup(script, text):
    assert_only_our_markup(script.markdown_to_html(text))
    # Wrapped in the things the converter does understand, too.
    assert_only_our_markup(script.markdown_to_html(f"**{text}** and `{text}` and {text}"))
    assert_only_our_markup(script.markdown_to_html(f"```\n{text}\n```\n{text}"))


def test_a_url_with_quotes_stays_inside_its_href(script):
    out = script.markdown_to_html('<https://evil.example/"onmouseover="alert(1)>')
    [(tag, attrs)] = _tags(out)
    assert tag == "a"
    assert set(attrs) == {"href", "rel"}
    assert attrs["href"] == 'https://evil.example/"onmouseover="alert(1)'
    assert "&quot;" in out


def test_javascript_and_data_links_are_plain_escaped_text(script):
    for text in ("<javascript:alert(1)>", "<data:text/html,<b>x</b>>", "[x](javascript:alert(1))"):
        out = script.markdown_to_html(text)
        assert "<a" not in out
        assert "href" not in out
        assert "<" not in re.sub(r"<(/?)(strong|span|code|pre)[^>]*>", "", out)


def test_random_hostile_soup_never_makes_live_markup(script):
    # A seeded shuffle of the pieces markup is made of; deterministic, so a failure repeats.
    pieces = [
        "<", ">", '"', "'", "`", "```", "\n", "\\", "**", "&", ";", "=", " ", "/", ":", "@",
        "<t:5:R>", "<@&7>", "<#7>", "@everyone", "<https://a.example/x>", "<script>",
        "onerror=", "javascript:", "</pre>", "<img ", "https://", "\u200b",
    ]  # fmt: skip
    rng = random.Random(20261022)  # noqa: S311 (a seeded shuffle, not a secret)
    for _ in range(600):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 25)))
        assert_only_our_markup(script.markdown_to_html(text))


def test_a_huge_timestamp_does_not_crash_the_converter(script):
    # `\d{1,12}` admits 999999999999, which is year 33658; datetime tops out at 9999.
    out = script.markdown_to_html("<t:999999999999:R>")
    assert "<t:" not in out
    assert_only_our_markup(out)


# --- timestamps ---


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, "in a few seconds"),
        (6 * 3600, "in 6 hours"),
        (-3 * 86400, "3 days ago"),
        (7 * 86400, "in 7 days"),
        (-60, "a minute ago"),
    ],
)
def test_timestamp_pills_read_against_the_fixed_now(script, offset, expected):
    stamp = int(script.NOW.timestamp()) + offset
    out = script.markdown_to_html(f"expires <t:{stamp}:R>")
    assert "<t:" not in out
    assert f">{expected}</span>" in out
    assert 'class="pill time"' in out


def test_a_malformed_timestamp_is_escaped_not_dropped_raw(script):
    for text in ("<t:abc:R>", "<t:5:F>", "<t:5>", "<t:-5:R>"):
        out = script.markdown_to_html(text)
        assert "<t:" not in out
        assert "&lt;t:" in out


# --- fences, chips, mentions ---


def test_a_fence_becomes_a_pre_holding_only_the_code(script):
    out = script.markdown_to_html("before\n```\nAAAAA-BBBBB-CCCCC-DDDDD-EEEEE\n```\nafter")
    assert "```" not in out
    [pre] = re.findall(r"<pre>.*?</pre>", out)
    assert re.sub(r"<[^>]+>", "", pre).removeprefix(script.COPY_GLYPH) == (
        "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
    )


def test_an_unclosed_fence_and_an_empty_fence_do_not_crash(script):
    assert "<pre>" not in script.markdown_to_html("```\nAAAAA")
    assert_only_our_markup(script.markdown_to_html("``````"))
    assert_only_our_markup(script.markdown_to_html("```"))


def test_markup_inside_a_chip_stays_text_and_mentions_inside_it_stay_unpilled(script):
    out = script.markdown_to_html("`@everyone <@&1> **x**`")
    assert out == '<code class="chip">@everyone &lt;@&amp;1&gt; **x**</code>'


def test_mentions_become_pills_and_never_keep_their_ids(script):
    out = script.markdown_to_html("@everyone <@&1411111111111111111> <#1411111111111111112>")
    assert out.count('class="pill mention"') == 3
    assert "1411111111111111111" not in out
    assert "1411111111111111112" not in out


def test_an_escaped_at_sign_is_not_a_mention(script):
    assert "pill" not in script.markdown_to_html("\\@everyone")


# --- the whole page ---


@pytest.fixture(scope="module")
def built():
    script = _load_script()
    cases = script.build_cases(script.NOW)
    return script, cases, script.to_html(cases)


def test_every_case_title_is_on_the_page(built):
    script, cases, page = built
    assert len(cases) == 14
    for title, items in cases:
        assert html.escape(title) in page
        assert items


def test_no_page_text_is_left_unconverted(built):
    _, _, page = built
    assert "<t:" not in page
    assert "```" not in page
    assert "&lt;@&amp;" not in page and "&lt;#" not in page
    assert "<@&" not in page


def test_every_fence_on_the_page_holds_only_a_shift_code(built):
    _, _, page = built
    bodies = re.findall(r"<pre>.*?<code>(.*?)</code></pre>", page, flags=re.DOTALL)
    assert bodies
    for body in bodies:
        assert re.fullmatch(r"[A-Z0-9]{5}(-[A-Z0-9]{5}){4}", body), body


def test_no_user_text_sits_in_a_chip_on_the_page(built):
    _, _, page = built
    chips = re.findall(r'<code class="chip">(.*?)</code>', page)
    assert chips
    for chip in chips:
        for user_text in ("mentalmars", "Gearbox", "example", "PRV", "Community", "Borderlands"):
            assert user_text not in chip


def test_the_page_loads_nothing_from_anywhere(built):
    script, _, page = built
    banned = {"script", "link", "img", "iframe", "frame", "object", "embed", "source", "video"}
    for tag, attrs in _tags(page):
        assert tag not in banned
        assert "src" not in attrs
        assert not any(k.startswith("on") for k in attrs)
        if tag == "a":
            assert set(attrs) == {"href", "rel"}
    for needle in ("@import", "@font-face", "url(", "http-equiv"):
        assert needle not in page
    # The stylesheet itself mentions no address at all, and the font stacks are system fonts.
    assert "http" not in script._CSS
    assert "//" not in script._CSS
    assert "ui-monospace, Menlo, Consolas, monospace" in script._CSS


def test_an_embed_with_hostile_title_description_and_footer_is_escaped(script):
    embed = discord.Embed(
        title="<script>alert(1)</script>",
        description='"><img src=x onerror=alert(1)> `<b>` <https://a.example/">',
        color=0x3FE37F,
    )
    embed.set_footer(text="<script>alert(2)</script>")
    page = script.to_html(
        [('<script>alert("t")</script>', [("<img src=x onerror=1>", embed), "<script>x</script>"])]
    )
    assert "<script" not in page
    assert not [t for t, _ in _tags(page) if t in {"script", "img", "b"}]
    assert "border-left-color: #3fe37f" in page


def test_the_output_is_byte_identical_across_runs_and_time_zones(script, tmp_path, monkeypatch):
    pages = []
    for i, tz in enumerate(("UTC", "Pacific/Auckland", "America/Los_Angeles")):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        assert script.main(["--out", str(tmp_path / str(i))]) == 0
        pages.append((tmp_path / str(i) / "index.html").read_bytes())
    monkeypatch.undo()
    time.tzset()
    assert pages[0] == pages[1] == pages[2]


# --- the command line ---


def test_without_the_flag_chrome_is_never_looked_for_or_run(script, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("take_screenshots was called without --screenshots")

    monkeypatch.setattr(script, "take_screenshots", boom)
    monkeypatch.setattr(script, "CHROME", tmp_path / "nope")
    assert script.main(["--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.html"]


def test_a_missing_chrome_is_a_one_line_exit_not_a_traceback(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CHROME", tmp_path / "no such chrome")
    with pytest.raises(SystemExit) as exc:
        script.main(["--out", str(tmp_path), "--screenshots"])
    assert isinstance(exc.value.code, str)
    assert "no such chrome" in exc.value.code
    assert "--screenshots" in exc.value.code
    assert "Traceback" not in exc.value.code


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, ["chrome"], stderr=b"boom: sandbox\xff"),
        subprocess.CalledProcessError(1, ["chrome"]),
        subprocess.TimeoutExpired(["chrome"], 120),
    ],
)
def test_a_chrome_failure_is_a_message_not_a_traceback(script, tmp_path, monkeypatch, failure):
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setattr(script, "CHROME", fake)

    def fail(argv, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(SystemExit) as exc:
        script.main(["--out", str(tmp_path / "out"), "--screenshots"])
    assert isinstance(exc.value.code, str)
    assert "desktop.png" in exc.value.code
    if getattr(failure, "stderr", None):
        assert "boom: sandbox" in exc.value.code


@pytest.fixture
def recorded_runs(script, tmp_path, monkeypatch):
    """Run `--screenshots` against a fake Chrome; return (out dir, calls, profile-existed flags)."""
    fake = tmp_path / "Fake Chrome"
    fake.write_text("")
    monkeypatch.setattr(script, "CHROME", fake)
    calls: list[tuple[list, dict]] = []
    existed: list[bool] = []

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        [profile] = [a for a in argv if a.startswith("--user-data-dir=")]
        existed.append(os.path.isdir(profile.split("=", 1)[1]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", record)
    out = tmp_path / "my out's dir"
    assert script.main(["--out", str(out), "--screenshots"]) == 0
    return out, calls, existed


def test_chrome_gets_a_scratch_profile_that_is_cleaned_up(recorded_runs):
    out, calls, existed = recorded_runs
    assert len(calls) == 2
    assert existed == [True, True]
    profiles = set()
    for argv, _ in calls:
        [flag] = [a for a in argv if a.startswith("--user-data-dir=")]
        profiles.add(flag.split("=", 1)[1])
    [profile] = profiles
    assert Path(profile).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not Path(profile).exists()
    assert out not in Path(profile).parents


def test_chrome_argv_is_a_list_of_strings_with_a_timeout_and_no_shell(recorded_runs):
    _, calls, _ = recorded_runs
    for argv, kwargs in calls:
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)
        assert kwargs.get("check") is True
        assert kwargs.get("timeout")
        assert not kwargs.get("shell")


def test_chrome_windows_stay_under_the_height_that_hangs_it(recorded_runs):
    _, calls, _ = recorded_runs
    for argv, _ in calls:
        [size] = [a for a in argv if a.startswith("--window-size=")]
        width, height = (int(n) for n in size.split("=", 1)[1].split(","))
        assert width > 0
        assert 0 < height <= 8192


def test_the_phone_shot_goes_through_a_frame_of_exactly_its_width(recorded_runs):
    out, calls, _ = recorded_runs
    (desktop, _), (phone, _) = calls
    assert desktop[-1] == (out / "index.html").resolve().as_uri()
    assert phone[-1] == (out / "phone-frame.html").resolve().as_uri()
    frame = (out / "phone-frame.html").read_text(encoding="utf-8")
    [(tag, attrs)] = [t for t in _tags(frame) if t[0] == "iframe"]
    assert attrs["width"] == "390"
    assert attrs["src"] == "index.html"
    # The file URIs survive a space and an apostrophe in the output directory's name.
    assert " " not in desktop[-1]
    assert "%20" in desktop[-1]


def test_a_nested_out_directory_is_created(script, tmp_path):
    out = tmp_path / "a" / "b" / "c"
    assert script.main(["--out", str(out)]) == 0
    assert (out / "index.html").is_file()
