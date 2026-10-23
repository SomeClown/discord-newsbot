"""Smoke test for `scripts/preview_shift_look.py`, the local preview of the terminal look.

It's a developer's toy, so this only checks that it keeps working and keeps its promises:
every case lands on the page, nothing is left unconverted, nothing on the page can run or
load, and the test never starts Chrome. The page's looks are for a human to judge.
"""

from __future__ import annotations

import html
import importlib.util
import subprocess
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).parent.parent / "scripts" / "preview_shift_look.py"
    spec = importlib.util.spec_from_file_location("preview_shift_look", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load_script()


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    """Nothing in here gets to start a process; a test that wants to see the argv fakes `run`."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"the preview script ran a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)


def test_the_page_has_every_case_and_nothing_unconverted(script, tmp_path):
    assert script.main(["--out", str(tmp_path)]) == 0
    page = (tmp_path / "index.html").read_text(encoding="utf-8")

    for title, _ in script.build_cases(script.NOW):
        assert html.escape(title) in page
    assert "<t:" not in page
    assert "```" not in page
    assert "<script" not in page
    # Nothing to fetch, either: no stylesheet, font, image or import from anywhere.
    for loader in ("<link", "<img", "<iframe", "@import", "url(", "src="):
        assert loader not in page
    # And it says what it is.
    assert "approximation of Discord, not Discord" in page


def test_the_output_is_the_same_every_run(script, tmp_path):
    assert script.to_html(script.build_cases(script.NOW)) == script.to_html(
        script.build_cases(script.NOW)
    )


def test_markdown_becomes_chips_pills_and_links_after_escaping(script):
    in_six_days = int(script.NOW.timestamp()) + 6 * 86400
    out = script.markdown_to_html(
        f"@everyone `> new SHiFT code ▌` <@&123> \\` ended <t:{in_six_days}:R> "
        "<https://example.com/a?b=1&c=2> **bold** <script>x</script>\n"
        "```\nAAAAA-BBBBB-CCCCC-DDDDD-EEEEE\n```"
    )
    assert '<span class="pill mention">@everyone</span>' in out
    assert '<span class="pill mention">@role</span>' in out
    assert '<code class="chip">&gt; new SHiFT code ▌</code>' in out
    assert "in 6 days" in out
    assert 'href="https://example.com/a?b=1&amp;c=2"' in out
    assert "<strong>bold</strong>" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<pre>" in out
    # The escaped backtick is a literal one and doesn't open a chip.
    assert " ` ended" in out


def test_a_zero_width_space_survives_the_converter(script):
    assert "@​everyone" in script.markdown_to_html("@​everyone")


def test_screenshots_without_chrome_exit_with_a_message(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CHROME", tmp_path / "nope")
    with pytest.raises(SystemExit, match="Chrome isn't at"):
        script.main(["--out", str(tmp_path), "--screenshots"])


def test_screenshots_ask_chrome_for_both_widths(script, tmp_path, monkeypatch):
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setattr(script, "CHROME", fake_chrome)
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", record)
    out = tmp_path / "out"
    assert script.main(["--out", str(out), "--screenshots"]) == 0

    assert [c[0] for c in calls] == [str(fake_chrome)] * 2
    for argv, name, width in zip(calls, ("desktop.png", "phone.png"), (1280, 390), strict=True):
        assert "--headless=new" in argv
        assert "--hide-scrollbars" in argv
        assert "--force-device-scale-factor=2" in argv
        assert any(a.startswith(f"--window-size={width},") for a in argv)
        assert f"--screenshot={out / name}" in argv
        assert argv[-1].startswith("file://")
