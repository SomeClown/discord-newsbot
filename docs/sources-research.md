# Seed source research (plan step 12)

Verified 2026-09-23 from a residential macOS connection. Every URL below was fetched live. "Newest" is the date of the newest entry in the response. Nothing here was run from a DigitalOcean IP, so Reddit behaviour in prod is still unknown.

## Findings

**Official RSS, per company**

- **Blizzard / Diablo IV: news.blizzard.com has no RSS.** All of `/rss`, `/feed`, `/en-us/feed/diablo-4/rss` return 404, and the page has no autodiscovery link. (`/en-us/feed/diablo-4` is an HTML page.) **There is a usable official feed anyway:** the Diablo IV forums run Discourse, and the Blizzard Tracker group feed `https://us.forums.blizzard.com/en/d4/groups/blizzard-tracker/posts.rss` carries every blue post. That includes the `@BlizzardEntertainment` mirrors of news.blizzard.com articles, hotfix notes, PSAs and known-issues posts. Newest entry: 2026-09-22 (HOTFIX 4). It's better than Steam, where Diablo IV's official announcements come only about once a month. Some noise: when a CM replies in a community thread, the feed lists that thread's title (e.g. "Blizzard is lying about the patch"). Each reply is also a separate item with its own URL, so one hotfix can show up 2 or 3 times. Clustering handles that.
- **Gearbox / Borderlands: no usable official RSS.** borderlands.2k.com has no feed (404s, no autodiscovery). The gearboxsoftware.com `/feed/` still answers, but its newest post is from 2021-12 and the homepage returns 403. Treat it as dead. Official coverage comes from the Steam announcements (good: weekly activities, update notes), the official Borderlands YouTube channel, the Gearbox YouTube channel, the Borderlands Bluesky account and the 2K Newsroom RSS. The 2K feed covers all 2K titles (NBA, WWE, Civ), so it's unscoped.
- **Pocketpair / Palworld: no official RSS.** pocketpair.jp and palworld.com have neither feeds nor autodiscovery. Official coverage comes from the Steam announcements (patch notes, the key source), the Pocketpair YouTube channel and the Palworld Bluesky account. The Bluesky account has been quiet since 2026-06-25; Pocketpair mainly posts on X, which is out of scope.

**Collector-affecting findings (for `impl-core`)**

1. **Bluesky search without auth is blocked right now.** `public.api.bsky.app/.../searchPosts` returns **HTTP 403 with an HTML body from BunnyCDN**, not a JSON error. `api.bsky.app` does the same. SPEC-DEV 7 holds. The collector must treat a non-JSON 403 as `skipped="auth"`, not crash on JSON decode.
2. **Official Bluesky accounts don't need search.** `https://bsky.app/profile/<did>/rss` works without auth, so they go in as plain `rss` sources. Two things to handle: (a) the handle form of the URL 302-redirects to the DID form, so either use DID URLs (done below) or set `follow_redirects=True`; (b) the items have **no `<title>`**, only `description`, `link` and `pubDate`, so the RSS collector must fall back to the first line of the description for the title.
3. **Reddit rate-limits hard even from a residential IP.** Firing 5 feeds back to back gave one 200 and four 429s. Spacing them about 45 s apart worked. `run_collectors` uses `asyncio.gather`, so all the subreddits would fire at once and most would 429. Suggest serializing requests to `reddit.com` with a gap of 10 s or more (a per-host semaphore), or keeping at most 2 subreddits.
4. **Use `/top/.rss?t=day`, not `/new/.rss`.** `/new` returns only the 25 newest posts. On r/Palworld or r/diablo4 that's a few hours of help and bug threads, so a once-a-day run sees a random slice. `/top/.rss?t=day` returns the day's 25 highest-voted posts (verified on r/Palworld; same Atom format).
5. The Steam `feeds=steam_community_announcements` filter in step 6 is needed. Unfiltered, Diablo IV's news is mostly PCGamesN, SteamDB "top sellers" and a Russian press feed. With the filter, all three apps return only official posts.
6. **Brave**: the endpoint is live (a request without a key returns 422 "x-subscription-token required"). I couldn't test results without a key.

**Alias and entity noise risks**

