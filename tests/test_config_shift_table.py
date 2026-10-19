"""The `shift_table` source in the config (design.md §16, plan Task 2).

Task 1 taught a collector to read a page of SHiFT code tables; this is the part
where the config learns the source exists and has opinions about it. A table is
always codes-only, lives under one SHiFT game, and carries a name short enough
to survive being printed in an alert twice. The loader is supposed to say all of
that in plain words, collected into the one `ConfigError`, instead of letting a
v2-shaped file walk into a `KeyError` and call it a day. These tests pin the
words, the dispatch (`one()` must never hand a table to the Bluesky collector),
and what `--check-sources` does with a page that is read but never stored.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsbot.collectors.base import build_catalog_collectors
from newsbot.collectors.bluesky import BlueskyCollector
from newsbot.collectors.shift_table import PageValidators, ShiftTableCollector, Validators
from newsbot.config import (
    ConfigError,
    Secrets,
    ShiftTableSource,
    effective_codes_only,
    load_config,
)
from newsbot.pipeline.run import run_check_sources

PRODLIKE = Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml"
PAGE_URL = "https://codes.example/borderlands-4-shift-codes/"
TABLE = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community}}'
CODE = "K4M7P-2QXZR-9TLWB-6NHVC-J3FDS"

SECRETS = Secrets(
    discord_token=None,
    anthropic_api_key="k",
    brave_api_key=None,
    bluesky_handle=None,
    bluesky_app_password=None,
)


@pytest.fixture(autouse=True)
def _brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def _v3(bl4_sources: str, *, palworld_sources: str = "", shift: str = "", shared: str = "") -> str:
    """A v3 file: palworld first, then borderlands4, each with the sources it's given."""
    return (
        "home_guild_id: 100000000000000001\n"
        + shift
        + "catalog:\n"
        + "  - key: palworld\n    name: Palworld\n"
        + (f"    sources:\n{palworld_sources}" if palworld_sources else "")
        + "  - key: borderlands4\n    name: Borderlands 4\n    aliases: [BL4]\n"
        + f"    sources:\n{bl4_sources}"
        + shared
    )


def _entry(body: str, indent: str = "      ") -> str:
    return f"{indent}- {body}\n"


def _error(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, text))
    return str(exc.value)


def _v2_with(entry: str, *, alerts_topics: str | None = None) -> str:
    """The prod-like v2 file with `entry` added to the top of its `sources:` list."""
    text = PRODLIKE.read_text()
    assert text.count("\nsources:\n") == 1
    text = text.replace("\nsources:\n", f"\nsources:\n  - {entry}\n")
    if alerts_topics is not None:
        old = "  topics: [borderlands4]\n  interval_minutes"
        assert old in text
        text = text.replace(old, f"  topics: {alerts_topics}\n  interval_minutes")
    return text


# --- the source parses, and it's codes-only whether it says so or not ---


def test_a_shift_table_under_its_game_loads_and_is_codes_only(tmp_path):
    cfg = load_config(_write(tmp_path, _v3(_entry(TABLE))))
    (source,) = cfg.catalog[1].sources
    assert isinstance(source, ShiftTableSource) and source.codes_only is None
    assert effective_codes_only(source)
    assert cfg.codes_only_source_names() == {"mentalmars"}


def test_codes_only_true_is_fine_and_says_nothing(tmp_path, caplog):
    entry = _entry(TABLE.replace("trust: community", "trust: community, codes_only: true"))
    cfg = load_config(_write(tmp_path, _v3(entry)))
    assert cfg.codes_only_source_names() == {"mentalmars"}
    assert not [r for r in caplog.records if "mentalmars" in r.getMessage()]


def test_codes_only_false_on_a_shift_table_is_an_error_that_says_why(tmp_path):
    entry = _entry(TABLE.replace("trust: community", "trust: community, codes_only: false"))
    message = _error(tmp_path, _v3(entry))
    assert (
        "catalog[1] (borderlands4).sources[0] (mentalmars): codes_only can't be false for a "
        "shift_table source; it's read only for SHiFT codes and their details. Remove the "
        "setting or set it to true"
    ) in message


def test_codes_only_false_is_an_error_in_the_v2_shape_too(tmp_path):
    entry = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community, '
    entry += "topics: [borderlands4], codes_only: false}"
    message = _error(tmp_path, _v2_with(entry))
    assert "sources[0] (mentalmars): codes_only can't be false for a shift_table source" in message


