# Finding sources for your own topics

`config.yaml`'s `sources` list is where this bot actually gets its news, and
finding a good one is mostly detective work: hunting down the right RSS URL,
figuring out which subreddit sort actually surfaces good posts, and noticing
when a keyword you thought was clever turns out to match a chess opening.
This is the recipe book from doing that for Borderlands 4, Palworld, and
Diablo IV (`docs/sources-research.md` has the full write-up, warts and all);
the lessons generalize to whatever game you're pointing this at.

## Steam: `steam_news`

If your game is on Steam, this is usually the best official source and the
easiest one to find. The `app_id` is the number in the store URL
(`store.steampowered.com/app/<app_id>/...`) or the first result on
[SteamDB](https://steamdb.info) for the game's name.

```yaml
- type: steam_news
  name: "Your Game Steam"
  app_id: 1234567
  topics: [yourgame]
  trust: official
```

The collector adds `feeds=steam_community_announcements` itself, which
matters more than it looks: without that filter, some apps' announcement
feeds fill up with SteamDB "top sellers" posts and unrelated press
aggregators, not the developer's own patch notes.

## YouTube channel feeds (currently not working)

Every YouTube channel used to have a plain RSS feed, no API key needed:

```
https://www.youtube.com/feeds/videos.xml?channel_id=<channel_id>
```

This bot used four of them. In late September 2026 all four started
returning 404, along with every other channel's feed we tried, while YouTube
reported no outage; so they were dropped from the example config. If you
want to try one anyway, get the channel's real `UC...` id (view the channel
page's source and search for `"channelId"`; the `@handle` isn't it, and the
first channel a search turns up is often a fan account), add it, and run
`--check-sources` before you trust it.

## Bluesky: profile RSS by DID

An official account's Bluesky posts are available as plain RSS with no
authentication needed, as long as you use the DID form of the profile URL:

```
https://bsky.app/profile/<did>/rss
```

The `@handle` form of the URL works too, but it 302-redirects to the DID
form, so using the DID directly saves a redirect on every fetch. Find the DID
from the handle form's page source (search for `"did":"did:plc:`) or a DID
resolver.

One quirk worth knowing before it confuses you: these feed items have no
`<title>`, only a `description`. The RSS collector falls back to the first
line of the description as the title, so a post that leads with a mention or
a link can produce an odd-looking headline. That's the feed's shape, not a
parsing bug.

