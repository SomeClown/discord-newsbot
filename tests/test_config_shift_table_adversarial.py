"""Adversarial round on the `shift_table` config wiring (plan Task 2, design.md §16).

The first round pinned the messages the plan names. This one goes looking for
the ways a config file can be wrong that the plan didn't name: urls that are
missing, blank, relative or carrying a password; names at the cap in every
alphabet, and in an alphabet YAML lets you spell that Python can't encode;
`codes_only` spelled the YAML 1.1 way; anchors that smuggle `codes_only: false`
past a reader's eyes; and a handful of "what does the loader do when three
things are wrong at once". Where the behavior is a decision the plan didn't
make (a table with `trust: official`, case-different names), the test pins what
happens today and says so, so changing it is a choice and not a surprise.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from newsbot.config import (
    ConfigError,
    ShiftTableSource,
    effective_codes_only,
    load_config,
)

ROOT = Path(__file__).parent.parent
PRODLIKE = Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml"
URL = "https://codes.example/borderlands-4-shift-codes/"
CATALOG_STEP_START = "# >>> catalog-step: start"
CATALOG_STEP_END = "# <<< catalog-step: end"


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


def _table(name: str = '"mentalmars"', url: str | None = f'"{URL}"', extra: str = "") -> str:
    """A flow-style shift_table entry; `url=None` leaves the key out altogether."""
    parts = ["type: shift_table", f"name: {name}"]
    if url is not None:
        parts.append(f"url: {url}")
    parts.append("trust: community")
    if extra:
        parts.append(extra)
    return "{" + ", ".join(parts) + "}"


def _v3(
    bl4: str, *, palworld: str = "", shift: str = "", shared: str = "", web_search: str = ""
) -> str:
    """A v3 file: palworld, then borderlands4, each with the (already-indented) sources given."""
    return (
        "home_guild_id: 100000000000000001\n"
        + shift
        + web_search
        + "catalog:\n"
        + "  - key: palworld\n    name: Palworld\n"
        + (f"    sources:\n{palworld}" if palworld else "")
        + "  - key: borderlands4\n    name: Borderlands 4\n    aliases: [BL4]\n"
        + f"    sources:\n{bl4}"
        + shared
    )


def _item(body: str, indent: str = "      ") -> str:
    return f"{indent}- {body}\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str):
    return load_config(_write(tmp_path, text))


def _error(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, text)
    return str(exc.value)


def _v2_with(entry: str) -> str:
    text = PRODLIKE.read_text()
    assert text.count("\nsources:\n") == 1
    return text.replace("\nsources:\n", f"\nsources:\n  - {entry}\n")


def _tables(cfg) -> list[ShiftTableSource]:
    return [s for g in cfg.catalog for s in g.sources if isinstance(s, ShiftTableSource)]


# --- the url ---


@pytest.mark.parametrize(
    ("url", "why"),
    [
        (None, "missing"),
        ('""', "blank"),
        ("null", "null"),
        ('"ftp://codes.example/x"', "not http(s)"),
        ('"file:///etc/passwd"', "a local file"),
        ('"codes.example/x"', "no scheme"),
        ('"//codes.example/x"', "scheme-relative"),
    ],
)
def test_a_url_that_is_not_an_http_address_is_a_config_error_naming_the_url(tmp_path, url, why):
    message = _error(tmp_path, _v3(_item(_table(url=url))))
    assert "shift_table.url" in message, why


def test_a_url_with_a_password_in_it_is_not_echoed_back_in_the_error(tmp_path):
    # A config error is what gets pasted into a chat when asking for help.
    message = _error(tmp_path, _v3(_item(_table(url='"ftp://owner:s3cret-pass@codes.example/x"'))))
    assert "s3cret-pass" not in message


@pytest.mark.parametrize(
    "url",
    [
        "http://codes.example/x",
        "http://localhost/x",
        "https://localhost/x",
        "https://127.0.0.1/x",
        "ftp://codes.example/x",
        "file:///etc/passwd",
        "codes.example/x",
        "//codes.example/x",
        "https://owner:pw@codes.example/x",
    ],
)
def test_a_shift_table_accepts_exactly_the_urls_an_rss_source_does(tmp_path, url):
    # Parity pin, not a security claim: config urls are the owner's, and the fetch
    # refuses private redirect targets. If someone tightens one source type's urls
    # (https only, say) this fails until the other follows.
    rss = f'{{type: rss, name: r, url: "{url}", trust: press}}'

    def accepted(entry: str) -> bool:
        try:
            _load(tmp_path, _v3(_item(entry)))
        except ConfigError:
            return False
        return True

    assert accepted(_table(url=f'"{url}"')) == accepted(rss)


def test_plain_http_and_localhost_are_accepted_today_like_rss(tmp_path):
    for url in ("http://codes.example/x", "http://localhost/x"):
        (table,) = _tables(_load(tmp_path, _v3(_item(_table(url=f'"{url}"')))))
        assert str(table.url).startswith(url.split("/x")[0])


# --- the name ---


@pytest.mark.parametrize("name", ['""', '"   "', '"\\t\\n"', "null"], ids=repr)
def test_a_blank_or_null_name_is_an_error(tmp_path, name):
    message = _error(tmp_path, _v3(_item(_table(name=name))))
    assert "shift_table.name" in message


def test_a_missing_name_is_an_error(tmp_path):
    entry = f'{{type: shift_table, url: "{URL}", trust: community}}'
    assert "shift_table.name: Field required" in _error(tmp_path, _v3(_item(entry)))


@pytest.mark.parametrize(
    ("glyph", "units"),
    [("n", 1), ("é", 1), ("\U0001f600", 2), ("\U00020bb7", 2)],
    ids=["ascii", "bmp", "emoji", "cjk-extension"],
)
def test_the_cap_counts_characters_so_100_of_anything_loads_and_101_does_not(
    tmp_path, glyph, units
):
    # `len()` counts code points, while the repo's `discord_len` counts UTF-16 units,
    # so 100 emoji are 200 units. That's still nowhere near 2000 even printed twice and
    # escaped, which is why the cap is allowed to be this simple. Pinned so nobody "fixes"
    # it to units and breaks a 60-emoji name that fits.
    ok = _load(tmp_path, _v3(_item(_table(name=f'"{glyph * 100}"'))))
    (table,) = _tables(ok)
    assert len(table.name) == 100 and len(table.name) * units <= 200
    message = _error(tmp_path, _v3(_item(_table(name=f'"{glyph * 101}"'))))
    assert "is 101 characters; the limit is 100" in message


def test_a_name_of_100_markdown_characters_loads_because_escaping_it_twice_still_fits(tmp_path):
    from newsbot.bot.format import discord_len, esc

    name = "_*`~|>@" * 14 + "ab"  # 100 characters, nearly all of them special
    assert len(name) == 100
    (table,) = _tables(_load(tmp_path, _v3(_item(_table(name=f'"{name}"')))))
    # The comment on the cap says the longest name, twice, fits in an alert.
    assert 2 * discord_len(esc(table.name)) < 2000


def test_a_name_that_cannot_be_encoded_is_rejected_at_load(tmp_path):
    # A YAML \\uD83D escape is half an emoji: it can't be saved as UTF-8 (source health, the
    # sightings) or counted in UTF-16 (the alert's size), so it would fail on every pass.
    message = _error(tmp_path, _v3(_item(_table(name='"half-an-emoji-\\uD83D"'))))
    assert "has a character that can't be saved (a lone surrogate)" in message


def _rss(name: str) -> str:
    return f'{{type: rss, name: {name}, url: "https://example.com/f", trust: press}}'


@pytest.mark.parametrize(
    ("name", "wanted"),
    [
        ('"two\\nlines"', "has a control character"),
        ('"tab\\there"', "has a control character"),
        ('"nul\\0byte"', "has a control character"),
        ('"esc\\e[31m"', "has a control character"),
        ('"\\u200b\\u200d"', "is empty or only invisible characters"),
        ('"\\u2060"', "is empty or only invisible characters"),
        ('"\\u00a0\\ufeff"', "is empty or only invisible characters"),
        ('"  padded"', "has spaces at the start or end"),
        ('"padded  "', "has spaces at the start or end"),
        ('"\\u00a0nbsp-first"', "has spaces at the start or end"),
    ],
)
@pytest.mark.parametrize("make", [_rss, lambda n: _table(name=n)], ids=["rss", "shift_table"])
def test_names_that_cant_be_shown_are_rejected_for_every_source_type(tmp_path, make, name, wanted):
    message = _error(tmp_path, _v3(_item(make(name))))
    assert wanted in message
    assert "it's shown in SHiFT alerts" in message


def test_an_error_about_a_name_never_echoes_a_raw_control_character(tmp_path):
    message = _error(tmp_path, _v3(_item(_rss('"esc\\e[31m two\\nlines \\u202e \\ud83d"'))))
    assert all(ch.isprintable() or ch == "\n" for ch in message)
    assert message.count("\n") == message.count("\n  - ")  # only the list's own newlines


def test_a_long_bad_name_is_cut_short_in_the_error(tmp_path):
    message = _error(tmp_path, _v3(_item(_rss('"' + "x" * 200 + '\\n"'))))
    assert "x" * 100 not in message and "..." in message


def test_invisible_characters_inside_a_real_name_are_fine(tmp_path):
    # Only a name that is nothing but invisible is a problem. A zero-width joiner in an
    # emoji sequence, or an inner space, is ordinary text.
    for name in ('"Family \\U0001f468\\u200d\\U0001f469 news"', '"two words"', '"caf\\u00e9"'):
        _load(tmp_path, _v3(_item(_rss(name))))


def test_a_v2_name_that_cant_be_shown_is_reported_once(tmp_path):
    message = _error(tmp_path, _v2_with(_rss('"line\\nbreak"')))
    assert message.count("has a control character") == 1


def test_the_web_search_block_name_is_under_the_cap_too(tmp_path):
    web_search = f'web_search:\n  name: "{"w" * 101}"\n'
    message = _error(
        tmp_path,
        _v3(_item(_table()), web_search=web_search),
    )
    assert "is 101 characters; the limit is 100" in message


def test_a_blank_source_name_for_a_bluesky_search_is_not_the_cap_checkers_problem(tmp_path):
    # The cap skips falsy names (Bluesky gets a default one later); it must not crash on None.
    entry = '{type: bluesky_search, query: "borderlands", trust: community}'
    _load(tmp_path, _v3(_item(entry) + _item(_table())))


# --- codes_only, every way YAML can spell it ---


@pytest.mark.parametrize("spelling", ["false", "no", "False", "off", '"false"', "0"])
def test_codes_only_false_in_any_spelling_is_still_an_error(tmp_path, spelling):
    message = _error(tmp_path, _v3(_item(_table(extra=f"codes_only: {spelling}"))))
    assert "codes_only can't be false for a shift_table source" in message


@pytest.mark.parametrize("spelling", ["true", "yes", "null", "~"])
def test_codes_only_true_or_empty_loads_and_stays_codes_only(tmp_path, spelling):
    cfg = _load(tmp_path, _v3(_item(_table(extra=f"codes_only: {spelling}"))))
    (table,) = _tables(cfg)
    assert effective_codes_only(table) is True
    assert cfg.codes_only_source_names() == {"mentalmars"}


def test_a_merge_key_cannot_smuggle_codes_only_false_past_the_check(tmp_path):
    sources = (
        f"      - &t {_table()}\n"
        "      - <<: *t\n"
        "        name: second-copy\n"
        "        codes_only: false\n"
    )
    message = _error(tmp_path, _v3(sources))
    assert "(second-copy): codes_only can't be false for a shift_table source" in message


def test_one_alias_under_two_games_is_a_duplicate_name_not_two_sources(tmp_path):
    shift = "shift:\n  games: [borderlands4, palworld]\n"
    text = _v3("      - *t\n", palworld=f"      - &t {_table()}\n", shift=shift)
    assert "duplicate source name 'mentalmars'" in _error(tmp_path, text)


# --- who else may share its name ---


def test_a_shift_table_named_like_the_reddit_source_is_a_duplicate(tmp_path):
    reddit = (
        '{type: rss, name: "r/Borderlands4", '
        'url: "https://www.reddit.com/r/Borderlands4/top/.rss", trust: community}'
    )
    text = _v3(_item(reddit) + _item(_table(name='"r/Borderlands4"')))
    assert "duplicate source name 'r/Borderlands4'" in _error(tmp_path, text)


def test_two_tables_with_the_same_name_are_a_duplicate_even_with_different_urls(tmp_path):
    other = _table(url='"https://other.example/codes"')
    assert "duplicate source name 'mentalmars'" in _error(
        tmp_path, _v3(_item(_table()) + _item(other))
    )


def test_names_that_differ_only_by_case_load_as_two_sources_with_one_warning(tmp_path, caplog):
    # The B+ rule (decide.py) compares names casefolded, so these two count as ONE source when
    # confirming a code, while source_health, the owner report and `codes_only_source_names`
    # (exact match) treat them as two. Not an error, because a config that loads today has to
    # keep loading; the loader says so once, naming both.
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        cfg = _load(tmp_path, _v3(_item(_table()) + _item(_table(name='"MentalMars"'))))
    assert cfg.codes_only_source_names() == {"mentalmars", "MentalMars"}
    twins = [r.getMessage() for r in caplog.records if "differ only by case" in r.getMessage()]
    assert len(twins) == 1
    assert "'mentalmars'" in twins[0] and "'MentalMars'" in twins[0]


def test_three_spellings_of_one_name_get_one_warning_naming_all_three(tmp_path, caplog):
    entries = "".join(_item(_rss(f'"{n}"')) for n in ("Gear", "GEAR", "gear"))
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        _load(tmp_path, _v3(entries))
    (twin,) = [r.getMessage() for r in caplog.records if "differ only by case" in r.getMessage()]
    assert all(f"'{n}'" in twin for n in ("Gear", "GEAR", "gear"))


def test_distinct_names_and_exact_duplicates_do_not_trigger_the_case_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        _load(tmp_path, _v3(_item(_table()) + _item(_table(name='"mentalmars mirror"'))))
        _error(tmp_path, _v3(_item(_table()) + _item(_table())))  # an error, not a case twin
    assert not [r for r in caplog.records if "differ only by case" in r.getMessage()]


def test_the_shipped_configs_have_no_case_twins(caplog):
    shipped = [
        ROOT / "config.example.yaml",
        ROOT / "config.minimal.yaml",
        *sorted((Path(__file__).parent / "fixtures").glob("config_*.yaml")),
    ]
    with caplog.at_level(logging.WARNING, logger="newsbot.config"):
        for path in shipped:
            load_config(path)
    assert not [r for r in caplog.records if "differ only by case" in r.getMessage()]


def test_two_tables_may_share_a_url_under_different_names(tmp_path):
    cfg = _load(tmp_path, _v3(_item(_table()) + _item(_table(name='"mentalmars mirror"'))))
    assert [str(t.url) for t in _tables(cfg)] == [URL, URL]


# --- trust ---


@pytest.mark.parametrize("trust", ["official", "press"])
def test_a_table_must_be_community_trust(tmp_path, trust):
    # §16 makes mentalmars a community list. With `official` its new codes would ping on their
    # own (ping_trust), and "official" is the rank details earn from Gearbox's own posts.
    entry = _table().replace("trust: community", f"trust: {trust}")
    message = _error(tmp_path, _v3(_item(entry)))
    assert (
        "catalog[1] (borderlands4).sources[0] (mentalmars): a shift_table source's trust must be "
        f"community (it's a community list, and not {trust})"
    ) in message


def test_community_trust_is_the_one_a_table_may_have(tmp_path):
    (table,) = _tables(_load(tmp_path, _v3(_item(_table()))))
    assert table.trust == "community" and effective_codes_only(table)


def test_a_v2_table_with_the_wrong_trust_is_an_error_too(tmp_path):
    entry = _table(extra="topics: [borderlands4]").replace("trust: community", "trust: official")
    assert "a shift_table source's trust must be community" in _error(tmp_path, _v2_with(entry))


def test_a_table_with_no_trust_or_a_bad_one_is_an_error(tmp_path):
    entry = f'{{type: shift_table, name: m, url: "{URL}"}}'
    assert "shift_table.trust: Field required" in _error(tmp_path, _v3(_item(entry)))
    bad = _table().replace("trust: community", "trust: trusted-ish")
    assert "shift_table.trust" in _error(tmp_path, _v3(_item(bad)))


# --- where it may live ---


def test_a_table_under_a_game_still_has_no_topics_of_its_own(tmp_path):
    message = _error(tmp_path, _v3(_item(_table(extra="topics: [borderlands4]"))))
    assert "(mentalmars): remove topics; a source listed under a game belongs to that game" in (
        message
    )


def test_shift_games_naming_only_other_games_rejects_the_table_and_the_unknown_game_together(
    tmp_path,
):
    shift = "shift:\n  games: [palworld, nope]\n"
    message = _error(tmp_path, _v3(_item(_table()), shift=shift))
    assert "shift.games names unknown game 'nope'" in message
    assert "catalog[1] (borderlands4).sources[0] (mentalmars): shift_table only feeds" in message


def test_a_table_next_to_a_dropped_reddit_feed_is_not_dropped_with_it(tmp_path):
    # The other non-SHiFT-game rule skips a Reddit feed with a warning; this one is an error
    # (plan §3.9), but a SHiFT game's table must survive the reddit-skipping pass untouched.
    reddit = (
        '{type: rss, name: "r/Palworld", url: "https://www.reddit.com/r/Palworld/.rss", '
        "trust: community}"
    )
    cfg = _load(tmp_path, _v3(_item(_table()), palworld=_item(reddit)))
    assert [t.name for t in _tables(cfg)] == ["mentalmars"]
    assert "r/Palworld" not in cfg.configured_source_names()


def test_configured_source_names_and_a_games_sources_include_the_table(tmp_path):
    cfg = _load(tmp_path, _v3(_item(_table())))
    assert "mentalmars" in cfg.configured_source_names()
    assert cfg.game_source_names("borderlands4") == ["mentalmars"]
    assert cfg.game_source_names("palworld") == []


# --- the v2 shape, with more than one thing wrong ---


def test_a_v2_table_with_three_problems_reports_all_three_in_one_error(tmp_path):
    long_name = "n" * 101
    entry = (
        f'{{type: shift_table, name: "{long_name}", url: "{URL}", '
        "trust: community, codes_only: false}"
    )
    message = _error(tmp_path, _v2_with(entry))
    assert "codes_only can't be false for a shift_table source" in message
    assert "a shift_table source must name exactly one topic" in message
    assert "is 101 characters; the limit is 100" in message
    assert "KeyError" not in message


def test_two_v2_tables_with_one_name_are_a_duplicate_not_a_key_error(tmp_path):
    entry = _table(extra="topics: [borderlands4]")
    text = _v2_with(entry).replace("\nsources:\n", f"\nsources:\n  - {entry}\n", 1)
    assert "duplicate source name 'mentalmars'" in _error(tmp_path, text)


def test_a_v2_table_named_like_an_rss_source_is_a_duplicate(tmp_path):
    cfg = load_config(PRODLIKE)
    taken = next(s.name for s in cfg.catalog[0].sources)
    entry = _table(name=f'"{taken}"', extra="topics: [borderlands4]")
    assert f"duplicate source name {taken!r}" in _error(tmp_path, _v2_with(entry))


def test_a_v2_table_whose_topics_is_a_bare_string_is_an_error_not_a_crash(tmp_path):
    message = _error(tmp_path, _v2_with(_table(extra="topics: borderlands4")))
    assert "topics" in message


def test_a_v2_table_scoped_to_a_known_game_adds_exactly_one_source_to_it(tmp_path):
    before = load_config(PRODLIKE)
    after = _load(tmp_path, _v2_with(_table(extra="topics: [borderlands4]")))
    bl4_before = next(g for g in before.catalog if g.key == "borderlands4")
    bl4_after = next(g for g in after.catalog if g.key == "borderlands4")
    assert [s.name for s in bl4_after.sources] == [
        "mentalmars",
        *(s.name for s in bl4_before.sources),
    ]
    for key in (g.key for g in before.catalog if g.key != "borderlands4"):
        a = next(g for g in after.catalog if g.key == key)
        b = next(g for g in before.catalog if g.key == key)
        assert [s.name for s in a.sources] == [s.name for s in b.sources]
    assert [s.name for s in after.shared_sources] == [s.name for s in before.shared_sources]


# --- the shipped files ---


def test_every_shipped_config_loads_and_obeys_the_table_rules(tmp_path):
    shipped = [
        ROOT / "config.example.yaml",
        ROOT / "config.minimal.yaml",
        *sorted((Path(__file__).parent / "fixtures").glob("config_*.yaml")),
    ]
    assert len(shipped) >= 5
    for path in shipped:
        cfg = load_config(path)
        for table in _tables(cfg):
            assert effective_codes_only(table), path
            assert table.name in cfg.codes_only_source_names(), path
        names = [s.name for g in cfg.catalog for s in g.sources if s.name]
        assert all(len(n) <= 100 for n in names), path


def test_the_minimal_example_has_no_table_and_nothing_codes_only_changed(tmp_path):
    cfg = load_config(ROOT / "config.minimal.yaml")
    assert _tables(cfg) == []
    assert "mentalmars" not in cfg.configured_source_names()


def test_the_mentalmars_line_sits_inside_the_catalog_step_markers():
    # The production runbook copies exactly what's between the markers; a table line
    # that landed outside them would load fine here and never reach the Droplet.
    lines = (ROOT / "config.example.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(CATALOG_STEP_START))
    end = next(i for i, line in enumerate(lines) if line.startswith(CATALOG_STEP_END))
    hits = [i for i, line in enumerate(lines) if "type: shift_table" in line]
    assert len(hits) == 1 and start < hits[0] < end


def test_the_table_line_is_the_one_under_borderlands4_in_the_example():
    lines = (ROOT / "config.example.yaml").read_text().splitlines()
    hit = next(i for i, line in enumerate(lines) if "type: shift_table" in line)
    owner = next(line for line in reversed(lines[:hit]) if line.startswith("  - key:"))
    assert owner.strip() == "- key: borderlands4"


def test_moving_shift_off_borderlands4_in_the_example_is_a_hard_error_for_the_table(tmp_path):
    # Unlike a Reddit feed (skipped with a warning), a table under a game outside shift.games is
    # an error, as the plan says. A self-hoster who edits `shift.games` in the shipped example
    # gets told which line to delete; pinned so the message keeps naming it.
    text = (ROOT / "config.example.yaml").read_text()
    assert "\n  games: [borderlands4]\n" in text
    edited = text.replace("\n  games: [borderlands4]\n", "\n  games: [diablo4]\n", 1)
    message = _error(tmp_path, edited)
    assert "(mentalmars): shift_table only feeds SHiFT detection, and borderlands4 isn't" in message


def test_deleting_the_table_line_from_the_example_still_loads(tmp_path):
    text = (ROOT / "config.example.yaml").read_text()
    kept = [line for line in text.splitlines(keepends=True) if "type: shift_table" not in line]
    cfg = _load(tmp_path, "".join(kept))
    assert _tables(cfg) == [] and cfg.codes_only_source_names() == {"r/Borderlands4"}
