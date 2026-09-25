"""Tests for newsbot.shift.match: the SHiFT code and Golden Key matchers.

This is the contract the plan calls out explicitly: what has to match,
what must never match, and that the pattern can't be turned into a
denial-of-service lever even though it looks like the kind of regex that
could be.
"""

import time

from newsbot.shift.match import find_codes, is_code, mentions_golden_key

CODE = "ABCDE-FGH12-34IJK-LMNOP-QR5ST"


# --- must match ---


def test_finds_exact_code():
    assert find_codes(CODE) == [CODE]


def test_finds_lowercase_code_and_uppercases_it():
    assert find_codes(CODE.lower()) == [CODE]


def test_finds_mixed_case_code():
    mixed = "aBcDe-FgH12-34iJk-lMnOp-Qr5sT"
    assert find_codes(mixed) == [CODE]


def test_finds_code_at_string_start():
    assert find_codes(f"{CODE} rest of the sentence") == [CODE]


def test_finds_code_at_string_end():
    assert find_codes(f"Redeem this: {CODE}") == [CODE]


def test_finds_code_surrounded_by_space():
    assert find_codes(f"code {CODE} here") == [CODE]


def test_finds_code_surrounded_by_parens():
    assert find_codes(f"code ({CODE}) here") == [CODE]


def test_finds_code_surrounded_by_quotes():
    assert find_codes(f'code "{CODE}" here') == [CODE]


def test_finds_code_surrounded_by_backticks():
    assert find_codes(f"code `{CODE}` here") == [CODE]


def test_finds_code_surrounded_by_colon_and_equals():
    assert find_codes(f"code:{CODE}") == [CODE]
    assert find_codes(f"code={CODE}") == [CODE]


def test_finds_code_surrounded_by_comma_and_period():
    assert find_codes(f"one, {CODE}, two") == [CODE]
    assert find_codes(f"code {CODE}.") == [CODE]


def test_finds_code_surrounded_by_newline():
    assert find_codes(f"line one\n{CODE}\nline two") == [CODE]


def test_finds_two_distinct_codes():
    other = "11111-22222-33333-44444-55555"
    assert find_codes(f"{CODE} and {other}") == [CODE, other]


def test_duplicate_code_returned_once_first_seen_order():
    assert find_codes(f"{CODE} again later {CODE}") == [CODE]


def test_finds_all_digit_groups():
    digits = "11111-22222-33333-44444-55555"
    assert find_codes(digits) == [digits]


# --- must NOT match ---


def test_rejects_four_five_five_five_five():
    assert find_codes("ABCD-FGH12-34IJK-LMNOP-QR5ST") == []


def test_rejects_five_five_five_five_four():
    assert find_codes("ABCDE-FGH12-34IJK-LMNOP-QR5S") == []


def test_rejects_six_char_group():
    assert find_codes("ABCDEF-GH123-34IJK-LMNOP-QR5ST") == []


def test_rejects_six_group_chain():
    assert find_codes("AAAA1-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF") == []


def test_rejects_four_group_chain():
    assert find_codes("AAAAA-BBBBB-CCCCC-DDDDD") == []


def test_rejects_code_embedded_in_longer_hyphenated_run():
    assert find_codes(f"XX-{CODE}-YY") == []


def test_rejects_adjacent_ascii_letter():
    assert find_codes(f"x{CODE}") == []
    assert find_codes(f"{CODE}x") == []


def test_rejects_adjacent_ascii_digit():
    assert find_codes(f"9{CODE}") == []
    assert find_codes(f"{CODE}9") == []


def test_rejects_adjacent_accented_letter():
    assert find_codes(f"é{CODE}") == []
    assert find_codes(f"{CODE}é") == []


def test_rejects_spaces_instead_of_hyphens():
    assert find_codes("ABCDE FGH12 34IJK LMNOP QR5ST") == []


def test_rejects_en_dash_and_em_dash_separators():
    assert find_codes("ABCDE–FGH12– 34IJK–LMNOP–QR5ST") == []
    assert find_codes("ABCDE—FGH12— 34IJK—LMNOP—QR5ST") == []


def test_rejects_non_breaking_hyphen_and_minus_sign_and_fullwidth_hyphen():
    for dash in ("‑", "−", "－"):
        assert find_codes(dash.join(["ABCDE", "FGH12", "34IJK", "LMNOP", "QR5ST"])) == []


def test_rejects_fullwidth_letters_and_digits():
    assert find_codes("ＡBCDE-FGH12-34IJK-LMNOP-QR5ST") == []
    assert find_codes("ABCDE-FGH1２-34IJK-LMNOP-QR5ST") == []


