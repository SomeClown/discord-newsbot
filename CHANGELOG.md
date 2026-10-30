# Changelog

What changed in each release of discord-newsbot, for the people who add the
bot to a server or run their own copy. It leaves out internal refactors,
test-only work and routine dependency bumps. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The first
tag is v1.1.0; the v1 release that went to production before it was never
tagged, so the history starts there.

Each version's section stands on its own, so it can be pasted as the notes for
that version's GitHub Release. An **Upgrading** note says what an operator has
to do beyond changing `TAG`, and how to go back when that takes more than the
old tag.

## [Unreleased]

## [3.4.3] - 2026-10-29

Web search stops handing out content-farm pages: results whose picture is borrowed from Reddit or Bing are dropped.

### Fixed

- Brave's news search returns content-farm pages for small games: language-model rewrites of Reddit threads, hosted on unrelated domains that were taken over, with titles ending in the year. They name the game, so the name check from v3.4.2 keeps them, and the summary model accepted all 10 of the ones found for WARDOGS in the last two weeks as stories. Comped servers that follow WARDOGS were sent links to them. A Brave result is now dropped, for every game, when its picture is hotlinked from Reddit's image hosts (`preview.redd.it`, `external-preview.redd.it`, `i.redd.it`) or from Bing's thumbnail search (`*.mm.bing.net/th...`). Real press hosts its own pictures. On a live Brave check it caught 8 of 8 farm results and 0 of 10 real press ones.
- Brave only sends its own proxy address for a picture, with the original address encoded inside it in a format that isn't documented. If a picture's address can't be read, the result is kept. If Brave changes the format, this filter quietly stops catching farms; nothing breaks.

### Changed

- A search that drops any farm results logs one INFO line with the count, with the fields `source_name`, `topic` and `farm_dropped`. If those lines stop showing up while farm pages keep appearing, Brave probably changed its format.
- Expect lower web-search counts from `--check-sources`.
- There is no setting for this filter, and `web_search.exclude_domains: []` does not turn it off.
- Items already stored from farm pages are left to age out. Stories already posted can't be taken back.

**Upgrading**

- Nothing to do: no config change and no migration. Rollback is `TAG=3.4.2`.

## [3.4.2] - 2026-10-29

Web search results have to name the game they were found for, so a Borderlands 4 search stops filing other games' news under Borderlands 4.

### Fixed