- **"Borderlands"** (alias) matches Borderlands 1/2/3, Tiny Tina, the 2024 film, SHiFT-code spam and non-games uses (Bluesky search for "borderlands" returns "Association for Borderlands Studies", a border-studies group). **Recommendation: drop it.** Keep `BL4`. The dedicated official sources (Steam, YouTube, Bluesky, r/Borderlands4) don't need it.
- **"D4"** (alias, matched case-insensitively) hits chess openings (`1.d4`), TTRPG dice (`d4`), the Nikon D4 and more. It's very noisy on Bluesky and in general press. **Recommendation: drop it**, and add the expansion names instead.
- **Diablo V** was announced at BlizzCon 2026 (the tracker has "Diablo V is Coming Spring 2029"). Those posts come through the dedicated D4 tracker feed and count as `diablo4`. That's either wanted or not; see the owner decisions.
- **Entities on unscoped press feeds**: "Take-Two" matches every GTA 6 and earnings story. "Blizzard" matches all WoW and Overwatch news. "2K" matches "NBA 2K" and "WWE 2K" (but not "2K27", thanks to the `(?!\w)` guard). They're only `uncertain`, and the LLM filters them, but they can fill the 60-item per-topic cap. Recommendation: trim the entities to the developer only, and make sure the cap sorts `uncertain` items below confident ones.
- Palworld's name is distinctive. It needs no alias.

**Low-quality or unusable sources, excluded**

- Per-game press tag feeds are stale or full of guides: RPS Palworld (newest 2026-07-27, "Best way to farm XP"), RPS BL4 (2025-11), Eurogamer BL4 (2026-02), VG247 (2025), PCGamesN BL4 (2026-06). The general feeds cover these topics. The exceptions are Eurogamer `diablo-iv` and PCGamesN `diablo-4` (both current), listed as optional.
- Wowhead and Icy Veins Diablo 4 pages return 403 behind Cloudflare. Blizzard Watch's domain doesn't resolve.
- r/borderlands3 kept returning 429, and it's about BL3 anyway. r/Diablo and r/Borderlands cover the whole series, so as dedicated sources they'd mislabel other games. Excluded in favour of r/diablo4 and r/Borderlands4.
- `youtube.com/@Borderlands` and `@Palworld` are not the official channels (a fan channel last active 2025-02, and an empty 2007 channel). The `@Diablo` handle page's first `channelId` is Diablo FR. The official Diablo channel ID is `UCxn8csYeZg6awRnZS-aqg0g`. `gearboxofficial.bsky.social` isn't domain-verified, so it's excluded.
- Take-Two IR RSS works, but it's all earnings and shareholder notices: little value.

## Verification table