# --- where it's allowed to live ---


def test_a_shift_table_in_shared_sources_is_told_where_it_goes(tmp_path):
    shared = "shared_sources:\n" + _entry(TABLE, indent="  ")
    message = _error(
        tmp_path,
        _v3(_entry("{type: steam_news, name: S, app_id: 1, trust: official}"), shared=shared),
    )
    assert "shared_sources[0] (mentalmars): shift_table goes under its game in catalog:" in message


def test_a_shift_table_under_a_game_outside_shift_games_is_an_error(tmp_path):
    message = _error(
        tmp_path,
        _v3(
            _entry("{type: steam_news, name: S, app_id: 1, trust: official}"),
            palworld_sources=_entry(TABLE),
        ),
    )
    assert (
        "catalog[0] (palworld).sources[0] (mentalmars): shift_table only feeds SHiFT "
        "detection, and palworld isn't in shift.games"
    ) in message


def test_leaving_shift_out_defaults_to_borderlands4_so_its_table_is_fine(tmp_path):
    # The default is filled in before the check runs; a file that never says
    # `shift:` must not trip over a rule about a list it didn't write.
    cfg = load_config(_write(tmp_path, _v3(_entry(TABLE))))
    assert cfg.shift.games == ["borderlands4"]


def test_a_second_shift_game_makes_a_table_under_it_legal(tmp_path):
    text = _v3(
        _entry("{type: steam_news, name: S, app_id: 1, trust: official}"),
        palworld_sources=_entry(TABLE),
        shift="shift:\n  games: [borderlands4, palworld]\n",
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.codes_only_source_names() == {"mentalmars"}


# --- the v2 shape ---


def test_a_v2_shift_table_scoped_to_one_topic_derives_under_that_game(tmp_path):
    entry = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community, '
    entry += "topics: [borderlands4]}"
    cfg = load_config(_write(tmp_path, _v2_with(entry)))
    bl4 = next(g for g in cfg.catalog if g.key == "borderlands4")
    table = next(s for s in bl4.sources if s.name == "mentalmars")
    assert isinstance(table, ShiftTableSource) and table.topics is None
    assert "mentalmars" not in [s.name for s in cfg.shared_sources]
    assert "mentalmars" in cfg.codes_only_source_names()


@pytest.mark.parametrize(
    "topics",
    ["", ", topics: []", ", topics: [borderlands4, palworld]"],
    ids=["no topics", "empty topics", "two topics"],
)
def test_a_v2_shift_table_must_name_exactly_one_topic(tmp_path, topics):
    entry = (
        f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community{topics}}}'
    )
    message = _error(tmp_path, _v2_with(entry))
    assert "source 'mentalmars': a shift_table source must name exactly one topic" in message


def test_a_v2_shift_table_with_an_unknown_topic_gets_the_usual_error_not_a_key_error(tmp_path):
    entry = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community, '
    entry += "topics: [nope]}"
    message = _error(tmp_path, _v2_with(entry))
    assert "source 'mentalmars' references unknown topic 'nope'" in message


def test_a_v2_shift_table_for_a_game_outside_alerts_topics_is_an_error(tmp_path):
    entry = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community, '
    entry += "topics: [palworld]}"
    message = _error(tmp_path, _v2_with(entry))
    assert "shift_table only feeds SHiFT detection, and palworld isn't in shift.games" in message


def test_an_empty_alerts_topics_means_every_game_so_nothing_is_wrong(tmp_path):
    # The v2 reading of an empty list is "every game", and it holds here too.
    entry = f'{{type: shift_table, name: "mentalmars", url: "{PAGE_URL}", trust: community, '
    entry += "topics: [palworld]}"
    cfg = load_config(_write(tmp_path, _v2_with(entry, alerts_topics="[]")))
    assert "mentalmars" in cfg.codes_only_source_names()


# --- the name cap ---


def _long(n: int) -> str:
    return "n" * n


def test_a_name_of_exactly_100_characters_is_fine(tmp_path):
    entry = _entry(f'{{type: steam_news, name: "{_long(100)}", app_id: 1, trust: official}}')
    load_config(_write(tmp_path, _v3(entry)))