def test_rejects_cyrillic_and_greek_lookalikes():
    assert find_codes("АBCDE-FGH12-34IJK-LMNOP-QR5ST") == []  # Cyrillic А
    assert find_codes("ABCDE-FGH12-34IJK-LMNOP-QR5SО") == []  # Cyrillic О
    assert find_codes("ABCDE-FGH12-34IJK-LMNOP-QR5SΟ") == []  # Greek Ο


def test_rejects_kelvin_sign_long_s_dotless_i_superscript_two():
    assert find_codes("KBCDE-FGH12-34IJK-LMNOP-QR5ST") == []  # Kelvin sign
    assert find_codes("ſBCDE-FGH12-34IJK-LMNOP-QR5ST") == []  # long s
    assert find_codes("ıBCDE-FGH12-34IJK-LMNOP-QR5ST") == []  # dotless i
    assert find_codes("ABCDE-FGH12-34IJK-LMNOP-QR5S²") == []  # superscript two


def test_rejects_arabic_indic_digits():
    assert find_codes("١BCDE-FGH12-34IJK-LMNOP-QR5ST") == []


def test_rejects_zero_width_space_inside():
    assert find_codes("ABCDE-FGH1​2-34IJK-LMNOP-QR5ST") == []


def test_rejects_soft_hyphen_inside():
    assert find_codes("ABCDE-FGH1­2-34IJK-LMNOP-QR5ST") == []


# --- QA item 6: `/` boundary and the at-least-one-digit rule ---


def test_rejects_code_preceded_by_slash():
    assert find_codes(f"path/{CODE}") == []


def test_rejects_code_followed_by_slash():
    assert find_codes(f"{CODE}/path") == []


def test_rejects_code_between_two_slashes():
    assert find_codes(f"https://example.com/redeem/{CODE}/confirm") == []


def test_rejects_all_letter_five_by_five_no_digit():
    assert find_codes("AAAAA-BBBBB-CCCCC-DDDDD-EEEEE") == []


def test_rejects_url_slug_shaped_like_a_code_no_digit_no_slash():
    # Five hyphen-joined, five-letter English words -- exactly the shape
    # CODE_RE's boundary rules alone can't distinguish from five real
    # groups. Both example slugs from the QA report.
    assert find_codes("https://example.com/shift-codes-early-today-guide/") == []
    assert find_codes("shift-codes-early-today-guide") == []


def test_all_letter_five_by_five_is_not_a_code():
    assert is_code("AAAAA-BBBBB-CCCCC-DDDDD-EEEEE") is False


def test_a_code_with_a_digit_still_matches_with_slash_boundary_in_effect():
    # The `/` boundary and the digit rule are both new restrictions, not
    # a regression against ordinary text -- a real code sitting in an
    # ordinary sentence (no slash touching it) still matches.
    assert find_codes(f"Redeem this code: {CODE} today") == [CODE]


# --- is_code ---


def test_is_code_true_for_exact_code():
    assert is_code(CODE) is True


def test_is_code_false_for_garbage():
    assert is_code("not-a-code") is False
    assert is_code(f" {CODE} ") is False
    assert is_code(f"{CODE}-EXTRA") is False


# --- golden key mentions ---


def test_golden_key_plural_capitalized():
    assert mentions_golden_key("Grab your Golden Keys now") is True


def test_golden_key_all_caps_singular():
    assert mentions_golden_key("GOLDEN KEY incoming") is True


def test_golden_key_newline_between_words():
    assert mentions_golden_key("golden\nkey drop") is True


def test_golden_key_hashtag_no_space():
    assert mentions_golden_key("#GoldenKeys are here") is True


def test_golden_keyboard_is_not_a_golden_key():
    assert mentions_golden_key("check out this golden keyboard") is False


def test_gold_key_is_not_golden_key():
    assert mentions_golden_key("here's a gold key for the vault") is False


# --- ReDoS sanity ---


def test_find_codes_stays_fast_on_a_megabyte_of_near_miss_dashes():
    text = "AAAAA-" * (1024 * 1024 // 6)
    start = time.monotonic()
    find_codes(text)
    assert time.monotonic() - start < 1.0


def test_find_codes_stays_fast_on_a_megabyte_of_single_char_dashes():
    text = "A-" * (1024 * 1024 // 2)
    start = time.monotonic()
    find_codes(text)
    assert time.monotonic() - start < 1.0