| Name | Type | URL / app_id / query | Topics | Trust | Verified 2026-09-23 | Rationale |
|---|---|---|---|---|---|---|
| Borderlands 4 Steam | steam_news | 1285190 | borderlands4 (dedicated) | official | OK, newest 2026-09-21 (with `feeds=` filter) | Update notes and weekly activities; best BL4 official source |
| Palworld Steam | steam_news | 1623730 | palworld (dedicated) | official | OK, newest 2026-09-15 | Patch notes; main Palworld official source |
| Diablo IV Steam | steam_news | 2344520 | diablo4 (dedicated) | official | OK, newest 2026-09-15 | Season launches only (roughly monthly); backstop |
| Diablo IV Blizzard Tracker | rss | https://us.forums.blizzard.com/en/d4/groups/blizzard-tracker/posts.rss | diablo4 (dedicated) | official | OK, 50 items, newest 2026-09-22 | Hotfixes, PSAs, news mirrors; replaces the missing Blizzard RSS |
| 2K Newsroom | rss | https://newsroom.2k.com/feed/rss | unscoped | official | OK, newest 2026-09-09 | BL4 DLC press releases; also NBA/WWE/Civ, so it needs a keyword match |
| Borderlands YouTube | rss | https://www.youtube.com/feeds/videos.xml?channel_id=UCHY2-UtRFv1WKf4o7cpEp3w | borderlands4 (dedicated) | official | OK, newest 2026-09-10 | Official trailers and reveals (428K subscribers) |
| Gearbox YouTube | rss | https://www.youtube.com/feeds/videos.xml?channel_id=UCSRO0JNUYTCjsk7VmMdNYKw | unscoped | official | OK, newest 2026-09-21 | DevCasts and DeadECHOs; also Risk of Rain, so unscoped |
| Pocketpair YouTube | rss | https://www.youtube.com/feeds/videos.xml?channel_id=UCz6cOlpF6os7JkAKnpMNriw | palworld (dedicated) | official | OK, newest 2026-09-20 | Palworld trailers. Also has Pocketpair Publishing trailers (Truckful); the LLM filters those |
| Diablo YouTube | rss | https://www.youtube.com/feeds/videos.xml?channel_id=UCxn8csYeZg6awRnZS-aqg0g | diablo4 (dedicated) | official | OK, newest 2026-09-18 | Nearly all Diablo IV (1.97M subscribers) |
| Borderlands Bluesky | rss | https://bsky.app/profile/did:plc:sxmjis6jypsib2k7rk6pjoau/rss | borderlands4 (dedicated) | official | OK, newest 2026-09-21 | borderlands.2k.com (domain-verified); X mirror; no titles |
| Palworld Bluesky | rss | https://bsky.app/profile/did:plc:g3nrnn7t2tvoekp5flrexiby/rss | palworld (dedicated) | official | OK, but newest 2026-06-25 (quiet) | en-palworld.pocketpair.jp; cheap to keep; expect health "0 items" |
| Diablo Bluesky | rss | https://bsky.app/profile/did:plc:wxfwmaqg4kgiwzxcygdt2myj/rss | diablo4 (dedicated) | official | OK, newest 2026-09-23 | diablo.blizzard.com; X mirror; posts rarely name the game, so dedicated |
| r/Borderlands4 | rss | https://www.reddit.com/r/Borderlands4/top/.rss?t=day | borderlands4 (dedicated) | community | OK (`/new` verified; `/top` format verified on r/Palworld) | Game-specific subreddit |
| r/Palworld | rss | https://www.reddit.com/r/Palworld/top/.rss?t=day | palworld (dedicated) | community | OK, newest 2026-09-23 | Heavy on fan art; LLM filters |
| r/diablo4 | rss | https://www.reddit.com/r/diablo4/top/.rss?t=day | diablo4 (dedicated) | community | OK (`/new`, newest 2026-09-23; 429 on the first tries) | Game-specific subreddit |
| PC Gamer | rss | https://www.pcgamer.com/rss/ | unscoped | press | OK, newest 2026-09-23 | Wide PC coverage; covers Palworld business news |
| Eurogamer | rss | https://www.eurogamer.net/feed | unscoped | press | OK, 100 items, newest 2026-09-23 | Wide coverage |
| GamesRadar+ | rss | https://www.gamesradar.com/rss/ | unscoped | press | OK, newest 2026-09-23 | Wide coverage |
| IGN | rss | https://www.ign.com/rss/articles/feed | unscoped | press | OK, newest 2026-09-23 | Wide, but includes film/TV/deals, so noisier. **Removed 2026-09-25:** returns 403 to the production Droplet's datacenter IP. |
| PCGamesN | rss | https://www.pcgamesn.com/mainrss.xml | unscoped | press | OK, 75 items, newest 2026-09-23 | Strongest day-to-day Diablo 4 coverage |
| *Optional:* Eurogamer Diablo IV tag | rss | https://www.eurogamer.net/feed/tag/games/diablo-iv | diablo4 (dedicated) | press | OK, newest 2026-09-23 | Some guides mixed in |
| *Optional:* PCGamesN Diablo 4 tag | rss | https://www.pcgamesn.com/diablo-4/rss | diablo4 (dedicated) | press | OK, newest 2026-09-23 | Mostly duplicates the main feed |
| *Optional:* RPS, GameSpot, Kotaku, Polygon | rss | /feed, /feeds/news/, /rss, /rss/index.xml | unscoped | press | All OK, 2026-09-23 | More volume, little extra coverage |
| Bluesky search × 3 | bluesky_search | "Borderlands 4", "Palworld", "Diablo 4" | dedicated each | community | **403 without auth (HTML from CDN)** | Works only with an app password (SPEC-DEV 7) |
| Brave News | web_search | templates, see below | per topic | press | Endpoint live (422 without key); results untested | Catches outlets not in the feed list |

**Web search queries.** The schema has only global `query_templates` formatted with `{name}`, not per-topic queries. The default `"{name} news"` becomes "Diablo IV news", while the press mostly writes "Diablo 4". Per-topic queries I'd use:

- BL4: `"Borderlands 4"`, `Borderlands 4 update OR patch OR DLC`
- Palworld: `Palworld`, `Palworld Pocketpair update OR lawsuit OR patch`
- Diablo IV: `"Diablo 4" OR "Diablo IV"`, `Diablo 4 season OR patch OR hotfix`

With the current schema, the closest templates are `["\"{name}\"", "{name} update OR patch OR season"]`. Supporting the exact queries above would take an optional `search_queries: list[str]` on `Topic`, which is a small schema change.

## Proposed config

```yaml
topics:
  - key: borderlands4
    name: "Borderlands 4"
    aliases: ["BL4"]                         # "Borderlands" dropped: matches BL1-3, Tiny Tina, film, border-studies
    entities: ["Gearbox"]                    # "2K"/"Take-Two" dropped: NBA 2K, GTA 6 floods
  - key: palworld
    name: "Palworld"
    aliases: []
    entities: ["Pocketpair"]
  - key: diablo4
    name: "Diablo IV"
    aliases: ["Diablo 4", "Lord of Hatred", "Vessel of Hatred"]   # "D4" dropped: chess/dice/camera noise
    entities: ["Blizzard"]                   # "Activision Blizzard"/"Microsoft Gaming" dropped: corporate noise

sources:
  # --- Official: Steam (collector adds feeds=steam_community_announcements) ---
  - {type: steam_news, name: "Borderlands 4 Steam", app_id: 1285190, topics: [borderlands4], trust: official}
  - {type: steam_news, name: "Palworld Steam",      app_id: 1623730, topics: [palworld],     trust: official}
  - {type: steam_news, name: "Diablo IV Steam",     app_id: 2344520, topics: [diablo4],      trust: official}
  # --- Official: RSS ---
  - {type: rss, name: "Diablo IV Blizzard Tracker", url: "https://us.forums.blizzard.com/en/d4/groups/blizzard-tracker/posts.rss", topics: [diablo4], trust: official}
  - {type: rss, name: "2K Newsroom",        url: "https://newsroom.2k.com/feed/rss", trust: official}
  - {type: rss, name: "Borderlands YouTube", url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCHY2-UtRFv1WKf4o7cpEp3w", topics: [borderlands4], trust: official}
  - {type: rss, name: "Gearbox YouTube",    url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCSRO0JNUYTCjsk7VmMdNYKw", trust: official}
  - {type: rss, name: "Pocketpair YouTube", url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCz6cOlpF6os7JkAKnpMNriw", topics: [palworld], trust: official}
  - {type: rss, name: "Diablo YouTube",     url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxn8csYeZg6awRnZS-aqg0g", topics: [diablo4], trust: official}
  - {type: rss, name: "Borderlands Bluesky", url: "https://bsky.app/profile/did:plc:sxmjis6jypsib2k7rk6pjoau/rss", topics: [borderlands4], trust: official}
  - {type: rss, name: "Palworld Bluesky",   url: "https://bsky.app/profile/did:plc:g3nrnn7t2tvoekp5flrexiby/rss", topics: [palworld], trust: official}
  - {type: rss, name: "Diablo Bluesky",     url: "https://bsky.app/profile/did:plc:wxfwmaqg4kgiwzxcygdt2myj/rss", topics: [diablo4], trust: official}
  # --- Community: Reddit (top of day; needs serialized fetching, see findings) ---
  - {type: rss, name: "r/Borderlands4", url: "https://www.reddit.com/r/Borderlands4/top/.rss?t=day", topics: [borderlands4], trust: community}
  - {type: rss, name: "r/Palworld",     url: "https://www.reddit.com/r/Palworld/top/.rss?t=day",     topics: [palworld],     trust: community}
  - {type: rss, name: "r/diablo4",      url: "https://www.reddit.com/r/diablo4/top/.rss?t=day",      topics: [diablo4],      trust: community}
  # --- Press: general feeds, keyword-matched ---
  - {type: rss, name: "PC Gamer",    url: "https://www.pcgamer.com/rss/",            trust: press}
  - {type: rss, name: "Eurogamer",   url: "https://www.eurogamer.net/feed",          trust: press}
  - {type: rss, name: "GamesRadar+", url: "https://www.gamesradar.com/rss/",         trust: press}
  - {type: rss, name: "IGN",         url: "https://www.ign.com/rss/articles/feed",   trust: press}
  - {type: rss, name: "PCGamesN",    url: "https://www.pcgamesn.com/mainrss.xml",    trust: press}
  # --- Bluesky search: only useful with BLUESKY_HANDLE/APP_PASSWORD (unauth = 403) ---
  - {type: bluesky_search, query: "\"Borderlands 4\"", topics: [borderlands4], trust: community}
  - {type: bluesky_search, query: "Palworld",          topics: [palworld],     trust: community}
  - {type: bluesky_search, query: "\"Diablo 4\"",      topics: [diablo4],      trust: community}
  # --- Web search (Brave News) ---
  - type: web_search
    queries_per_topic: 2
    query_templates: ["\"{name}\"", "{name} update OR patch OR season"]
    trust: press
```

