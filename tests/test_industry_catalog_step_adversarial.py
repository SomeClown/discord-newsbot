"""Task 9's adversarial round: the industry entry and the append production will run.

Production gets the entry with `cp config.yaml config.pre-industry.yaml`, then
`printf '\\n' >> config.yaml`, then one awk line over `config.example.yaml`, so the part
worth breaking is that append: what the extracted text looks like, what it does to files
that end in awkward ways, and what happens when it lands under a key it wasn't meant for.
The happy path (prod-like fixture plus the awk output equals the full catalog step) is
already in `test_prod_catalog_step.py`; none of that is repeated here.

Every config is built in `tmp_path`; the one fake-network test uses `httpx.MockTransport`.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from newsbot.config import ConfigError, load_check_sources_secrets, load_config
from newsbot.pipeline.run import _group_results, render_check_sources_report, run_check_sources

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "config.example.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "config_v2_prodlike.yaml"
RSS_FIXTURE = (ROOT / "tests" / "fixtures" / "feeds" / "rss20_sample.xml").read_bytes()

CATALOG_AWK = "/^# >>> catalog-step: start/{f=1;next} /^# <<< catalog-step: end/{f=0} f"
INDUSTRY_AWK = "/^# >>> industry-step: start/{f=1;next} /^# <<< industry-step: end/{f=0} f"

START = "# >>> industry-step: start"
END = "# <<< industry-step: end"

PROD_EXTRAS = """
home_guild_id: 1552824311608512532
owner_channel_id: 1555677520806944859
comped_guild_ids: [100000000000000001]
"""

# The brief's list, in its order: (name, url). All five are trust: press.
FIVE_SOURCES = [
    ("GamesIndustry.biz", "https://www.gamesindustry.biz/feed/news"),
    ("Game Developer", "https://www.gamedeveloper.com/rss.xml"),
    ("VGC", "https://www.videogameschronicle.com/category/news/feed/"),
    ("Gematsu Industry", "https://www.gematsu.com/c/industry/feed"),
    ("PC Gamer Industry", "https://www.pcgamer.com/feeds/tag/gaming-industry/"),
]


@pytest.fixture(autouse=True)
def _brave(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")


def _awk(program: str) -> str:
    if shutil.which("awk") is None:
        pytest.skip("no awk on this machine")
    return subprocess.run(  # noqa: S603
        ["awk", program, "config.example.yaml"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _without_industry(blocks: str) -> str:
    """The catalog-step text production already holds: the industry region cut out."""
    kept: list[str] = []
    skipping = False
    for line in blocks.splitlines(keepends=True):
        if line.startswith(START):
            skipping = True
        if not skipping:
            kept.append(line)
        if line.startswith(END):
            skipping = False
    return "".join(kept)


@pytest.fixture(scope="module")
def industry_text() -> str:
    out = _awk(INDUSTRY_AWK)
    assert out.strip()
    return out


@pytest.fixture(scope="module")
def prod_today() -> str:
    """Prod-like config as it stands today: v2 shape, prod extras, the 21-game catalog step."""
    blocks = _awk(CATALOG_AWK)
    return FIXTURE.read_text() + PROD_EXTRAS + "\n" + _without_industry(blocks)


def _write(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    # newline="" so a CRLF test keeps its CRLFs instead of having them translated.
    with path.open("w", newline="") as handle:
        handle.write(text)
    return path


def _append(before: str, industry: str) -> str:
    """`printf '\\n' >> config.yaml` then the awk output appended."""
    return before + "\n" + industry


# --- the markers and the extracted text ---


def test_each_industry_marker_is_a_whole_line_at_column_zero_exactly_once():
    lines = EXAMPLE.read_text().splitlines()
    assert lines.count(START) == 1
    assert lines.count(END) == 1
    # No other line may begin like a marker, or awk's prefix match would fire twice.
    assert [ln for ln in lines if ln.startswith("# >>> industry-step")] == [START]
    assert [ln for ln in lines if ln.startswith("# <<< industry-step")] == [END]


def test_the_awk_output_is_exactly_the_lines_between_the_markers(industry_text):
    lines = EXAMPLE.read_text().splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == START)
    end = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == END)
    assert industry_text == "".join(lines[start + 1 : end])
    assert START not in industry_text
    assert END not in industry_text
    assert industry_text.endswith("\n"), "an unterminated last line would glue onto the next append"


def test_the_extracted_block_carries_no_top_level_key_and_no_tab(industry_text):
    # A column-zero, non-comment line would be a new top-level key in production's file.
    top_level = [ln for ln in industry_text.splitlines() if ln and not ln[0].isspace()]
    assert top_level == []
    assert "\t" not in industry_text
    assert "\r" not in industry_text


def test_the_extracted_list_item_is_indented_like_the_catalogs_other_items(industry_text):
    # The append only works because `  - key: industry` lines up with the `  - key: tarkov`
    # above it in production's file. Take the indent from the example, not from a constant.
    example_lines = EXAMPLE.read_text().splitlines()
    catalog_at = example_lines.index("catalog:")
    other_indents = {
        len(ln) - len(ln.lstrip())
        for ln in example_lines[catalog_at + 1 :]
        if re.match(r"\s*- key: ", ln) and "industry" not in ln
    }
    industry_indents = {
        len(ln) - len(ln.lstrip())
        for ln in industry_text.splitlines()
        if re.match(r"\s*- key: ", ln)
    }
    assert len(other_indents) == 1
    assert industry_indents == other_indents


def test_the_extracted_block_is_one_catalog_entry_with_the_five_sources_exactly(industry_text):
    parsed = yaml.safe_load("catalog:\n" + industry_text)
    assert list(parsed) == ["catalog"]
    assert len(parsed["catalog"]) == 1
    entry = parsed["catalog"][0]
    assert entry["key"] == "industry"
    assert entry["name"] == "Gaming industry"
    assert entry["match_name"] is False
    assert not {"aliases", "entities", "search_queries"} & set(entry)
    assert [(s["name"], s["url"]) for s in entry["sources"]] == FIVE_SOURCES
    assert {s["type"] for s in entry["sources"]} == {"rss"}
    assert {s["trust"] for s in entry["sources"]} == {"press"}
    assert set(entry) == {"key", "name", "match_name", "sources"}


def test_the_industry_region_sits_inside_the_catalog_step_output_unchanged(industry_text):
    blocks = _awk(CATALOG_AWK)
    # The nested markers ride along as comments in the full catalog step...
    assert START in blocks and END in blocks
    # ...with exactly the industry text between them, and nothing after the entry but comments.
    between = blocks.split(START + "\n", 1)[1].split(END, 1)[0]
    assert between == industry_text
    after = blocks.split(END, 1)[1]
    assert all(not ln.strip() or ln.lstrip().startswith("#") for ln in after.splitlines())


def test_the_catalog_step_without_the_industry_region_has_no_industry_entry(prod_today):
    assert START not in prod_today
    assert END not in prod_today
    assert "key: industry" not in prod_today
    assert "Gaming industry" not in prod_today


# --- the append, against files that end in awkward ways ---


def _reference(tmp_path: Path, prod_today: str, industry: str):
    return load_config(_write(tmp_path, _append(prod_today, industry), "reference.yaml"))


TAILS = {
    "ends with a newline": "",
    "no trailing newline": "NO_NEWLINE",
    "trailing comments": "\n# kept for later\n# more notes\n",
    "trailing blank lines": "\n\n\n\n",
    "comment with no newline at the end": "\n# last words",
    "trailing spaces on a blank line": "\n   \n",
}


@pytest.mark.parametrize("tail", list(TAILS), ids=list(TAILS))
def test_appending_after_an_awkward_end_of_file_still_adds_industry_last(
    tail, tmp_path, prod_today, industry_text
):
    spec = TAILS[tail]
    if spec == "NO_NEWLINE":
        # printf '\n' finishes the last line, so this must behave like the plain case.
        before = prod_today.rstrip("\n")
    else:
        before = prod_today + spec
    cfg = load_config(_write(tmp_path, _append(before, industry_text)))
    assert [g.key for g in cfg.catalog][-1] == "industry"
    assert len(cfg.catalog) == 22
    assert cfg.model_dump() == _reference(tmp_path, prod_today, industry_text).model_dump()


def test_even_without_the_printf_a_file_with_no_final_newline_still_loads_industry_last(
    tmp_path, prod_today, industry_text
):
    # Without the printf the entry's first comment line glues onto the last source line, which
    # is still valid YAML (a trailing comment after the flow mapping). So the printf is a
    # courtesy rather than a requirement; pin that the file loads either way.
    before = prod_today.rstrip("\n")
    cfg = load_config(_write(tmp_path, before + industry_text))
    assert [g.key for g in cfg.catalog][-1] == "industry"


def test_a_crlf_file_still_gets_industry_last(tmp_path, prod_today, industry_text):
    text = _append(prod_today, industry_text).replace("\n", "\r\n")
    cfg = load_config(_write(tmp_path, text))
    assert [g.key for g in cfg.catalog][-1] == "industry"
    assert len(cfg.catalog) == 22


def test_the_backup_copy_made_before_the_append_is_the_unchanged_21_game_config(
    tmp_path, prod_today, industry_text
):
    live = _write(tmp_path, prod_today)
    backup = tmp_path / "config.pre-industry.yaml"
    shutil.copy(live, backup)  # cp config.yaml config.pre-industry.yaml
    with live.open("a") as handle:
        handle.write("\n" + industry_text)
    assert len(load_config(backup).catalog) == 21
    assert load_config(backup).catalog[-1].key == "tarkov"
    assert len(load_config(live).catalog) == 22
    # The backup is a prefix of the live file: the append only ever added to the end.
    assert live.read_text().startswith(backup.read_text())


def test_the_append_touches_nothing_but_the_catalog(tmp_path, prod_today, industry_text):
    before = load_config(_write(tmp_path, prod_today, "before.yaml"))
    after = load_config(_write(tmp_path, _append(prod_today, industry_text), "after.yaml"))
    b, a = before.model_dump(), after.model_dump()
    assert a.pop("catalog")[:-1] == b.pop("catalog")
    assert a == b


# --- running it twice, and a file that has no catalog to extend ---


def test_running_the_append_twice_is_refused_loudly(tmp_path, prod_today, industry_text):
    doubled = _append(_append(prod_today, industry_text), industry_text)
    with pytest.raises(ConfigError) as err:
        load_config(_write(tmp_path, doubled))
    message = str(err.value)
    assert "duplicate game key 'industry'" in message
    assert "the industry entry must be the last catalog entry" in message
    for name, _ in FIVE_SOURCES:
        assert f"duplicate source name '{name}'" in message


# --- the entry landing under the wrong key ---


LATER_KEYS = {
    "flow list": "comped_guild_ids: [5]\n",
    "scalar": "later: x\n",
    "block mapping": "extra_block:\n  a: 1\n",
    "lounge mapping": "lounge:\n  welcome:\n    enabled: false\n",
    "block list at column zero": "comped_guild_ids:\n- 100000000000000001\n",
    "block list indented four": "comped_guild_ids:\n    - 100000000000000001\n",
}


@pytest.mark.parametrize("later", list(LATER_KEYS), ids=list(LATER_KEYS))
def test_a_key_after_the_catalog_makes_the_append_fail_loudly_not_load_wrong(
    later, tmp_path, prod_today, industry_text
):
    text = _append(prod_today + later, industry_text)
    # PyYAML's own error or the config's, either way the validation step stops.
    with pytest.raises((ConfigError, yaml.YAMLError)):
        load_config(_write(tmp_path, text))


def test_an_integer_block_list_after_the_catalog_swallows_the_entry_and_is_refused(
    tmp_path, prod_today, industry_text
):
    # comped_guild_ids written as a block list at the catalog's own indent: the entry becomes
    # a second list item, a mapping where an id should be. pydantic says so.
    text = _append(prod_today + "comped_guild_ids:\n  - 100000000000000001\n", industry_text)
    with pytest.raises(ConfigError, match="comped_guild_ids"):
        load_config(_write(tmp_path, text))


def test_a_v2_sources_list_after_the_catalog_swallows_the_entry_and_is_refused(
    tmp_path, prod_today, industry_text
):
    text = _append(
        prod_today
        + "sources:\n  - {type: rss, name: X, url: 'https://example.com/f', trust: press}\n",
        industry_text,
    )
    with pytest.raises((ConfigError, yaml.YAMLError)):
        load_config(_write(tmp_path, text))


def test_an_unknown_key_with_a_block_list_after_the_catalog_logs_a_warning(
    tmp_path, prod_today, industry_text, caplog
):
    # The only signal the owner gets in the case below is this log line, so pin that it exists.
    text = _append(prod_today + "later:\n  - 1\n", industry_text)
    with caplog.at_level(logging.WARNING):
        load_config(_write(tmp_path, text))
    assert any("ignoring unknown top-level keys" in r.getMessage() for r in caplog.records)


# --- the entry against --check-sources, with no network ---


async def test_check_sources_for_industry_lists_exactly_the_five_sources_in_order():
    cfg = load_config(EXAMPLE)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, content=RSS_FIXTURE, headers={"content-type": "application/rss+xml"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await run_check_sources(
            cfg, load_check_sources_secrets({}), http, games=["industry"]
        )

    # Only the industry entry is counted.
    assert list(report.topic_counts) == ["industry"]
    # Its group lists the five sources, in the catalog's order, each fetched cleanly.
    groups = dict(_group_results(cfg, report.results))
    names = [r.source_name for r in groups["Gaming industry"]]
    assert names == [name for name, _ in FIVE_SOURCES]
    assert all(r.error is None for r in groups["Gaming industry"])
    # Each of the five URLs was requested once, and no other game's own source was.
    for _, url in FIVE_SOURCES:
        assert seen.count(url) == 1
    other_games_sources = {s.name for g in cfg.catalog if g.key != "industry" for s in g.sources}
    assert not other_games_sources & {r.source_name for r in report.results}
    rendered = render_check_sources_report(cfg, report, web_search_note=None)
    for name, _ in FIVE_SOURCES:
        assert name in rendered


async def test_check_sources_for_another_game_never_touches_the_industry_feeds():
    cfg = load_config(EXAMPLE)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, content=RSS_FIXTURE, headers={"content-type": "application/rss+xml"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await run_check_sources(
            cfg, load_check_sources_secrets({}), http, games=["palworld"]
        )

    assert list(report.topic_counts) == ["palworld"]
    assert not {url for _, url in FIVE_SOURCES} & set(seen)
