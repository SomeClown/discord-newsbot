"""Adversarial tests for the `lounge:` config block, beyond test_config_lounge.py.

Angle (lounge plan, task 1, test-engineer brief 2026-09-29): the welcome
template is a tiny language with two words in it, and people will try to
teach it a third. This file throws odd braces, format-string tricks and
astral characters at it, leans on the length math until it squeaks, and feeds
the quote-source list every wrong shape a hand-edited YAML file can produce.

Where the code accepts something a stricter owner might not want, the test
pins what happens today and says so in a comment. Those tests are
documentation with teeth, not endorsements; change the code and the test
tells you what you just changed. Nothing here touches the network or the
real filesystem outside pytest's tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from newsbot.config import ConfigError, QuoteSourceCfg, _source_problem, load_config
from newsbot.lounge.default_sources import DEFAULT_WIKIQUOTE_PAGES
from newsbot.lounge.welcome import (
    WORST_CASE_MENTION,
    WORST_CASE_SERVER,
    render_welcome,
    unknown_placeholders,
    worst_case_length,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent

_BASE = {
    "guild_id": 1,
    "digest": {"time": "09:00", "timezone": "UTC"},
    "topics": [{"key": "palworld", "name": "Palworld", "channel_id": 2}],
    "sources": [
        {
            "type": "steam_news",
            "name": "Palworld Steam",
            "app_id": 1623730,
            "topics": ["palworld"],
            "trust": "official",
        }
    ],
}


def _write(tmp_path: Path, lounge: object, *, name: str = "config.yaml") -> Path:
    doc = dict(_BASE)
    if lounge is not ...:
        doc["lounge"] = lounge
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc, allow_unicode=True))
    return p


def _load(tmp_path: Path, lounge: object):
    return load_config(_write(tmp_path, lounge))


def _errors(tmp_path: Path, lounge: object) -> str:
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, lounge)
    return str(exc_info.value)


def _quote(sources: object, **extra: object) -> dict:
    return {"daily_quote": {"sources": sources, **extra}}


def _welcome(message: str, *, enabled: bool = True) -> dict:
    return {"channel_id": 5, "welcome": {"enabled": enabled, "message": message}}


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# --- render_welcome never evaluates anything ---


def _r(template: str, member: str = "<@1>", server: str = "S") -> str:
    return render_welcome(template, member_mention=member, server_name=server)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{member}", "<@1>"),
        ("{server}", "S"),
        ("{member}{member}{server}", "<@1><@1>S"),
        # Doubled braces are NOT an escape: the inner pair still matches.
        ("{{member}}", "{<@1>}"),
        ("{member}}", "<@1>}"),
        ("{{member}", "{<@1>"),
        ("}{", "}{"),
        ("{", "{"),
        ("}", "}"),
        ("{}", "{}"),
        ("{ member }", "{ member }"),
        ("{Member}", "{Member}"),
        ("{member.__class__}", "{member.__class__}"),
        ("{0}", "{0}"),
        ("{member!r}", "{member!r}"),
        ("{member:>9999}", "{member:>9999}"),
        ("{member[0]}", "{member[0]}"),
        ("｛member｝", "｛member｝"),
        ("{member}\r\n{server}  \n", "<@1>\r\nS  \n"),
        ("", ""),
    ],
)
def test_render_is_a_regex_substitution_not_str_format(template, expected):
    assert _r(template) == expected


def test_render_does_not_resubstitute_inside_values():
    assert _r("{member}", member="{server}", server="{member}") == "{server}"
    assert _r("{server}", member="{server}", server="{member}") == "{member}"


def test_render_leaves_percent_and_backslash_in_values_alone():
    # re.sub with a function never interprets the returned string as a
    # replacement template, so backslashes survive.
    assert _r("{server}", server=r"a\1\g<0>%s") == r"a\1\g<0>%s"


def test_render_does_not_call_format_on_a_hostile_server_name():
    assert _r("hi {server}", server="{0.__class__}") == "hi {0.__class__}"


def test_unknown_placeholders_reports_each_name_once_in_order():
    assert unknown_placeholders("{b} {a} {b} {member} {server} {a}") == ["b", "a"]


def test_unknown_placeholders_sees_the_inside_of_doubled_braces():
    assert unknown_placeholders("{{oops}}") == ["oops"]
    assert unknown_placeholders("{{member}}") == []


@pytest.mark.parametrize("template", ["{", "}", "}{", "{{", "{member", "member}", "｛x｝"])
def test_unbalanced_and_lookalike_braces_are_not_placeholders(template):
    assert unknown_placeholders(template) == []


def test_empty_braces_are_an_unknown_placeholder_with_empty_name():
    assert unknown_placeholders("{}") == [""]


# --- worst-case length math ---


def test_worst_case_constants_are_what_the_comments_claim():
    assert len(WORST_CASE_MENTION) == 23 and _u16(WORST_CASE_MENTION) == 23
    assert len(WORST_CASE_SERVER) == 100 and _u16(WORST_CASE_SERVER) == 200


def test_worst_case_length_of_each_placeholder():
    assert worst_case_length("{member}") == 23
    assert worst_case_length("{server}") == 200
    assert worst_case_length("") == 0


def test_worst_case_length_counts_astral_fixed_text_as_two_units():
    assert worst_case_length("\U0001f600") == 2
    assert worst_case_length("\U0001f600{member}") == 25


def test_worst_case_length_counts_crlf_as_two():
    assert worst_case_length("a\r\nb") == 4


def test_worst_case_length_counts_trailing_whitespace():
    assert worst_case_length("a   \n") == 5


def test_worst_case_bound_dominates_real_20_digit_mention_and_100_char_astral_name():
    template = "x{member}y{server}z{member}"
    real = _r(template, member="<@18446744073709551615>", server="\U0001f9d9" * 100)
    assert _u16(real) <= worst_case_length(template)


def test_worst_case_bound_dominates_a_100_char_bmp_name():
    template = "{server}!"
    real = _r(template, member="<@1>", server="x" * 100)
    assert _u16(real) < worst_case_length(template)


def test_repeated_placeholders_scale_linearly():
    assert worst_case_length("{member}" * 83) == 23 * 83
    assert worst_case_length("{server}" * 10) == 2000


@pytest.mark.parametrize(
    ("template", "ok"),
    [
        ("a" * 1977 + "{member}", True),  # exactly 2000
        ("a" * 1978 + "{member}", False),  # 2001
        ("a" * 1800 + "{server}", True),  # exactly 2000
        ("a" * 1801 + "{server}", False),
        ("\U0001f600" * 988 + "a{member}", True),  # 1976 + 1 + 23
        ("\U0001f600" * 988 + "aa{member}", False),
        ("a" * 2000, True),
        ("a" * 2001, False),
        ("\U0001f600" * 1000, True),
        ("\U0001f600" * 1000 + "a", False),
        ("{server}" * 10, True),
        ("{server}" * 10 + "a", False),
    ],
)
def test_load_config_accepts_exactly_2000_and_rejects_2001(tmp_path, template, ok):
    lounge = _welcome(template)
    if ok:
        assert _load(tmp_path, lounge).lounge.welcome.message == template
    else:
        assert "Discord's limit is 2000" in _errors(tmp_path, lounge)


def test_length_error_reports_the_computed_length(tmp_path):
    assert "is 2001 characters" in _errors(tmp_path, _welcome("a" * 2001))


def test_length_is_checked_even_while_disabled(tmp_path):
    msg = _errors(tmp_path, _welcome("a" * 2001, enabled=False))
    assert "limit is 2000" in msg


def test_unknown_placeholder_is_checked_even_while_disabled(tmp_path):
    msg = _errors(tmp_path, _welcome("hi {memebr}", enabled=False))
    assert "{memebr}" in msg


@pytest.mark.parametrize(
    "bad", ["{member.__class__}", "{0}", "{member!r}", "{member:>9999}", "{}", "{Member}"]
)
def test_format_string_tricks_are_rejected_by_load_config(tmp_path, bad):
    assert "unknown placeholder" in _errors(tmp_path, _welcome(f"hi {bad}"))


def test_doubled_braces_around_a_known_name_pass_validation(tmp_path):
    # Documented behavior: `{{member}}` is not an escape, it renders as
    # `{<@id>}`. Nothing rejects it. An owner who wanted a literal `{member}`
    # can't have one; that seems fine for a welcome message.
    cfg = _load(tmp_path, _welcome("{{member}}"))
    assert cfg.lounge.welcome.message == "{{member}}"


def test_lone_and_lookalike_braces_pass_validation(tmp_path):
    # Documented: only a well-formed `{name}` is treated as a placeholder,
    # so `{`, `}{` and fullwidth braces sit in the message as literal text.
    for msg in ["{", "}{", "｛memebr｝"]:
        assert _load(tmp_path, _welcome(msg)).lounge.welcome.message == msg


def test_several_unknown_placeholders_are_each_reported(tmp_path):
    msg = _errors(tmp_path, _welcome("{a} {b} {a}"))
    assert msg.count("unknown placeholder") == 2


def test_whitespace_only_message_is_an_error_when_enabled(tmp_path):
    assert "message is required" in _errors(tmp_path, _welcome(" \n\t ", enabled=True))


def test_whitespace_only_message_is_fine_while_disabled(tmp_path):
    assert _load(tmp_path, _welcome("  ", enabled=False)).lounge.welcome.enabled is False


def test_message_is_not_stripped_or_altered_by_load(tmp_path):
    msg = "  hi {member}\r\n  \n"
    assert _load(tmp_path, _welcome(msg)).lounge.welcome.message == msg


@pytest.mark.parametrize("value", [None, 5, ["a"], True])
def test_non_string_message_is_a_shape_error(tmp_path, value):
    lounge = {"channel_id": 5, "welcome": {"enabled": True, "message": value}}
    assert "lounge.welcome.message" in _errors(tmp_path, lounge)


# --- quote sources: entry shapes ---


@pytest.mark.parametrize(
    "entry",
    [
        "Oscar Wilde",
        None,
        5,
        ["wikiquote", "Oscar Wilde"],
        {},
        {"wikiquote": "A", "file": "b"},
        {"wikiquote": "A", "wikiquote2": "b"},
    ],
)
def test_entry_that_is_not_a_single_key_mapping_is_rejected(tmp_path, entry):
    assert "must have exactly one of wikiquote, file or url" in _errors(tmp_path, _quote([entry]))


def test_two_key_entry_names_the_keys_it_found(tmp_path):
    msg = _errors(tmp_path, _quote([{"wikiquote": "A", "url": "https://x.example/"}]))
    assert "found:" in msg and "wikiquote" in msg and "url" in msg


@pytest.mark.parametrize(
    "value",
    [5, 1.5, None, True, False, ["Oscar Wilde"], {"a": "b"}, {"wikiquote": "x"}, "", "   ", "\n\t"],
)
@pytest.mark.parametrize("kind", ["wikiquote", "file", "url"])
def test_non_string_or_blank_values_are_rejected(tmp_path, kind, value):
    assert "must be a non-empty string" in _errors(tmp_path, _quote([{kind: value}]))


def test_unknown_kind_is_rejected_with_the_allowed_list(tmp_path):
    msg = _errors(tmp_path, _quote([{"Wikiquote": "Oscar Wilde"}]))
    assert "unknown key 'Wikiquote'" in msg and "wikiquote, file or url" in msg


def test_non_string_key_in_entry_is_rejected(tmp_path):
    msg = _errors(tmp_path, _quote([{5: "x"}]))
    assert "unknown key 5" in msg


def test_every_bad_entry_is_reported_in_one_go(tmp_path):
    msg = _errors(tmp_path, _quote(["a", {"nope": "x"}, {"url": 3}, {"wikiquote": "ok"}]))
    for i in (0, 1, 2):
        assert f"sources[{i}]" in msg
    assert "sources[3]" not in msg


def test_values_are_stripped_at_parse_time(tmp_path):
    cfg = _load(
        tmp_path, _quote([{"wikiquote": "  Oscar Wilde \n"}, {"url": " https://x.example/a "}])
    )
    assert [s.value for s in cfg.lounge.daily_quote.sources] == [
        "Oscar Wilde",
        "https://x.example/a",
    ]


# --- quote sources: omitted, empty, null ---


def test_omitted_sources_means_the_default_list(tmp_path):
    cfg = _load(tmp_path, {"daily_quote": {"enabled": False}})
    got = [(s.kind, s.value) for s in cfg.lounge.daily_quote.sources]
    assert got == [("wikiquote", t) for t in DEFAULT_WIKIQUOTE_PAGES]


def test_absent_lounge_block_also_gets_the_default_list(tmp_path):
    cfg = _load(tmp_path, ...)
    assert len(cfg.lounge.daily_quote.sources) == len(DEFAULT_WIKIQUOTE_PAGES)


def test_empty_sources_list_is_an_error_even_while_disabled(tmp_path):
    assert "sources is empty" in _errors(tmp_path, _quote([]))


def test_null_sources_behaves_like_omitted(tmp_path):
    # Documented: `sources:` with nothing after it is YAML null, and pydantic
    # takes None as "not given", so it silently means the built-in list. An
    # owner who blanked the list to mean "none" gets Oscar Wilde instead.
    cfg = _load(tmp_path, _quote(None))
    assert len(cfg.lounge.daily_quote.sources) == len(DEFAULT_WIKIQUOTE_PAGES)


@pytest.mark.parametrize("value", ["Oscar Wilde", 5, {"wikiquote": "x"}, True])
def test_sources_that_is_not_a_list_is_a_shape_error(tmp_path, value):
    assert "lounge.daily_quote.sources" in _errors(tmp_path, _quote(value))


# --- wikiquote titles ---


@pytest.mark.parametrize("ch", list("#<>[]{}|"))
def test_wikiquote_title_with_mediawiki_illegal_character_is_rejected(tmp_path, ch):
    assert "isn't a page title" in _errors(tmp_path, _quote([{"wikiquote": f"Oscar{ch}Wilde"}]))


def test_title_full_of_illegal_characters_is_one_error(tmp_path):
    msg = _errors(tmp_path, _quote([{"wikiquote": "|#[]{}<>"}]))
    assert msg.count("isn't a page title") == 1


@pytest.mark.parametrize(
    "title", ["http://en.wikiquote.org/wiki/Oscar_Wilde", "HTTPS://x", "https://en.wikiquote.org"]
)
def test_wikiquote_urls_are_rejected(tmp_path, title):
    assert "isn't a page title" in _errors(tmp_path, _quote([{"wikiquote": title}]))


def test_wikiquote_title_that_merely_starts_with_http_is_rejected(tmp_path):
    # Documented false positive: the URL guard is a prefix check, so a page
    # titled "HTTP cookies" (if Wikiquote had one) would be refused.
    assert "isn't a page title" in _errors(tmp_path, _quote([{"wikiquote": "HTTP cookies"}]))


def test_wikiquote_length_limit_is_255_characters(tmp_path):
    assert _load(tmp_path, _quote([{"wikiquote": "a" * 255}]))
    assert "isn't a page title" in _errors(tmp_path, _quote([{"wikiquote": "a" * 256}]))


def test_wikiquote_length_limit_counts_characters_not_bytes(tmp_path):
    # Documented: MediaWiki's limit is 255 bytes of UTF-8, this checks
    # characters. 255 astral characters is 1,020 bytes and sails through;
    # the fetch would just find no such page.
    assert _load(tmp_path, _quote([{"wikiquote": "\U0001f600" * 255}]))


@pytest.mark.parametrize("title", ["Oscar\nWilde", "Oscar\x00Wilde", "Oscar\tWilde", "\x07bell"])
def test_wikiquote_title_with_control_characters_is_accepted_today(tmp_path, title):
    # Documented gap: nothing rejects control characters or interior newlines
    # in a title (MediaWiki would). Leading and trailing ones are stripped;
    # interior ones stay. Worth a look before the fetcher builds a URL.
    cfg = _load(tmp_path, _quote([{"wikiquote": title}]))
    assert cfg.lounge.daily_quote.sources[0].value == title.strip()


def test_wikiquote_title_with_leading_colon_or_slash_is_accepted_today(tmp_path):
    # Documented: namespace prefixes and subpages (`Talk:Oscar Wilde`,
    # `Oscar Wilde/Quotes`) pass; only characters that MediaWiki bans outright
    # are checked.
    for t in ["Talk:Oscar Wilde", ":Oscar Wilde", "Oscar Wilde/Quotes", "../x"]:
        assert _load(tmp_path, _quote([{"wikiquote": t}]))


def test_source_problem_is_none_for_a_good_title_and_names_the_index():
    ok = QuoteSourceCfg(kind="wikiquote", value="Oscar Wilde")
    bad = QuoteSourceCfg(kind="wikiquote", value="a|b")
    assert _source_problem(0, ok) is None
    assert "sources[7].wikiquote" in _source_problem(7, bad)


# --- url sources ---


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/q.txt",
        "HTTPS://example.com/q.txt",
        "HtTpS://example.com/q.txt",
        "https://example.com",
        "https://example.com:8443/x",
        "https://[::1]/x",
        "https://[2001:db8::1]:8443/x",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://user:pass@example.com/x",
        "https://user@example.com/x",
        "https://example.com/a b",
        "https://example.com:99999/x",
    ],
)
def test_url_forms_accepted_today(tmp_path, url):
    # Documented: validation only demands an https scheme and a hostname.
    # Loopback, private ranges, IPv6 literals, embedded credentials and bogus
    # ports all pass; the config file is the owner's own, so this is trust,
    # not a hole, but the fetcher (task 5+) is where SSRF-ish limits belong.
    cfg = _load(tmp_path, _quote([{"url": url}]))
    assert cfg.lounge.daily_quote.sources[0].value == url


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https:///path",
        "https:example.com",
        "https://:443/x",
        "https://[::1",
        "https://@/x",
        "//example.com/x",
        "example.com/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "ws://example.com",
        "https ://example.com",
    ],
)
def test_url_forms_rejected(tmp_path, url):
    assert "https://" in _errors(tmp_path, _quote([{"url": url}]))


@pytest.mark.parametrize("url", ["http://example.com/x", "HTTP://example.com/x"])
def test_plain_http_gets_its_own_message(tmp_path, url):
    assert "uses plain http" in _errors(tmp_path, _quote([{"url": url}]))


def test_url_with_trailing_space_is_stripped_and_accepted(tmp_path):
    cfg = _load(tmp_path, _quote([{"url": "https://example.com/x "}]))
    assert cfg.lounge.daily_quote.sources[0].value == "https://example.com/x"


def test_url_with_uppercase_scheme_is_kept_verbatim(tmp_path):
    # Documented: the value isn't lowercased, so it's stored as written.
    cfg = _load(tmp_path, _quote([{"url": "HTTPS://Example.com/x"}]))
    assert cfg.lounge.daily_quote.sources[0].value == "HTTPS://Example.com/x"


def test_urls_differing_only_by_scheme_case_are_not_detected_as_duplicates(tmp_path):
    # Documented gap: `key` for a url is the stripped string, so
    # `HTTPS://x/` and `https://x/` (and host case, trailing slash) count as
    # two sources and would get two separate quote decks.
    cfg = _load(tmp_path, _quote([{"url": "HTTPS://x.example/a"}, {"url": "https://x.example/a"}]))
    keys = {s.key for s in cfg.lounge.daily_quote.sources}
    assert len(keys) == 2


def test_url_with_control_characters_inside_is_accepted_today(tmp_path):
    # Documented gap: urlsplit strips tabs and newlines silently, so this
    # parses as https://example.com/x and passes; the stored value still
    # contains the raw characters.
    cfg = _load(tmp_path, _quote([{"url": "https://exam\nple.com/x"}]))
    assert "\n" in cfg.lounge.daily_quote.sources[0].value


# --- file sources ---


def test_relative_file_is_anchored_at_the_config_directory_not_cwd(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = _load(tmp_path, _quote([{"file": "quotes.txt"}]))
    assert cfg.lounge.daily_quote.sources[0].value == str(tmp_path / "quotes.txt")


def test_absolute_file_is_left_alone(tmp_path):
    cfg = _load(tmp_path, _quote([{"file": "/var/lib/quotes.txt"}]))
    assert cfg.lounge.daily_quote.sources[0].value == "/var/lib/quotes.txt"


def test_tilde_is_not_expanded(tmp_path):
    # Documented: `~/quotes.txt` becomes `<config dir>/~/quotes.txt`, a
    # directory literally named `~`. Probably not what anyone typing it meant.
    cfg = _load(tmp_path, _quote([{"file": "~/quotes.txt"}]))
    assert cfg.lounge.daily_quote.sources[0].value == str(tmp_path / "~" / "quotes.txt")


def test_dotdot_is_kept_verbatim_not_normalized(tmp_path):
    cfg = _load(tmp_path, _quote([{"file": "../shared/quotes.txt"}]))
    assert cfg.lounge.daily_quote.sources[0].value == str(tmp_path / ".." / "shared" / "quotes.txt")


def test_file_that_does_not_exist_still_loads(tmp_path):
    # Existence is the fetcher's problem at draw time, not startup's.
    assert _load(tmp_path, _quote([{"file": "nope.txt"}]))


def test_file_path_is_not_resolved_through_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    cfg_path = tmp_path / "link" / "config.yaml"
    doc = dict(_BASE)
    doc["lounge"] = _quote([{"file": "q.txt"}])
    cfg_path.write_text(yaml.safe_dump(doc))
    cfg = load_config(cfg_path)
    assert cfg.lounge.daily_quote.sources[0].value == str(tmp_path / "link" / "q.txt")


def test_same_file_spelled_with_dot_slash_or_absolute_is_caught_as_duplicate(tmp_path):
    # Path() collapses `./`, and the absolute spelling matches the anchored
    # one, so all three spellings land on one key.
    msg = _errors(
        tmp_path,
        _quote([{"file": "q.txt"}, {"file": "./q.txt"}, {"file": str(tmp_path / "q.txt")}]),
    )
    assert "sources[1] repeats sources[0]" in msg and "sources[2] repeats sources[0]" in msg


def test_same_file_reached_through_dotdot_is_not_caught_as_duplicate(tmp_path):
    # Documented gap: `..` isn't collapsed, so `a/../q.txt` and `q.txt` are
    # one file but two keys.
    cfg = _load(tmp_path, _quote([{"file": "q.txt"}, {"file": "a/../q.txt"}]))
    assert len({s.key for s in cfg.lounge.daily_quote.sources}) == 2


def test_same_relative_file_repeated_is_a_duplicate(tmp_path):
    msg = _errors(tmp_path, _quote([{"file": "q.txt"}, {"file": "q.txt"}]))
    assert "sources[1] repeats sources[0]" in msg


def test_file_and_url_and_wikiquote_with_same_text_do_not_collide(tmp_path):
    cfg = _load(tmp_path, _quote([{"wikiquote": "x"}, {"file": "x"}, {"url": "https://x.example"}]))
    assert len({s.key for s in cfg.lounge.daily_quote.sources}) == 3


# --- duplicate detection and key stability ---


@pytest.mark.parametrize(
    "variant",
    [
        "oscar_Wilde",
        "oscar Wilde",
        "Oscar  Wilde",
        "  Oscar_Wilde ",
        "Oscar__Wilde",
        "_Oscar_Wilde_",
    ],
)
def test_wikiquote_variants_of_one_page_are_duplicates(tmp_path, variant):
    msg = _errors(tmp_path, _quote([{"wikiquote": "Oscar Wilde"}, {"wikiquote": variant}]))
    assert "sources[1] repeats sources[0]" in msg


def test_only_the_first_letter_is_case_insensitive(tmp_path):
    cfg = _load(tmp_path, _quote([{"wikiquote": "Oscar Wilde"}, {"wikiquote": "Oscar wilde"}]))
    assert len({s.key for s in cfg.lounge.daily_quote.sources}) == 2


def test_three_way_duplicate_reports_both_repeats_against_the_first(tmp_path):
    msg = _errors(tmp_path, _quote([{"wikiquote": "A"}, {"wikiquote": "a"}, {"wikiquote": "A_"}]))
    assert "sources[1] repeats sources[0]" in msg
    assert "sources[2] repeats sources[0]" in msg


def test_wikiquote_key_is_normalized_form():
    assert QuoteSourceCfg(kind="wikiquote", value="oscar_ wilde").key == "wikiquote:Oscar wilde"


def test_key_ignores_surrounding_whitespace_for_url_and_file():
    assert QuoteSourceCfg(kind="url", value=" https://x/ ").key == "url:https://x/"
    assert QuoteSourceCfg(kind="file", value=" /a ").key == "file:/a"


def test_keys_survive_reordering_and_neighbor_edits(tmp_path):
    a = [{"wikiquote": "Oscar Wilde"}, {"file": "q.txt"}, {"url": "https://x.example/a"}]
    before = {s.key for s in _load(tmp_path, _quote(a)).lounge.daily_quote.sources}
    shuffled = [a[2], a[0], a[1], {"wikiquote": "Mark Twain"}]
    after = _load(tmp_path, _quote(shuffled)).lounge.daily_quote.sources
    assert before <= {s.key for s in after}


def test_keys_survive_unrelated_config_edits(tmp_path):
    lounge = _quote([{"wikiquote": "Oscar Wilde"}, {"file": "q.txt"}])
    k1 = [s.key for s in _load(tmp_path, lounge).lounge.daily_quote.sources]
    lounge["daily_quote"]["time"] = "21:30"
    lounge["daily_quote"]["enabled"] = False
    lounge["channel_id"] = 99
    k2 = [s.key for s in _load(tmp_path, lounge).lounge.daily_quote.sources]
    assert k1 == k2


def test_relative_file_key_follows_the_config_file_location(tmp_path):
    # Documented: moving config.yaml changes every relative `file:` key (and
    # so resets that source's no-repeat deck). Absolute paths are immune.
    lounge = _quote([{"file": "q.txt"}])
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ka = load_config(_write(tmp_path / "a", lounge)).lounge.daily_quote.sources[0].key
    kb = load_config(_write(tmp_path / "b", lounge)).lounge.daily_quote.sources[0].key
    assert ka != kb


def test_quote_source_cfg_is_frozen():
    src = QuoteSourceCfg(kind="url", value="https://x/")
    with pytest.raises(ValidationError):
        src.value = "https://y/"


# --- channel_id ---


@pytest.mark.parametrize("bad", [True, False, 0, -1, -(2**63), 1.5, "abc", "", [], {}, "1.5"])
def test_bad_channel_ids_are_rejected(tmp_path, bad):
    assert "lounge.channel_id" in _errors(tmp_path, {"channel_id": bad})


@pytest.mark.parametrize(("raw", "want"), [("123456789", 123456789), (7.0, 7), (1, 1)])
def test_lax_channel_ids_are_coerced(tmp_path, raw, want):
    # Documented: quoted digits and whole floats are accepted, the same as
    # every other channel_id in the config.
    assert _load(tmp_path, {"channel_id": raw}).lounge.channel_id == want


@pytest.mark.parametrize("big", [2**63 - 1, 2**64, 10**30])
def test_huge_channel_ids_are_accepted_today(tmp_path, big):
    # Documented: no upper bound, so a value Discord could never issue loads
    # fine and only fails when the bot tries to fetch the channel.
    assert _load(tmp_path, {"channel_id": big}).lounge.channel_id == big


def test_channel_id_null_is_the_same_as_absent(tmp_path):
    assert _load(tmp_path, {"channel_id": None}).lounge.channel_id is None


@pytest.mark.parametrize("feature", ["welcome", "daily_quote"])
def test_enabled_feature_without_channel_is_an_error(tmp_path, feature):
    lounge = {
        feature: {"enabled": True, "message": "hi"} if feature == "welcome" else {"enabled": True}
    }
    assert "lounge.channel_id is required" in _errors(tmp_path, lounge)


def test_disabled_features_need_no_channel(tmp_path):
    assert _load(tmp_path, {"welcome": {"enabled": False}, "daily_quote": {"enabled": False}})


# --- time ---


@pytest.mark.parametrize("t", ["8:00", "24:00", "12:60", "08:00 ", " 08:00", "0800", "", "٠٨:٠٠"])
def test_bad_times_are_rejected(tmp_path, t):
    assert "lounge.daily_quote.time" in _errors(tmp_path, {"daily_quote": {"time": t}})


@pytest.mark.xfail(
    strict=True,
    reason="_TIME_RE ends in `$`, which matches before a trailing newline, so '08:00\\n' loads",
)
def test_time_with_trailing_newline_is_rejected(tmp_path):
    assert "lounge.daily_quote.time" in _errors(tmp_path, {"daily_quote": {"time": "08:00\n"}})


@pytest.mark.parametrize("t", ["00:00", "23:59", "08:00"])
def test_good_times_are_accepted(tmp_path, t):
    assert _load(tmp_path, {"daily_quote": {"time": t}}).lounge.daily_quote.time == t


def test_unquoted_yaml_time_is_a_shape_error_not_a_crash(tmp_path):
    # `time: 12:30` unquoted is a YAML 1.1 sexagesimal integer (750). Times
    # with a leading zero, like 08:30, happen to survive as strings.
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(_BASE) + "lounge:\n  daily_quote:\n    time: 12:30\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "lounge.daily_quote.time" in str(exc_info.value)


# --- unknown keys, everywhere ---


@pytest.mark.parametrize(
    "lounge",
    [
        {"chanel_id": 5},
        {"welcome": {"enabld": True}},
        {"welcome": {"extra": 1}},
        {"daily_quote": {"sourcez": []}},
        {"daily_quote": {"timezone": "UTC"}},
        {"quote": {}},
    ],
)
def test_unknown_keys_are_rejected_at_every_level(tmp_path, lounge):
    assert "Extra inputs are not permitted" in _errors(tmp_path, lounge)


@pytest.mark.parametrize("value", [None, [], "on", 5, True])
def test_lounge_block_of_the_wrong_type_is_a_shape_error(tmp_path, value):
    assert "lounge" in _errors(tmp_path, value)


def test_empty_lounge_mapping_is_the_same_as_absent(tmp_path):
    cfg = _load(tmp_path, {})
    assert cfg.lounge.channel_id is None and not cfg.lounge.welcome.enabled


@pytest.mark.parametrize("name", ["config.example.yaml", "config.minimal.yaml"])
def test_shipped_configs_load_with_default_lounge(name):
    cfg = load_config(ROOT / name)
    assert cfg.lounge.channel_id is None
    assert len(cfg.lounge.daily_quote.sources) == len(DEFAULT_WIKIQUOTE_PAGES)


def test_every_fixture_config_without_a_lounge_block_still_loads():
    for p in sorted(FIXTURES.glob("config_*.yaml")):
        if p.name == "config_lounge.yaml":
            continue
        load_config(p)


# --- multiple errors and the two-stage design ---


def test_all_cross_check_errors_are_reported_together(tmp_path):
    lounge = {
        "welcome": {"enabled": True, "message": "{nope} " + "a" * 2000},
        "daily_quote": {
            "enabled": True,
            "sources": [
                {"wikiquote": "a|b"},
                {"url": "http://x.example"},
                {"wikiquote": "Z"},
                {"wikiquote": "z"},
            ],
        },
    }
    msg = _errors(tmp_path, lounge)
    for needle in [
        "lounge.channel_id is required",
        "{nope}",
        "limit is 2000",
        "isn't a page title",
        "uses plain http",
        "sources[3] repeats sources[2]",
    ]:
        assert needle in msg, needle


def test_shape_errors_stop_before_cross_checks(tmp_path):
    # Documented: pydantic fails first, so a bad channel_id hides the
    # placeholder and length problems until it's fixed. Two runs to see all.
    lounge = {"channel_id": 0, "welcome": {"enabled": True, "message": "{nope}"}}
    msg = _errors(tmp_path, lounge)
    assert "lounge.channel_id" in msg
    assert "unknown placeholder" not in msg


def test_shape_errors_in_sibling_fields_are_reported_together(tmp_path):
    lounge = {"channel_id": 0, "daily_quote": {"time": "99:99", "sources": ["bad"]}}
    msg = _errors(tmp_path, lounge)
    assert "lounge.channel_id" in msg
    assert "lounge.daily_quote.time" in msg
    assert "must have exactly one" in msg


def test_lounge_errors_join_other_cross_check_errors(tmp_path):
    doc = dict(_BASE)
    doc["admin_permission"] = "not_a_flag"
    doc["lounge"] = {"welcome": {"enabled": True, "message": "{nope}"}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    msg = str(exc_info.value)
    assert "not_a_flag" in msg and "{nope}" in msg


# --- the built-in list ---


def _norm(title: str) -> str:
    return QuoteSourceCfg(kind="wikiquote", value=title).key


def test_default_list_is_non_empty_strings_without_duplicates():
    assert DEFAULT_WIKIQUOTE_PAGES
    assert all(isinstance(t, str) and t.strip() == t and t for t in DEFAULT_WIKIQUOTE_PAGES)
    keys = [_norm(t) for t in DEFAULT_WIKIQUOTE_PAGES]
    assert len(keys) == len(set(keys))


def test_default_list_passes_the_same_validation_as_user_sources():
    for i, t in enumerate(DEFAULT_WIKIQUOTE_PAGES):
        assert _source_problem(i, QuoteSourceCfg(kind="wikiquote", value=t)) is None


def test_default_list_has_no_modern_or_themed_pages():
    lowered = [t.lower() for t in DEFAULT_WIKIQUOTE_PAGES]
    for banned in ["fight club", "hunter s. thompson", "friendship", "(film)", "(book)", "(tv"]:
        assert not any(banned in t for t in lowered), banned


def test_default_list_is_a_tuple_so_nobody_mutates_it():
    assert isinstance(DEFAULT_WIKIQUOTE_PAGES, tuple)


def test_default_list_source_file_lists_modern_examples_only_as_comments():
    src = (ROOT / "newsbot" / "lounge" / "default_sources.py").read_text()
    for line in src.splitlines():
        if "Fight Club" in line or "Thompson" in line:
            assert line.lstrip().startswith("#")


def test_defaults_are_distinct_objects_per_load(tmp_path):
    a = _load(tmp_path, {}).lounge.daily_quote.sources
    b = _load(tmp_path, {}).lounge.daily_quote.sources
    assert a == b and a is not b
