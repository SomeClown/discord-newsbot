"""v3.4.1: a months-old article got into a digest through Brave's dates.

playstationcouch.com stamps every page's datePublished with the moment it's served,
so Brave's `page_age` made a June article look a day old, and the summarize prompt
carried no dates for the model to catch it with. Two fixes, pinned here: Brave
results from sites with untrustworthy dates are dropped (`exclude_domains`), and
the prompt now tells the model today's date and each item's published date.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from test_summaries import LA, PAC, FakeLLM, World

from newsbot.collectors.base import RawItem, build_catalog_collectors
from newsbot.collectors.web_search import WebSearchCollector
from newsbot.config import ConfigError, Secrets, Topic, WebSearchSource, load_config
from newsbot.pipeline import summaries
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.prompts import SYSTEM_PROMPT, _item_payload, build_prompt
from newsbot.pipeline.summarize import summarize_topic

PALWORLD = Topic(key="palworld", name="Palworld", channel_id=1, aliases=[], entities=[])
BL4 = Topic(key="borderlands4", name="Borderlands 4", channel_id=1, aliases=[], entities=[])

COUCH_URL = "https://playstationcouch.com/post.php?id=647"
COUCH_TITLE = "Borderlands 4 Update 1.8: Release Date, Zane DLC, and Cross-Save Detailed"
ORDINARY_URL = "https://www.pcgamer.com/borderlands-4-patch-notes"


async def _no_sleep(_seconds: float) -> None:
    return None


async def _collect(results: list[dict], **source_fields) -> list[RawItem]:
    body = json.dumps({"results": results}).encode()
    source = WebSearchSource(type="web_search", queries_per_topic=1, trust="press", **source_fields)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await WebSearchCollector(source, [BL4], "brave-key", sleep=_no_sleep).collect(http)


def _result(url: str, title: str = "A result", page_age: str | None = "2026-10-28T17:09:24"):
    return {"url": url, "title": title, "description": "About it.", "page_age": page_age}


# --- Part B: exclude_domains in the collector ---


async def test_the_playstationcouch_result_is_dropped_and_the_ordinary_one_kept():
    items = await _collect(
        [
            _result(COUCH_URL, COUCH_TITLE),
            _result(ORDINARY_URL, "Borderlands 4 patch notes"),
        ]
    )

    assert [i.url for i in items] == [ORDINARY_URL]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.playstationcouch.com/post.php?id=647",
        "https://m.playstationcouch.com/post.php?id=647",
        "https://a.b.playstationcouch.com/x",
        "https://PlayStationCouch.COM/x",
        "https://playstationcouch.com./x",
        "http://playstationcouch.com:8080/x",
    ],
)
async def test_the_domain_and_its_subdomains_are_dropped(url):
    assert await _collect([_result(url)]) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://notplaystationcouch.com/x",
        "https://playstationcouch.com.evil.example/x",
        "https://example.com/playstationcouch.com",
        "https://example.com/?u=playstationcouch.com",
        "https://playstationcouch.community/x",
    ],
)
async def test_look_alikes_are_kept(url):
    assert [i.url for i in await _collect([_result(url)])] == [url]


async def test_an_empty_exclude_list_keeps_it():
    items = await _collect([_result(COUCH_URL, COUCH_TITLE)], exclude_domains=[])

    assert [i.url for i in items] == [COUCH_URL]


async def test_an_override_replaces_the_default_rather_than_adding_to_it():
    items = await _collect(
        [_result(COUCH_URL), _result("https://liars.example/a"), _result(ORDINARY_URL)],
        exclude_domains=["liars.example"],
    )

    assert [i.url for i in items] == [COUCH_URL, ORDINARY_URL]


async def test_an_unreadable_url_is_not_dropped_by_this_check():
    items = await _collect([_result("not a url at all"), _result("https://[broken/x")])

    assert [i.url for i in items] == ["not a url at all", "https://[broken/x"]


async def test_an_idn_entry_matches_the_ascii_form_of_the_host():
    items = await _collect(
        [_result("https://www.xn--bcher-kva.example/x")], exclude_domains=["bücher.example"]
    )

    assert items == []


# --- Part B: exclude_domains in config ---


_MIN = 'catalog:\n  - {key: palworld, name: "Palworld"}\n'


def _load(tmp_path: Path, web_search: str):
    path = tmp_path / "config.yaml"
    path.write_text(_MIN + web_search)
    return load_config(path)


def _secrets() -> Secrets:
    return Secrets(
        discord_token=None,
        anthropic_api_key="x",
        brave_api_key="k",
        bluesky_handle=None,
        bluesky_app_password=None,
    )


def test_the_default_list_is_playstationcouch():
    source = WebSearchSource(type="web_search", trust="press")

    assert source.exclude_domains == ["playstationcouch.com"]


def test_a_v3_block_that_does_not_set_it_gets_the_default(tmp_path):
    cfg = _load(tmp_path, "web_search: {trust: press}\n")

    assert cfg.web_search.exclude_domains == ["playstationcouch.com"]


def test_a_v3_block_override_replaces_the_default(tmp_path):
    cfg = _load(tmp_path, "web_search: {exclude_domains: [liars.example]}\n")

    assert cfg.web_search.exclude_domains == ["liars.example"]


def test_a_v3_block_can_turn_it_off(tmp_path):
    cfg = _load(tmp_path, "web_search: {exclude_domains: []}\n")

    assert cfg.web_search.exclude_domains == []


def test_the_setting_reaches_the_collector_the_bot_builds(tmp_path):
    cfg = _load(tmp_path, "web_search: {exclude_domains: [liars.example]}\n")

    (ws,) = [c for c in build_catalog_collectors(cfg, _secrets()) if c.source_type == "web_search"]

    assert ws._source.exclude_domains == ["liars.example"]


def _v2_with(tmp_path: Path, extra: str) -> Path:
    text = (Path(__file__).parent / "fixtures" / "config_v2_prodlike.yaml").read_text()
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("    queries_per_topic: 2\n    trust: press\n", extra, 1))
    return path


def test_a_v2_web_search_source_carries_it_into_the_global_block(tmp_path):
    path = _v2_with(tmp_path, "    trust: press\n    exclude_domains: [liars.example]\n")

    assert load_config(path).web_search.exclude_domains == ["liars.example"]


def test_a_v2_web_search_source_that_does_not_set_it_gets_the_default(tmp_path):
    path = _v2_with(tmp_path, "    trust: press\n")

    assert load_config(path).web_search.exclude_domains == ["playstationcouch.com"]


def test_entries_are_stripped_lowercased_and_lose_a_trailing_dot(tmp_path):
    cfg = _load(tmp_path, 'web_search: {exclude_domains: ["  Liars.Example. ", "B.Example"]}\n')

    assert cfg.web_search.exclude_domains == ["liars.example", "b.example"]


@pytest.mark.parametrize(
    "entry",
    [
        '""',
        '"   "',
        '"."',
        '"https://liars.example"',
        '"https://liars.example/"',
        '"liars.example/news"',
        '"liars.example:8080"',
        '"user@liars.example"',
        '"liars example"',
        '"liars.example?x=1"',
        '"liars.example#top"',
        '"liars..example"',
        '".liars.example"',
        '"*.liars.example"',
    ],
)
def test_an_entry_that_is_not_a_bare_hostname_is_a_config_error_naming_the_key(tmp_path, entry):
    with pytest.raises(ConfigError) as exc_info:
        _load(tmp_path, f"web_search: {{exclude_domains: [{entry}]}}\n")

    message = str(exc_info.value)
    assert "exclude_domains" in message
    assert "bare hostname" in message


def test_a_bad_entry_on_a_v2_source_names_the_key_too(tmp_path):
    path = _v2_with(tmp_path, "    trust: press\n    exclude_domains: ['https://x.example']\n")

    with pytest.raises(ConfigError, match="exclude_domains"):
        load_config(path)


# --- Part C: the prompt carries the dates ---


def _topic_item(published_at: datetime | None, **over) -> TopicItem:
    item = RawItem(
        url="https://example.com/a",
        title="A title",
        excerpt="An excerpt.",
        source_name="Feed",
        trust="press",
        published_at=published_at,
    )
    return TopicItem(item=item, topic_key="palworld", uncertain=False)


def test_the_user_message_gets_a_today_line_right_after_the_topic_line():
    _, user = build_prompt(PALWORLD, [_topic_item(None)], [], today=date(2026, 10, 29))

    lines = user.split("\n")
    assert lines[0] == "Topic: Palworld (key: palworld)"
    assert lines[1] == "Today's date: 2026-10-29"
    assert lines[2] == ""


def test_without_today_the_user_message_is_what_it_always_was():
    item = _topic_item(None)

    _, plain = build_prompt(PALWORLD, [item], [])
    _, explicit = build_prompt(PALWORLD, [item], [], today=None)

    assert plain == explicit
    assert "Today's date" not in plain
    assert plain.startswith("Topic: Palworld (key: palworld)\n\n")


def test_the_today_line_is_the_only_difference():
    item = _topic_item(None)

    _, without = build_prompt(PALWORLD, [item], [])
    _, with_today = build_prompt(PALWORLD, [item], [], today=date(2026, 10, 29))

    assert with_today.replace("Today's date: 2026-10-29\n", "", 1) == without


def test_published_is_a_utc_date():
    payload = _item_payload(0, _topic_item(datetime(2026, 10, 28, 17, 9, 24, tzinfo=UTC)))

    assert payload["published"] == "2026-10-28"


def test_published_is_the_utc_date_not_the_local_one():
    late_in_la = datetime(2026, 10, 28, 20, 30, tzinfo=timezone(timedelta(hours=-7)))

    assert _item_payload(0, _topic_item(late_in_la))["published"] == "2026-10-29"


def test_published_is_null_when_unknown():
    payload = _item_payload(0, _topic_item(None))

    assert payload["published"] is None


def test_the_payload_key_order_is_pinned():
    payload = _item_payload(0, _topic_item(None))

    assert list(payload) == [
        "n",
        "url",
        "title",
        "excerpt",
        "source",
        "trust",
        "published",
        "uncertain",
    ]


def test_the_user_message_shows_published_as_a_date_or_null():
    _, user = build_prompt(
        PALWORLD,
        [_topic_item(datetime(2026, 6, 18, 9, 0, tzinfo=UTC)), _topic_item(None)],
        [],
    )

    items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
    assert [i["published"] for i in items] == ["2026-06-18", None]


_UNCERTAIN_BULLET = (
    "- Items marked uncertain matched this topic only through a loosely related "
    "entity, not the game's name; weigh them accordingly and feel free to leave "
    "one out if it doesn't actually belong.\n"
)
_DATES_BULLET = (
    "- Each item has a published date (null when its source gave none), and today's date "
    "is given above the items. Sources sometimes get their dates wrong, so also read the "
    "item's own text: if it shows the item is old news (it calls something upcoming or new "
    "whose date is weeks or months before today, or it carries a dateline "
    "weeks or months before today), "
    "set relevant to false for a story built only on such items. This is only about news "
    "that is no longer news: a guide, a deal that's still running, or a code that hasn't "
    "expired is welcome whatever its date.\n"
)


def test_the_rule_is_in_the_system_prompt_verbatim_after_the_uncertain_bullet():
    assert _UNCERTAIN_BULLET + _DATES_BULLET in SYSTEM_PROMPT
    system, _ = build_prompt(PALWORLD, [_topic_item(None)], [])
    assert _UNCERTAIN_BULLET + _DATES_BULLET in system
    assert system.index(_DATES_BULLET) < system.index("- Write headlines and summaries")


async def test_summarize_topic_passes_today_to_the_prompt():
    llm = FakeLLM()

    await summarize_topic(llm, PALWORLD, [_topic_item(None)], [], today=date(2026, 10, 29))

    assert "Today's date: 2026-10-29\n" in llm.calls[0][1]


async def test_the_production_path_passes_the_utc_date_at_the_time_the_prompt_is_built(
    tmp_path, monkeypatch
):
    world = World(tmp_path, monkeypatch)
    world.guild(PAC, "comped", LA, ["palworld"])
    # 20:30 in Los Angeles is 03:30 the next day in UTC, so a local date would give the game away.
    end = datetime(2026, 10, 1, 20, 30, tzinfo=timezone(timedelta(hours=-7)))
    world.clock.t = end
    world.item("palworld", "raid boss patch", end - timedelta(hours=6))
    game = next(g for g in world.cfg.catalog if g.key == "palworld")

    await summaries._summarize(world.deps(), game, None)

    (_, user) = world.llm.calls[0]
    assert re.search(r"^Today's date: 2026-10-02$", user, re.MULTILINE)
    items = json.loads(user.split("<items>\n", 1)[1].split("\n</items>", 1)[0])
    assert items[0]["published"] == "2026-10-01"
