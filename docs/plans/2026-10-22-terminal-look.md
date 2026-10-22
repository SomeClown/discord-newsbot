# Plan: a terminal look for SHiFT alerts and `/shift` replies (v3.2.0)

- **Plan file:** `docs/plans/2026-10-22-terminal-look.md`
- **Source of truth:** `docs/design.md` §17 (approved 2026-10-22), which sits on §16 and its "As built (2026-10-09)"
- **Branch:** `feat/terminal-look` (the design is commit f3014be)
- **Gate:** `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q; echo "exit $?"`. That's about 11,840 tests in about 5 minutes. Only the 2 APScheduler xfails are expected, and the printed exit code has to be 0.
- **Rules for every agent:**
  - Comments and docstrings go in the owner's voice (CLAUDE.md, "Documentation voice"). No `--` used as a dash, and no `" -- "` in any `newsbot/` string literal (`tests/test_dash_rewrite_tripwire.py`).
  - Exactly one `AllowedMentions(everyone=True, ...)` in the codebase (`tests/test_mentions_tripwire.py`). Nothing in this plan touches mentions or pings.
  - Any user, config or scraped text in a message goes through `esc()`. **No user, config or scraped text ever goes inside an inline-code chip.** Chips hold only the fixed strings from `newsbot/bot/terminal.py`.
  - The SHiFT code stays alone in a plain ```` ``` ```` fence. Nothing else goes inside it.
  - No new dependencies. `pyproject.toml` changes only its version (Task 6).
  - Never read `.env*` (except `.env.example`), `config.dev.yaml` or anything under `data/`. Never run `docker compose ... config`. Don't run the bot. No network.
  - Workflow: each code task goes to `implement`, then `test-engineer` runs an adversarial round on that task. `qa` reviews after Task 6, and `docs` runs last.

---

## 1. Goal and approach

**What exists when this is done.** Every new SHiFT alert, continuation, roundup, follow-up and test alert, and every edit of an alert posted from v3.2.0 on, renders in the §17 look:
- a prompt chip header (`` `> new SHiFT code ▌` ``);
- the code alone in its fence;
- a `` `reward` … · `expires` … · `via` … `` line;
- a `` `src` … `` line.

`/shift codes` and `/shift history` stay paged embeds with a `#3FE37F` stripe, prompt titles, `` `[golden keys]` `` headings and the same entry layout. Alerts posted before v3.2.0 (14 days of edits at most) keep their v3.1 layout whenever they're edited. A script, `scripts/preview_shift_look.py`, renders every case through the real renderers into a Discord-dark HTML page and can screenshot it at desktop and phone widths.

**Approach: restyle the renderers only.**
- The fixed vocabulary moves into a new dependency-free module, `newsbot/bot/terminal.py`. It holds the cursor, the `prompt()` and `label()` helpers, the header and label words, group headings, page titles, the green, and `is_terminal_header()`.
- `format.py` builds everything from it.
- Delivery, edits, paging, claims, pings and the database don't change.

**How old and new records are told apart: no migration.** The stored header is a reliable signal:
- Every v3.1 header is one of a closed set of fixed strings (`**New SHiFT code(s)**`, `**New Golden Key code(s)**`, `**[TEST] …**`, `**(continued)**`), with or without a ping prefix. None of them can contain `▌`, and the headers carry no user text.
- Every v3.2 header contains `▌` followed by a closing backtick.

So `render_alert_edit` picks the terminal layout only when `"▌`"` is in the stored header, and the frozen v3.1 ("classic") layout for anything else. I chose this over a `layout` column because a column needs migration 010, a rollback story, and a backfill for rows that already say all of this.

The unknown or garbage case falls back to classic on purpose. Classic entries are strictly smaller than terminal ones (no `` `src` `` chip, shorter details labels), so a classic render always fits a message packed for either layout. The reverse doesn't hold, which is exactly §17's reason for keeping old records classic.

### The exact strings (the contract every task builds to)

**`terminal.py`**
- `CURSOR = "\u258c"` (▌, one UTF-16 unit)
- `prompt(text) = f"`> {text} {CURSOR}`"`. It raises `ValueError` if `text` contains a backtick, as a guard against anyone ever passing in non-fixed text.
- `label(word) = f"`{word}`"`
- Label words: `reward`, `expires`, `expired`, `via`, `src`, `first seen`
- `SHIFT_GREEN = 0x3FE37F`
- `is_terminal_header(header) = f"{CURSOR}`" in header`
- Group heading words: `golden keys`, `cosmetics`, `permanent`, `other`

**Alert headers (message content)**