- Brave Search results were filed under the game whose query found them, whether or not they mentioned it. A week of production data showed 25 of 50 web results under Borderlands 4 never named it (Modern Warfare 4 patch notes, Assassin's Creed IV, Tarkov, Minecraft). Comped servers' summaries threw those out, but a free server's headline list shows stored items as they are, and so do a comped server's fallback headlines. A web result now has to name its game or one of its aliases (a confident match) or its studio from `entities` (an uncertain one), or it isn't stored. A game's own feeds are unchanged and still don't have to name the game.
- A game name written with a non-breaking space or a doubled space between its words now matches, for every source; it used to be missed. A name is also checked in the title and the excerpt separately, so a title ending in "Borderlands" and an excerpt starting "4 reasons" no longer add up to Borderlands 4.
- A Brave result whose title or link isn't text is skipped, where it used to fail that game's whole web search.

### Changed

- Expect fewer, more relevant web-search headlines, and lower web-search counts from `--check-sources`.
- A game set to `match_name: false` (Rust in the example catalog) now keeps a web result only if it says one of its aliases or its studio (for Rust, "Facepunch"), so give such a game aliases or entities that show up in press text.
- Items already stored under the wrong game are left to age out; there is no cleanup.

**Upgrading**

- Nothing to do: no config change and no migration. Rollback is `TAG=3.4.1`.

## [3.4.1] - 2026-10-29

Fixes an old article showing up in a digest as if it were today's news, by blocking the site that faked its date and by telling the summary what day it is.

### Fixed

- A June article came through web search with a made-up fresh date and ended up in a comped server's digest as an "upcoming" patch. The site (playstationcouch.com) stamps every page with the moment you load it, and Brave Search passed that date along, so the 24-hour lookback believed it. Results from that site are now dropped before they're stored.
- Summaries (comped servers) now get today's date and each story's date, and drop a story whose own text shows it's weeks or months old, such as an "upcoming" date that has already passed. Guides, deals that are still running and live codes are kept whatever their date. The Gaming industry pick now sees each story's date too.

### Added

- `web_search.exclude_domains`: a list of sites whose Brave results are dropped. Each entry is a hostname like `example.com` (no `https://`, path or port), and it covers the site's subdomains too. The default is `[playstationcouch.com]`. Setting the list replaces the default, so keep that entry if you still want it blocked, and `[]` turns the check off. A bad entry is a startup error that names it.

**Upgrading**

- Nothing to do: no config change and no migration. Rollback is `TAG=3.4.0`.

## [3.4.0] - 2026-10-29

Links in digests, the Gaming industry post and `/news` show the site's name instead of the whole address, so posts read shorter on a phone.

### Changed

- Digests, the Gaming industry post and `/news` now show each source as a link with the site's name, like `pcgamer.com`, instead of the whole web address. It reads shorter on a phone. A headline keeps its text and gets the link at the end; a comped story's links read `pcgamer.com · store.steampowered.com`, with a second link to the same site numbered `pcgamer.com (2)`. SHiFT alerts, `/shift` replies and the admin run report look the same as before.
- Messages already posted keep their old links. The full address is still underneath each new link, so a very long list can move a story or headline into "+N more" a little sooner than before.
- A link whose address contains a space or `>`, or `(` or `)` in the site name, is left off these lists instead of shown. Any other `(` or `)` in an address is written `%28` or `%29`, which nearly every site reads the same.

## [3.3.1] - 2026-10-24

A security release: three ways a hostile feed or a talked-into-it model could dress text up as something it isn't, all found reviewing v3.3.0.

### Security

- An AI-written headline or summary can no longer fake a second story block in a comped digest or on `/news`. Story text is shown on one line, and a leading copy of the bot's own labels ("🟢 OFFICIAL ·" and the rest) is removed, along with any invisible characters in front of it.
- Feed text can no longer close the prompt's `<items>` or `<prior_stories>` section early: `<` is escaped inside the item data, and item titles sent to Claude are cut at 300 characters. The prompt's wording is unchanged.
- An item whose own link is a Discord invite or gift link (`discord.gg`, `dsc.gg`, `discord.me`, `discord.gift`, and `discord.com` or `discordapp.com` invite and gift pages) is dropped at collection. The check reads the address the way a browser does, so percent-encoding, odd capitals, full-width letters, dot segments and backslashes don't sneak one past.
- Such a link is never shown anywhere a stored link becomes clickable: digests, headline lists, `/news`, SHiFT alerts and `/shift history`. That covers items stored before this release. A SHiFT code from a post that linked an invite is still found and posted; only the link is left off.

### Changed

- A run of spaces inside an AI summary now shows as one space.

**Upgrading**

- Nothing to do: no config change and no migration. Rollback is `TAG=3.3.0`.

## [3.3.0] - 2026-10-24

### Added

- A "Gaming industry" catalog entry (`key: industry`) for news that isn't about
  any one game: studios closing, layoffs, acquisitions, new games and hardware,
  platform and policy changes. A server follows it like a game, in whatever
  channel it gives it. It doesn't count toward the 10-game limit.
- Four trade-press feeds feed it: GamesIndustry.biz news, Game Developer, VGC
  news and Gematsu Industry. They are RSS press feeds only (anything else on
  this entry is a startup error), and they are never keyword-matched, searched
  or shown to another game.
- A new daily pick: for each digest time, one Claude call chooses at most 5
  stories by significance. Comped servers get summaries in the model's order.
  Free servers get the same stories as the sources' own headlines and links,
  with no AI text, and never wait on a model call. The call is made even when
  no server is comped, because free servers get the pick too.
- Sources for the four thinnest games in `config.example.yaml`: FFXIV gets its
  official Bluesky account and Massively OP's tag feed; Fortnite gets Insider
  Gaming's tag feed and Epic's status page (shared, limited to Fortnite);
  VALORANT and ARC Raiders get Insider Gaming's tag feeds. New aliases:
  `Evercold` (FFXIV), `Fortnitemares` and `FNCS` (Fortnite).

### Changed

- The privacy policy, terms and project page now say that an AI picks the
  industry headlines and that Anthropic receives public news text for any
  server that follows the entry.
- Migration 010 changes the 10-game limit so it leaves `industry` out. It runs
  at startup.

**Upgrading**

- Nothing is required. Industry news is opt-in: it exists only once your
  `config.yaml` catalog has the entry. To add it, copy the block between the
  `industry-step` markers in `config.example.yaml` to the end of your
  `catalog:` (put it last, so every game keeps its place), then restart. Back up `config.yaml` first.
- Your catalog is your own copy, so the new thin-game sources reach you only if
  you copy them from `config.example.yaml` too.
- Rollback needs the config as well as the tag: restore the pre-industry
  `config.yaml` and set `TAG=3.2.2`, which refuses the industry entry. No
  database restore is needed.

## [3.2.2] - 2026-10-23

### Security

- Feed fetches check for private addresses on every hop. Before, redirects were
  followed first and the final host was checked after the request had already
  landed there. Now every URL's host (the configured one included) is checked
  before it is requested, and the address the connection actually reached is
  checked before any body is read. Redirects are followed by hand, at most 5,
  and a hop from https to http is refused. The lounge's text-file fetcher gets
  the same two checks.
- Not covered: there is no connection pinning, so a name that rebinds its DNS
  can still receive the request, though nothing it returns comes back into the
  bot. A proxy in the environment would be judged as the peer.

**Upgrading**

- No config or database change. A feed that redirects from https to http, or
  past 5 hops, is now refused. Rollback is `TAG=3.2.1`.

## [3.2.1] - 2026-10-23

### Fixed

- A burst of "confirmed by a second source" SHiFT follow-ups is now one claim,
  one ping and as many messages as it takes. The first message pings; the rest
  start with `> confirmed by a second source (continued) ▌` and never ping.
  Before, a follow-up held about 15 codes (4 in the worst case), the leftovers
  waited for a second ping, and on a day when the ping cap was spent they were
  skipped for good.

**Upgrading**

- No migration, config or command change. Rollback is `TAG=3.2.0`.

## [3.2.0] - 2026-10-22

### Changed

- SHiFT alerts, continuations, roundups, follow-ups, test alerts and edits get
  a terminal look: a `> new SHiFT code ▌` header, the code alone in its code
  block (so the copy button copies only the code), a line with the reward,
  expiry and where the details came from, and a `src` line. No color, no images, and the same look
  in every server.
- `/shift codes` and `/shift history` are still paged embeds, now with a green
  stripe, `> current SHiFT codes ▌` and `> SHiFT code history ▌` titles and
  `[golden keys]` style headings.
- Alerts posted before the upgrade keep their old layout when edited, for the
  14 days that alerts can be edited.

### Fixed

- Markdown inside a URL, or in a `[text](target)` link, is now escaped in the
  bot's messages, so scraped text can't close a code block or open one. This
  reaches every message the bot builds, the lounge's daily quote included.

**Upgrading**

- No migration, config key or command change. After deploying, check that the
  `> ` in the `/shift codes` title draws properly on desktop and phone.
- Rollback is `TAG=3.1.4`. A v3.1.4 edit of an alert posted under 3.2.0 draws
  the old blocks under the new header; it fits and is only cosmetic, for up to
  14 days.

## [3.1.4] - 2026-10-22

### Changed

- The Claude SDK moves to 2.1.0, which sends API version `2026-10-21`. That
  version refuses `temperature`, `top_p` and `top_k`, refuses documents in
  assistant turns and citations on user text, and drops some Admin API fields.
  The bot sent or called none of these after 3.1.3, so nothing about the
  summaries changes. Checked on real news before release: 14 Haiku 5.5 calls,
  none failed, no headline fallbacks.

**Upgrading**

- No config change. Rollback is `TAG=3.1.3`.

## [3.1.3] - 2026-10-22

### Changed

- Comped servers' summaries move from Claude Haiku 4.5 to Claude Haiku 5.5 at
  low effort, with no `temperature` (5.5 refuses anything but the default).
  On the same real news, measured 2026-10-22: $0.0043 against $0.0300 for
  Haiku 4.5, in about the same time. The summaries stayed closer to their
  sources, and kept some guides that Haiku 4.5 had dropped. Medium effort was
  barely different, so it stays low.
- The token cap for a summary call goes from 4096 to 16000, because 5.5 thinks
  by default and thinking counts against it.
- Spend estimates in `/newsbot status` and run reports use Haiku 5.5's prices
  ($0.10 and $0.50 per million input and output tokens).

**Upgrading**

- No config change; the model is set in code, not in `config.yaml`. Rollback is
  `TAG=3.1.2`.

## [3.1.2] - 2026-10-22

### Security

- certifi 2026.10.19 drops the `emSign ECC Root CA - G3` root, whose
  intermediate CA keys were compromised (GHSA-6q48-6c7w-9gw2, critical). The
  collectors and the lounge's quote fetchers trust certifi's bundle, so they
  were exposed to it. The Claude client and Discord's client use the system
  certificate store, and the image's Debian store never had that root.
- aiohttp 3.14.5 clears eight advisories, nearly all in its server, which the
  bot doesn't run.

**Upgrading**

- No code or config change. Rollback is `TAG=3.1.1`.

## [3.1.1] - 2026-10-21

### Added

- The owner's alert channel now hears about every server the bot joins
  (`Joined a new server: <name> (<id>), <N> members. Now in <M> servers;
  comped.`) and every removal. After a restart, it gets one summary each of
  servers that joined or left while the bot was down. Before this the bot only
  logged joins, so a new server arrived unannounced. Alerts go to
  `owner_channel_id`, falling back to `admin_channel_id`.
- Five games in the example catalog: Valheim, SCUM, DayZ, Active Matter and
  Escape from Tarkov, after ARC Raiders (21 games in all). Steam news is the
  backbone for each; Valheim also has its verified Bluesky account. The rest
  have no usable feed (JavaScript sites, or a 403 to automated requests), so
  their digests are thin between patches.
- An **Add to Discord** button with the install link and getting-started steps
  on the project page and in the README.

### Fixed

- A link or invite with a word character in front of it (`_https://`) is now
  defused like any other link in the bot's messages.

**Upgrading**

- No migration or config key change. Your catalog is your own copy of the
  example's, so the five new games appear only if you copy their blocks over.
- Rollback is `TAG=3.1.0`.

## [3.1.0] - 2026-10-19

### Added

- A `shift_table` source type reads mentalmars' Borderlands 4 SHiFT code tables
  once an hour. It's codes-only and community trust, and the page is never
  stored as news. A first listing is quiet, more than 5 new codes in a pass go
  out as an unpinged roundup, and a page with more than 500 codes fails loudly.
  The example config has it under Borderlands 4.
- Gearbox's own single-code posts are read for a plain "N Golden Keys" and an
  expiry date. The reader gives up on anything ambiguous: ranges, negation,
  past tense, a weekday that doesn't match its date, or a date before the post.
- SHiFT alerts get a details line, such as `3 Golden Keys · expires Oct 14,
  2026 (<t:…:R>) · details via mentalmars`. The expiry is a Discord timestamp,
  so its countdown never goes stale. When details arrive later, the alert is
  edited in place for 14 days. An edit repeats the original ping choice, and
  Discord doesn't notify anyone on an edit.
- `/shift history days:` is the old dated list of every code the bot has seen.
- Migration 009 adds `code_details` (reward, kind and expiry per code) and
  `guild_alert_messages` (one record per posted alert, kept 30 days).
- `NOTICE`, crediting Teren Bryson.

### Changed

- `/shift codes` is now the list of codes that should still work, grouped as
  Golden Keys, Cosmetics, Permanent and Other, with a `type:` filter. A code
  with no known expiry counts as current for 180 days.
- Dead codes never alert or ping. A typo in the table can't silence Gearbox,
  and a Gearbox sighting within 72 hours brings back a code the table silenced.
- Room for the details line is reserved under every code, so one alert message
  holds about 4 codes where it held 18.
- The license is Apache 2.0, up from MIT. Anyone who redistributes the code or
  a modified copy has to carry the `NOTICE` file. v3.0.5 and earlier stay MIT.
- The privacy page now covers the alert records and mentalmars, and the terms
  now say rewards and expiry dates are passed along as found.

**Upgrading**

- Migration 009 runs at startup and needs nothing from you.
- To get the mentalmars tables, add the `shift_table` line from
  `config.example.yaml` under `borderlands4` in your catalog (a `shift_table`
  source has to be community trust, under a game in `shift.games`).
- Rollback needs the config as well as the tag: take the `shift_table` line out
  and set `TAG=3.0.5`, which refuses that source type. No database restore is
  needed.

## [3.0.5] - 2026-10-04

### Added

- ARC Raiders joins the catalog, last in the list (16 games), with Steam news.
- `config.example.yaml` marks the blocks a deployment with its own catalog needs
  (`# >>> catalog-step: start` to `# <<< catalog-step: end`), so they can be
  copied in one go.

### Changed

- The owner's daily report lists only failing sources the current config still
  collects from. Before, sources you had dropped stayed in it, frozen at their
  last failure count.

### Removed

- The two FFXIV Lodestone feeds. From a datacenter address they answer 403 on
  IPv4 and don't connect on IPv6 (fine from a home connection). FFXIV keeps its
  Steam feed and the shared press feeds.

**Upgrading**

- If your own `config.yaml` carries the Lodestone feeds, delete those two
  lines; otherwise they will keep failing. Nothing else to do.

## [3.0.4] - 2026-10-04

### Changed

- Dependency updates only: anthropic 1.11.0, ruff and the GitHub Actions
  versions. No change to behavior or config.

## [3.0.3] - 2026-10-04

### Changed

- Reddit is for SHiFT codes only. Reddit denied the Data API request under its
  Responsible Builder Policy, which forbids sharing Reddit data without written
  approval. `r/Borderlands4` is the only Reddit source left, and it is read only
  to spot codes. Its items are never stored, so no digest, summary, `/news`
  command or prompt can show one.
- New source setting `codes_only: true` for `rss`, `steam_news` and
  `bluesky_search` sources (and shared sources). Any `reddit.com` source is
  codes-only whether or not the flag is set, and `codes_only: false` on one is a
  config error that says why.
- A Reddit source for a game outside `shift.games` is skipped with a warning at
  load.

**Upgrading**

- Check your config for Reddit sources: ones for games with no SHiFT codes are
  dropped, and `codes_only: false` on any of them stops the bot from loading
  the file.
- Reddit items stored before this version are not removed early; they age out
  with the 90-day retention. Until then they can show in `/news search` and in
  a comped server's prior-story headlines.

## [3.0.2] - 2026-10-04

### Fixed

- A server's run report has its Sources line back (the distinct sources behind
  its games, counted the way `/newsbot status` counts them, with the first
  failing source's short error), and a comped server's report has its Claude
  line back (the summaries' cost, when they were prepared, and "shared" when
  another comped server uses the same summary). Both went missing in 3.0.0.
- A comped digest's report says "stories" again, not "items"; a free digest's
  headlines still say "items", and a mixed one says "6 stories, 2 headlines".

## [3.0.1] - 2026-10-02

### Fixed

- Reddit requests go stalest first, priority subreddits included. From the
  Droplet, Reddit let roughly one request through per hourly pass, so the first
  subreddit in the config always won and another was never asked. They now take
  turns. A subreddit that gets a 429 isn't stamped as fetched, so it moves up
  the line, and the 429 logs its Retry-After.

## [3.0.0] - 2026-10-01

One public bot that any server can install, with per-server settings. The first
tag with these; the version number is a major because the config changes shape.

### Added

- Setup by slash command: `/newsbot setup`, `follow`, `unfollow`, `games`,
  `settings`, `shift`, `status`, `preview` and `run-now`. Each server picks up
  to 10 games from a catalog (15 at release), a digest time, a time zone and a
  channel per game.
- Two tiers. Free servers get headline digests and SHiFT alerts. Comped servers
  also get Claude summaries and web search, on the owner's keys. The first
  server (the friend's) is imported once and comped; `comped_guild_ids` lists
  others.
- Collection runs hourly and is shared by every server; each server gets its
  own digest at its own time, with catch-up after downtime and no item skipped
  or repeated.
- SHiFT alerts per server, with a ping the admin chooses (nobody by default, a
  role or `@everyone`). A community code that first posts without a ping gets
  one follow-up, which does ping, once a second independent source confirms it
  within 24 hours.
- Cooldowns of one `/newsbot preview` and one confirmed `run-now` per server
  per 10 minutes, and a check that the bot is really in the server before an
  admin command changes anything.
- `/owner servers` in the owner's home server (counts only).
- A server's settings are deleted when the bot is removed.
- Migrations 005 to 008.
- Top-level config keys `catalog:`, `shared_sources:`, `web_search:`,
  `home_guild_id`, `owner_channel_id` and `comped_guild_ids`, all documented in
  `config.example.yaml`.

### Changed

- `config.yaml` holds global settings and the catalog. Per-server settings
  (channels, digest time and time zone, SHiFT, lounge) live in the database
  and are set with commands.
- The lounge's `/newsbot quote-now` is now `/lounge quote-now`.
- `/news` reads stored headlines on free servers and stories on comped ones.

**Upgrading**

- A v2-shaped `config.yaml` still loads. On the first start with no servers in
  the database, the bot imports that setup once as the first server (comped)
  and logs exactly what it imported and which old keys can now be deleted.
  Migrations 005 to 008 run at startup.
- A self-hosted copy that wants Claude summaries and web search lists its own
  server in `comped_guild_ids`; any other server starts on the free tier.
- `owner_channel_id` is the owner's own alert channel and must be in
  `home_guild_id`. With it unset, owner alerts go to `admin_channel_id`, which
  is right for a single-server copy and wrong for a public one.
- The operator docs carry no v2 to v3 migration or rollback steps. If you keep
  the old config keys alongside `catalog:`, the file still loads under
  `TAG=2.2.0`, which still reads and writes the migrated tables, so going back
  needs no database restore.

## [2.2.0] - 2026-09-29

### Added

- A lounge. The bot welcomes new members with a static message from
  `config.yaml` (`{member}` and `{server}` are filled in; it mentions only the
  new member, never welcomes bots, waits for rules screening and posts at most
  once per member per 24 hours). It also posts one daily quote ("Today's pour",
  08:00 in the digest's time zone by default) from Wikiquote pages, your own
  `%`-separated file, or an https raw URL. With no sources set it uses a
  built-in list of public-domain authors. A missed day is skipped.
- `/newsbot quote-now` (admin) posts today's quote early, and asks first if
  today's already went out.
- The startup permission check covers the lounge channel.

### Fixed

- `digest.time` and the topic-key patterns accepted a trailing newline.
- Two processes opening an older database at once could race on its migration.
- A long exception no longer makes a job's crash alert vanish; the alert is one
  escaped line, capped to Discord's limit.

**Upgrading**

- Welcomes need the privileged **Server Members** intent switched on in the
  Developer Portal before you deploy with them on. With welcomes off it isn't
  needed. If it's missing, the bot says so and waits 10 minutes before exiting,
  to keep a restart loop from hitting Discord's daily login limit.
- Add a `lounge:` block to `config.yaml` to turn the lounge on (see
  `docs/deploy.md` section 18). Stay away from 07:55 to 08:05 America/Los_Angeles
  when you deploy, since a restart there can skip the day's quote.
- Rollback is `TAG=2.1.1`, with no database restore.

## [2.1.1] - 2026-09-27

### Fixed

- The admin run report shows each failing source's whole first error line. It
  was cut at 60 characters, right where an HTTP status error's URL starts;
  the cap is now 250. The URL is still defused so it isn't clickable.

## [2.1.0] - 2026-09-27

Everything a stranger needs to run their own copy.

### Added

- `docs/self-host.md`, a setup guide written after a stranger test, and
  `docs/finding-sources.md`, recipes for finding sources for your own game.
- `NEWSBOT_CONTACT` (a URL or an email) fills in the contact part of the
  User-Agent on every outbound request. Unset, the bot says so once at startup;
  a value an HTTP header can't carry is ignored with its own warning.
- `digest.subject`, which defaults to "video games", so a copy about something
  else doesn't get a prompt insisting it's about games.
- `--check-sources` checks every configured source without Claude or Discord
  keys. `--now` pins the clock so `--fixtures` dry runs are repeatable.
- `config.minimal.yaml`: one topic, three sources, alerts off.
- Images for arm64 as well as amd64. Earlier tags are amd64 only.
- `/newsbot status` shows the last sweep in local time.

### Changed

- The README is written for people running their own copy, and SHiFT alerts
  are documented as Borderlands-only.
- User-facing strings no longer use dashes.

### Removed

- The YouTube feeds from the example config. All four returned 404.

**Upgrading**

- Add `NEWSBOT_CONTACT` to `.env` and set `TAG=2.1.0`. No config change.

## [2.0.0] - 2026-09-26

Each game posts to its own channel, and SHiFT alerts get a channel of their
own. This is a breaking config change.

### Added

- One embed per game per day, in that game's channel, with coverage notes in
  the embed footer. A game with no news posts nothing.
- Publishing resumes: a retry posts only the games not yet posted, and a send
  that actually landed isn't repeated. A permanent error (403 or 404) stops the
  run and keeps the record of what posted.
- `/shift codes days:<1-90> public:<bool>` lists the known codes, newest first,
  with markers for codes that came from a roundup, are old, or were already
  around when alerts started. It pages, never pings, and exists only when
  alerts are on.
- A startup check that sends one admin alert naming each configured channel
  with missing permissions. It never blocks startup.
- The run report has a jump link for each posted game, and a publish failure
  lists posted and not-posted games.
- Migration 003 (additive): `alerted_codes.from_roundup`.

### Changed

- `topics[].channel_id` is required for every topic, and `alerts.channel_id` is
  required when alerts are on.
- Fresh roundup codes now post without a ping (at most 50 per check, with an
  admin note when more arrive).

### Removed

- `digest.channel_id`. The bot refuses to start with it present and says why.

### Fixed

- A 4xx or a cancellation during publishing could lose the ids of what had
  already posted.

**Upgrading**

- Do not restart v1.3.0 against a v2 config: v1.3.0 requires
  `digest.channel_id` and rejects `alerts.channel_id`. `docs/deploy.md`
  section 17 has the order that keeps the old config in place until the new
  image is ready.
- Create one channel per game and one for SHiFT codes. Per game channel the bot
  needs View Channel, Send Messages and Embed Links; in the SHiFT channel it
  also needs Mention @everyone, @here, and All Roles.
- Edit a copy of `config.yaml` (drop `digest.channel_id`, add each topic's
  `channel_id`, add `alerts.channel_id`), then swap it in right before
  deploying. Migration 003 runs at startup.
- Rollback: restore the v1 `config.yaml` and set `TAG=1.3.0`. No database
  restore is needed.

## [1.3.0] - 2026-09-26

### Added

- An admin run report. After every digest run that posts (the scheduled one,
  a startup catch-up or `/newsbot run-now`), the bot sends one plain-text
  message to the admin channel: posted or posted with gaps, the run type, story
  counts per game, this run's source results (the first 3 failures and "+N
  more"), the Claude cost, the duration, and a jump link to the digest.
  Previews, failed runs and skipped runs send none. It never says who ran
  `run-now`.
- A privacy policy and terms of service, published with GitHub Pages from
  `site/`.

### Changed

- The run report is on whenever `admin_channel_id` is set. Turn it off with
  `digest.report_to_admin: false`.

### Fixed

- A cancellation after the digest was saved, in the report or the SHiFT code
  check, could relabel a posted digest as failed.

## [1.2.0] - 2026-09-25

### Added

- SHiFT code alerts. An hourly sweep (no Claude, no Brave) plus a check in the
  09:00 run post any new `XXXXX-XXXXX-XXXXX-XXXXX-XXXXX` code found in
  Borderlands 4 sources to the digest channel. Each code alerts once, ever. A
  code pings `@everyone` only when an official or press source reports it;
  community-only codes post without a ping. At most 3 pings a day
  (`alerts.max_pings_per_day`), counted per Pacific day. The first sweep is
  silent, items older than 48 hours are recorded silently, and a post with more
  than 5 codes is recorded as a roundup without alerting.
- `/newsbot test-alert`, which posts a fake alert. It exists only with
  `alerts.allow_test_command: true`.

**Upgrading**

- Alerts ship disabled. To turn them on, add `alerts: {enabled: true, topics:
  [borderlands4]}` to `config.yaml` and give the bot's role **Mention
  @everyone, @here, and All Roles** in the digest channel, or the ping is
  silently dropped (`docs/deploy.md` section 15).

## [1.1.1] - 2026-09-25

### Fixed

- `/newsbot status` shows only the sources your config currently has. A source
  you removed (IGN, for one) used to stay in the list forever. A configured
  source that hasn't run yet shows as "not run yet" in the healthy count.
- `scripts/deploy.sh` reports the `TAG` pinned in `.env`.

## [1.1.0] - 2026-09-25

The first tagged release.

### Added

- `scripts/deploy.sh` for routine updates on the Droplet: it pulls the repo,
  takes a backup, refuses to run in the 09:00 to 09:15 America/Los_Angeles
  digest window (`--force` overrides it) and pulls and restarts the container.
  `--rollback TAG` deploys a specific image tag for one run.
- CI publishes semver image tags (`1.1.0`, `1.1`) for each release tag, so
  `TAG` can be pinned to a release.
- A systemd timer for the daily database backup (`deploy/systemd/`).

### Changed

- The Anthropic SDK moves from 0.x to 1.8.0 (`anthropic>=1.8,<2`).

### Removed

- IGN from the example config.

**Upgrading**

- If you run from source, reinstall with `pip install -r requirements.txt`.
  If you run the published image, pin `TAG=1.1.0`.

[Unreleased]: https://github.com/SomeClown/discord-newsbot/compare/v3.4.3...HEAD
[3.4.3]: https://github.com/SomeClown/discord-newsbot/compare/v3.4.2...v3.4.3
[3.4.2]: https://github.com/SomeClown/discord-newsbot/compare/v3.4.1...v3.4.2
[3.4.1]: https://github.com/SomeClown/discord-newsbot/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/SomeClown/discord-newsbot/compare/v3.3.1...v3.4.0
[3.3.1]: https://github.com/SomeClown/discord-newsbot/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/SomeClown/discord-newsbot/compare/v3.2.2...v3.3.0
[3.2.2]: https://github.com/SomeClown/discord-newsbot/compare/v3.2.1...v3.2.2
[3.2.1]: https://github.com/SomeClown/discord-newsbot/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/SomeClown/discord-newsbot/compare/v3.1.4...v3.2.0
[3.1.4]: https://github.com/SomeClown/discord-newsbot/compare/v3.1.3...v3.1.4
[3.1.3]: https://github.com/SomeClown/discord-newsbot/compare/v3.1.2...v3.1.3
[3.1.2]: https://github.com/SomeClown/discord-newsbot/compare/v3.1.1...v3.1.2
[3.1.1]: https://github.com/SomeClown/discord-newsbot/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.5...v3.1.0
[3.0.5]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.4...v3.0.5
[3.0.4]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.3...v3.0.4
[3.0.3]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.2...v3.0.3
[3.0.2]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.1...v3.0.2
[3.0.1]: https://github.com/SomeClown/discord-newsbot/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/SomeClown/discord-newsbot/compare/v2.2.0...v3.0.0
[2.2.0]: https://github.com/SomeClown/discord-newsbot/compare/v2.1.1...v2.2.0
[2.1.1]: https://github.com/SomeClown/discord-newsbot/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/SomeClown/discord-newsbot/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/SomeClown/discord-newsbot/compare/v1.3.0...v2.0.0
[1.3.0]: https://github.com/SomeClown/discord-newsbot/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/SomeClown/discord-newsbot/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/SomeClown/discord-newsbot/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/SomeClown/discord-newsbot/releases/tag/v1.1.0
