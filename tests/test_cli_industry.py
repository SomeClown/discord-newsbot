"""The owner's `/newsbot preview` path for the industry entry, run offline through `main`.

Task 10 of the industry plan has the owner run a real preview on a comped scratch server
and then a free one. This is that recipe with the network and the model taken out: a hybrid
config (the v3 catalog with the industry entry, plus a v2 `topics:` block so the one-time
import makes the server comped), fixture feeds, and a stub model with canned stories.

The two things worth watching are the ones that went wrong on paper first. A comped server
gets the first five of the model's ranked stories, in the model's order, and a general-feed
headline that merely says "Gaming industry" never lands under industry. And once the
server is free, a preview shows the stored pick as plain source headlines without asking
the model again, which the second stub enforces by failing the test if it is asked.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from newsbot.pipeline import run as run_module
from newsbot.pipeline.run import main
from newsbot.store import repo
from newsbot.store.db import connect

CONFIG_V3_INDUSTRY = Path(__file__).parent / "fixtures" / "config_v3_industry.yaml"
NOW_ISO = "2026-10-23T16:00:00Z"  # 09:00 Pacific: the digest is due
GUILD = 123456789012345678
PAL_CHANNEL = 111111111111111111
INDUSTRY_CHANNEL = 111111111111111112
GENERAL_TITLE = "Gaming industry layoffs hit Palworld studio Pocketpair"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _industry_item(n: int) -> dict:
    return {
        "url": f"https://industry.example.com/story-{n}",
        "title": f"Source headline {n}",
        "excerpt": f"Trade press excerpt {n}.",
        "source_name": "Industry Feed A",
        "trust": "press",
        "published_at": f"2026-10-23T{15 - n:02d}:00:00+00:00",
        "topics": ["industry"],
    }


def _story(headline: str, summary: str, urls: list[str]) -> dict:
    return {
        "headline": headline,
        "summary": summary,
        "label": "reported",
        "item_urls": urls,
        "relevant": True,
        "update_of_headline": None,
    }


@pytest.fixture
def scenario(tmp_path):
    """A hybrid config, a fixtures dir with `llm.json` in it, and a database path."""
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "industry.json").write_text(json.dumps([_industry_item(n) for n in range(1, 8)]))
    (feeds / "general.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://general.example.com/pocketpair-layoffs",
                    "title": GENERAL_TITLE,
                    "excerpt": "Pocketpair is cutting staff.",
                    "source_name": "General Feed",
                    "trust": "press",
                    "published_at": "2026-10-23T14:30:00+00:00",
                    "topics": None,
                }
            ]
        )
    )
    (feeds / "palworld.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://example.com/palworld/patch",
                    "title": "Palworld v0.7 Patch Notes",
                    "excerpt": "Fixes several crash issues.",
                    "source_name": "Palworld Steam",
                    "trust": "official",
                    "published_at": "2026-10-23T13:00:00+00:00",
                    "topics": ["palworld"],
                }
            ]
        )
    )
    # Seven industry stories in the model's order, which is not the feed's order on purpose:
    # the digest has to keep the model's ranking and cut at five.
    ranked = [6, 2, 7, 1, 5, 3, 4]
    llm = {
        "palworld": [
            _story(
                "Stub Palworld headline",
                "Stub Palworld summary.",
                ["https://example.com/palworld/patch"],
            )
        ],
        "industry": [
            _story(
                f"Stub industry headline {n}",
                f"Stub industry summary {n}.",
                [f"https://industry.example.com/story-{n}"],
            )
            for n in ranked
        ],
    }
    (feeds / "llm.json").write_text(json.dumps(llm))

    hybrid = (
        f"guild_id: {GUILD}\n"
        "digest: {time: '09:00', timezone: 'America/Los_Angeles', lookback_hours: 24,"
        " max_items_per_topic: 60}\n"
        "topics:\n"
        f'  - {{key: palworld, name: "Palworld", channel_id: {PAL_CHANNEL}}}\n'
        f'  - {{key: industry, name: "Gaming industry", channel_id: {INDUSTRY_CHANNEL}}}\n'
    ) + CONFIG_V3_INDUSTRY.read_text()
    config = tmp_path / "config.yaml"
    config.write_text(hybrid)
    return {"config": config, "feeds": feeds, "db": str(tmp_path / "t.db"), "ranked": ranked}


def _args(s, *extra):
    return [
        "--config",
        str(s["config"]),
        "--db",
        s["db"],
        "--fixtures",
        str(s["feeds"]),
        "--stub-llm",
        str(s["feeds"] / "llm.json"),
        "--now",
        NOW_ISO,
        *extra,
    ]


def _industry_section(out: str) -> str:
    marker = f"--- #{INDUSTRY_CHANNEL} · Gaming industry ---"
    assert marker in out
    return out.split(marker, 1)[1]


def test_a_comped_preview_shows_palworld_then_the_first_five_ranked_industry_stories(
    scenario, capsys
):
    assert main(_args(scenario, "--dry-run")) == 0
    out = capsys.readouterr().out

    pal_at = out.index(f"--- #{PAL_CHANNEL} · Palworld ---")
    industry_at = out.index(f"--- #{INDUSTRY_CHANNEL} · Gaming industry ---")
    assert pal_at < industry_at
    assert "Stub Palworld headline" in out[pal_at:industry_at]

    section = _industry_section(out)
    shown = [n for n in scenario["ranked"] if f"Stub industry headline {n}" in section]
    assert shown == scenario["ranked"][:5]
    positions = [section.index(f"Stub industry headline {n}") for n in shown]
    assert positions == sorted(positions)
    for n in scenario["ranked"][5:]:
        assert f"Stub industry headline {n}" not in out
    assert "more" not in section  # no "+N more" tail on the industry post
    assert GENERAL_TITLE not in section
    assert "general.example.com" not in section


def test_a_forced_run_saves_the_pick_and_a_free_preview_then_shows_source_headlines(
    scenario, capsys, monkeypatch
):
    assert main(_args(scenario, "--post-to-stdout", "--force")) == 0
    capsys.readouterr()

    with closing(connect(scenario["db"])) as conn:
        saved = conn.execute(
            "SELECT COUNT(*) FROM game_summaries WHERE game_key = 'industry'"
        ).fetchone()[0]
        assert saved >= 1
        assert repo.get_guild(conn, GUILD).tier == "comped"

    # The owner's second step: the same server, now free.
    with closing(sqlite3.connect(scenario["db"])) as raw:
        raw.execute("UPDATE guilds SET tier = 'free' WHERE guild_id = ?", (GUILD,))
        raw.commit()

    real_stub = run_module.StubLLM

    class IndustryTrap(real_stub):
        async def emit_stories(self, system, user):
            if "(key: industry)" in user:
                pytest.fail("the model was asked for the industry pick on a free preview")
            return await super().emit_stories(system, user)

    monkeypatch.setattr(run_module, "StubLLM", IndustryTrap)

    assert main(_args(scenario, "--dry-run")) == 0
    out = capsys.readouterr().out
    section = _industry_section(out)

    # The stored pick, as the source's own titles, in the model's order.
    shown = [n for n in scenario["ranked"] if f"Source headline {n}" in section]
    assert shown == scenario["ranked"][:5]
    positions = [section.index(f"Source headline {n}") for n in shown]
    assert positions == sorted(positions)
    assert "Stub industry" not in out
    assert "Stub Palworld" not in out  # a free server gets headlines for games too
    # The general item is Palworld news by keyword, and only Palworld news.
    assert GENERAL_TITLE in out.split(f"--- #{INDUSTRY_CHANNEL}", 1)[0]
    assert GENERAL_TITLE not in section