| Case | Header |
|---|---|
| New, any | `{ping}` + `` `> {[test] }new {SHiFT\|Golden Key} code{s} ▌` `` |
| Continuation | `` `> {[test] }new SHiFT codes (continued) ▌` `` (never pinged) |
| Roundup | `` `> SHiFT roundup ▌` 7 codes from {esc(name)} · <{link}> `` (`1 code` when it's one; the count is the group's claimed size, not the message's) |
| Roundup continuation | `` `> SHiFT roundup (continued) ▌` `` |
| Follow-up | `{ping}` + `` `> confirmed by a second source ▌` `` |

- `Golden Key` is used when every code in the batch is golden.
- The plural follows the batch, as today, because the header has to be fixed before packing (it's part of the budget). A batch of 2 or more whose first message carries only one code would need an entry that fills a message alone, which the 300-unit caps make unreachable in practice. Even then, the header says "codes" and a continuation follows, which still reads right.
- `[test] ` also goes on test continuations, so a test batch can't look real halfway down.

**Details line (persisted wording: alerts, edits, roundups, follow-ups)**
- Parts joined by `" · "`:
  - `` `reward` {esc(reward)} ``
  - `` `expires` {Mon D, YYYY} (<t:N:R>) ``, or `` `expires` never `` for a permanent code
  - `` `via` {name} `` or `` `via` {a} and {b} ``
- Credits work as today: who supplied the reward and the expiry, and for a permanent code, who set the category.
- With neither a reward nor an expiry, there's no line.

**Relative wording (pages)**
- The `expires` value is `in N hours`, `in under an hour`, `tomorrow`, `Oct 14, 2026`. With `with_days`, a date reads `Oct 14 (in 5 days)`, with the year only if it's another year.
- Permanent reads `never`.
- Already passed reads `` `expired` Oct 12, 2026 `` (the label swaps). History hides it behind its marker, and `/shift codes` can't list an expired code, so this is a fallback, not a look.

**Alert entry**
```
```\nCODE\n```
[details line]
`src` {Golden Key: }{esc(source)}{ · <url>}
```
`Golden Key: ` appears only in a mixed batch, from the stored `golden_prefix` on edit. The link is still the only thing an entry sheds.

**Follow-up entry:** the fence plus its details line, with no `` `src` `` line. Entries are joined by a blank line under the header. The `` `via` `` part stays: it's part of the details line, and `src` is the separate line §17 drops.

**Roundup entry:** the fence plus its details line (as today, with the new wording).

**`/shift codes` entry**
```
```\nCODE\n```
`reward` {esc(reward) | unknown} · `expires` {value | unknown}{ · esc(origin)}
```
For a permanent code the value is `never`, and its origin (when there is one) follows as plain escaped text.

**`/shift history` entry**
```
```\nCODE\n```
[details line, relative; expiry hidden when the marker is "expired"]
`src` {esc(source) ≤100}{ · <url>}
`first seen` YYYY-MM-DD{ · marker}
```
The marker is plain text (`expired`, `from a roundup`, `old post`, `already around when alerts started`): it's a value, and chips are for labels. Shedding order is unchanged: the link, then the source name is truncated.

**Pages**
- Group headings are `` `[golden keys]` ``. A group that runs onto another page gets `` `[golden keys (continued)]` ``.
- Titles are plain text and are **not** passed through `esc()`, because `esc` turns `> ` into `\> `. They're fixed constants:
  - `> current SHiFT codes ▌`
  - `> current SHiFT codes: golden keys ▌` (and so on for each `type:`)
  - `> SHiFT code history ▌`
- `render_history_pages` loses its `title` parameter, so no dynamic title path exists.
- Every page uses `color=SHIFT_GREEN`, the empty page included. `_PALETTE` is untouched (digests use it).
- Footers are unchanged.

**Budget arithmetic**
- The new `details_reserve(names)` is `1 + 9 + 300 + 3 + 10 + 32 + 3 + 6 + L + 5 + L = 369 + 2L`:
  - `` "`reward` " `` = 9
  - `"Sep 30, 2026 (<t:" + 11 digits + ":R>)"` = 32
  - `` " · `via` " `` = 3 + 6
- Today's reserve is `364 + 2L`. The line grows 5 units and the `` `src` `` chip adds 6, so about 11 units per code in all. §17 guessed about 19. Prod's `L` is 19, so the reserve goes from 402 to 407.
- The continuation header grows from 15 to 33 or more units, and `solo_budget` already takes the larger of the two headers.
- Existing tests that pin the worst-case line at exactly the reserve (`tests/test_format_details.py` around lines 384–499 and 541) carry over with the new constants.

---

## 2. Tasks

### Task 1: Vocabulary module, a frozen classic layout, and the edit dispatch (no visible change)

**What to do**
1. **Write the pinning test first, and run it against the current code (it must pass before anything changes).** Create `tests/test_format_classic_edits.py` with literal expected strings for `render_alert_edit` on v3.1 headers. It covers:
   - headers: `@everyone **New SHiFT code**`, `<@&123> **New SHiFT codes**`, `**New Golden Key code**`, `**[TEST] New SHiFT code**`, `**(continued)**`;
   - details: none, reward only, expiry only, permanent, two different via names;
   - a mixed batch with `golden_prefix=True`;
   - the link shed when the message is over 2,000 units.

   Also pin that a message packed under the **v3.1.4 reserve, hard-coded as `364 + 2L`**, still fits a worst-case classic edit with its link.
2. Create `newsbot/bot/terminal.py` with everything listed under "terminal.py" in §1. Give it a module docstring in the owner's voice: why it's a costume, why no user text ever goes in a chip, and that the cursor doesn't blink and never will.
3. In `newsbot/bot/format.py`, add a section `# --- the v3.1 ("classic") alert layout, kept for edits of pre-3.2.0 alerts ---` holding exact, frozen copies of today's persisted pieces:
   - `_classic_alert_block` (today's `_alert_block`);
   - `_classic_details_line` (today's `details_line` with `relative=False, show_expiry=True`, including its `"details via "` wording);
   - `_classic_live_expiry`.

   Each copy takes its own local string constants, so the later tasks can change the shared ones freely. The section docstring says "DEPRECATED: delete in the first release more than 14 days after v3.2.0 is deployed; nothing can edit a pre-3.2.0 alert after that."
4. Change `render_alert_edit`: if `terminal.is_terminal_header(header)`, it renders with today's `_alert_block`/`_line_for` (which Task 2 restyles). Otherwise it uses the classic copies. Both branches keep today's link-shedding loop and `ValueError`. Docstring: the stored header is the layout marker, and why unknown means classic (the smaller of the two).
5. In `newsbot/shift/edits.py`, add one paragraph to the module docstring: records from v3.1.x are edited in their own layout, decided by the header.

**Files**
- new `newsbot/bot/terminal.py`
- `newsbot/bot/format.py` (classic section, dispatch in `render_alert_edit`)
- `newsbot/shift/edits.py` (docstring only)
- new `tests/test_format_classic_edits.py`
- new `tests/test_terminal_vocabulary.py`:
  - `prompt` refuses a backtick;
  - `is_terminal_header` is true for every new header shape (all pings, test, golden, continuation, follow-up, roundup) and false for every v3.1 shape and for `""`, `" "`, `"@everyone"`, `"▌"` with no backtick, `` "`> x`" `` with no cursor.

**Verify**
- `pytest tests/test_format_classic_edits.py tests/test_terminal_vocabulary.py tests/test_shift_edits.py tests/test_qa1_edits.py tests/test_qa2_edits.py -q` passes with **no existing test edited**.
- The gate exits 0.

### Task 2: Alerts and edits in the new look (details line, reserve, entry, headers)

**What to do**
1. Restyle `details_line`:
   - Split today's `_expiry_phrase`/`_live_expiry` into value producers: `_live_expiry_value(row) -> str | None` returns `"Oct 14, 2026 (<t:N:R>)"`, `"never"` or None. `_expiry_value(row, now, *, with_days) -> tuple[str, str] | None` returns `(label word, value)`.
   - Build the persisted and relative lines exactly as in §1, with labels from `terminal`.
   - Replace `_EXPIRY_DATE_MAX`/`_EXPIRY_PHRASE_MAX`/`_DETAIL_VIA` with `_EXPIRY_VALUE_MAX = 32` and the label-aware pieces.
   - Rewrite `details_reserve` to `369 + 2L`, built from the same constants (no hand-typed 369), and update its docstring arithmetic.
2. Keep `/shift codes` entries looking exactly as today in this task. Have `_current_entry` compose its old text (`expires …`, `permanent`, `expiry unknown`) from `_expiry_value`, so Task 4 owns that change. History's details line does change here, because it *is* `details_line(relative=True)`.
3. `_alert_block`: add the `` `src` `` label in front of the source line (Golden Key prefix after the label).
4. Headers in `render_code_alerts`:
   - Replace `_alert_title` with a terminal header builder (new, golden, test, plural by batch).
   - Replace `_CONTINUATION_HEADER` with a continuation-header function of `test`. The roundup continuation is Task 3; until then roundups keep using a local copy of the old constant, renamed `_CLASSIC_CONTINUATION`.
   - The ping prefix, `RenderedAlert.header` and `golden_prefix` work as now.
5. `render_alert_edit`'s terminal branch now draws the new blocks. No other change.
6. Update the `displayed()` docstring in `newsbot/shift/details.py` ("the word permanent" becomes "`expires` never"). Its tuple must **not** change: the restyle mustn't make any stored message due for an edit.
7. Update every test that pins today's alert, edit or details-line text to the §1 strings. Expected breakage, by the grep in §5:
   - `test_format_details.py`
   - `test_format_details_markup_adversarial.py`
   - `test_format_alert_packing.py`
   - `test_format_code_alerts.py`
   - `test_format_alert_invariants_adversarial.py`
   - `test_shift_delivery_details.py`
   - `test_shift_delivery_details_adversarial.py`
   - `test_shift_edits.py`
   - `test_shift_edits_adversarial.py`
   - `test_qa1_edits.py`
   - `test_qa1_misc.py`
   - `test_qa3_batch2_fixes.py`
   - `test_shift_fanout*.py`
   - `test_code_alert_poster*.py`
   - `test_format_code_page.py`, `test_format_shift_pages*.py`, `test_commands_shift_current*.py` (history's details line only)
   - `test_format_roundup_alerts.py`, `test_shift_followups*.py` (details-line wording only)

   Tests that call `render_alert_edit` directly with a v3.1 header literal stay as they are, since they now exercise the classic branch. Note that next to each one, and convert any whose point was "today's layout" to a terminal header.
8. Add `tests/chip_check.py`, a test helper (not a test file). `chips(text)` returns the contents of every inline-code span outside fences, treating `` \` `` as literal. `ALLOWED_CHIPS` is built from `terminal`'s constants.

**Files**
- `newsbot/bot/format.py`
- `newsbot/shift/details.py` (docstring)
- new `tests/chip_check.py`
- the test files listed above
- new `tests/test_format_terminal_alerts.py`:
  - every header shape;
  - the entry layout;
  - permanent shows `` `expires` never ``;
  - the reserve formula equals the real worst-case line plus one;
  - for every rendered alert and edit in the file, `chips()` ⊆ `ALLOWED_CHIPS`, each fence's body `is_code`, and `<t:` never appears inside a chip;
  - after a pack-then-worst-case edit, the message stays ≤ 2,000 units with the link kept;
  - delivering under v3.2.0 against a database whose records came from v3.1-shaped headers makes nothing newly due.

**Verify**
- `pytest tests/test_format_terminal_alerts.py tests/test_format_classic_edits.py tests/test_format_details.py tests/test_format_alert_packing.py tests/test_shift_edits.py tests/test_shift_delivery_details.py -q` passes.
- The gate exits 0.

### Task 3: Roundups and follow-ups in the new look

**What to do**
1. **Roundups.**
   - `_roundup_header(source_name, item_url, count, *, max_len)` builds `` `> SHiFT roundup ▌` {n} code{s} from {esc(name)} · <link> ``.
   - Shedding is unchanged: the link first, then the name is truncated. The fixed part used in the arithmetic now includes the chip and the count text.
   - The count is `len(group)` of the codes being rendered, which after a partial claim is the claimed ones.
   - Continuations use `` `> SHiFT roundup (continued) ▌` ``.
   - Remove `_ROUNDUP_HEADER_PREFIX` and `_CLASSIC_CONTINUATION`.
   - It's never pinged; the nonce scheme is unchanged.
2. **Follow-ups.**
   - `_followup_content` becomes: the header (ping prefix plus `` `> confirmed by a second source ▌` ``), a blank line, then each code's fence and its details line when it has one, with entries joined by a blank line.
   - There's no more one-liner/rows switch.
   - `fit_followup_codes`, `FOLLOWUP_MAX_CODES`, the nonce and the ping work as now. Update their docstrings (the "one short line" wording).
3. Update `tests/test_format_roundup_alerts.py`, `tests/test_shift_followups.py` and `tests/test_shift_followups_adversarial.py` (plus any fanout test that pins `SHiFT codes from a roundup` or `Confirmed by a second source`).

**Files**
- `newsbot/bot/format.py`
- the three test files above
- new `tests/test_format_terminal_roundups_followups.py`:
  - count singular/plural, and the count after a shrunken claim;
  - a 2,000-unit URL and a 100-character hostile name shed in the right order and stay ≤ 2,000;
  - a continuation never pings;
  - 20 detail-less follow-up codes fit in one message;
  - worst-case details make `fit_followup_codes` return fewer, and the rest stay queued (through `_deliver_followups`);
  - the `chips()` ⊆ `ALLOWED_CHIPS` and fence invariants hold for both.

**Verify**
- `pytest tests/test_format_terminal_roundups_followups.py tests/test_format_roundup_alerts.py tests/test_shift_followups.py tests/test_shift_followups_adversarial.py tests/test_shift_fanout.py -q` passes.
- The gate exits 0.

### Task 4: `/shift codes` and `/shift history` pages

**What to do**
1. `_page_embeds(title, descriptions, footer, empty)`:
   - `color=terminal.SHIFT_GREEN`;
   - the title goes in **unescaped**, still `_truncate_utf16`'d. The docstring says titles are fixed constants and why (`esc` turns `> ` into `\> `).
2. `render_current_pages`:
   - titles from `terminal`;
   - `_CURRENT_GROUPS` headings become the lowercase words;
   - `_pack_sections` draws headings as `` `[{h}]` `` / `` `[{h} (continued)]` `` (the format comes from `terminal`);
   - `_current_entry` uses the §1 layout (fence, `` `reward` `` / `` `expires` `` line, origin as plain text after `never`).
3. `render_history_pages`:
   - drop the `title` parameter; it always uses the history title;
   - `_code_page_block` uses the §1 layout (fence, details line, `` `src` `` line, `` `first seen` `` line with the plain marker), keeping today's shedding order and its `max_len` arithmetic over the new lines.
4. `newsbot/bot/commands.py`: the `history` command stops passing `title=`.
5. Update:
   - `tests/test_format_shift_pages.py`, `tests/test_format_shift_pages_adversarial.py` (the hostile-title tests at about lines 170 and 442 go, replaced by "the title is the constant whatever the input");
   - `tests/test_format_code_page.py`;
   - `tests/test_commands_shift_current.py` (the `embed.title == "/shift history"` assertion at about line 536);
   - `tests/test_commands_shift_current_adversarial.py`;
   - any `/shift` handler test pinning the title or `color`.

**Files**
- `newsbot/bot/format.py`
- `newsbot/bot/commands.py`
- the test files above
- new `tests/test_format_terminal_pages.py`:
  - every page of both commands is green, the empty one included;
  - exact titles for `all/golden/cosmetic/permanent` and history;
  - headings and continued headings;
  - a 300-unit reward plus a 300-unit origin entry fits a page;
  - the chip and fence invariants;
  - a history entry with a 4,000-unit URL sheds its link, then truncates its source, and stays ≤ 4,096.

**Verify**
- `pytest tests/test_format_terminal_pages.py tests/test_format_shift_pages.py tests/test_format_shift_pages_adversarial.py tests/test_commands_shift_current.py tests/test_commands_shift_current_adversarial.py tests/test_format_code_page.py -q` passes.
- The gate exits 0.

### Task 5: The local preview script

**What to do**
1. Create `scripts/preview_shift_look.py` (stdlib only), with a module docstring in the owner's voice saying it's an approximation of Discord, not Discord. It imports the real renderers and data classes (`CodeCandidate`, `CodeDetailsRow`, `CodeView`, `CurrentCode`) and uses a fixed `NOW = 2026-10-22 18:00 UTC`, so the output is deterministic.
2. `build_cases(now)` returns `(title, list of message contents or embeds)` for each case:
   1. pinged new code with details;
   2. unpinged community code, no details;
   3. all-golden pair under a role ping;
   4. a mixed batch (`Golden Key:` in `src`);
   5. a batch that continues (pass a prod-like `details_reserve(["mentalmars", "Borderlands (Bluesky)"])` and six codes);
   6. a permanent code;
   7. the worst case (a 300-unit reward, two 19-character via names, a long link);
   8. an edit, before and after (`render_alert_edit`);
   9. a pre-3.2.0 alert edited (a v3.1 header, the classic layout), so the owner sees the mixed fortnight;
   10. a roundup and its continuation;
   11. a follow-up with and without details;
   12. a test alert;
   13. `/shift codes` (all, `type: golden`, empty);
   14. `/shift history` with the `expired`, `from a roundup` and `old post` markers.
3. `to_html(cases)` converts Discord markdown with a small hand-written converter:
   - fences become `<pre>` blocks with a copy-icon glyph;
   - inline code becomes gray chips (`` \` `` stays literal);
   - backslash escapes are dropped;
   - `<t:N:R>` becomes a timestamp pill with relative text from `NOW`;
   - `<https://…>` becomes a link;
   - `@everyone` and `<@&id>` become mention pills;
   - zero-width spaces stay.

   Everything is `html.escape`d before markup is added. Embeds render as cards with a 4 px left border in the embed's color, the title, the description and the footer.

   The CSS imitates the dark theme: background `#313338`, text `#dbdee1`, chips `#2b2d31`, a `ui-monospace, Menlo, Consolas, monospace` stack. No external fonts or URLs.
4. CLI:
   - `python scripts/preview_shift_look.py [--out build/shift-look] [--screenshots]` writes `index.html` (`build/` is already gitignored).
   - `--screenshots` runs headless Chrome (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=W,H --screenshot=…`) at 1280 px (`desktop.png`) and 390 px (`phone.png`), with a tall height so it's one long page.
   - Use `subprocess.run([...], check=True)` with a list argv and a `# noqa: S603` comment explaining that the argv is fixed.
   - If Chrome isn't there, exit with a clear message rather than a traceback.
5. Add a smoke test, `tests/test_preview_script.py`. It loads the script by path (the `importlib.util.spec_from_file_location` pattern in `tests/test_load_many_guilds.py:578`) and runs `main(["--out", str(tmp_path)])`. Assert that:
   - `index.html` exists;
   - every case title is in it;
   - no raw `<t:`, no unconverted ```` ``` ```` and no `<script` is in it;
   - `--screenshots` is never invoked (patch `subprocess.run` to fail if called without the flag).

**Files:** new `scripts/preview_shift_look.py`, new `tests/test_preview_script.py`.

**Verify**
- `pytest tests/test_preview_script.py -q` passes.
- `python scripts/preview_shift_look.py --out build/shift-look` exits 0 and writes `build/shift-look/index.html`.
- The gate exits 0. Run `--screenshots` locally once; not in CI.

### Task 6: Version 3.2.0

**What to do**
- Set `version = "3.2.0"` in `pyproject.toml` and `_FALLBACK_VERSION = "3.2.0"` in `newsbot/useragent.py`.
- Then run `pip install -e . --no-deps` (venv active).

**Files:** `pyproject.toml`, `newsbot/useragent.py`.

**Verify**
- `python -c "import importlib.metadata as m; print(m.version('newsbot'))"` prints `3.2.0`. If the distribution name differs, use the one in `pyproject.toml`.
- `pytest tests/test_useragent.py tests/test_useragent_tripwire.py -q` passes.
- The gate exits 0.

### Task 7: `qa` review

**What to do.** Review the whole diff against §17's anti-goals and this plan's §1 contract:
- the copy icon copies only the code;
- no field lost versus v3.1 (reward, expiry, source, link) in any message kind;
- no ping or mention change;
- old records edit classic;
- the budgets.

Run the preview with `--screenshots` and look at `phone.png` for wrapping that breaks a line awkwardly. Fixes go back to `implement`.

**Verify.** The gate exits 0 after any fixes.

### Task 8: Docs (`docs` agent, owner's voice)

**What to do**
- `docs/design.md` §17 gets an "As built (date)" subsection:
  - the header-as-layout-marker decision and why unknown means classic;
  - the real reserve, `369 + 2L` (+11 per code, not ~19);
  - plural by batch;
  - `[test]` on continuations;
  - the `expired` label fallback;
  - history markers as plain text;
  - titles not escaped;
  - the preview script.
- `README.md`: the follow-up description (about line 272), the roundup header (about line 282), and the example details line (about line 318), which becomes `` `reward` 3 Golden Keys · `expires` Oct 14, 2026 (in 3 days) · `via` mentalmars `` with the same explanation.
- `docs/self-host.md` around line 513 (the follow-up wording).
- `CLAUDE.md`:
  - the current-plan pointer and the v3.2.0 status line (built, not yet deployed);
  - a to-do: "delete the classic alert layout in `format.py` in the first release more than 14 days after v3.2.0 deploys".
- Leave `site/privacy.html` unless the owner says otherwise (see Owner decisions).

**Files:** `docs/design.md`, `README.md`, `docs/self-host.md`, `CLAUDE.md`.

**Verify.** `rg -n "details via|Confirmed by a second source|SHiFT codes from a roundup" README.md docs/self-host.md` returns no lines describing current behavior. The gate exits 0.

---

## 3. Data / schema changes

**None.** No migration, no new column, no config key. `guild_alert_messages.header` already stores exactly what went out, and it now doubles as the layout marker (§1). `rendered_at` stamps and `displayed()` are unchanged, so deploying v3.2.0 makes no stored message due for an edit. Rollback is `TAG=3.1.4` with no config change and no database restore.

## 4. Risks and coupling

1. **Rollback mixes looks.** If v3.1.4 edits a v3.2.0 record, it draws classic blocks under the stored terminal header. That fits, because classic is smaller, so the cost is cosmetic for at most 14 days. Worth a line in the runbook.
2. **Embed titles and `> `.** Discord doesn't render block quotes in embed titles as far as I know, and the preview can't prove that. The owner checks `/shift codes` in PacketQueue right after deploy. If a client shows it oddly, the fix is changing one constant in `terminal.py`.
3. **Chips and escaping.** A reward or name with backticks is escaped to `` \` `` and so can't close a label chip. The `chips()` invariant in Tasks 2–4 is what holds this. Any new string that skips `esc()` breaks it, so test-engineer should attack it.
4. **Packing.** About 11 more units per code plus a longer continuation header, so a long batch can occasionally fit one code fewer. That's absorbed by the existing arithmetic, and Task 2's pack-then-worst-edit test proves it.
5. **The shared-file hotspot.** All of Tasks 1–4 edit `newsbot/bot/format.py`, so they must run strictly in order, not in parallel. Task 2 touches the most tests (about 20 files). If a dispatch runs long, split it: details line plus reserve first, then entries plus headers, keeping the gate green between them.
6. **Classic code is dead weight after 14 days.** It's tracked by the CLAUDE.md to-do in Task 8.
7. **The CLI's `--collect` preview** prints new-look alerts. That's expected, and nothing pins it as far as the grep shows.

## 5. Testing strategy

**Existing tests that pin today's text and will change** (from a grep for the old headers, `details via`, `Confirmed by a second source`, `Current SHiFT codes`, `First seen`, `reward unknown`, `expiry unknown` and `Golden Key: `):
- `test_shift_delivery_details_adversarial` (34 hits), `test_format_shift_pages` (29), `test_format_shift_pages_adversarial` (26), `test_format_details` (23), `test_format_details_markup_adversarial` (16)
- `test_shift_delivery_details`, `test_qa1_edits`, `test_format_alert_packing` (15 each)
- `test_commands_shift_current_adversarial` (13), `test_shift_edits_adversarial` (12)
- `test_commands_shift_current`, `test_shift_table_adversarial`, `test_format_code_alerts`, `test_shift_followups`, `test_format_roundup_alerts`, `test_format_alert_invariants_adversarial`, `test_shift_edits`
- smaller counts in `test_shift_fanout*`, `test_qa1_misc`, `test_qa3_batch2_fixes`, `test_format_code_page`, `test_code_alert_poster*` and `test_code_alert_editor`

Hits in `test_shift_official*`, `test_details_merge*`, `test_repo_code_details*`, `test_shift_details_detection*`, migration tests and fixtures are mostly source text ("Golden Keys", "expires" in Gearbox posts), not rendered output; leave them unless they fail. Tests that hand `render_alert_edit` a v3.1 header literal keep passing through the classic branch. That's intended; annotate them.

**New tests, per task:** listed in each task above.

**Adversarial cases for test-engineer**
- Rewards and source names made of `` ` ``, ` `` `, ```` ``` ````, `` `reward` ``, `▌`, `> ` at the start of a line, `\`, `@everyone`, `<t:1:R>`, `<@&1>`, bidi controls and astral emoji: `chips()` stays a subset of `ALLOWED_CHIPS`, and no chip swallows a value.
- A stored header that's terminal-looking but hand-made, one with only the cursor, one with ping text in the wrong place, and the ping-stripped terminal header: layout choice and `_ping_for` both behave.
- A worst-case new-layout edit (300-unit reward, 100-character names full of `*`, an 11-digit stamp, a 2,000-unit URL) on a message packed with the real reserve: ≤ 2,000 units, link kept.
- The same edit with the classic header on a message packed with `364 + 2L`.
- The tightness check: `discord_len(line) + 1 == details_reserve(...)` for the constructed worst line.
- Batches of 1, 2, 5, 6 and 50 codes, mixed and all-golden, pinged and not, `test=True`: header plural and kind, continuation headers, ping only on the first message, nonces unchanged from v3.1.4 for the same inputs (nonces don't hash content, so they must not move).
- Follow-ups with a role mention near its maximum length; 20 codes with no details; 20 codes with worst-case details.
- A roundup of 1 code ("1 code"), a claim shrinking a group of 7 to 3 ("3 codes"), and a roundup whose header must shed.
- Pages: every `type:`, an empty list, entries that land exactly at 4,096, a group continued across three pages, history with each marker.
- The color on every embed; the title equals the constant byte for byte.
- No `" -- "` in new literals, and the mentions tripwire still at one.

## 6. Out of scope

- ANSI color, images, embeds for alerts, and per-server style settings (§17 decisions).
- Digests, run reports, `/newsbot status`, `/news`, the lounge and owner alerts.
- Editing roundups or follow-ups.
- Any change to pings, caps, D13/D14 rules, claims, edit pacing or retention.
- Deleting the classic layout (after 14 days, per the to-do).
- A dev-bot run; the owner chose screenshots (1B).
- Release steps beyond the usual: build and check the image locally before tagging, the owner reviews `desktop.png`/`phone.png` before merge, deploy outside the 08:00 and 09:00 PT windows, then check `/shift codes` in PacketQueue.

## Owner decisions

1. **The `site/privacy.html` example.** It describes the stored header as "a title such as "New SHiFT code"". The stored data hasn't changed, only the title's wording. My recommendation is to leave the page alone, since editing it moves its "last updated" date for a cosmetic difference. Otherwise update the example to `` `> new SHiFT code ▌` `` and bump the date. Only the owner can change the policy's text.

Nothing else needs the owner; every other open point in §17's "Left to the builder" is settled in §1 above.

## Build log

Gate: `source .venv/bin/activate && ruff check . && ruff format --check . && pytest -q` (expect: only the 2 APScheduler xfails; exit code 0)
Branch: feat/terminal-look, from 1fcd04b
Work dir: /Users/someclown/dev/discord-newsbot/.git/foundry-run/2026-10-22-terminal-look
Owner decisions before Task 1: leave `site/privacy.html` alone (1A). Owner sees the preview screenshots after Task 5, before the qa review (Tasks 7 and 8 here are run-plan's qa pass and docs step).

### Progress
- Task 3: complete (from 7170941; gate exit 0, 13269 passed; spec check PASS with 2 notes; adversarial: 100 tests incl. nonce differential vs v3.1.4, 0 bugs)
- Task 2: complete (from 8c03cac; gate exit 0, 13132 passed; spec check PASS with 3 notes; adversarial: 697 tests, 0 bugs)
- Task 1: complete (from 1fcd04b; gate exit 0, 12340 passed; spec check: 2 minor gaps, accepted; adversarial: 418 tests incl. a byte-for-byte differential against v3.1.4's format.py, 0 bugs)

### Decisions made during the build
- Ruling (Task 3): `FOLLOWUP_MAX_CODES` stays 20. With a fence per code, a follow-up now fits about 14 typical codes per message (3 at worst-case details); the rest stay queued and drain on later passes, and each extra follow-up message spends one of the server's daily pings. Why: follow-ups confirm community codes one or two at a time in practice, and the owner chose fenced follow-ups (§17, 1A). If wrong: a rare big confirmation burst costs a server an extra ping or two; for the owner's final report.
- Ruling (Task 2): a lone entry now tops out at 1,997 UTF-16 units instead of 2,000, because the plain continuation header (33) is longer than the pinged single-code header (30) and `solo_budget` takes the larger, as §1 predicts. `test_batch_at_exactly_the_2000_unit_boundary_does_not_split` was retargeted to 1,997. If wrong: an entry between 1,998 and 2,000 units can't go out alone; the 300-unit caps make that unreachable in practice.
- Ruling (Task 2 spec check): the comment-only rewording in `fanout.py` and `repo.py` (outside the brief's file list) is accepted; it keeps the comments true.
- Ruling (setup): briefs cut by heading with `brief.py` in the work dir, because the plan's nested fence samples (§1) flip the Foundry `task-brief` script's fence tracking and it can't find any task. Why: same text, by heading. If wrong: none for this build; the script wants a fix in the Foundry (count fence length, or ignore fences that open mid-sample).
- Ruling (Task 1 spec check): the classic edit branch no longer raises when `details` is non-empty and `now` is None (the old `_line_for` guard). Accepted: the classic line uses the live Discord timestamp and never needs a clock, and `render_alert_edit` is typed and always called with `now`. If wrong: a caller passing `now=None` gets a classic render instead of a ValueError.
- Ruling (Task 1 spec check): `PAGE_TITLE_CURRENT` / `PAGE_TITLE_HISTORY` in `terminal.py` ratified (§1 puts page titles there); Task 4 adds the per-`type:` titles beside them.

### Parked
- Task 3 (spec check): stale comment at `newsbot/shift/fanout.py:237` ("Confirmed by a second source"); for the docs task. `tests/chip_check.py` now builds `ALLOWED_CHIPS` from the `terminal` constants, so a typo in a constant would pass the chip check; the Task 3 tests pin the literal chip strings, which covers it.
- Task 2 (adversarial, pre-existing, for the qa pass): `esc()` leaves a "1. " numbered-list start after a newline unescaped and `_detail_text` keeps newlines, so a reward holding "\n1. x" draws a list item; bidi controls are stripped from rewards but not source names (config-controlled); `escape_mentions` only defuses 17–20 digit ids (`allowed_mentions` is the backstop); a reward line starting with spaces then `> ` isn't escaped. All live in shared helpers the classic layout depends on, so changing them is a v3.1 behavior change too.
- Task 2 (spec check): comments and docstrings that still say "details via" (`newsbot/config.py:63,291`, `newsbot/shift/details.py:18,57`, the `CodeDetailsRow` docstring in `newsbot/store/models.py`). Comment-only; for the docs task.
- Task 1: the differential tests in `tests/test_terminal_task1_adversarial.py` load v3.1.4's `format.py` from commit 1fcd04b and skip when git or that commit isn't reachable (CI's shallow checkout, or after the branch is gone). Fine for now; they and the classic layout both go when the classic section is deleted.