Searching Bluesky (`bluesky_search` sources, as opposed to an official
account's own RSS) needs `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in
`.env`. Without them, an unauthenticated search request gets a 403 with an
HTML body, not even a JSON error, and the source is skipped with a coverage
note rather than a health failure.

## Subreddits: `/top/.rss?t=day`, not `/new/.rss`

```
https://www.reddit.com/r/YourSubreddit/top/.rss?t=day
```

Use `/top/.rss?t=day`, not `/new/.rss`. `/new` returns only the 25 newest
posts, and on a subreddit that gets help and bug-report threads all day, a
once-a-day run would see whatever happened to post in the last hour rather
than what people actually cared about. `/top` with `t=day` returns the day's
25 highest-voted posts instead, which is a much better sample for a daily
digest.

Two gotchas, both real and both outside this bot's control:

- **Reddit rate-limits aggressively**, even from a residential connection.
  Firing several subreddit feeds at once can produce a wall of 429s; spacing
  requests out helps but doesn't eliminate it.
- **Reddit (and some other sites, IGN among them) return 403 to datacenter
  IPs**, meaning a source that works fine from your laptop can fail once
  the bot is actually running on a cloud provider's box. If it happens,
  it shows up in `/newsbot status` as a source health failure, not a crash;
  `--check-sources` (below) is the fast way to find out before you deploy.

## Discourse forums: group RSS

If the developer runs an official forum on Discourse (a lot of studios do,
even when their main site has no feed at all), a specific user group's
activity is often available as its own RSS feed:

```
https://<forum-host>/<category>/groups/<group-slug>/posts.rss
```

This was the find that mattered most for Diablo IV: `news.blizzard.com` has
no RSS anywhere (checked `/rss`, `/feed`, and the page's own autodiscovery;
nothing), but the Diablo IV forums' "Blizzard Tracker" group feed mirrors
every official post a community manager makes, including the same content
that would have been on a news blog if one existed. It's a little noisier
than a dedicated news feed (a CM's reply in a random community thread shows
up under that thread's title, and a busy hotfix thread can appear two or
three times as different community members get individual replies), but
normalize-and-dedupe handles the duplicates, and it beats waiting for a
monthly Steam announcement.

Look for a "Community Team," "Blue Posts," or similarly named group on the
forum; the group's own page usually links its RSS feed, or you can guess the
URL pattern above from an existing group's URL.

## Press feeds

A general games-press RSS feed (`pcgamer.com/rss/`, a site's `/feed`, etc.)
with no `topics` set is a fine way to catch coverage that a game-specific
feed misses, at the cost of needing the keyword matcher to sort out what's
actually relevant. Two things worth checking before committing to one:

- **Is it actually current?** A per-game tag feed on a press site
  (`site.com/tag/your-game/rss`) looks tempting but is often stale or full
  of old guides; check the newest entry's date before adding it.
- **Does it 403 from where the bot actually runs?** See the Reddit/IGN note
  above; a feed that works from your own connection isn't guaranteed to work
  from a Droplet.

## Choosing aliases and entities: the Borderlands and Diablo lessons

`topics[].aliases` are exact-ish name matches (a confident hit); `entities`
are looser matches, marked `uncertain` and sorted below confident matches.
Both get matched case-insensitively against an item's title and excerpt, so
a short or common word is a bigger liability than it looks:

- **"Borderlands"** as a bare alias matched Borderlands 1 through 3, Tiny
  Tina's spinoffs, the 2024 movie, and (genuinely) an academic "Association
  for Borderlands Studies." `BL4` and `Borderlands4` don't have that
  problem, so those are the whole alias list for that topic.
- **"D4"** as an alias matched chess notation (`1.d4`), tabletop dice
  (`d4`), a camera model, and assorted other noise, entirely unrelated to
  Diablo IV. Spelling the name out (`Diablo 4`, `Diablo IV`, plus any
  expansion subtitles) avoids all of it.

The general rule: before adding an alias, ask whether the word has a life
outside your game. If it does, either drop it (the dedicated official
sources below don't need it anyway) or use a longer, less ambiguous form.

Entities work the same way, one level looser: a developer or publisher name
is a reasonable entity (it'll flag as `uncertain`, which is the point), but
a corporate parent that also owns unrelated franchises (a publisher's own
name matching every other game they publish) fills up the per-topic item cap
with noise the LLM then has to filter back out. Keep entities scoped to the
studio actually making your game, not everything upstream of it.

An **entity-only match is always `uncertain`.** It's still useful (it keeps
a post from being silently dropped just because it never says the game's
name), but it's ranked below a confident name/alias match when the
per-topic cap has to cut something.

## The dedicated-source rule

A source whose `topics` list names **exactly one** topic key is a *dedicated
source*: every item it returns counts as a confident match for that topic
even if the text never mentions the game by name at all. That's what keeps
a Steam post titled "v0.6.2 Update" or an undifferentiated subreddit feed
from being silently dropped for never saying the game's name out loud.

This matters most for a source that names your game's own developer's
account, but not the game itself: an official Bluesky account posting "New
hotfix live, details below" is a confident match if it's a dedicated source,
and an entity-only `uncertain` match (or dropped entirely) if it isn't.

A source with several topics, or none, still needs an actual keyword hit;
the dedicated-source rule only applies when a source is scoped that
narrowly.

## `search_queries`: your own Brave News phrasing

The default web search templates (`"{name} news"`, `"{name} update OR patch
OR season"`) guess at phrasing the press doesn't actually use; `"Diablo IV
news"` is not a phrase anyone writes. A topic's own `search_queries` list
overrides the global templates for that topic:

```yaml
topics:
  - key: yourgame
    name: "Your Game"
    search_queries:
      - "\"Your Game\""
      - "Your Game update OR patch OR DLC"
```

Quote a multi-word exact phrase (`"\"Diablo 4\""`) the way Brave's own search
syntax expects; `OR` needs to be capitalized to work as a boolean operator
rather than a literal word.

## Brave's free tier, and the math

`web_search` sources cost real requests against Brave's News Search API.
`queries_per_topic` (default 2) times however many topics you have is the
requests-per-run figure; multiply by however many times a day you'd run this
(once, for the daily digest; the SHiFT alert sweep deliberately excludes
`web_search` and never touches Brave at all) to get requests per day. Three
topics at the default `queries_per_topic: 2` is 6 requests a run, about 180
a month: comfortably inside Brave's free tier as shipped in
`config.example.yaml`. Scale the arithmetic to your own topic count before
assuming it'll stay free.

## Verifying with `--check-sources`

Once you've added a source, don't wait for tomorrow's digest to find out if
it actually works:

```bash
python -m newsbot.pipeline.run --config config.yaml --check-sources
```

This runs every configured collector for real, once, and prints a report:
item counts and the first error line per source, plus which topics matched
how many items. It needs no Anthropic key and no Discord token; it only
needs `BRAVE_API_KEY` (and Bluesky's, if you've set those up) for the
sources that actually use them, and says so plainly for anything skipped.
It's the fastest way to find out a feed URL is wrong, a subreddit is
blocking the connection you're running from, or a brand-new alias is
matching nothing at all, before any of that shows up as a confusing gap in
tomorrow's digest.