## Owner decisions

1. **Aliases and entities**: accept dropping `Borderlands` and `D4` as aliases, and `2K`, `Take-Two`, `Activision Blizzard` and `Microsoft Gaming` as entities?
2. **Diablo V**: should Diablo V news count under the Diablo IV topic? It comes through the dedicated tracker, YouTube and Bluesky feeds either way. Or add a separate topic later?
3. **Bluesky**: create an app password? Without one, the three search sources are always skipped. The official accounts work regardless, through RSS.
4. **Reddit**: keep 3 subreddits with serialized fetching, and accept a possible block from DigitalOcean?
5. **Web search**: add per-topic `search_queries` (a schema change), or live with the global templates?

### Decided 2026-09-23

1. **Aliases and entities**: trimmed as recommended, and then some. Borderlands 4 keeps `BL4` and `Borderlands4`, drops bare `Borderlands`; entities are developer-only (`Gearbox`). Diablo IV drops `D4`, keeps `Diablo 4` plus the expansion names, and adds `Diablo V`/`Diablo 5` (see #2); entities are developer-only (`Blizzard`). Palworld's entities are `Pocketpair`. No `2K`, `Take-Two`, `Activision Blizzard` or `Microsoft Gaming` anywhere.
2. **Diablo V**: counts under `diablo4` for now, via new aliases `Diablo V` and `Diablo 5` (on top of the coverage that already arrives through the dedicated tracker/YouTube/Bluesky feeds). A separate `diablo5` topic is a config edit away whenever it has enough of its own news to be worth splitting out.
3. **Bluesky**: no app password yet. The three `bluesky_search` sources stay in `config.example.yaml`, commented to explain they're skipped until `BLUESKY_HANDLE`/`BLUESKY_APP_PASSWORD` are both set; the official-account RSS feeds don't need a password and are in regardless.
4. **Reddit**: keeping all three subreddits, on `/top/.rss?t=day` per finding 4, with a comment that Reddit may block datacenter IPs and that source health will show it if so.
5. **Web search**: added the `search_queries` schema change. `config.example.yaml` sets it for all three topics; Borderlands 4 and Diablo IV use the phrasing this doc recommended above, Palworld stays plain.

### Decided 2026-09-27

- **YouTube channel feeds removed.** All four (`Borderlands YouTube`, `Gearbox YouTube`, `Pocketpair YouTube`, `Diablo YouTube`) returned 404 on every run for days, as did unrelated channels' feeds, with no reported YouTube outage. Removed from `config.example.yaml`, the dev config, and production (owner decision), after confirming with `--check-sources`.

## v3 catalog (2026-10)

Plan task 17: sources, aliases and search queries for the 12 games joining the public app's catalog. Verified 2026-09-30 from the same residential macOS connection as the first round. The proposed entries are in `docs/plans/2026-09-30-public-app-catalog.yaml`, pending owner approval (D11).

### How this was checked

- Discovery: web search for each game's official channels, a look for RSS autodiscovery links on each official news page (one request per page), and DNS lookups of `_atproto.<domain>` to find domain-verified Bluesky accounts without calling the Bluesky API at all.
- Verification: every candidate went through the project's own collectors (the same `build_collectors` and RSS and Steam code the bot runs), driven by a small scratch script that ran them one at a time, 2 seconds apart, and 35 seconds apart for Reddit. The User-Agent was the bot's own, with `NEWSBOT_CONTACT` set to the repo URL. I used a script instead of `--check-sources` because the CLI runs non-Reddit sources concurrently and I wanted each feed fetched once and saved, so the alias noise could be checked offline afterwards without fetching anything twice.
- Each source was fetched once. Reddit's 429 came back for 3 of the 12 subreddits (r/VALORANT, r/Warframe, r/WarDogs) even at 35 seconds apart; each retry 60 seconds later worked.
- "Items" below is what the source returned. "24h" and "7d" count how many of those were published in the last day and week. Steam and Bluesky return their newest 20 to 30 posts however old they are, so a quiet game still shows 20 items.

### Findings

**Official sources are thinner than for the first three games.** Four of the twelve (Fortnite, VALORANT, Marvel Rivals, Aniimo) have no working official RSS or Bluesky, and two of those (Fortnite and VALORANT) aren't on Steam either. For Fortnite and VALORANT the only dedicated source is the subreddit, so their free digests will lean on community posts plus whatever the shared press feeds say.

**Free servers see uncertain matches as headlines.** In v2, entity matches were `uncertain` and the LLM threw most of the noise away. In v3's free tier there's no LLM, so an entity match goes straight into a headline list. That's why the entities below are much shorter than they'd be for a comped server: publisher names like Activision, NetEase, Riot Games, Valve, Square Enix, Epic Games and Team17 are all left out, because in testing each one matched more unrelated stories than relevant ones.

**Common-word names.**
- **Rust** gets `match_name: false`, with aliases `Rust Console Edition` and `playrust` plus the entity `Facepunch`. None of the shared press feeds happened to say "rust" on the test day, but the word is also a metal, a programming language, a 2024 film and an idiom, and the plan already calls for it. The cost is that a press story that says only "Rust" and never "Facepunch" is missed; the dedicated Steam, Facepunch and Reddit feeds carry the game regardless.
- **Destiny 2** and **Apex Legends** keep `match_name: true`, because the full names are specific. The rule is that the short forms never become aliases. The test data backs that up: bare "Destiny" matched an r/ffxiv post ("It will tell me your destiny"), and bare "Apex" matched a Witcher 3 article, a Warframe hotfix and a Call of Duty post.
- **Call of Duty** keeps `match_name: true`. The short forms `CoD` and `COD` aren't aliases (case-insensitive matching makes them the fish and cash on delivery). `Warzone` is an alias with a small risk ("turned into a warzone"); every hit in testing was the game. `Respawn` isn't an entity for Apex, because it's also an everyday gaming verb.
- **WARDOGS** matches the single word "Wardogs", so the 2016 film "War Dogs" doesn't collide. Every press hit (5 across four feeds) was about the game.

**Destiny 2 has gone quiet.** Its Steam announcements and Bluesky account both stop in late June 2026, and Eurogamer reports that regular updates aren't coming back. The sources are still worth keeping (cheap, official, and they'll wake up if anything ships), but expect "0 items" in source health most days, the way Palworld's Bluesky was.