def test_a_name_of_101_characters_is_an_error_that_says_why(tmp_path):
    entry = _entry(f'{{type: steam_news, name: "{_long(101)}", app_id: 1, trust: official}}')
    message = _error(tmp_path, _v3(entry))
    assert (
        f"source name '{_long(101)}' is 101 characters; the limit is 100 "
        "(it's shown in SHiFT alerts)"
    ) in message


def test_the_cap_covers_a_shift_table_a_shared_source_and_a_default_bluesky_name(tmp_path):
    table = _entry(TABLE.replace("mentalmars", _long(120)))
    shared = "shared_sources:\n" + _entry(
        f'{{type: rss, name: "{_long(130)}", url: "https://example.com/f", trust: press}}',
        indent="  ",
    )
    bluesky = _entry(f'{{type: bluesky_search, query: "{_long(110)}", trust: community}}')
    message = _error(tmp_path, _v3(table + bluesky, shared=shared))
    assert "is 120 characters" in message
    assert "is 130 characters" in message
    assert f"source name 'Bluesky: {_long(110)}' is {len('Bluesky: ') + 110} characters" in message


def test_a_long_v2_name_is_reported_once(tmp_path):
    entry = f'{{type: rss, name: "{_long(101)}", url: "https://example.com/f", trust: press}}'
    message = _error(tmp_path, _v2_with(entry))
    assert message.count("is 101 characters") == 1


def test_the_shipped_example_stays_under_the_cap():
    # Every name in the example loads, so none is over; this is here so a long
    # new one fails with the loader's words and not a surprise in somebody's alert.
    load_config(Path(__file__).parent.parent / "config.example.yaml")


# --- the dispatcher ---


def _bl4_cfg(tmp_path: Path):
    return load_config(_write(tmp_path, _v3(_entry(TABLE))))


def test_one_never_hands_a_shift_table_to_bluesky(tmp_path):
    cfg = _bl4_cfg(tmp_path)
    collectors = build_catalog_collectors(cfg, SECRETS)
    (collector,) = collectors
    assert isinstance(collector, ShiftTableCollector)
    assert not isinstance(collector, BlueskyCollector)
    assert (collector.name, collector.source_type, collector.rate_limit_key) == (
        "mentalmars",
        "shift_table",
        None,
    )


async def test_a_shared_validators_object_reaches_the_collector_every_pass(tmp_path):
    # The collectors are rebuilt each pass; the dict they share must not be.
    cfg = _bl4_cfg(tmp_path)
    validators = PageValidators({PAGE_URL: Validators('"abc"', None)})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for _ in range(2):
            (collector,) = build_catalog_collectors(cfg, SECRETS, validators=validators)
            assert await collector.collect(http) == []
    assert [r.headers.get("If-None-Match") for r in seen] == ['"abc"', '"abc"']


async def test_without_validators_the_request_is_unconditional(tmp_path):
    cfg = _bl4_cfg(tmp_path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        (collector,) = build_catalog_collectors(cfg, SECRETS)
        # A 304 is only an answer to a conditional request. This one wasn't, so (QA batch 1,
        # fix 10) the 304 is an error; the point of the test is the request that went out.
        with pytest.raises(httpx.HTTPStatusError):
            await collector.collect(http)
    assert "If-None-Match" not in seen[0].headers
    assert "If-Modified-Since" not in seen[0].headers


# --- --check-sources ---

# One undated row: fresh as far as the lookback is concerned, and matched to its
# game by the dedicated-source rule, so if it weren't kept out of the counts it
# would show up as a story for Borderlands 4.
ONE_ROW_PAGE = f"""<html><body><h2>Codes</h2>
<table><tr><th>SHiFT Code</th><th>Reward</th></tr>
<tr><td>{CODE}</td><td>3 Golden Keys</td></tr></table></body></html>"""


async def test_check_sources_lists_the_table_and_keeps_its_items_out_of_the_counts(tmp_path):
    cfg = _bl4_cfg(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ONE_ROW_PAGE.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await run_check_sources(cfg, SECRETS, http)

    (result,) = report.results
    assert (result.source_name, result.source_type, result.error) == (
        "mentalmars",
        "shift_table",
        None,
    )
    assert [item.title for item in result.items] == [CODE]  # read, so the table shows it
    assert report.topic_counts == {"palworld": 0, "borderlands4": 0}  # and never counted
