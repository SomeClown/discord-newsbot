"""Find SHiFT redeem codes (and Golden Key mentions) in collected text.

A SHiFT code is five groups of five letters or digits joined by hyphens --
`XXXXX-XXXXX-XXXXX-XXXXX-XXXXX` -- and Gearbox hands them out everywhere:
tweets, patch notes, a throwaway line in a Steam announcement. This module
is the one place that goes looking for that shape. It is deliberately
boring: a fixed, anchored pattern with no user-supplied regex anywhere
near it, because a redeem-code matcher is not where anyone wants to
discover what ReDoS means.

The boundary lookarounds are the part worth staring at before touching
them. A code has to stand alone -- not glued to another letter, digit, or
hyphen -- or it isn't a code, it's a fragment of something longer (a
tracking id, a hash, a sentence that happens to contain five letters and a
dash). Getting that wrong in either direction means either missing real
codes buried in a sentence or, worse, matching a piece of one that isn't a
code at all and pinging a channel for nothing.
"""

from __future__ import annotations

import re

# Five ASCII alnum groups joined by hyphens, with boundary lookarounds that
# block any adjacent Unicode letter or digit (`\W`/`\w` are Unicode-aware
# by default, which is exactly what's wanted: a code glued to "café" or a
# Cyrillic word is still glued, not standalone), any adjacent literal
# hyphen (so a six-group chain or a code embedded in a longer dash-joined
# run never matches a five-group slice of itself), and any adjacent literal
# `/` (so a URL path segment shaped like a code -- a slug such as
# `/shift-codes-early-today-guide/`, or five real groups sitting right up
# against a path separator -- doesn't match either; a query string's
# `?code=...` is unaffected, since `=` isn't blocked).
#
# Deliberately no re.IGNORECASE: the flag doesn't just relax A-Z/a-z, it
# widens what an ASCII-looking class matches to include Unicode characters
# that case-fold to one of those letters -- 'İ', 'ı', 'ſ', the Kelvin sign,
# all real bugs waiting to happen here. Both cases are already spelled out
# in the character class, so nothing is lost by leaving it off, and a
# homograph attack loses its favorite trick.
#
# Never NFKC-normalize the input before matching, either -- normalization
# is exactly what would turn a fullwidth "Ａ" or a Kelvin sign into the
# ASCII letter it's impersonating, defeating the whole point of rejecting
# them here.
CODE_RE = re.compile(
    r"(?<![^\W_])(?<!-)(?<!/)([A-Za-z0-9]{5}(?:-[A-Za-z0-9]{5}){4})(?![^\W_])(?!-)(?!/)"
)

# Real SHiFT codes are virtually always a mix of letters and digits; a
# hyphenated URL slug built entirely out of English words (the boundary
# rules above can't tell "shift-codes-early-today-guide" from five real
# groups, since both are five hyphen-joined five-letter runs) or an
# all-letter placeholder example (AAAAA-BBBBB-CCCCC-DDDDD-EEEEE, the kind
# that shows up in this very codebase's own docs and fixtures) essentially
# never contains one. Requiring at least one ASCII digit somewhere in the
# 25 characters costs almost nothing against a real code (see design.md
# §12's clarifications for the actual math) and rejects both of those at
# once, on top of (not instead of) the `/` boundary above.
_HAS_DIGIT_RE = re.compile(r"\d")

# "Golden Key", "golden keys", "#GoldenKeys", "golden\nkey" all count;
# "golden keyboard" and "gold key" don't. `[\s_-]*` (not `+`) is what makes
# "GoldenKey" itself count as a mention -- the separator between "golden"
# and "key(s)" is optional, not required.
GOLDEN_RE = re.compile(r"\bgolden[\s_-]*keys?\b", re.IGNORECASE)


def find_codes(text: str) -> list[str]:
    """Every standalone SHiFT code in `text`, uppercased, deduped, first-seen order.

    "First-seen order" matters downstream: when a batch of new codes goes
    into one alert message (A3), the order they're announced in should
    match the order they turned up in, not whatever order a set() would
    give back today.
    """
    seen: set[str] = set()
    codes: list[str] = []
    for match in CODE_RE.finditer(text):
        code = match.group(1).upper()
        if code in seen or not _HAS_DIGIT_RE.search(code):
            continue
        seen.add(code)
        codes.append(code)
    return codes


def is_code(value: str) -> bool:
    """Whether `value` is, on its own, exactly one standalone SHiFT code.

    Used to validate a code typed into `/newsbot test-alert` -- CODE_RE's
    boundary lookarounds trivially succeed at the start and end of a bare
    string (there's no character there to fail them), so this is just
    "does the whole string match the shape", case included, plus the same
    at-least-one-digit rule `find_codes` applies.
    """
    return CODE_RE.fullmatch(value) is not None and bool(_HAS_DIGIT_RE.search(value))


def mentions_golden_key(text: str) -> bool:
    """Whether `text` mentions "golden key(s)" anywhere, case-insensitive."""
    return GOLDEN_RE.search(text) is not None
