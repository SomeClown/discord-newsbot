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
| IGN | rss | https://www.ign.com/rss/articles/feed | unscoped | press | OK, newest 2026-09-23 | Wide, but includes film/TV/deals, so noisier |
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