**Reddit load.** This adds 12 subreddits, one per game, to the 3 already configured. At the collector's 35 second Reddit gap, 15 feeds take about 9 minutes of every hourly pass. That still fits in an hour, but 3 of 12 got a 429 at that spacing in testing, and none of this has been tried from the Droplet's datacenter IP. If Reddit starts failing in prod, the first candidates to drop are the games that have good official sources anyway (Warframe, Final Fantasy XIV, Counter-Strike 2).

### Per game

**Fortnite** (`fortnite`). Name matching only; no entities (Epic Games mostly brings Epic Games Store freebie stories).
- r/FortNiteBR: 25 items, 25 in 24h.
- Shared press: 4 Fortnite stories in the week's items (Eurogamer 1, PCGamesN 3), 1 in the last 24h.
- Rejected: fortnite.com/news (403 to the research User-Agent, so no autodiscovery check was possible); Fortnite Insider `/feed/` (403); no domain-verified Fortnite or Epic Games Bluesky account (the `fortniteofficial.bsky.social` style accounts aren't domain-verified, so they're out, the same rule as `gearboxofficial` in the first round). Fortnite isn't on Steam.

**Call of Duty** (`callofduty`, current game plus Warzone). Aliases `Warzone`, `Modern Warfare 4`, `MW4`, `Black Ops 7`, `BO7`; entities `Infinity Ward`, `Treyarch`. The aliases need updating each autumn when a new game ships.
- Call of Duty Steam (app 1938090, the umbrella app): 20 items, 2 in 24h, 3 in 7d. Carries Black Ops 7, Warzone and MW4 announcements.
- Call of Duty Bluesky (callofduty.com, domain-verified): 25 items, 1 in 24h.
- r/CallOfDuty: 24 items, 24 in 24h.
- Shared press: 8 matches across the week, all about the game.
- Rejected: the Modern Warfare 4 Steam app (4435490) and the Warzone Steam app (1962663) both returned 0 announcements; their posts go to the umbrella app. Recheck MW4 after it launches on 2026-10-23. Charlie Intel's feed returns 50 items, but the newest is from 2025-01, so it's stale. `Activision` dropped as an entity (Halo and Xbox layoff stories). callofduty.com/blog timed out, so its RSS is unverified.

