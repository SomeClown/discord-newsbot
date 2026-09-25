"""URLs in model-written text shouldn't become clickable (QA step 20, group 2).

`item_urls` is the only field `postprocess` was ever supposed to police
for URLs -- `headline` and `summary` are free text the model writes, and
nothing stopped it from writing "click http://evil.example/free-loot" into
a headline and having `format.py`'s `<url>` markup or a client's own
autolinker turn that into something clickable. A Shift-code phishing item
is exactly the shape that makes this dangerous: the code itself is wanted
in the digest (see CLAUDE.md's content policy), but a URL riding along
next to it is not.

This is layer one (strip the token before it's even stored). `test_format.
py` covers layer two (`esc()` defusing anything that slips through with a
zero-width space).
"""

from __future__ import annotations

from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import StoriesOut, StoryOut, postprocess

BORDERLANDS4 = Topic(
    key="borderlands4", name="Borderlands 4", aliases=["BL4"], entities=["Gearbox"]
)


def _item(url="https://real.example.com/a"):
    return RawItem(
        url=url,
        title="Shift code roundup",
        excerpt="Free loot codes.",
        source_name="community",
        trust="community",
        published_at=None,
    )


def _topic_item(**kwargs) -> TopicItem:
    return TopicItem(item=_item(**kwargs), topic_key="borderlands4", uncertain=False)


def _story(**overrides) -> StoryOut:
    defaults = dict(
        headline="Headline",
        summary="Summary.",
        label="reported",
        item_urls=["https://real.example.com/a"],
        relevant=True,
    )
    defaults.update(overrides)
    return StoryOut(**defaults)


def test_http_url_removed_from_headline():
    items = [_topic_item()]
    story = _story(headline="New Shift codes! Claim at http://evil.example/free-loot now")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "http://" not in drafts[0].headline
    assert "evil.example" not in drafts[0].headline
    assert "[link removed]" in drafts[0].headline


def test_https_url_removed_from_summary():
    items = [_topic_item()]
    story = _story(summary="Redeem your code at https://evil.example/steal?code=abc for a prize.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "https://" not in drafts[0].summary
    assert "evil.example" not in drafts[0].summary


def test_steam_scheme_url_removed():
    items = [_topic_item()]
    story = _story(summary="Open steam://run/12345 to install the fake tool.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "steam://" not in drafts[0].summary


def test_discord_scheme_url_removed():
    items = [_topic_item()]
    story = _story(summary="Join us at discord://invite/abc123 for free codes.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "discord://" not in drafts[0].summary


def test_bare_www_token_removed():
    items = [_topic_item()]
    story = _story(summary="Grab the code from www.evil-example.com/loot before it expires.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "www.evil-example.com" not in drafts[0].summary


def test_shift_code_scam_summary_keeps_the_code_but_drops_the_url():
    # The actual behavior worth pinning: a Shift-code item text keeps the
    # code (codes are wanted per CLAUDE.md's content policy) while a URL
    # riding along with it gets defused.
    items = [_topic_item()]
    story = _story(
        headline="New Borderlands 4 Shift code: TRICK-4CLIK-3BAIT-URLS9-9WXYZ",
        summary=(
            "Redeem code TRICK-4CLIK-3BAIT-URLS9-9WXYZ for a free legendary. "
            "Some sites are spoofing this -- only use https://evil-shift.example/redeem "
            "if you want your account stolen, otherwise redeem in-game."
        ),
    )
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    draft = drafts[0]
    assert "TRICK-4CLIK-3BAIT-URLS9-9WXYZ" in draft.headline
    assert "TRICK-4CLIK-3BAIT-URLS9-9WXYZ" in draft.summary
    assert "https://" not in draft.summary
    assert "evil-shift.example" not in draft.summary


def test_uppercase_scheme_url_is_removed():
    items = [_topic_item()]
    story = _story(summary="Redeem at HTTPS://evil.example/steal for a prize.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "HTTPS://" not in drafts[0].summary
    assert "evil.example" not in drafts[0].summary


def test_markdown_masked_link_scheme_is_removed():
    # "[legit text](https://evil.example)" -- the token regex is
    # scheme-anchored, not markdown-aware, so it matches starting at the
    # "https://" inside the parens and eats through to the next
    # whitespace (including the closing paren). The link is gone either
    # way; this pins that the scheme specifically never survives.
    items = [_topic_item()]
    story = _story(summary="See [official patch notes](https://evil.example/x) for details.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "https://" not in drafts[0].summary
    assert "evil.example" not in drafts[0].summary


def test_url_with_no_scheme_and_no_www_prefix_is_left_alone():
    # "real.example.com" with no scheme and no www prefix isn't a token
    # anything will autolink -- stripping it would just be mangling
    # ordinary prose that happens to mention a domain-shaped word.
    items = [_topic_item()]
    story = _story(summary="See real.example.com for details, not a clickable link here.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert "real.example.com" in drafts[0].summary
