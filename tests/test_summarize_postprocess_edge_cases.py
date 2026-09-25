"""Edge cases for `newsbot.pipeline.summarize.postprocess`, beyond the implementer's tests.

`test_summarize.py` already pins the headline cases: a hallucinated URL
dropped but the story kept, a story with nothing but bad URLs dropped,
`relevant: false` dropped, the official-downgrade, and a normalized
`update_of_headline` match. This file goes after what's left: URL variants
that *look* like the same URL as something we collected but technically
aren't (canonicalization's job, re-litigated here from postprocess's side),
Unicode/punctuation edges in `update_of_headline` matching, and what happens
when the model repeats itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from newsbot.collectors.base import RawItem
from newsbot.config import Topic
from newsbot.pipeline.filter import TopicItem
from newsbot.pipeline.summarize import StoriesOut, StoryOut, postprocess
from newsbot.store.models import PriorStory

_HEADLINE_MAX = 200
_SUMMARY_MAX = 400

DIABLO4 = Topic(key="diablo4", name="Diablo IV", aliases=["Diablo 4", "D4"], entities=["Blizzard"])


def _item(url="https://real.example.com/a", *, trust="official"):
    return RawItem(
        url=url,
        title="Diablo IV update",
        excerpt="Some patch notes.",
        source_name="Blizzard News",
        trust=trust,
        published_at=None,
    )


def _topic_item(**kwargs) -> TopicItem:
    return TopicItem(item=_item(**kwargs), topic_key="diablo4", uncertain=False)


def _story(**overrides) -> StoryOut:
    defaults = dict(
        headline="Headline",
        summary="Summary.",
        label="official",
        item_urls=["https://real.example.com/a"],
        relevant=True,
    )
    defaults.update(overrides)
    return StoryOut(**defaults)


def _prior(id_, headline, created_at=None) -> PriorStory:
    return PriorStory(
        id=id_, headline=headline, created_at=created_at or datetime(2026, 9, 20, tzinfo=UTC)
    )


# --- URL variants: canonical-equivalent should match, genuinely different shouldn't ---


def test_utm_param_variant_of_a_known_url_still_matches():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["https://real.example.com/a?utm_source=twitter&utm_medium=social"])
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts) == 1
    assert drafts[0].item_urls == ["https://real.example.com/a"]


def test_uppercase_host_variant_of_a_known_url_still_matches():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["https://REAL.EXAMPLE.COM/a"])
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts) == 1


def test_trailing_slash_variant_of_a_known_url_still_matches():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["https://real.example.com/a/"])
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts) == 1


def test_http_variant_of_a_known_https_url_does_not_match():
    # canonicalize() deliberately doesn't merge http and https (see its
    # docstring); a model that "helpfully" downgrades the scheme hands us
    # a URL we never collected, and postprocess treats it as such.
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["http://real.example.com/a"])
    assert postprocess(StoriesOut(stories=[story]), items, []) == []


def test_javascript_scheme_url_is_dropped_not_crashed_on():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["javascript:alert(document.cookie)"])
    assert postprocess(StoriesOut(stories=[story]), items, []) == []


def test_javascript_url_alongside_a_real_one_only_drops_the_bad_one():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=["https://real.example.com/a", "javascript:alert(1)"])
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts) == 1
    assert drafts[0].item_urls == ["https://real.example.com/a"]


def test_empty_item_urls_to_start_with_drops_the_story():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(item_urls=[])
    assert postprocess(StoriesOut(stories=[story]), items, []) == []


def test_duplicate_url_within_one_story_is_deduped_not_repeated():
    items = [_topic_item(url="https://real.example.com/a")]
    # Same URL twice, once with tracking params -- both canonicalize to the
    # same string, and a story shouldn't cite its own source twice.
    story = _story(
        item_urls=["https://real.example.com/a", "https://real.example.com/a?utm_source=x"]
    )
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert drafts[0].item_urls == ["https://real.example.com/a"]


# --- official-label downgrade, interacting with URL filtering ---


def test_official_label_downgraded_when_its_only_official_url_was_hallucinated():
    # The item backing the "official" claim doesn't exist in our collected
    # set at all -- postprocess has to re-derive trust from what's left,
    # not from what the model claimed before filtering.
    items = [_topic_item(url="https://real.example.com/a", trust="community")]
    story = _story(
        label="official",
        item_urls=["https://real.example.com/a", "https://made-up.example.com/official-source"],
    )
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts) == 1
    assert drafts[0].label == "reported"


def test_reported_label_is_never_upgraded_even_with_an_official_item():
    # Downgrade only ever goes one direction; a story that only claims
    # "reported" doesn't get bumped up just because a linked item happens
    # to be official.
    items = [_topic_item(url="https://real.example.com/a", trust="official")]
    story = _story(label="reported", item_urls=["https://real.example.com/a"])
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert drafts[0].label == "reported"


# --- update_of_headline normalization: case, NFKC, punctuation, whitespace ---


def test_update_of_headline_matches_across_case_and_punctuation_and_whitespace():
    items = [_topic_item()]
    prior = [_prior(3, "Diablo   IV: Season 12 Launches!!")]
    story = _story(update_of_headline="diablo iv season 12 launches")
    drafts = postprocess(StoriesOut(stories=[story]), items, prior)
    assert drafts[0].update_of_story_id == 3


def test_update_of_headline_matches_across_nfkc_equivalent_accents():
    # "é" as one precomposed codepoint vs "e" + a combining acute accent --
    # visually and semantically the same character, different bytes until
    # NFKC composes them back together.
    items = [_topic_item()]
    prior = [_prior(4, "Café mod adds new recipes")]  # precomposed é
    story = _story(update_of_headline="café mod adds new recipes")  # e + combining acute
    drafts = postprocess(StoriesOut(stories=[story]), items, prior)
    assert drafts[0].update_of_story_id == 4


def test_update_of_headline_matches_across_fullwidth_nfkc_forms():
    # NFKC folds fullwidth (as seen from some CJK-locale sources) digits
    # and letters down to their ordinary ASCII forms.
    items = [_topic_item()]
    prior = [_prior(5, "Season 12 launches")]
    story = _story(update_of_headline="Ｓｅａｓｏｎ １２ ｌａｕｎｃｈｅｓ")
    drafts = postprocess(StoriesOut(stories=[story]), items, prior)
    assert drafts[0].update_of_story_id == 5


def test_update_of_headline_that_normalizes_to_empty_string_gives_none():
    items = [_topic_item()]
    prior = [_prior(6, "Season 12 launches")]
    story = _story(update_of_headline="!!! ... ???")
    drafts = postprocess(StoriesOut(stories=[story]), items, prior)
    assert drafts[0].update_of_story_id is None


def test_update_of_headline_with_no_prior_stories_at_all_gives_none():
    items = [_topic_item()]
    story = _story(update_of_headline="Some prior headline that doesn't exist")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert drafts[0].update_of_story_id is None


def test_update_of_headline_newest_wins_when_multiple_prior_match():
    # `prior` is newest-first, matching repo.recent_headlines' contract;
    # postprocess takes the first normalized match, so it must pick the
    # newest of two identically-worded prior stories, not the oldest.
    items = [_topic_item()]
    prior = [
        _prior(20, "Season 12 launches", created_at=datetime(2026, 9, 22, tzinfo=UTC)),
        _prior(10, "Season 12 launches", created_at=datetime(2026, 9, 19, tzinfo=UTC)),
    ]
    story = _story(update_of_headline="Season 12 Launches")
    drafts = postprocess(StoriesOut(stories=[story]), items, prior)
    assert drafts[0].update_of_story_id == 20


# --- duplicate stories from the model ---


def test_duplicate_stories_with_identical_headline_and_urls_both_survive():
    # Merging same-event coverage into one story is the model's job (the
    # prompt says so explicitly); postprocess doesn't second-guess it by
    # deduping stories itself. Pinning this so a future "helpful" dedupe
    # pass doesn't silently start dropping legitimately distinct stories
    # that happen to share a headline.
    items = [_topic_item(url="https://real.example.com/a")]
    story_a = _story(headline="Same headline", item_urls=["https://real.example.com/a"])
    story_b = _story(headline="Same headline", item_urls=["https://real.example.com/a"])
    drafts = postprocess(StoriesOut(stories=[story_a, story_b]), items, [])
    assert len(drafts) == 2


# --- length truncation (QA step 20, group 6b) ---
#
# StoryOut used to enforce headline/summary length with pydantic's
# max_length -- but the SDK's structured-output mode strips schema-level
# length constraints before sending the schema to the model, so a model
# response that actually ran long would fail pydantic validation with no
# way to ever succeed (a whole StoriesOut response rejected over one
# over-length field). Length limits are now enforced by truncating in
# postprocess() instead, after any max_length Field constraint was
# removed from StoryOut itself.


def test_overlong_headline_no_longer_raises_a_validation_error():
    # This is the actual bug: constructing a StoryOut with a headline
    # this long used to raise pydantic.ValidationError.
    StoryOut(
        headline="H" * 500,
        summary="short",
        label="reported",
        item_urls=["https://real.example.com/a"],
        relevant=True,
    )


def test_postprocess_truncates_an_overlong_headline():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(headline="H" * 500)
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts[0].headline) <= _HEADLINE_MAX
    assert drafts[0].headline.endswith("…")


def test_postprocess_truncates_an_overlong_summary():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(summary="S" * 1000)
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert len(drafts[0].summary) <= _SUMMARY_MAX
    assert drafts[0].summary.endswith("…")


def test_postprocess_leaves_a_short_headline_and_summary_untouched():
    items = [_topic_item(url="https://real.example.com/a")]
    story = _story(headline="Short headline", summary="Short summary.")
    drafts = postprocess(StoriesOut(stories=[story]), items, [])
    assert drafts[0].headline == "Short headline"
    assert drafts[0].summary == "Short summary."