**Marvel Rivals** (`marvelrivals`). Alias `MarvelRivals`; no entities (`NetEase` matched unrelated NetEase business news).
- Marvel Rivals Steam (2767030): 20 items, 0 in 24h, 1 in 7d. Weekly patch notes.
- r/marvelrivals: 25 items, 25 in 24h.
- No official RSS on marvelrivals.com and no domain-verified Bluesky.

**VALORANT** (`valorant`). Alias `VCT`; no entities (`Riot Games` would mostly be League of Legends).
- r/VALORANT: 25 items (after one 429 and a retry).
- Rejected: the domain-verified VALORANT (valorant.riotgames.com) and Riot Games (riotgames.com) Bluesky accounts both returned 0 items. playvalorant.com has no RSS. Not on Steam.
- Noise note: the one press "match" in testing was a Dexerto story that mentioned VALORANT in passing. A name this distinctive is fine; the problem is Dexerto (see shared sources).

**Counter-Strike 2** (`cs2`). Aliases `CS2`, `Counter-Strike`; no entities (`Valve` matched Steam store and hardware stories).
- Counter-Strike 2 Steam (730): 20 items, 0 in 24h, 4 in 7d.
- HLTV News (press, dedicated): 10 items, 4 in 24h, 10 in 7d. Esports news only, but that's a big part of CS2 news.
- r/GlobalOffensive: 25 items. (Still the main CS subreddit despite the name.)
- Rejected: Counter-Strike Bluesky (counter-strike.net) returns 9 items, newest 2025-02, so it's abandoned. Valve's Bluesky is all Steam hardware.

**Apex Legends** (`apexlegends`). Alias `ApexLegends`; no entities. Never alias bare `Apex`.
- Apex Legends Steam (1172470): 20 items, 1 in 24h, 6 in 7d.
- r/apexlegends: 25 items.
- No official RSS on ea.com and no domain-verified Bluesky.

**Rust** (`rust`, `match_name: false`). Aliases `Rust Console Edition`, `playrust`; entity `Facepunch`.
- Rust Steam (252490): 20 items, newest 2026-09-03. Monthly update posts on the first Thursday, so 0 on most days is normal.
- Rust Facepunch News (`https://rust.facepunch.com/rss/news`, found by autodiscovery): 20 items, newest 2026-09-03. Same monthly posts as Steam; clustering merges them.
- r/playrust: 21 items.

**Destiny 2** (`destiny2`). Aliases `Destiny2`, `This Week in Destiny`; no entities (`Bungie` matched Marathon news 19 times in a week).
- Destiny 2 Steam (1085660): 20 items, newest 2026-06-18.
- Destiny 2 Bluesky (destinythegame.bungie.net): 29 items, newest 2026-06-24.
- r/DestinyTheGame: 25 items.
- Bungie News RSS (`https://www.bungie.net/en/Rss/News`): 25 items, newest 2026-09-24, but mostly Marathon. Proposed as a shared source restricted to `destiny2`, so only posts that name Destiny 2 get through (6 of the 25).
- Rejected: Bungie's own Bluesky (5 items, mostly studio announcements), and as a dedicated source the Bungie RSS above.

