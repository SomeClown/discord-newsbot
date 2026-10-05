# quotes/

`rage-quit-tavern.txt` is Rage Quit Tavern's own list for the lounge's daily quote ("Today's pour", 08:00 Pacific). It replaces the eight Wikiquote pages the lounge started with. It's not the bot's default list and nothing in the code or `config.example.yaml` points at it; it's one server's bar playlist, kept in the repo so it has a history.

It holds 362 quotes. The brief said about 400. Everything is filled except Jack Handey, who has 12 of his 50: Wikiquote has no Deep Thoughts page, and his sourced quotes are spread thin across a handful of topic pages. I'd rather ship a short list than a padded one. The numbers and the reasoning are at the top of `rage-quit-tavern-review.md`, which is also where to skim every quote with its page, its full original citation and its tags.

## The rules

- **Sourced only.** Every quote went through `newsbot/lounge/wikiquote.py`, which skips Disputed, Misattributed, "Quotes about" and the other sections Wikiquote doesn't vouch for. No misattribution, ever; that one is long-standing.
- **The tone filter.** Each quote has humor, irreverent humor (Adams, Python), snark (Thompson, Mencken) or an irreverent pop-culture reference (Fight Club). No slurs, nothing built on mocking people for who they are, nothing cruel instead of funny. The borderline ones are marked "owner check" in the review file.
- **Mencken is the exception.** His quotes stay whatever their tone, at the owner's request.
- **Verbatim.** Quote text is as Wikiquote gives it, apart from whitespace and footnote markers like `[1]`.
- **No duplicates**, including the same line from two pages.

## Adding or removing a quote

The file is in the `fortune` format: entries separated by a line that is just `%`. Each entry is the quote, then an attribution line as its last line:

```
Space is big. Really big.
~ Douglas Adams, The Hitchhiker's Guide to the Galaxy, Chapter 8
%
```

- The attribution line starts with `~ ` (the lounge's `ATTRIBUTION_PREFIX`), so the post looks the way a Wikiquote quote does. Without it the attribution would just be more quote.
- Keep the attribution short: author or speaker, then the work, then the year if the source gives one (`~ Hunter S. Thompson, The Proud Highway (1997)`). No chapters, page numbers or ISBNs. Don't name a work the quote didn't come from; if all you know is the author, the author alone is correct.
- Plain titles, no asterisks for italics. The bot escapes asterisks, so `*Title*` would post with the asterisks showing.
- Multi-line dialogue is fine if it's short and the exchange is the joke. Keep each speaker's name at the start of their line.
- There are no comments in the file. The fortune format doesn't have any, so a comment would be posted as a quote, which is a lot of ways to ruin a Tuesday.
- To remove one, delete the entry (its lines and the `%` after it).
- A rendered message has to fit in 2,000 UTF-16 units (header, escaped text and attribution); the bot drops an entry that doesn't. There is no shorter limit for a file entry, and the longest quotes here (a few Python sketches) run to about 1,750. `tests/test_curated_quotes.py` fails if one is too long, if two are the same, or if one is missing its attribution line, so run `pytest -q tests/test_curated_quotes.py` after editing.
- Anything new has to pass the rules above. If it came from Wikiquote, run it through the parser first.

Two quirks of a plain file, neither worth a code change:

- **No link line.** A Wikiquote quote posts a "From Wikiquote:" link under the attribution. A file entry can't, because the escaping that protects the channel from stray links also breaks them.
- **The tilde.** The bot escapes the whole entry, so the `~` posts as `\~`, which Discord draws as a plain `~`. It looks the same on screen.

## How it reaches production

The file doesn't ship in the image; the Droplet reads it from its `data/` folder. In `docker-compose.prod.yml`, `./data` is mounted at `/data` inside the container, so the lounge config needs the in-container absolute path. (A relative `file:` path is resolved against the config file's folder, which is inside the read-only image, so it won't work.)

1. Copy the file to the Droplet as `/opt/newsbot/data/rage-quit-tavern-quotes.txt`.
2. Make it readable by the container's user: `sudo chown 10001:10001 /opt/newsbot/data/rage-quit-tavern-quotes.txt`.
3. In the Droplet's `config.yaml`, point the lounge's quote sources at it, replacing the Wikiquote entries:

   ```yaml
   lounge:
     daily_quote:
       sources:
         - file: /data/rage-quit-tavern-quotes.txt
   ```

   (Only the `sources:` list changes; the rest of the `lounge:` block stays as it is.) Edit a copy, validate it with `load_config` from a checkout, then move it into place, the same way every other config change goes.
4. Restart the bot, but not between 09:00 and 09:15 Pacific, and avoid about 07:55 to 08:05, which is quote time.

The file is read fresh each time the source is picked, so later edits to `data/rage-quit-tavern-quotes.txt` take effect without a restart. A new file source starts with a fresh no-repeat deck, so the lounge won't remember the quotes it has already used from the Wikiquote pages. Every quote here is fair game again on its first lap.