**Warframe** (`warframe`). No aliases needed; entity `Digital Extremes`.
- Warframe Steam (230410): 20 items, 2 in 24h, 5 in 7d.
- Warframe PC Update Notes (`https://forums.warframe.com/forum/3-pc-update-notes.xml/`): 25 items, 3 in 7d. Updates and hotfixes.
- Warframe Bluesky (warframe.com): 24 items, 5 in 24h, 24 in 7d. The busiest official account in the set.
- r/Warframe: 25 items (after one 429 and a retry).
- Rejected: the forum's News section feed (`166-news.xml`) doesn't parse (invalid XML at line 149).

**Final Fantasy XIV** (`ffxiv`). Aliases `FFXIV`, `FF14`, `Final Fantasy 14`, `Dawntrail`, `Evercold`; entity `Naoki Yoshida`. `Evercold` comes from a PCGamesN headline ("Ahead of FF14 Evercold") and looks like the next expansion's name; the owner should confirm it. `Square Enix` is left out.
- Final Fantasy XIV Steam (39210): 20 items, 0 in 7d. Mostly event and patch note posts.
- FFXIV Lodestone Topics (`https://na.finalfantasyxiv.com/lodestone/news/topics.xml`): 20 items, 1 in 7d. Announcements and events.
- FFXIV Lodestone News (`.../lodestone/news/news.xml`): 20 items, 5 in 7d. Maintenance, server issues and update notices; more housekeeping, but that's what players check.
- r/ffxiv: 25 items.
- Both Lodestone feeds were found by autodiscovery on the Lodestone front page.

**Aniimo** (`aniimo`): **include.** Entity `Pawprint Studio`.
- Aniimo Steam (4126040): 12 items, newest 2026-09-29. Real patch notes and maintenance posts since the 2026-09-15 launch, so the official-source bar is met.
- r/Aniimo: 25 items, on topic (the studio recognizes it as the official fan subreddit).
- Noise: none. The name matched nothing outside Aniimo's own sources. The flip side is that none of the four press feeds mentioned it in the test window, so its digest will be Steam plus Reddit.
- No domain-verified Bluesky; the studio posts on X.

**WARDOGS** (`wardogs`): **include.** Entity `Bulkhead` (the studio; 1 hit, relevant).
- WARDOGS Steam (1867240): 20 items, 1 in 24h, 2 in 7d. Patches, maintenance and sales milestones since the 2026-09-10 early access launch.
- r/WarDogs: 25 items (after one 429 and a retry). Not studio-run, but active and on topic.
- Noise: none found. All 5 press matches were about the game.
- Rejected: Bulkhead's Bluesky (bulkhead.com) returns 7 items, newest 2025-03, so it's dormant. Team17's Bluesky is almost all Worms, and `Team17` as an entity would bring the same.

### Shared sources

- **Add:** Bungie News, restricted to `destiny2` (above).
- **HLTV News** goes under `cs2`, not shared: it's Counter-Strike only.
- **Rejected:** Dexerto (`/feed/` works, 50 items, but it's mostly viral and general news; only 2 of 50 matched a catalog game, one of them a false positive), Dot Esports (`/feed` doesn't parse), Charlie Intel (stale since 2025-01), Fortnite Insider (403), Valve Bluesky (hardware), Team17 Bluesky (Worms), Riot Games Bluesky (0 items).
- Today's five shared feeds stay as they are. Over the week of items they returned, they matched Call of Duty 8 times, WARDOGS 5, Fortnite 4, Counter-Strike 2 3, Destiny 2 2, Warframe 2 (one a false positive: a fashion game's article that mentioned Warframe in passing) and Final Fantasy XIV 1. Marvel Rivals, VALORANT, Apex Legends, Rust and Aniimo got nothing from them, so those games depend on their own sources.

### Unverified, worth a look later

- A Call of Duty blog RSS (the page timed out).
- vlr.gg for VALORANT esports news (not fetched).
- Dexerto's per-game category feeds, which might be cleaner than the main feed (not fetched).
- The Modern Warfare 4 Steam app after launch.

### Owner decisions (D11)

1. Approve the 12 entries as proposed, including Aniimo and WARDOGS (both met the bar: official Steam posts, no match noise).
2. Accept Fortnite and VALORANT going out with only a subreddit as their dedicated source.
3. Accept the short entity lists, trading some press recall for clean free-tier headlines.
4. Confirm `Evercold` as a Final Fantasy XIV alias.
5. Accept 12 more subreddits (15 in all, about 9 minutes of each hourly pass), or pick some to drop now.
