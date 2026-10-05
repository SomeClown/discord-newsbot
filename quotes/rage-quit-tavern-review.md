# Rage Quit Tavern quotes: review sheet

Total: **362** quotes in `quotes/rage-quit-tavern.txt` (the brief asked for about 400). **13** are flagged "owner check". The attribution under each quote is the short form that posts in the lounge; the parser's full original citation sits next to it.

## Counts and shortfalls

| Source | Target | Taken | Shortfall |
|---|---|---|---|
| Douglas Adams (the Hitchhiker's books) | 65 | 65 |  |
| Monty Python | 55 | 55 |  |
| Jack Handey (Deep Thoughts) | 50 | 12 | 38 |
| Hunter S. Thompson | 35 | 35 |  |
| H. L. Mencken | 35 | 35 |  |
| Terry Pratchett | 25 | 25 |  |
| Fight Club | 20 | 20 |  |
| Dorothy Parker | 20 | 20 |  |
| Kurt Vonnegut | 15 | 15 |  |
| Groucho Marx | 10 | 10 |  |
| George Carlin | 10 | 10 |  |
| Mark Twain | 10 | 10 |  |
| Blackadder | 10 | 10 |  |
| The Big Lebowski | 10 | 10 |  |
| Red Dwarf | 10 | 10 |  |
| Futurama | 10 | 10 |  |
| Office Space | 10 | 10 |  |
| **Total** | 400 | 362 | 38 |

**The one shortfall: Jack Handey, 12 of 50.** Wikiquote has no Deep Thoughts page, and the "Jack Handey" page holds 9 sourced quotes. The topic pages the owner approved (Boxing, Key, War, Embarrassment) plus Sand, which turned up in a Wikiquote search, add three more that the parser accepts as Handey's: one each from Boxing, Sand and Embarrassment. The "Key" and "War" Handey quotes are the same lines already on the Handey page (the War one differs by a comma), so I counted them once. I searched Wikiquote for his name, "Deep Thoughts" and his book titles; those five topic pages were the only other hits. I didn't backfill from anywhere else.

Three of the twelve have a citation that isn't a clean Deep Thoughts one (Boxing: "Jack Handey view on boxing"; Sand: "as quoted in Quotes about Wisdom, Quotations Book"; Embarrassment: "in The History of the Snowman, p. 145"), so those post as just "Jack Handey" with no work named, because I won't name a work the page doesn't clearly give.

**Monty Python, 55 of 55.** Filled by treating *The Meaning of Life* and *Flying Circus* as works in the extraction script (see below). Split: Holy Grail 8 (all the page offers), Life of Brian 12 (of 21), Meaning of Life 10 (of 30), Flying Circus 25 (of 154).

## How the selection was made

1. Fetched each page from Wikiquote's API with `fetch_page` from the repo and parsed it with the bot's own `parse_page`, with the bot's User-Agent, one request at a time and a two-second pause. Revisions used are listed at the bottom.
2. Read every parsed quote. Kept the ones with humor, irreverent humor, snark or an irreverent pop-culture reference. Mencken is kept whatever the tone (his exception), so his are tagged "Mencken: any tone".
3. Dropped anything built on a slur or on mocking people for who they are, anything cruel rather than funny, quotes that need the scene around them, entries whose citation says they are only "credited" or a variation of someone else's line, and quotes with a trailing page number in the text (the text is verbatim; the only edits are whitespace and `[1]`-style markers).
4. Dialogue keeps each speaker's name on its own line, as the page gives it. Single lines from film and TV pages get the speaker in the attribution where the page names one, and never otherwise.
5. Duplicates checked by the lounge's own `quote_hash`, on both the whole entry and the quote body alone.

**One-off extraction settings (in my scratch script, not in `newsbot/`).** The bot's parser is untouched; I changed two things in the throwaway script before calling it, and only for the pages named here.

- *Page kind.* `_page_kind` was told to treat *Monty Python's The Meaning of Life*, *Monty Python's Flying Circus* and the `Futurama/Season N` pages as works, so their dialogue sections count. Without that the parser sees "author" pages and discards every dialogue block. Season 6 and 7 then yielded quotes (217 and 127); seasons 1 to 5 and 8 still yield none (their markup isn't dialogue lists the parser reads). Season 6 gave 6 of Futurama's 10; the other 4 come from the *Bender's Big Score* page.
- *Length.* The parser's 400-character cutoff (`MAX_WIKIQUOTE_CHARS`) exists for whole Wikiquote pages, and a file source has no such cutoff. What applies to a file entry is `fits()`: the rendered message (header, escaped entry) must be at most 2,000 UTF-16 units. I used that as the limit. The longest entry in the file renders at **1777** units, and only five entries run past 1,300 (the Bring Out Your Dead scene, the Holy Hand Grenade, Brian's Latin lesson, the Messiah crowd, and the blancmange tennis sketch).

**Attributions.** Short form: author or speaker, then work, then year where the page gives one: "Hunter S. Thompson, The Proud Highway (1997)". No chapters, pages, ISBNs, letter recipients or dates beyond the year. Where a citation is only "as quoted in" somewhere, or names no work (Usenet, a speech, a toast), the attribution is just the author (or "Terry Pratchett, Usenet"), because naming the secondhand source as if it were the work would be wrong. Years for TV and film are the release or first-air year (Flying Circus uses the series' first-air year: 1969, 1970, 1972, 1974); those are not all on the Wikiquote pages. The Hitchhiker's page's own attributions don't name the author, so "Douglas Adams, " is put in front of the book and year.

**Titles are plain, not in asterisks.** The bot's `esc()` escapes every asterisk, so `*Title*` would post as `\*Title\*` and Discord would show the asterisks literally. I checked the escaped output, and plain titles it is.

**Link line.** The Wikiquote source posts a "From Wikiquote: <link>" line under each quote. A plain file can't, because `esc()` puts a zero-width space after `https:` and escapes underscores, which kills the link. These post as text and the `~ ` attribution line only.

Tags are per source (a few of the Thompson, Carlin and Twain lines are more snark than humor and the other way round; I didn't hand-label each one).


## Douglas Adams (the Hitchhiker's books) (65)

**1.**

> Far out in the uncharted backwaters of the unfashionable end of the western spiral arm of the Galaxy lies a small unregarded yellow sun. Orbiting this at a distance of roughly ninety-two million miles is an utterly insignificant little blue-green planet whose ape-descended life forms are so amazingly primitive that they still think digital watches are a pretty neat idea.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Introduction
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**2.**

> "Time is an illusion. Lunchtime doubly so."
> "Very deep," said Arthur, "you should send that in to the Reader's Digest. They've got a page for people like you."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 2
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**3.**

> "This must be Thursday," said Arthur to himself, sinking low over his beer, "I never could get the hang of Thursdays."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 2
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**4.**

> The ships hung in the sky in much the same way that bricks don't.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 3
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**5.**

> "If I asked you where the hell we were," said Arthur weakly, "would I regret it?"
> Ford stood up. "We're safe," he said.
> "Oh good," said Arthur.
> "We're in a small galley cabin," said Ford, "in one of the spaceships of the Vogon Constructor Fleet."
> "Ah," said Arthur, "this is obviously some strange usage of the word safe that I wasn't previously aware of."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 5
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**6.**

> "You know," said Arthur, "it's at times like this, when I'm trapped in a Vogon airlock with a man from Betelgeuse, and about to die of asphyxiation in deep space that I really wish I'd listened to what my mother told me when I was young."
> "Why, what did she tell you?"
> "I don't know, I didn't listen."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 7
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**7.**

> "This is terrific," Arthur thought to himself, "Nelson's Column has gone, McDonald's have gone, all that's left is me and the words Mostly harmless. Any second now all that will be left is Mostly harmless. And yesterday the planet seemed to be going so well."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 7
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**8.**

> "Space," it says, "is big. Really big. You just won't believe how vastly, hugely, mindbogglingly big it is. I mean, you may think it's a long way down the road to the chemist, but that's just peanuts to space. Listen..." and so on.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 8
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**9.**

> The fabulously beautiful planet Bethselamin is now so worried about the cumulative erosion by ten billion visiting tourists a year that any net imbalance between the amount you eat and the amount you excrete while on the planet is surgically removed from your body weight when you leave: so every time you go to the lavatory there it is vitally important to get a receipt.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 8
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**10.**

> Arthur looked up. "Ford!" he said, "there's an infinite number of monkeys outside who want to talk to us about this script for Hamlet they've worked out."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**11.**

> "Ford," he said, "you're turning into a penguin. Stop it."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**12.**

> He reached out and pressed an invitingly large red button on a nearby panel. The panel lit up with the words Please do not press this button again.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**13.**

> "Come on," he droned, "I've been ordered to take you down to the bridge. Here I am, brain the size of a planet and they ask me to take you down to the bridge. Call that job satisfaction? 'Cos I don't."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**14.**

> "Sorry, did I say something wrong?" said Marvin, dragging himself on regardless. "Pardon me for breathing, which I never do anyway so I don't know why I bother to say it, oh God I'm so depressed. Here's another one of those self-satisfied doors. Life! Don't talk to me about life."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**15.**

> "If there's anything more important than my ego around, I want it caught and shot now."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 12
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**16.**

> He had found a Nutri-Matic machine which had provided him with a plastic cup filled with a liquid that was almost, but not quite, entirely unlike tea.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 17
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**17.**

> Curiously enough, the only thing that went through the mind of the bowl of petunias as it fell was Oh no, not again. Many people have speculated that if we knew exactly why the bowl of petunias had thought that we would know a lot more about the nature of the Universe than we do now.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**18.**

> "For instance, on the planet Earth, man had always assumed that he was more intelligent than dolphins because he had achieved so much—the wheel, New York, wars, and so on—whilst all the dolphins had ever done was muck about in the water having a good time. But conversely, the dolphins had always believed that they were far more intelligent than man... for precisely the same reasons."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 23
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**19.**

> "I'd far rather be happy than right any day."
> "And are you?"
> "No, that's where it all falls down, of course."
> "Pity," said Arthur with sympathy. "It sounded like quite a good lifestyle otherwise."

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 30
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**20.**

> It said: "The History of every major Galactic Civilization tends to pass through three distinct and recognizable phases, those of Survival, Inquiry and Sophistication, otherwise known as the How, Why and Where phases.
> "For instance, the first phase is characterized by the question How can we eat? the second by the question Why do we eat? and the third by the question Where shall we have lunch?"

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 35
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**21.**

> The story so far:
> In the beginning the Universe was created.
> This has made a lot of people very angry and been widely regarded as a bad move.

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 1
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**22.**

> "Share and Enjoy" is the company motto of the hugely successful Sirius Cybernetics Corporation Complaints division, which now covers the major land masses of three medium sized planets and is the only part of the Corporation to have shown a consistent profit in recent years.

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 2
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**23.**

> "Concentrate," hissed Zaphod, "on his name."
> "What is it?" asked Arthur.
> "Zaphod Beeblebrox the Fourth."
> "What?"
> "Zaphod Beeblebrox the Fourth. Concentrate!"
> "The Fourth?"
> "Yeah. Listen, I'm Zaphod Beeblebrox, my father was Zaphod Beeblebrox the Second, my grandfather Zaphod Beeblebrox the Third..."
> "What?"
> "There was an accident with a contraceptive and a time machine. Now concentrate!"

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 3
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**24.**

> "Well sir," snapped the fragile little creature, "if you could be a little cool about it..."
> "Look," said Zaphod. "I'm up to here with cool, okay? I'm so amazingly cool you could keep a side of meat in me for a month. I'm so hip I have trouble seeing over my pelvis. Now will you move before I blow it?"

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**25.**

> "Mr. Beeblebrox, sir," said the insect in awed wonder, "you're so weird you should be in movies."
> "Yeah," said Zaphod patting the thing on a glittering pink wing, "and you, baby, should be in real life."

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**26.**

> "I'm looking for someone."
> "Who?" hissed the insect.
> "Zaphod Beeblebrox," said Marvin, "he's over there."
> The insect shook with rage. It could hardly speak.
> "Then why did you ask me?"
> "I just wanted something to talk to," said Marvin.
> "What!"
> "Pathetic, isn't it?"

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**27.**

> "Have another drink," said Trillian. "Enjoy yourself."
> "Which?" said Arthur. "The two are mutually exclusive."
> "Poor Arthur, you're really not cut out for this life are you?"
> "You call this life?"
> "You're starting to sound like Marvin."
> "Marvin is the clearest thinker I know."

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 16
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**28.**

> "The first ten million years were the worst," said Marvin, "and the second ten million years, they were the worst too. The third ten million years I didn't enjoy at all. After that I went into a bit of a decline."

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**29.**

> "The best conversation I had was over forty million years ago," continued Marvin. ..."And that was with a coffee machine."

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**30.**

> "Well, I wish you'd just tell me rather than try to engage my enthusiasm," said Marvin, "because I haven't got one."

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**31.**

> "Er..." [Zarquon] said, "hello. Er, look, I'm sorry I'm a bit late. I've had the most ghastly time, all sorts of things cropping up at the last moment."
> He seemed nervous of the expectant awed hush. He cleared his throat.
> "Er, how are we for time?" he said, "have I just got a min—"
> And so the Universe ended.

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**32.**

> "I wonder who this ship belongs to anyway," said Arthur.
> "Me," said Zaphod.
> "No. Who it really belongs to."
> "Really me," insisted Zaphod, "look, property is theft, right? Therefore theft is property. Therefore this ship is mine, OK?"
> "Tell the ship that," said Arthur.

- Posts as: Douglas Adams, The Restaurant at the End of the Universe (1980)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 20
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**33.**

> For a moment or two the old man didn't reply. He was staring at the instruments with the air of one who is trying to convert Fahrenheit to centigrade in his head while his house is burning down.

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 4
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**34.**

> "My doctor says that I have a malformed public-duty gland and a natural deficiency in moral fibre," Ford muttered to himself, "and that I am therefore excused from saving Universes."

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 6
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**35.**

> "My capacity for happiness," he added, "you could fit into a matchbox without taking out the matches first." —Marvin

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 7
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**36.**

> "Voon," [the mattress] wurfed at last, "and was it a magnificent occasion?"
> "Reasonably magnificent. The entire thousand-mile-long bridge spontaneously folded up its glittering spans and sank weeping into the mire, taking everybody with it."

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 7
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**37.**

> [The Guide] had some advice to offer on drunkenness.
> "Go to it," it said, "and good luck."
> It was cross-referenced to the entry concerning the size of the Universe and the ways of coping with that.

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**38.**

> There is an art, it says, or rather, a knack to flying. The knack lies in learning how to throw yourself at the ground and miss. ... Clearly, it is this second part, the missing, which presents the difficulties.

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**39.**

> [Zaphod] sat up sharply and started to pull clothes on. He decided that there must be someone in the Universe feeling more wretched, miserable and forsaken than himself, and he determined to set out and find him.
> Halfway to the bridge it occurred to him that it might be Marvin, and he returned to bed.

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**40.**

> He hoped and prayed that there wasn't an afterlife. Then he realized there was a contradiction involved here and merely hoped that there wasn't an afterlife.

- Posts as: Douglas Adams, Life, the Universe and Everything (1982)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 33
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**41.**

> The storm had now definitely abated, and what thunder there was now grumbled over more distant hills, like a man saying "And another thing..." twenty minutes after admitting he's lost the argument.

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 3
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**42.**

> The moon was out in a watery way. It looked like a ball of paper from the back pocket of jeans that have just come out of the washing machine, which only time and ironing would tell if it was an old shopping list or a five pound note.

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 7
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**43.**

> He paused and maneuvered his thoughts. It was like watching oil tankers doing three-point turns in the English Channel.

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 9
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**44.**

> Ford: "Life," he said, "is like a grapefruit."
> Creature: "Er, how so?"
> Ford: "Well, it's sort of orangey-yellow and dimpled on the outside, wet and squidgy in the middle. It's got pips inside, too. Oh, and some people have half a one for breakfast."

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 23
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**45.**

> The Hitchhiker's Guide to the Galaxy ... says of the Sirius Cybernetics Corporation products that "it is very easy to be blinded to the essential uselessness of them by the sense of achievement you get from getting them to work at all."

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 35
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**46.**

> "I come in peace," [the silver robot] said, adding after a long moment of further grinding, "take me to your Lizard."

- Posts as: Douglas Adams, So Long, and Thanks for All the Fish (1984)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 36
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**47.**

> One of the problems has to do with the speed of light and the difficulties involved in trying to exceed it. You can't. Nothing travels faster than the speed of light with the possible exception of bad news, which obeys its own special laws.

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 1
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**48.**

> The thing they wouldn't be expecting him to do was to be there in the first place. Only an absolute idiot would be sitting where he was, so he was winning already. A common mistake that people make when trying to design something completely foolproof is to underestimate the ingenuity of complete fools.

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 12
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**49.**

> The major difference between a thing that might go wrong and a thing that cannot possibly go wrong is that when a thing that cannot possibly go wrong goes wrong it usually turns out to be impossible to get at or repair.

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 12
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**50.**

> "The insurance business is completely screwy now. You know they've reintroduced the death penalty for insurance company directors?"
> "Really?" said Arthur. "No, I didn't. For what offense?"
> Trillian frowned.
> "What do you mean, offense?"
> "I see."

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 13
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**51.**

> "I leaped out of a high-rise office window."
> This cheered Arthur up. "Oh!" he said. "Why don't you do it again?"
> "I did."
> "Hmmm," said Arthur, disappointed. "Obviously no good came of it."

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**52.**

> "What was the self-sacrifice?"
> "I jettisoned half of a much-loved and I think irreplaceable pair of shoes."
> "Why was that self-sacrifice?"
> "Because they were mine!" said Ford, crossly.
> "I think we have different value systems."
> "Well, mine's better."

- Posts as: Douglas Adams, Mostly Harmless (1992)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 18
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**53.**

> Humans are not proud of their ancestors, and rarely invite them round to dinner.

- Posts as: Douglas Adams, The Hitchhiker's Guide to the Galaxy (TV series)
- Full citation on the page: The Hitchhiker's Guide to the Galaxy, TV Series, Episode 1
- Wikiquote page: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**54.**

> Driving a Porsche in London is like bringing a Ming vase to a football game.

- Posts as: Douglas Adams
- Full citation on the page: Douglas Adams, As quoted in Don't Panic: The Official Hitchhikers Guide to the Galaxy Companion (1988) by Neil Gaiman
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**55.**

> A learning experience is one of those things that say, "You know that thing you just did? Don't do that."

- Posts as: Douglas Adams, The Daily Nexus (2000)
- Full citation on the page: Douglas Adams, Interview in The Daily Nexus (5 April 2000), reprinted in The Salmon of Doubt
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**56.**

> SHOEBURYNESS (abs.n.) The vague uncomfortable feeling you get when sitting on a seat which is still warm from somebody else's bottom

- Posts as: Douglas Adams, The Meaning of Liff (1983)
- Full citation on the page: Douglas Adams, The Meaning of Liff (1983)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**57.**

> WOKING (vb.) To enter the kitchen with the precise determination to perform something only to forget what it is just before you do it.

- Posts as: Douglas Adams, The Meaning of Liff (1983)
- Full citation on the page: Douglas Adams, The Meaning of Liff (1983)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**58.**

> The seat received him in a loose and distant kind of way, like an aunt who disapproves of the last fifteen years of your life and will therefore furnish you with a basic sherry, but refuses to catch your eye.

- Posts as: Douglas Adams, Dirk Gently's Holistic Detective Agency (1987)
- Full citation on the page: Douglas Adams, Dirk Gently's Holistic Detective Agency (1987)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**59.**

> Thor was the God of Thunder and, frankly, acted like it.

- Posts as: Douglas Adams, The Long Dark Tea-Time of the Soul (1988)
- Full citation on the page: Douglas Adams, The Long Dark Tea-Time of the Soul (1988), Ch. 7
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**60.**

> The Great Zaganza said: "You are very fat and stupid and persistently wear a ridiculous hat which you should be ashamed of."

- Posts as: Douglas Adams, The Long Dark Tea-Time of the Soul (1988)
- Full citation on the page: Douglas Adams, The Long Dark Tea-Time of the Soul (1988), Ch. 35
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**61.**

> "Stotting" is jumping upward with all four legs simultaneously. My advice: do not die until you've seen a large black poodle stotting in the snow.

- Posts as: Douglas Adams, The Salmon of Doubt (2002)
- Full citation on the page: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**62.**

> Anything that is in the world when you're born is normal and ordinary and is just a natural part of the way the world works. Anything that's invented between when you're fifteen and thirty-five is new and exciting and revolutionary and you can probably get a career in it. Anything invented after you're thirty-five is against the natural order of things.

- Posts as: Douglas Adams, The Salmon of Doubt (2002)
- Full citation on the page: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**63.**

> The hotel shop only had two decent books, and I'd written both of them.

- Posts as: Douglas Adams, The Salmon of Doubt (2002)
- Full citation on the page: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**64.**

> I love deadlines. I love the whooshing noise they make as they go by.

- Posts as: Douglas Adams, The Salmon of Doubt (2002)
- Full citation on the page: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**65.**

> My favourite piece of information is that Branwell Brontë, brother of Emily and Charlotte, died standing up leaning against a mantelpiece, in order to prove it could be done. This is not quite true, in fact. My absolute favourite piece of information is the fact that young sloths are so inept that they frequently grab their own arms and legs instead of tree limbs, and fall out of trees.

- Posts as: Douglas Adams, The Salmon of Doubt (2002)
- Full citation on the page: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote page: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent


## Monty Python (55)

**1.**

> Dead Collector: Bring out jer dead!
> [A large man appears with a (seemingly) dead man over his shoulder]
> Large Man: Here's one.
> Dead Collector: Nine pence.
> "Dead" Man: I'm not dead.
> Dead Collector: What?
> Large Man: Nothing. [hands the collector his money] Here's your nine pence.
> "Dead" Man: I'm not dead!
> Dead Collector: 'Ere, he says he's not dead.
> Large Man: Yes he is.
> "Dead" Man: I'm not.
> Dead Collector: He isn't.
> Large Man: Well, he will be soon, he's very ill.
> "Dead" Man: I'm getting better.
> Large Man: No you're not, you'll be stone dead in a moment.
> Dead Collector: Well, I can't take him like that. It's against regulations.
> "Dead" Man: I don't want to go on the cart.
> Large Man': Oh, don't be such a baby.
> Dead Collector: I can't take him.
> "Dead" Man: I feel fine.
> Large Man: Oh, do me a favor.
> Dead Collector: I can't.
> Large Man: Well, can you hang around for a couple of minutes? He won't be long.
> Dead Collector: I promised I'd be at the Robinsons'. They've lost nine today.
> Large Man: Well, when's your next round?
> Dead Collector: Thursday.
> "Dead" Man: I think I'll go for a walk.
> Large Man: You're not fooling anyone, you know. Isn't there anything you could do?
> "Dead" Man: I feel happy. I feel happy.
> [The collector paces for an idea, then whacks the body with his club, solving the problem]
> Large Man: Ah, thank you very much.
> Dead Collector: Not at all. See you on Thursday.
> Large Man: Right.

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: Bring out your dead, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**2.**

> [Arthur and Patsy "ride" through the village]
> Large Man: Who's that then?
> Dead Collector: I dunno. Must be a king.
> Large Man: Why?
> Dead Collector: He hasn't got shit all over him.

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: Must be a king, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**3.**

> Bedevere: How do you know she is a witch?
> Peasant: She looks like one.
> [Crowd indistinctly shouts]
> Bedevere: Bring her forward!
> Girl: I'm not a witch.
> Bedevere: But you are dressed as one...
> Girl: They dressed me up like this. [Crowd murmurs]
> Girl: And this isn't my nose. This is a false one.
> Bedevere: [inspects the nose and confirms] Well?
> Peasant: Well, we did do the nose.
> Bedevere: The nose?
> Peasant: And the hat. She's a witch!
> Peasant Crowd: Burn her!
> Bedevere: Did you dress her up like this?
> Peasant Crowd: No, no, no! [beat] Yes, yes. A bit. But she's got a wart.
> Bedevere: What makes you think she is a witch?
> Peasant: Well, she turned me into a newt!
> [Bedevere gives him a disbelieving look]
> Bedevere: A newt?
> [Silence]
> Peasant: I got better.
> Peasant Crowd: Burn her anyway!

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: Turned me into a newt, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**4.**

> [King Arthur and his knights "ride" through grassy plains when suddenly Divine male on clouds speaks off-screen]
> God: Arthur! Arthur! King of the Britons!
> [The knights grovel]
> God: [In an annoyed tone] Oh, don't grovel! If there's one thing I can't stand, it's people groveling.
> [The knights stand back up]
> King Arthur: Sorry.
> God: And don't apologize! Every time I try to talk to somebody it's "sorry this," and, "forgive me that," and, "I'm not worthy." What are you doing now?!
> [The camera cuts to Arthur and his knights averting their gaze from God]
> King Arthur: I'm averting my eyes, oh Lord.
> God: Well don't! It's like those miserable psalms. They're so depressing. Now knock it off!
> King Arthur: Yes, Lord!

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: The quest for the holy grail!, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**5.**

> Frenchman: You don't frighten us, English pig-dogs! Go and boil your bottoms, sons of a silly person! I blow my nose at you, so-called Ah-thoor Keeng, you and all your silly English K-n-n-n-n-n-n-n-niggets! [makes taunting gestures at them]
> Sir Galahad: What a strange person.
> King Arthur: Now, look here, my good man--
> Frenchman: I don't want to talk to you no more, you empty-headed animal food trough wiper! I fart in your general direction! Your mother was a hamster and your father smelt of elderberries!
> Sir Galahad: Is there someone else up there we could talk to?
> Frenchman: No, now go away or I shall taunt you a second time!

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: French taunts, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent
- **owner check**: the French taunter's spelling of 'knights' looks like a slur on screen

**6.**

> Head Knight: The Knights Who Say Ni demand a sacrifice!
> King Arthur: Knights of Ni, we are but simple travelers who seek the enchanter who lives beyond these woods--
> Knights who say Ni: Ni! Ni! Ni! Ni!
> King Arthur: Oh, ow!
> Head Knight: We shall say "Ni" again to you, if you do not appease us.
> King Arthur: Well, what do you want?
> Head Knight: We want... a shrubbery!! [jarring chord]

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: Knights who say Ni, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**7.**

> Sir Lancelot: Look, my liege! [trumpets blare to a shot of a castle]
> King Arthur: [awed] Camelot!
> Sir Galahad: Camelot!
> Sir Lancelot: Camelot!
> Patsy: It's only a model.
> King Arthur: Shhoosh! Knights, I bid you welcome to your new home. Let us ride to Camelot!
> [The inhabitants of Camelot sing "Knights of the Round Table"]
> Knights of the Round Table: [singing and dancing] We're knights of the Round Table, we dance whene'er we're able. We do routines and chorus scenes with footwork impeccable, We dine well here in Camelot, we eat ham and jam and Spam a lot. / We're knights of the Round Table, our shows are for-mi-dable. But many times we're given rhymes that are quite un-sing-able, We're opera mad in Camelot, we sing from the diaphragm a lot. / In war we're tough and able, Quite in-de-fa-ti-gable. Between our quests we sequin vests and impersonate Clark Gable / It's a busy life in Camelot.
> Knight: [somberly] I have to push the pram a lot.
> [Cut back to Arthur]
> King Arthur: On second thought, let's not go to Camelot. It is a silly place.

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: It's a silly place, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**8.**

> King Arthur: [Holding the Holy Hand Grenade of Antioch] How does it... um... how does it work?
> Sir Lancelot: I know not, my liege.
> King Arthur: Consult the Book of Armaments.
> Brother Maynard: Armaments, chapter two, verses nine through twenty-one.
> Cleric: [reading] And Saint Attila raised the hand grenade up on high, saying, "O Lord, bless this thy hand grenade, that with it thou mayst blow thine enemies to tiny bits, in thy mercy." And the Lord did grin. And the people did feast upon the lambs, and sloths, and carp, and anchovies, and orangutans, and breakfast cereals, and fruit bats, and large chunks...
> Brother Maynard: Skip a bit, Brother...
> Cleric: And the Lord spake, saying, "First shalt thou take out the Holy Pin. Then shalt thou count to three, no more, no less. Three shall be the number thou shalt count, and the number of the counting shall be three. Four shalt thou not count, neither count thou two, excepting that thou then proceed to three. Five is right out. Once the number three, being the third number, be reached, then lobbest thou thy Holy Hand Grenade of Antioch towards thy foe, who, being naughty in My sight, shall snuff it.
> Brother Maynard: Amen.
> All: Amen.
> King Arthur: Right. One... two... five!
> Galahad: Three, sir.
> King Arthur: Three! [throws the grenade]

- Posts as: Monty Python and the Holy Grail (1975)
- Full citation on the page: Holy hand grenade, Monty Python and the Holy Grail
- Wikiquote page: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**9.**

> What Jesus blatantly fails to appreciate is that it's the meek who are the problem.

- Posts as: Reg, Monty Python's Life of Brian (1979)
- Full citation on the page: Reg, Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**10.**

> Now, you listen here: 'e's not the Messiah, 'e's a very naughty boy! Now go away!

- Posts as: Mandy, Monty Python's Life of Brian (1979)
- Full citation on the page: Mandy, Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**11.**

> Oh, what I wouldn't give to be spat at in the face. I sometimes hang awake at night dreaming of being spat at in the face.

- Posts as: Prisoner, Monty Python's Life of Brian (1979)
- Full citation on the page: Prisoner, Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**12.**

> You lucky bastards! You lucky, jammy bastards!

- Posts as: Prisoner, Monty Python's Life of Brian (1979)
- Full citation on the page: Prisoner, Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**13.**

> Mandy: So you're astrologers, are you? Well what is he then?
> Wise man: Mmmm?
> Mandy: What star sign is he?
> Wise man: Well, Capricorn.
> Mandy: Ehh, Capricorn, eh? What are they like?
> Wise men: He is the son of God, our Messiah. King of the Jews.
> Mandy: And that's Capricorn, is it?
> Wise man: No, no, no. That's just him.
> Mandy: Ohh, I was going to say, 'Otherwise, there'd be a lot of them.'

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**14.**

> [The audience members at the back of the crowd are having trouble hearing the Sermon on the Mount]
> Man: I think it was, "Blessed are the cheesemakers"!
> Gregory's wife: What's so special about the cheesemakers?
> Gregory: Well, obviously it's not meant to be taken literally. It refers to any manufacturer of...dairy products.

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**15.**

> Brian: There's no pleasing some people.
> Ex-leper: That's just what Jesus said, sir.

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**16.**

> [Brian is caught defacing a wall at night]
> Centurion: What's this then? "Romanes eunt domus"? "People called Romanes, they go the 'ouse"?
> Brian: It–it says "Romans go home".
> Centurion: No it doesn't. What's Latin for "Roman"? Come on. Come on!
> Brian: "Romanus"?
> Centurion: Goes like?
> Brian: "Annus"?
> Centurion: Vocative plural of "annus" is...?
> Brian: "Anni."
> Centurion: [writing] "Romani". "Eunt"? What is "eunt"?
> Brian: "Go".
> Centurion: Conjugate the verb "to go".
> Brian: Ire, eo, is, it, imus, itis, eunt.
> Centurion: So "eunt" is?
> Brian: Third person plural, present indicative. "They go".
> Centurion: But "Romans go home" is an order, so you must use the? [tugs on Brian's ear]
> Brian: Ah, imperative?
> Centurion: Which is?
> Brian: Uh, uhm, "I"! "I"!
> Centurion: How many Romans?
> Brian: Aah! Plural, plural! "Ite"! "Ite"!
> Centurion: [writing] "Ite". "Domus"? Nominative? "Go home", this is motion towards, isn't it, boy?
> Brian: Dative? [centurion angrily draws his sword to Brian's throat] Ah! Not dative! Not the dative, sir! Ah! Ah! Oh! Accusative, accusative! "Domum", sir. "Ad domum".
> Centurion: Except that "domus" takes the?
> Brian: The locative, sir?
> Centurion: Which is?
> Brian: "Domum"!
> Centurion: "Domum". [writing] "Um". Understand?
> Brian: Yes, sir.
> Centurion: Now write it out a hundred times.
> Brian: Yes sir. Thank you, sir. Hail Caesar sir.
> Centurion: Hail Caesar. If it's not done by sunrise, I'll cut your balls off!
> Brian: Oh, thank you sir. Thank you, sir. Hail Caesar and everything, sir!
> [At sunrise, the wall is covered in writing]
> Brian: Finished!
> Centurion: Right. Now don't do it again. [leaves]
> [Brian climbs down the ladder and looks at the wall, then sees the morning guards approach and runs.]

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**17.**

> Reg: All right, but apart from the sanitation, the medicine, education, wine, public order, irrigation, roads, the fresh-water system, and public health, what have the Romans ever done for us?
> PFJ Member: Brought peace?
> Reg: Oh, peace? SHUT UP!

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**18.**

> Simon: Tell them to stop it. I hadn't said a word for eighteen years till he came along.
> Crowd: A miracle! He is the Messiah!
> Simon: Well, he hurt my foot!
> Crowd: Hurt my foot, Lord! Hurt my foot. Hurt mine...
> Arthur: Hail, Messiah! [kneels]
> Brian: I'm not the Messiah!
> Arthur: I say you are, Lord, and I should know, I've followed a few!
> Crowd: Hail, Messiah!
> Brian: I'm not the Messiah! Will you please listen?! I'm not the Messiah, do you understand?! Honestly!
> Woman: [pauses] Only the true Messiah denies his divinity!
> Brian: What?! Well, what sort of chance does that give me?! All right, I am the Messiah!
> Crowd: He is! He is the Messiah! [bow]
> Brian: Now, FUCK OFF!!!
> [Silence]
> Arthur: How shall we fuck off, oh Lord?
> Brian: Oh, just go away! Leave me alone!
> Simon: You told these people to eat my juniper berries. You break my bloody foot. You break my vow of silence, and then you try and clean up on my juniper bushes! [strangles Brian]
> Brian: Oh, lay off!
> Arthur: [stops Simon from strangling him] This is the Messiah, the Chosen One!
> Simon: No, he's not. [strangles Brian again]
> Arthur: AN UNBELIEVER!
> Crowd: An Unbeliever!
> Arthur: Persecute! Kill the heretic!
> [The Crowd grab Simon and carried him away to his death]
> Brian: Leave him alone! Leave him alone! Leave him alone. Put him down. Please!
> [As the crowd leaves, Judith appears]
> Judith: Brian.
> Brian: Judith.

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent
- **owner check**: F-word and a mob lynching; the joke is the crowd, check

**19.**

> Brian: Look, you've got it all wrong! You don't need to follow me. You don't need to follow anybody! You've got to think for yourselves! You're all individuals!
> Crowd: [in unison] Yes! We're all individuals!
> Brian: You're all different!
> Crowd: [in unison] Yes, we are all different!
> Man in crowd: I'm not...
> Crowd: Shhh!

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**20.**

> Nisus Wettus: Crucifixion?
> Mr. Cheeky: Ah, no. Freedom.
> Nisus Wettus: What?
> Mr. Cheeky: Eh, freedom for me. They said I hadn't done anything, so I can go free and live on an island somewhere.
> Nisus Wettus: Oh, oh that´s jolly good well. Off you go then.
> Mr. Cheeky: No, I'm only pulling your leg, it's crucifixion really.
> Nisus Wettus: [laughing] Oh, I see, very good. Well...
> Mr. Cheeky: Yes I know, out the door, one cross each, line on the left.

- Posts as: Monty Python's Life of Brian (1979)
- Full citation on the page: Monty Python's Life of Brian
- Wikiquote page: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**21.**

> Obstetrician 1: Get the EEG, the BP monitor, and the AVV.
> Obstetrician 2: And get the machine that goes "Ping!".
> Obstetrician 1: And get the most expensive machine - in case the Administrator comes.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part I: The Miracle of Birth, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**22.**

> [As the doctors drop the baby into an incubator, the mother looks up]
> Patient: Is it a boy or a girl?
> Obstetrician 1: Now, I think it's a little early to start imposing roles on it, don't you? Now, a word of advice. You may find that you suffer for some time a totally irrational feeling of depression. PND is what we doctors call it. So it's lots of happy pills for you, and you can find out all about the birth when you get home. It's available on Betamax, VHS, and Super 8.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part I: The Miracle of Birth, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent
- **owner check**: a doctor's joke about imposing roles on a baby; mild, check

**23.**

> Mr Blackitt: When Martin Luther nailed his protest up to the church door in 1517, he may not have realised the full significance of what he was doing, but four hundred years later, thanks to him, my dear, I can wear whatever I want on my John Thomas. And Protestantism doesn't stop at the simple condom. Oh, no! I can wear French Ticklers if I want.
> Mrs Blackitt: You what?
> Mr Blackitt: French Ticklers, Black Mambos, Crocodile Ribs...Sheaths that are designed not only to protect but also to enhance the stimulation of sexual congress.
> Mrs Blackitt: Have you got one?
> Mr Blackitt: Have I got one? Well, no. But I can go down the road any time I want and walk into Harry's and hold my head up high, and say in a loud steady voice: 'Harry I want you to sell me a condom. In fact, today I think I'll have a French Tickler, for I am a Protestant.'
> Mrs Blackitt: Well, why don't you?
> Mr Blackitt: But they! They cannot. Because their Church never made the great leap out of the Middle Ages, and the domination of alien episcopal supremacy.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part I: The Miracle of Birth, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent
- **owner check**: Protestant condom sermon; adult-only joke, check

**24.**

> Headmaster: [supposedly reading from The Bible] And spotteth twice they the camels before the third hour. And so the Midianites went forth to Ram Gilead in Kadesh Bilgemath by Shor Ethra Regalion, to the house of Gash-Bil-Betheul-Bazda, he who brought the butter dish to Balshazar and the tent peg to the house of Rashomon, and there slew they the goats, yea, and placed they the bits in little pots. Here endeth the lesson.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part II: Growth and Learning, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**25.**

> Chaplain and students: [singing a hymn]
> O Lord, please don't burn us. Don't grill or toast your flock. Don't put us on the barbecue Or simmer us in stock. Don't braise or bake or boil us, Or stir-fry us in a wok.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part II: Growth and Learning, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**26.**

> Ainsworth: During the night old Perkins got his leg bitten sort of...off.
> Dr. Livingstone: Eh? Been in the wars, have we? Well, let's take a look at this one leg of yours. Yes...Yes, well, this is nothing to worry about.
> Perkins: Oh, good.
> Dr. Livingstone: There's a lot of it about - probably a virus. Keep warm, plenty of rest, and if you're playing any football try and favour the other leg.
> Perkins: So it'll just grow back again, will it?
> Dr. Livingstone: Er...I think I'd better come clean with you about this. It's not a virus, I'm afraid. You see, a virus is what we doctors call 'very, very small'. So small, it could not possibly have made off with the whole leg. What we're looking for here for is, I think - and this is no more than an educated guess, I'd like to make that clear - is some multicellular life form with stripes, huge razor-sharp teeth, about eleven feet long, and of the genus felis horribilis - what we doctors, in fact, call a tiger.
> Pakenham-Walsh, Ainsworth and Perkins: A tiger?!
> Soldiers and warriors: [outside the tent] A tiger?!
> [The African warriors flee in terror]
> Pakenham-Walsh: A tiger in Africa?
> Ainsworth: Erm, well, it's probably escaped from the zoo.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part III: Fighting Each Other, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**27.**

> Mrs. Hendy: Oh! I never knew that Schopenhauer was a philosopher!
> Mr. Hendy: Oh, yeah! He's the one that begins with an s, like Nietzsche.
> Mrs. Hendy: Does Nietzsche begin with an S?
> Mr. Hendy: There's an s in Nietzsche.
> Mrs. Hendy: Oh, wow! Yes there is. Do all philosophers have an s in them?
> Mr. Hendy: Yeah, I think most of them do.
> Mrs. Hendy: Oh. Does that mean Salena Jones is a philosopher?
> Mr. Hendy: Right, she could be. She sings about the meaning of life.
> Mrs. Hendy: Yeah, that's right, but I don't think she writes her own material.
> Mr. Hendy: No. Maybe Schopenhauer writes her material?
> Mrs. Hendy: No. Burt Bacharach writes it.
> Mr. Hendy: There's no s in Burt Bacharach.
> Mrs. Hendy: Or in Hal David.
> Mr. Hendy: Who's Hal David?
> Mrs. Hendy: He writes the lyrics, Burt just writes the tunes, only now he's married to Carole Bayer Sager.
> [Pause]
> Mr. Hendy: Waiter! This conversation isn't very good!

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part IV: Middle Age, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**28.**

> Chairman: Item six on the agenda, the Meaning of Life. Now Harry, you've had some thoughts on this.
> Harry: That's right, yeah. I've had a team working on this over the past few weeks, and what we've come up with can be reduced to two fundamental concepts. One, people are not wearing enough hats. Two, matter is energy. In the Universe there are many energy fields which we cannot normally perceive. Some energies have a spiritual source which act upon a person’s soul. However, this soul does not exist ab initio as orthodox Christianity teaches; it has to be brought into existence by a process of guided self-observation. However, this is rarely achieved owing to man's unique ability to be distracted from spiritual matters by everyday trivia.
> [Pause]
> Max: What was that about hats again?

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part V: Live Organ Transplants, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**29.**

> [Maître-D offers Mr Creosote an after-dinner mint]
> Maître-D: It's only wafer thin.
> Mr Creosote: Look. I couldn't eat another thing. I'm absolutely stuffed. Bugger off.
> Maître-D: Oh, sir, just— just one.
> Mr Creosote: All right. Just one.
> Maître-D: Just the one, monsieur. Voilà. [places the mint in his mouth] Bon appétit! [dives behind a cordon as Mr Creosote swells up and explodes] Thank you, sir, and now here's the check.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part VI: The Autumn Years, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**30.**

> Geoffrey: Now look here. You barge in here quite uninvited, break glasses and announce quite casually that we're all dead. Well, I would remind you that you are a guest in this house, and-
> Grim Reaper: [pokes Geoffrey in the eye] Be quiet! You Englishmen! You're all so fucking pompous. None of you have got any balls!
> American wife: Can I just ask you a question?
> Grim Reaper: What?
> American wife: How can we all have died at the same time?
> Grim Reaper: [turns and points] The salmon mousse.
> [Everyone except the Grim Reaper is shocked by this revelation]
> Geoffrey: Darling, you didn't use canned salmon, did you?
> English wife: I'm most dreadfully embarrassed.
> Grim Reaper: Now, the time has come. Follow. Follow me.
> [Geoffrey stands up, picks up a pistol and fires 5 shots, which all pass through the Grim Reaper, who turns round]
> Geoffrey: [sheepishly] Just testing...sorry
> Grim Reaper: Follow me...now.

- Posts as: Monty Python's The Meaning of Life (1983)
- Full citation on the page: Part VII: Death, Monty Python's The Meaning of Life
- Wikiquote page: [Monty Python's The Meaning of Life](https://en.wikiquote.org/wiki/Monty_Python's_The_Meaning_of_Life)
- Tags: irreverent

**31.**

> Pepperpot 1: I can't tell the difference between Whizzo butter and this dead crab.
> Interviewer: Yes, we find that 9 out of 10 British housewives can't tell the difference between Whizzo Butter and a dead crab.
> Various Pepperpots: It's true...We can't...No.
> Pepperpot 2: Here. Here! You're on television, aren't you?
> Interviewer: [humbly] Yes, yes...
> Pepperpot 2: He does the thing with one of those silly women who can't tell Whizzo Butter from a dead crab.
> Various Pepperpots: Yeah, yeah.
> Pepperpot 3: You try that around here, young man, and we'll slit your face.
> Pepperpot 4: [quietly] Yeah, with a razor.

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, Whither Canada? [1.01], Whizzo Butter, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**32.**

> Reporter: This morning, shortly after 11:00, comedy struck this little house on Dibley Road. Sudden...violent...comedy.

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, Whither Canada? [1.01], The Funniest Joke in the World, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**33.**

> Superman One: Oh look...is it a stockbroker?
> Superman Two: Is it a quantity Surveyor?
> Superman Three: Is it a church warden?
> All Supermen: NO! It's Bicycle Repair Man!

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, How to Recognise Different Types of Trees From Quite a Long Way Away [1.03], Bicycle Repair Man, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**34.**

> Arthur Nudge: Eh? Know what I mean? Know what I mean? Nudge, nudge! Know what I mean? Say no more! A nod's as good as a wink to a blind bat, say no more, say no more!
> Man: Look, are you insinuating something?
> Arthur Nudge: Oh, no no no no...yes.
> Man: Well?
> Arthur Nudge: Well, you're a man of the world, squire...you've been there, you've been around.
> Man: What do you mean?
> Arthur Nudge: Well, I mean, you've done it...you've slept...with a lady.
> Man: Yes.
> Arthur Nudge: What's it like?

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, How to Recognise Different Types of Trees From Quite a Long Way Away [1.03], Nudge, Nudge, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**35.**

> Superintendant Praline: Next we have number four - "Crunchy Frog". Am I right in thinking there's a real frog in here?
> Mr. Milton: Yes, a little one.
> Superintendant Praline: What sort of frog?
> Mr. Milton: A dead frog.
> Superintendant Praline: Is it cooked?
> Mr. Milton: No.
> Superintendant Praline: What, a raw frog?!
> Mr. Milton: We use only the finest baby frogs, dew picked and flown from Iraq, cleansed in finest quality spring water, lightly killed, and then sealed in a succulent Swiss quintuple smooth treble cream milk chocolate envelope and lovingly frosted with glucose.
> Superintendant Praline: That's as may be, it's still a frog.
> Mr. Milton: Well, what else?
> Superintendant Praline: Don't you even take the bones out?
> Mr. Milton: If we took the bones out, it wouldn't be crunchy, would it?

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, It's the Arts [1.06], Crunchy Frog sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**36.**

> [Mr. Salzburg has just fired two of his writers for his new film, and is closing in on another one]
> Mr. Salzburg: You!
> Writer 4: Ah, well, I...I think it's an excellent idea!
> Mr. Salzburg: Are you a yes man?
> Writer 4: No! I mean, I have a few things against it!
> Mr. Salzburg: So you think it's lousy!
> Writer 4: No, no! I mean, it takes time!
> Mr. Salzburg: ARE YOU BEING INDECISIVE?
> Writer 4: Yo! Nes! Perhaps! [runs out the door]

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, It's the Arts [1.06], 20th Century Vole, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**37.**

> Police officer: A blancmange, eh?
> Woman: That's right. I was just playing a game of doubles with Sandra, Jocasta, Alec and David, when...
> Police Officer: Hold on. That's five. Five people! How'd you play doubles with five people? Sounds a bit funny if you ask me, playing doubles with five people!
> Woman: Well, we often play like that. Jocasta plays on the side, receiving service. It helps to speed the game up and make it a lot faster, and Jocasta isn't left out.
> Police Officer: Look, are you asking me to believe that the five of you was playing doubles, while on the very next court there was a blancmange playing by itself?
> Woman: That's right.
> Police Officer: Well, answer me this, then: Why didn't Jocasta play the blancmange at singles, while you and Sandra and Alec and David played a proper game of doubles with four people?
> Woman: Because Jocasta always plays with us! She's a friend of ours!
> Police Officer: Call that friendship? Messing up a perfectly good game of doubles?
> Woman: It's not messing it up, officer! We like to play with five!
> Police Officer: Look, it's your affair if you want to play with five people, but don't go calling it doubles! At Wimbledon, if Fred Stolle and Tony Roche played Charlie Passarell and Cliff Drysdale and Peaches Bartkowicz, they wouldn't go calling it doubles!
> Woman: Well, what about the blancmange?
> Police Officer: That could play Ann Haydon-Jones and her husband, Pip!

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, You're No Fun Anymore [1.07], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**38.**

> Colonel: Watkins, why did you join the army?
> Watkins: For the water-skiing and the travel, sir. Not for the killing, sir. I asked them to put it on my form, sir: "no killing".
> Colonel: Watkins, are you a pacifist?
> Watkins: No, sir. I'm not a pacifist, sir: I'm a coward.

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, Full Frontal Nudity [1.08], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**39.**

> Mr. Praline: It's not pining, it's passed on! This parrot is no more! It has ceased to be! It's expired and gone to meet its maker! This is a late parrot! It's a stiff! Bereft of life, it rests in peace! If you hadn't nailed it to the perch, it would be pushing up the daisies! It's run down the curtain and joined the choir invisible! This is an ex-parrot!

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, Full Frontal Nudity [1.08], Dead Parrot Sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**40.**

> Woman: Well, I object to all this sex on the television. I mean, I keep falling off.

- Posts as: Monty Python's Flying Circus (1969)
- Full citation on the page: Series 1, The Ant, an Introduction [1.09], Mt. Kilimanjaro sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**41.**

> Interviewer: Minister, I'll put the first question to you. In your plan, "A Better Britain For Us", you promised to build 88 thousand million billion houses a year in the greater London area alone. In fact, you've built only three in the last 15 years. Are you a bit disappointed in this result?
> Minister: No, no. I'd like to answer this question, if I may, in two ways: Firstly, in my normal voice; and then in a kind of silly, high-pitched whine.

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, Face the Press [2.01], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**42.**

> Reg: I don't know! Mr. Wentworth just told me to come in here and say that there was trouble at the mill, that's all! I didn't expect a kind of Spanish Inquisition!
> [Three men in red uniforms burst through the door]
> Cardinal Ximinez: Nobody expects the Spanish Inquisition!

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, The Spanish Inquisition [2.02], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**43.**

> Doctor: [emerging from under a Scotsman's kilt] Look, would you please go away? I'm trying to examine this man! It's all right, I'm a doctor...actually I'm a gynecologist, but this is my lunch hour.

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, Déjà Vu [2.03], The Poet McTeagle, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**44.**

> Man at Less Naughty Chemist's: I'd like some aftershave.
> Less Naughty Chemist: Certainly sir, walk this way please...
> Man at Less Naughty Chemist's: If I could walk that way I wouldn't need aftershave.

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, The Buzz Aldrin Show [2.04], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**45.**

> Announcer #1: Well, it's five past nine and nearly time for six past nine. On BBC 2 now, it'll shortly be six and a half minutes past nine. Later on this evening, it'll be ten o'clock and at 10:30 we'll be joining BBC 2 in time for 10:33, and don't forget tomorrow when it'll be 9:20. Those of you who missed 8:45 on Friday will be able to see it again this Friday at a quarter to nine. Now, here is a time check. It's six and a half minutes to the big green thing.
> Announcer #2: You're a looney.
> Announcer #1: I get so bored. I get so bloody bored.

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, It's A Living [2.06], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**46.**

> Mr. Brando: Yes, we have quite a number of idiots banking here.
> Interviewer: What kind of money is there in idioting?
> Mr. Brando: Well, nowadays the really blithering idiot can make anything up to 10,000 pounds a year if he's the head of some big industrial combine. But of course the more old fashion idiot still refuses to take money. He takes bits of string, wood, dead budgerigars, sparrows, anything. But it does make the cashier's job very difficult.

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, The Attila the Hun Show [2.07], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**47.**

> TV Announcer: It's just gone eight o'clock and time for the penguin on top of your television set to explode.
> [The penguin explodes]
> Pepperpot 1: How did he know that was going to happen?
> TV Announcer: It was an inspired guess. And now...

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, How to Recognise Different Parts of the Body [2.09], Exploding Penguin sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**48.**

> Mr. Last: You've got a pet halibut?
> Mr. Praline: Yes. I chose him out of a thousand. I didn't like the others; they were all too flat.
> Mr. Last: You're a loony!
> Mr. Praline: I AM NOT A LOONY! Why should I be tarred with the epithet "loony" merely because I have a pet halibut? I've heard tell that Sir Gerald Nabarro has a pet prawn called Simon, and you wouldn't call Sir Gerald a loony, would you? Furthermore, Dawn Pelforth, the lady show jumper, had a clam called Sir Stefford after the late Chancellor, Allen Bullock has two pikes, both called Norman, and the late, great Marcel Proust had a haddock! If you're calling the author of À la recherche du temps perdu a loony, I shall have to ask you to step outside!

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, Scott of the Antarctic [2.10], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**49.**

> Mr. Vibrating: I'm very sorry, but I'm not allowed to argue unless you've paid.
> Man: Aha! If I didn't pay, then why are you arguing? Got you!
> Mr. Vibrating: No you haven't.
> Man: Yes I have. If you're still arguing, I must have paid.
> Mr. Vibrating: Not necessarily. I could be arguing in my spare time.
> Man: Oh, I've had enough of this!
> Mr. Vibrating: No, you haven't.
> Man: Oh, shut up!

- Posts as: Monty Python's Flying Circus (1972)
- Full citation on the page: Series 3, The Money Programme [3.03], The Argument Clinic, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**50.**

> Mrs Bun: Have you got anything without Spam in it?
> Waitress: Well, Spam, egg, sausage, and Spam; that's not got much Spam in it.
> Mrs Bun: I don't want any Spam!
> Mr Bun: Why can't she have egg, bacon, Spam, and sausage?
> Mrs Bun: That's got Spam in it!
> Mr Bun: Not as much as Spam, egg, sausage, and Spam.
> Mrs. Bun: Look, could I have egg, bacon, Spam and sausage without the Spam?
> Waitress: Bleurgh!
> Mrs. Bun: What do you mean "Ugh?" I don't like Spam!
> Vikings: [singing] Spam, Spam, Spam, Spam... Lovely Spam! Wonderful Spam!
> Waitress: [banging spoon on pot] SHUT UP! SHUT UP! SHUT UP! You can't have egg, bacon, Spam and sausage without the Spam.
> Mrs. Bun: Why not?!
> Waitress: WELL, it wouldn't be egg, bacon, Spam and sausage, would it?
> Mrs. Bun: I DON'T LIKE SPAM!
> Mr. Bun: Now don't make a fuss, dear; I'll have your Spam. I love it! I'm having Spam, Spam, Spam, Spam, Spam, Spam, baked beans, Spam, Spam and Spam!
> Waitress: Baked beans are off.
> Mr. Bun: In that case, can I have Spam instead?
> Waitress: You mean Spam, Spam, Spam, Spam, Spam, Spam, Spam, Spam, Spam, Spam, and Spam?
> Mr. Bun: Yes!
> Waitress: Bleurgh!

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, Spam [2.12], Spam, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**51.**

> Sailor 1: Still no sign of land. How long is it?
> Sailor 2: That's a rather personal question, sir.
> Sailor 1: You stupid git! I meant 'how long have we been in the lifeboat?' You've spoiled the atmosphere!
> Sailor 2: Sorry, sir.
> Sailor 1: Shut up! We'll have to start again. Still no sign of land. How long is it?
> Sailor 2: 33 days, sir.
> Sailor 3: 33 days?
> Sailor 2: I don't think we can hold out much longer. I don't think I did spoil the atmosphere that time.
> Sailor 1: Shut up!
> Sailor 2: Well, I don't think I did!
> Sailor 1: Of course you did!
> Sailor 1: Do you think I spoiled the atmosphere?
> Sailor 3: Yes, I think you did.
> Sailor 1: Look, shut up! Shut up! Still no sign of land. How long is it?
> Sailor 2: 33 days, sir.
> Sailor 4: Have we started again?

- Posts as: Monty Python's Flying Circus (1970)
- Full citation on the page: Series 2, Royal Episode 13 (or The Queen Will Be Watching) [2.13], Lifeboat Sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**52.**

> Mrs. Conclusion: Hello, Mrs Premise.
> Mrs. Premise: Hello, Mrs Conclusion.
> Mrs. Conclusion: Busy day?
> Mrs. Premise: Busy! I've just spent four hours burying the cat.
> Mrs. Conclusion: Four hours to bury a cat?
> Mrs. Premise: Yes, it wouldn't keep still. Wriggling about, howling its head off.
> Mrs. Conclusion: Oh - it wasn't dead then?
> Mrs. Premise: Well, no, no, but it's not at all a well cat. So, as we were going away for a fortnight's holiday, I thought I'd better bury it just to be on the safe side.
> Mrs. Conclusion: Quite right. You don't want to come back from Sorrento to a dead cat. It'd be so anticlimactic. Yes, kill it now, that's what I say.
> Mrs. Premise: Yes.
> Mrs. Conclusion: We're going to have our budgie put down.
> Mrs. Premise: Really? Is it very old?
> Mrs. Conclusion: No. We just don't like it.

- Posts as: Monty Python's Flying Circus (1972)
- Full citation on the page: Series 3, Whicker's World [3.01], Mrs. Premise and Mrs. Conclusion Visit Jean-Paul Sartre, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**53.**

> Mr. Barnard: What do you want?
> Man: Well I was told outside...
> Mr. Barnard: Don't give me that you snotty-faced heap of parrot droppings!
> Man: What?
> Mr. Barnard: Shut your festering gob you tit! Your type makes me puke! You vacuous toffee-nosed malodorous pervert!
> Man: Look, I came here for an argument!
> Mr. Barnard: [apologetic] Oh, I'm sorry. This is abuse. No, you want room 12A next door.
> Man: I see, sorry!
> Mr. Barnard: Not at all. [the man exits] Stupid git.

- Posts as: Monty Python's Flying Circus (1972)
- Full citation on the page: Series 3, The Money Programme [3.03], The Argument Clinic, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**54.**

> Mr. Gumby: ARE YOU THE BRAIN SPECIALIST?
> Dr. Gumby: HELLO!
> Mr. Gumby: ARE YOU THE BRAIN SPECIALIST?
> Dr. Gumby: NO! NO I AM NOT THE BRAIN SPECIALIST. NO, NO I AM NOT...YES, YES I AM!
> Mr. Gumby: MY BRAIN HURTS!
> Dr. Gumby: WELL LET'S TAKE A LOOK AT IT, MR GUMBY! [starts to open trousers]
> Mr. Gumby: NO NO, THE BRAIN IN MY HEAD!
> Dr. Gumby: AHH! [hits Mr. Gumby's head a few times] IT WILL HAVE TO COME OUT!
> Mr. Gumby: OUT? OF MY HEAD?
> Dr. Gumby: YES! ALL THE BITS OF IT. NURSE! NUUURRSE! NUUUURSE! [nurse appears] NUUUURSE! NUUUURSE! [spots her and starts] NURSE, TAKE MR GUMBY TO A BRAIN SURGEON!
> Nurse: Yes, doctor.
> Dr. Gumby: WHERE'S THE LANCET? WHERE'S THE BLOODY LANCET! [rubs his head] MY BRAIN HURTS TOO!

- Posts as: Monty Python's Flying Circus (1972)
- Full citation on the page: Series 3, The War Against Pornography [3.06], Gumby Brain Specialist sketch, Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent

**55.**

> Roger Last: Good evening. Tonight on 'Is There' we examine the question, 'Is there a life after death?' And here to discuss it are three dead people.

- Posts as: Monty Python's Flying Circus (1972)
- Full citation on the page: Series 3, E. Henry Thripshaw's Disease [3.10], Monty Python's Flying Circus
- Wikiquote page: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent


## Jack Handey (Deep Thoughts) (12)

**1.**

> If trees could scream, would we be so cavalier about cutting them down? We might, if they screamed all the time, for no good reason.

- Posts as: Jack Handey, Deep Thoughts (1992)
- Full citation on the page: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**2.**

> I can picture in my mind a world without war, a world without hate. And I can picture us attacking that world because they'd never expect it.

- Posts as: Jack Handey, Deep Thoughts (1992)
- Full citation on the page: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**3.**

> If a kid asks where rain comes from, I think a cute thing to tell him is "God is crying." And if he asks why God is crying, another cute thing to tell him is "Probably because of something you did."

- Posts as: Jack Handey, Deep Thoughts (1992)
- Full citation on the page: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor
- **owner check**: 'God is crying ... because of something you did'; dark for a kid joke

**4.**

> It takes a big man to cry, but it takes an even bigger man to laugh at that man.

- Posts as: Jack Handey, Deep Thoughts (1992)
- Full citation on the page: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**5.**

> IF YOU ever drop your keys into a river of molten lava, let 'em go, because man, they're gone.

- Posts as: Jack Handey, Deeper Thoughts (1993)
- Full citation on the page: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**6.**

> TO ME, it's a good idea to always carry two sacks of something when you walk around. That way, if anybody says, "Hey, can you give me a hand?" you can say, "Sorry, got these sacks."

- Posts as: Jack Handey, Deeper Thoughts (1993)
- Full citation on the page: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**7.**

> IF YOU GO through a lot of hammers each month, I don't think it necessarily means you're a hard worker. It may just mean that you have a lot to learn about proper hammer maintenance.

- Posts as: Jack Handey, Deeper Thoughts (1993)
- Full citation on the page: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**8.**

> MAYBE in order to understand mankind, we have to look at the word itself. Basically, it's made up of two separate words — "mank" and "ind." What do these words mean? It's a mystery, and that's why so is mankind.

- Posts as: Jack Handey, Deeper Thoughts (1993)
- Full citation on the page: Jack Handey, Deeper Thoughts: All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**9.**

> I guess we were kinda poor when we were kids, but we didn't know it. That's because my dad always refused to let us look at the family's financial records.

- Posts as: Jack Handey, Fuzzy Memories (1996)
- Full citation on the page: Jack Handey, Fuzzy Memories (1996), Andrews McMeel Publishing, ISBN 0-8362-1040-9
- Wikiquote page: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**10.**

> To me, boxing is like a ballet, except there's no music, no choreography and the dancers hit each other.

- Posts as: Jack Handey
- Full citation on the page: Boxing, Jack Handey view on boxing,
- Wikiquote page: [Boxing](https://en.wikiquote.org/wiki/Boxing)
- Tags: irreverent, humor

**11.**

> A wise man can pick up a grain of sand and envision a whole universe.

- Posts as: Jack Handey
- Full citation on the page: Sand, Jack Handey, as quoted in "Quotes about Wisdom", Quotations Book, p. 15
- Wikiquote page: [Sand](https://en.wikiquote.org/wiki/Sand)
- Tags: irreverent, humor

**12.**

> I bet when neanderthal kids would make a snowman, someone would always end up saying, 'Don't forget the thick, heavy brows.' Then they would get all embarrassed because they remembered they had the big husky brows too, and they'd get mad and eat the snowman.

- Posts as: Jack Handey
- Full citation on the page: Jack Handey, in The History of the Snowman, p. 145.
- Wikiquote page: [Embarrassment](https://en.wikiquote.org/wiki/Embarrassment)
- Tags: irreverent, humor


## Hunter S. Thompson (35)

**1.**

> When the going gets weird, the weird turn pro.

- Posts as: Hunter S. Thompson, Fear and Loathing at the Super Bowl, Rolling Stone (1974)
- Full citation on the page: Hunter S. Thompson, 1970s, "Fear and Loathing at the Super Bowl" (Rolling Stone #155, (28 February 1974); republished in Gonzo Papers, Vol. 1: The Great Shark Hunt: Strange Tal…
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**2.**

> To Richard Milhous Nixon, who never let me down.

- Posts as: Hunter S. Thompson, The Great Shark Hunt (1979)
- Full citation on the page: Hunter S. Thompson, 1970s, epigraph to Gonzo Papers, Vol I : The Great Shark Hunt: Strange Tales from a Strange Time (1979), p. 7
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**3.**

> No point mentioning those bats, I thought. The poor bastard will see them soon enough.

- Posts as: Hunter S. Thompson, Fear and Loathing in Las Vegas (1971)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing in Las Vegas (1971)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**4.**

> The kids are turned off from politics, they say. Most of 'em don't even want to hear about it. All they want to do these days is lie around on waterbeds and smoke that goddamn marrywanna... yeah, and just between you and me Fred thats probably all for the best.

- Posts as: Hunter S. Thompson, Fear and Loathing: On the Campaign Trail '72 (1973)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**5.**

> A nervous blonde nymphet who thought that politics was some kind of game played by old people, like bridge.

- Posts as: Hunter S. Thompson, Fear and Loathing: On the Campaign Trail '72 (1973)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**6.**

> So much for Objective Journalism. Don't bother to look for it here — not under any byline of mine; or anyone else I can think of. With the possible exception of things like box scores, race results, and stock market tabulations, there is no such thing as Objective Journalism. The phrase itself is a pompous contradiction in terms.

- Posts as: Hunter S. Thompson, Fear and Loathing: On the Campaign Trail '72 (1973)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**7.**

> The massive, frustrated energies of a mainly young, disillusioned electorate that has long since abandoned the idea that we all have a duty to vote. This is like being told you have a duty to buy a new car, but you have to choose immediately between a Ford and a Chevy.

- Posts as: Hunter S. Thompson, Fear and Loathing: On the Campaign Trail '72 (1973)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**8.**

> Jesus man! You don't look for acid! Acid finds you when it thinks you're ready.

- Posts as: Hunter S. Thompson, Fear and Loathing: On the Campaign Trail '72 (1973)
- Full citation on the page: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**9.**

> The TV business is uglier than most things. It is normally perceived as some kind of cruel and shallow money trench through the heart of the journalism industry, a long plastic hallway where thieves and pimps run free and good men die like dogs, for no good reason.

- Posts as: Hunter S. Thompson, Generation of Swine (1988)
- Full citation on the page: Hunter S. Thompson, 1980s, Generation of Swine (1988), Originally published in the San Francisco Examiner (4 November 1985), this is often quoted as concluding with the statement "There's also a negative…
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**10.**

> He could shake your hand and stab you in the back at the same time.

- Posts as: Hunter S. Thompson, He Was A Crook (1994)
- Full citation on the page: Hunter S. Thompson, 1990s, He Was A Crook (1994)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**11.**

> Ah, fortune and fame shall follow me...and I shall dwell in the world of the chosen for a few moments of fleeting ecstasy; ere the seven burly lads turn into creditors and hustle me off to debtors' prison at last.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Porter Bibb III (6 February 1957), p. 44
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**12.**

> But fie on these unanswered queries and fie on those who pose them. There are stories to be written, drinks to be drunk, women to be ravished, and … alas, money to be made. We shall ride the bouncing ball and fight gamely to avoid being on the bottom when it bounces. … that is all ye know and all ye need to know. Amen.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Lieutenant Colonel Frank Campbell (6 January 1958), p. 96
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**13.**

> Events of the past two years have virtually decreed that I shall wrestle with the literary muse for the rest of my days. And so, having tasted the poverty of one end of the scale, I have no choice but to direct my energies toward the acquisition of fame and fortune. Frankly, I have no taste for either poverty or honest labor, so writing is the only recourse left me.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Arch Gerhart (29 January 1958), p. 106
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**14.**

> I may sound a little black, but I'm really pretty well adjusted.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Kay Menyers (17 March 1958), p. 109
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**15.**

> Sacrificing good men to journalism is like sending William Faulkner to work for Time magazine.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Jerome H. Walker (7 December 1958), p. 142
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**16.**

> Once I establish credit, I may be able to function. A man needs credit. Especially when he has no money.

- Posts as: Hunter S. Thompson, The Proud Highway (1997)
- Full citation on the page: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Dwight Martin (21 February 1964), p. 440
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**17.**

> Disgusting as he usually was, on rare occasions he showed flashes of stagnant intelligence. But his brain was so rotted with drink and dissolute living that whenever he put it to work it behaved like an old engine that had gone haywire from being dipped in lard.

- Posts as: Hunter S. Thompson, The Rum Diary (1998)
- Full citation on the page: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**18.**

> What passed for society was a loud, giddy whirl of thieves and pretentious hustlers, a dull sideshow full of quacks and clowns and philistines with gimp mentalities.

- Posts as: Hunter S. Thompson, The Rum Diary (1998)
- Full citation on the page: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**19.**

> It was the kind of town that made you feel like Humphrey Bogart: you came in on a bumpy little plane, and, for some mysterious reason, got a private room with a balcony overlooking the town and the harbor; then you sat there and drank until something happened. I felt a tremendous distance between me and everything real.

- Posts as: Hunter S. Thompson, The Rum Diary (1998)
- Full citation on the page: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**20.**

> For myself, I would much prefer to be stuck with Kentucky in the NCAA Tournament, than stuck with George Bush in the White House. It is the difference between losing your wallet at a cock fight and losing all your credit cards forever, along with your job and your house and your ability to earn enough money to pay off your sports-gambling debts or even a six-pack on game day. . .

- Posts as: Hunter S. Thompson, What's Better Than the Tournament? (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, "What's Better Than the Tournament?' (18 March 2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark
- **owner check**: names a sitting president of his day; partisan and crude, funny, but your call

**21.**

> There was no time for scholarly details, and, besides, I have always believed that a man can fairly be judged by the standards and taste of his choices in matters of high-level plagiarism.

- Posts as: Hunter S. Thompson, Prisoner of Denver, Vanity Fair (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, "Prisoner of Denver", in Vanity Fair (June 2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**22.**

> If you're going to be crazy, you have to get paid for it or else you're going to be locked up.

- Posts as: Hunter S. Thompson, BankRate.com interview (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, BankRate.com Interview (1 November 2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**23.**

> Richard Nixon could tell us a lot about peaking too early. He was a master of it, because it beat him every time. He never learned and neither did Bush the Elder.

- Posts as: Hunter S. Thompson, Welcome to the Big Darkness (2003)
- Full citation on the page: Hunter S. Thompson, 2000s, Welcome to the Big Darkness (2003)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**24.**

> Paranoia is just another word for ignorance.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**25.**

> I shit on the chest of Fun.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**26.**

> We shit on the chest of Weird.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**27.**

> I have a theory that the truth is never told during the nine-to-five hours.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**28.**

> The only difference between the Sane and the Insane, is IN and yet within this world, the Sane have the power to have the Insane locked up.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**29.**

> All political power comes from the barrel of either guns, pussy, or opium pipes, and people seem to like it that way.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark
- **owner check**: crude line about power; your call

**30.**

> We are like pygmies lost in a maze of haze. We are not at war, we are having a nervous breakdown,again.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**31.**

> I understand that fear is my friend, but not always. Never turn your back on fear. It should always be in front of you, like a thing that might have to be killed.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**32.**

> The only ones left with any confidence at all are the New Dumb. It is the beginning of the end of our world as we knew it. Doom is the operative ethic.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**33.**

> I was also drunk, crazy and heavily armed at all times. People trembled and cursed when I came into a public room and started screaming in German.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**34.**

> I knew a Buddhist once, and I've hated myself ever since. The whole thing was a failure.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**35.**

> Music has always been a matter of energy to me, a question of fuel. Sentimental people call it inspiration, but what they really mean is fuel. I have always needed fuel. I am a serious consumer. On some nights I still believe that a car with the gas needle on empty can run about fifty more miles if you have the right music very loud on the radio.

- Posts as: Hunter S. Thompson, Kingdom of Fear (2004)
- Full citation on the page: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote page: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark


## H. L. Mencken (35)

**1.**

> An idealist is one who, on noticing that a rose smells better than a cabbage, concludes that it is also more nourishing.

- Posts as: H. L. Mencken, The Smart Set (1915)
- Full citation on the page: H. L. Mencken, 1910s, Smart Set (1910s), "A Few Pages of Notes," The Smart Set (January 1915); later published in A Little Book in C Major (1916)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**2.**

> Democracy is the theory that the common people know what they want, and deserve to get it good and hard.

- Posts as: H. L. Mencken, The Smart Set (1915)
- Full citation on the page: H. L. Mencken, 1910s, Smart Set (1910s), "A Few Pages of Notes," The Smart Set (January 1915); later published in A Little Book in C major (1916), and A Mencken Crestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**3.**

> Progress: The process whereby the human race has got rid of whiskers, the vermiform appendix and God.

- Posts as: H. L. Mencken, A Book of Burlesques (1916)
- Full citation on the page: H. L. Mencken, 1910s, A Book of Burlesques (1916), A Book of Burlesques (1916)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**4.**

> Socialism is the theory that the desire of one man to get something he hasn't got is more pleasing to a just God than the desire of some other man to keep what he has got.

- Posts as: H. L. Mencken, A Little Book in C Major (1916)
- Full citation on the page: H. L. Mencken, 1910s, A Little Book in C Major (1916), A Little Book in C Major, New York, NY, John Lane Company (1916) p. 51
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**5.**

> The objection to Puritans is not that they try to make us think as they do, but that they try to make us do as they think.

- Posts as: H. L. Mencken, A Little Book in C Major (1916)
- Full citation on the page: H. L. Mencken, 1910s, A Little Book in C Major (1916), A Little Book in C Major, New York, NY, John Lane Company (1916) p. 53
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**6.**

> It is the dull man who is always sure, and the sure man who is always dull.

- Posts as: H. L. Mencken, Prejudices, Second Series (1920)
- Full citation on the page: H. L. Mencken, 1920s, Prejudices, Second Series (1920) Ch. 1
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**7.**

> When I mount the scaffold at last these will be my farewell words to the sheriff: Say what you will against me when I am gone, but don't forget to add, in common justice, that I was never converted to anything.

- Posts as: H. L. Mencken, Baltimore Evening Sun (1922)
- Full citation on the page: H. L. Mencken, 1920s, Baltimore Evening Sun (12 June 1922)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**8.**

> What is any political campaign save a concerted effort to turn out a set of politicians who are admittedly bad and put in a set who are thought to be better. The former assumption, I believe is always sound; the latter is just as certainly false. For if experience teaches us anything at all it teaches us this: that a good politician, under democracy, is quite as unthinkable as an honest burglar.

- Posts as: H. L. Mencken, Prejudices, Fourth Series (1924)
- Full citation on the page: H. L. Mencken, 1920s, Prejudices, Fourth Series (1924)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**9.**

> The basic fact about human existence is not that it is a tragedy, but that it is a bore. It is not so much a war as an endless standing in line. The objection to it is not that it is predominantly painful, but that it is lacking in sense.

- Posts as: H. L. Mencken, Baltimore Evening Sun (1926)
- Full citation on the page: H. L. Mencken, 1920s, Baltimore Evening Sun (9 August 1926)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**10.**

> No one in this world, so far as I know—and I have researched the records for years, and employed agents to help me—has ever lost money by underestimating the intelligence of the great masses of the plain people. Nor has anyone ever lost public office thereby.

- Posts as: H. L. Mencken, Chicago Tribune (1926)
- Full citation on the page: H. L. Mencken, 1920s, 'Notes On Journalism' in the Chicago Tribune (19 September 1926)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**11.**

> Shave a gorilla and it would be almost impossible, at twenty paces, to distinguish him from a heavyweight champion of the world. Skin a chimpanzee, and it would take an autopsy to prove he was not a theologian.

- Posts as: H. L. Mencken, Baltimore Evening Sun (1927)
- Full citation on the page: H. L. Mencken, 1920s, Baltimore Evening Sun (4 April 1927)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**12.**

> The older I grow the more I distrust the familiar doctrine that age brings wisdom.

- Posts as: H. L. Mencken, Prejudices, Third Series (1922)
- Full citation on the page: H. L. Mencken, 1920s, Prejudices, Third Series (1922), Ch. 3
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**13.**

> Faith may be defined briefly as an illogical belief in the occurrence of the improbable.

- Posts as: H. L. Mencken, Prejudices, Third Series (1922)
- Full citation on the page: H. L. Mencken, 1920s, Prejudices, Third Series (1922), Ch. 14 "Types of Men" - 3 : The Believer
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**14.**

> When A annoys or injures B on the pretense of saving or improving X, A is a scoundrel.

- Posts as: H. L. Mencken, Newspaper Days: 1899-1906 (1941)
- Full citation on the page: H. L. Mencken, 1940s–present, Newspaper Days: 1899-1906 (1941)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**15.**

> In the present case it is a little inaccurate to say I hate everything. I am strongly in favor of common sense, common honesty and common decency. This makes me forever ineligible to any public office of trust or profit in the Republic. But I do not repine, for I am a subject of it only by force of arms.

- Posts as: H. L. Mencken
- Full citation on the page: H. L. Mencken, 1940s–present, As quoted in LIFE magazine, Vol. 21, No. 6, (5 August 1946), p. 52; this has also been paraphrased as "It is inaccurate to say I hate everything. I a…
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**16.**

> Nature abhors a moron.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**17.**

> Conscience is the inner voice that warns us somebody may be looking.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**18.**

> A celebrity is one who is known to many persons he is glad he doesn't know.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**19.**

> Platitude — An idea (a) that is admitted to be true by everyone, and (b) that is not true.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**20.**

> Remorse — Regret that one waited so long to do it.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**21.**

> Self-respect — The secure feeling that no one, as yet, is suspicious.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**22.**

> Before a man speaks it is always safe to assume that he is a fool. After he speaks, it is seldom necessary to assume it.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**23.**

> Democracy is the art and science of running the circus from the monkey cage.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**24.**

> Lawyer — One who protects us against robbers by taking away the temptation.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**25.**

> Theology — An effort to explain the unknowable by putting it into terms of the not worth knowing.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**26.**

> Creator — A comedian whose audience is afraid to laugh.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**27.**

> Sunday — A day given over by Americans to wishing that they themselves were dead and in Heaven, and that their neighbors were dead and in Hell.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**28.**

> A newspaper is a device for making the ignorant more ignorant and the crazy crazier.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**29.**

> Puritanism: The haunting fear that someone, somewhere, may be happy.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949), Sententiæ: The Citizen and the State, p. 624
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**30.**

> If x is the population of the United States and y is the degree of imbecility of the average American, then democracy is the theory that x × y is less than y.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949), Sententiæ: The Citizen and the State
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**31.**

> We must respect the other fellow's religion, but only in the sense and to the extent that we respect his theory that his wife is beautiful and his children smart.

- Posts as: H. L. Mencken, Minority Report (1956)
- Full citation on the page: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 1
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**32.**

> Human life is basically a comedy. Even its tragedies often seem comic to the spectator, and not infrequently they actually have comic touches to the victim. Happiness probably consists largely in the capacity to detect and relish them. A man who can laugh, if only at himself, is never really miserable.

- Posts as: H. L. Mencken, Minority Report (1956)
- Full citation on the page: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 15
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**33.**

> It is impossible to imagine the universe run by a wise, just and omnipotent God, but it is quite easy to imagine it run by a board of gods. If such a board actually exists it operates precisely like the board of a corporation that is losing money.

- Posts as: H. L. Mencken, Minority Report (1956)
- Full citation on the page: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 79
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**34.**

> The kind of man who wants the government to adopt and enforce his ideas is always the kind of man whose ideas are idiotic.

- Posts as: H. L. Mencken, Minority Report (1956)
- Full citation on the page: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 323
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**35.**

> In this world of sin and sorrow there is always something to be thankful for. As for me, I rejoice that I am not a Republican.

- Posts as: H. L. Mencken, A Mencken Chrestomathy (1949)
- Full citation on the page: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote page: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone
- **owner check**: partisan jab; kept under the Mencken rule


## Terry Pratchett (25)

**1.**

> Only in our dreams are we free. The rest of the time we need wages.

- Posts as: Terry Pratchett
- Full citation on the page: Terry Pratchett, General sources, Cited in Power Quotes: For Life, Business, and Leadership (2018) by Danai Krokou, ISBN 1-63157-750-6
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**2.**

> Eight years involved with the nuclear industry have taught me that when nothing can possibly go wrong and every avenue has been covered, then is the time to buy a house on the next continent.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**3.**

> As they say in Discworld, we are trying to unravel the Mighty Infinite using a language which was designed to tell one another where the fresh fruit was.

- Posts as: Terry Pratchett, Relatively Einstein (2005)
- Full citation on the page: Terry Pratchett, General sources, Relatively Einstein episode 3, "Fantasy Physics" (18 January 2005); the Discworld version of this statement appears in Night Watch (2002)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**4.**

> Wikipedia, eh? Must be accurate then!

- Posts as: Terry Pratchett, The Age interview (2007)
- Full citation on the page: Terry Pratchett, General sources, The age interview (17 Feb 2007)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**5.**

> Nerds are the only people who know how to operate the video recorder.

- Posts as: Terry Pratchett, Desert Island Discs (1997)
- Full citation on the page: Terry Pratchett, General sources, Desert Island Discs (1997)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**6.**

> Never trust any complicated cocktail that remains perfectly clear until the last ingredient goes in, and then immediately clouds.

- Posts as: Terry Pratchett, alt.fan.pratchett (1993)
- Full citation on the page: Terry Pratchett, Usenet, alt.fan.pratchett (22 November 1993)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**7.**

> "Educational" refers to the process, not the object. Although, come to think of it, some of my teachers could easily have been replaced by a cheeseburger.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet, In response to a comment that if television is educational because watching it can teach you a lot about society, then a cheeseburger is also educati…
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**8.**

> I don't like the place at all. It's all wrong. An imposition on the Landscape. I reckon that Stonehenge was build by the contemporary equivalent of Microsoft, whereas Avebury was definitely an Apple circle.

- Posts as: Terry Pratchett, alt.fan.pratchett (1997)
- Full citation on the page: Terry Pratchett, Usenet, On Stonehenge, at alt.fan.pratchett (8 June 1997)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**9.**

> Over the centuries, mankind has tried many ways of combatting the forces of evil... prayer, fasting, good works and so on. Up until Doom, no one seemed to have thought about the double-barrel shotgun. Eat leaden death, demon...

- Posts as: Terry Pratchett, alt.fan.pratchett (1998)
- Full citation on the page: Terry Pratchett, Usenet, alt.fan.pratchett (30 May 1998)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**10.**

> 'They can ta'k our lives but they can never ta'k our freedom!' Now there's a battle cry not designed by a clear thinker...

- Posts as: Terry Pratchett, alt.fan.pratchett (1999)
- Full citation on the page: Terry Pratchett, Usenet, Referring to a statement in the movie Braveheart, at alt.fan.pratchett (11 January 1999)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**11.**

> It's amazing how fast gold works.

- Posts as: Terry Pratchett, alt.fan.pratchett (2002)
- Full citation on the page: Terry Pratchett, Usenet, On building the clacks, at alt.fan.pratchett (18 June 2002)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**12.**

> Go on, prove me wrong. Destroy the fabric of the universe. See if I care.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**13.**

> This isn't life in the fast lane, it's life in the oncoming traffic.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**14.**

> I mean, I wouldn't pay more than a couple of quid to see me, and I'm me.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**15.**

> Death isn't online. If he was, there would be a sudden drop in the death rate. Although it'd be interesting to see if he'd post things like: DON'T YOU THINK I SOUND LIKE JAMES EARL JONES?

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**16.**

> Too many people want to have written.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**17.**

> Up until now I'd always thought RSI meant 'I hate my damn job'.

- Posts as: Terry Pratchett, Usenet
- Full citation on the page: Terry Pratchett, Usenet
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**18.**

> When they're standing right in front of you, kings are a kind of speech impediment.

- Posts as: Terry Pratchett, The Carpet People (1971)
- Full citation on the page: Terry Pratchett, The Carpet People (1971; 1992)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**19.**

> Most armies are in fact run by their sergeants — the officers are there just to give things a bit of tone and prevent warfare from becoming a mere lower-class brawl.

- Posts as: Terry Pratchett, The Carpet People (1971)
- Full citation on the page: Terry Pratchett, The Carpet People (1971; 1992)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**20.**

> It is well known that a vital ingredient of success is not knowing that what you're attempting can't be done.

- Posts as: Terry Pratchett, Equal Rites (1987)
- Full citation on the page: Terry Pratchett, Equal Rites (1987)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**21.**

> Everyone's heard of Erwin Schrodinger's famous thought experiment. You put a cat in a box with a bottle of poison, which many people would suggest is about as far as you need to go.

- Posts as: Terry Pratchett, The Unadulterated Cat (1989)
- Full citation on the page: Terry Pratchett, The Unadulterated Cat (1989)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**22.**

> The trouble with having an open mind, of course, is that people will insist on coming along and trying to put things in it.

- Posts as: Terry Pratchett, Diggers (1990)
- Full citation on the page: Terry Pratchett, The Nome Trilogy (1989 - 1990), Diggers (1990)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**23.**

> Everything makes sense a bit at a time. But when you try to think of it all at once, it comes out wrong.

- Posts as: Terry Pratchett, Only You Can Save Mankind (1992)
- Full citation on the page: Terry Pratchett, Only You Can Save Mankind (1992)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**24.**

> I asked a teacher what the opposite of a miracle was and she, without thinking, I assume, said it was an act of God.
> You shouldn't say something like that to the kind of kid who will grow up to be a writer; we have long memories.

- Posts as: Terry Pratchett, I create gods all the time - now I think one might exist (2008)
- Full citation on the page: Terry Pratchett, "I create gods all the time - now I think one might exist" (2008)
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**25.**

> Tolkien's dead. J. K. Rowling said no. Philip Pullman couldn't make it. Hi, I'm Terry Pratchett.

- Posts as: Terry Pratchett
- Full citation on the page: Terry Pratchett, Misc, t-shirt worn by Pratchett at conventions
- Wikiquote page: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor


## Fight Club (20)

**1.**

> I am Jack's... complete lack of surprise.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**2.**

> On a long enough time line, the survival rate for everyone drops to zero.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**3.**

> I am Jack's wasted life.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**4.**

> When you have insomnia, you're never really asleep... and you're never really awake.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**5.**

> With insomnia, nothing's real. Everything's far away. Everything's a copy of a copy of a copy.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**6.**

> When you have a gun in your mouth, you can only speak in vowels.

- Posts as: The Narrator, Fight Club (1999)
- Full citation on the page: The Narrator, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**7.**

> Self-improvement is masturbation. Now, self-destruction...

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**8.**

> It's only after we've lost everything that we're free to do anything.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**9.**

> You are not your job. You're not how much money you have in the bank. You're not the car you drive. You're not the contents of your wallet. You're not your fucking khakis. You're the all-singing, all-dancing crap of the world.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**10.**

> The things you own end up owning you.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**11.**

> You have to consider the possibility that God does not like you, never wanted you, in all probability he hates you. It's not the worst thing that could happen.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent
- **owner check**: 'God hates you', irreverent about religion

**12.**

> Sticking feathers up your butt does not make you a chicken.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**13.**

> First you've gotta know - not fear, know - that someday you're gonna die.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**14.**

> We're consumers. We are the byproducts of a lifestyle obsession. Murder, crime, poverty, these things don't concern me. What concerns me are celebrity magazines, television with 500 channels, some guy's name on my underwear. Rogaine, Viagra, Olestra.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**15.**

> Listen up, maggots! You are not special. You are not a beautiful or unique snowflake. You are the same decaying organic matter as everything else. We are the all-singing, all-dancing crap of the world.

- Posts as: Tyler Durden, Fight Club (1999)
- Full citation on the page: Tyler Durden, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**16.**

> A condom is the glass slipper of our generation. You slip one on when you meet a stranger. You dance all night, and then you throw it away. The condom, I mean, not the stranger.

- Posts as: Marla Singer, Fight Club (1999)
- Full citation on the page: Marla Singer, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent
- **owner check**: condom joke; mild, but check

**17.**

> It's a bridesmaid's dress. I got it at a second-hand store. It was loved intensely for one night.. then cast aside.

- Posts as: Marla Singer, Fight Club (1999)
- Full citation on the page: Marla Singer, Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**18.**

> Narrator: When people think you're dying, they really, really listen to you, instead of just …
> Marla Singer: … instead of just waiting for their turn to speak?
> Narrator: Yeah. Yeah.

- Posts as: Fight Club (1999)
- Full citation on the page: Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**19.**

> Tyler Durden: You decide your level of involvement!
> Narrator: I will! I want to know certain things first.
> Everyone: The first rule of Project-- (Narrator: SHUT. UP.)

- Posts as: Fight Club (1999)
- Full citation on the page: Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**20.**

> Narrator: I know it seems like I have more than one side sometimes...
> Marla Singer: More than one side? You're Dr. Jekyll and Mr. Jackass!

- Posts as: Fight Club (1999)
- Full citation on the page: Fight Club (film)
- Wikiquote page: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent


## Dorothy Parker (20)

**1.**

> And she had It. It, hell; she had Those.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, Regarding a character in Elinor Glyn's novel It; in her review, "Madame Glyn Lectures on 'It,' with Illustrations" in The New Yorker (November 26, 19…
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**2.**

> Salary is no object: I want only enough to keep body and soul apart.

- Posts as: Dorothy Parker, The New Yorker (1928)
- Full citation on the page: Dorothy Parker, The New Yorker (February 4, 1928)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**3.**

> And it is that word 'hummy,' my darlings, that marks the first place in The House at Pooh Corner at which Tonstant Weader fwowed up.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, Her "Constant Reader" book review of The House at Pooh Corner by A. A. Milne, in The New Yorker (October 20, 1928)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**4.**

> That would be a good thing for them to cut on my tombstone: Wherever she went, including here, it was against her better judgment.

- Posts as: Dorothy Parker, But the One on the Right (1929)
- Full citation on the page: Dorothy Parker, "But the One on the Right" in The New Yorker (1929)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**5.**

> The House Beautiful is, for me, the play lousy.

- Posts as: Dorothy Parker, The New Yorker (1931)
- Full citation on the page: Dorothy Parker, Review of "The House Beautiful" by Channing Pollock, The New Yorker (March 21, 1931)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**6.**

> Drink and dance and laugh and lie,
> Love, the reeling midnight through,
> For tomorrow we shall die!
> (But, alas, we never do.)

- Posts as: Dorothy Parker, Death and Taxes (1931)
- Full citation on the page: Dorothy Parker, "The Flaw in Paganism" in Death and Taxes (1931)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**7.**

> The ones I like ... are "cheque" and "enclosed."

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, On the most beautiful words in the English language, as quoted in The New York Herald Tribune (December 12, 1932)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**8.**

> I might repeat to myself, slowly and soothingly, a list of quotations beautiful from minds profound; if I can remember any of the damn things.

- Posts as: Dorothy Parker, Here Lies (1939)
- Full citation on the page: Dorothy Parker, "The Little Hours" in Here Lies (1939)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**9.**

> I'm never going to accomplish anything; that's perfectly clear to me. I'm never going to be famous. My name will never be writ large on the roster of Those Who Do Things. I don't do anything. Not one single thing. I used to bite my nails, but I don't even do that any more.

- Posts as: Dorothy Parker, Here Lies (1939)
- Full citation on the page: Dorothy Parker, "The Little Hours" in Here Lies (1939)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**10.**

> One more drink and I'd have been under the host.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, As quoted in Try and Stop Me by Bennett Cerf (1944)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**11.**

> There's a hell of a distance between wise-cracking and wit. Wit has truth in it; wise-cracking is simply calisthenics with words.

- Posts as: Dorothy Parker, The Paris Review (1956)
- Full citation on the page: Dorothy Parker, Interview, The Paris Review (Summer 1956)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**12.**

> It's not the tragedies that kill us; it's the messes.

- Posts as: Dorothy Parker, The Paris Review (1956)
- Full citation on the page: Dorothy Parker, Interview, The Paris Review (Summer 1956)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**13.**

> All those writers who write about their own childhood! Gentle God, if I wrote about mine you wouldn't sit in the same room with me.

- Posts as: Dorothy Parker, The Paris Review (1956)
- Full citation on the page: Dorothy Parker, Interview in The Paris Review, Issue #13 (Summer 1956)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**14.**

> [On being told of Calvin Coolidge's death] How do they know? (Coolidge was known as a man who said very few words.)

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, Quoted in Writers at Work 1st Series by Malcolm Cowley (1958)
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**15.**

> Too fucking busy, and vice versa.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, Response to an editor pressuring her for overdue work, as quoted in The Unimportance of Being Oscar (1968) by Oscar Levant, p. 89
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**16.**

> You can lead a horticulture, but you can't make her think.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, Parker's answer when asked to use the word horticulture during a game of Can-You-Give-Me-A-Sentence?, as quoted in You Might as well Live by John Kea…
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**17.**

> What fresh hell can this be?

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, "If the doorbell rang in her apartment, she would say, 'What fresh hell can this be?' — and it wasn't funny; she meant it." You might as well live: t…
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**18.**

> If you have any young friends who aspire to become writers, the second greatest favor you can do them is to present them with copies of The Elements of Style. The first greatest, of course, is to shoot them now, while they're happy.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, From a review of the revised edition of The Elements of Style by William Strunk Jr. and E. B. White published in Esquire (November 1959).
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**19.**

> Brevity is the soul of lingerie.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, "Our Mrs Parker" (1934), Caption written for Vogue 1916
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**20.**

> Katharine Hepburn delivered a striking performance that ran the gamut of emotions, from A to B.

- Posts as: Dorothy Parker
- Full citation on the page: Dorothy Parker, "Our Mrs Parker" (1934), Woollcott writes in While Rome Burns that Parker had "recently...achieved an equal compression in reporting on The Lake, Miss Hepburn, it seems, had…
- Wikiquote page: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor


## Kurt Vonnegut (15)

**1.**

> This speech will be very short. After all, you asked me to come here — I didn't ask you.

- Posts as: Kurt Vonnegut
- Full citation on the page: Kurt Vonnegut, Various quotations
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**2.**

> The thing I just wrote [Slaughterhouse Five] is my masterpiece. The rest of it is going to be crap from now on.

- Posts as: Kurt Vonnegut
- Full citation on the page: Kurt Vonnegut, Various quotations
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**3.**

> Science fiction is like other writing. It is just novels and short stories with machines.

- Posts as: Kurt Vonnegut
- Full citation on the page: Kurt Vonnegut, Various quotations
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**4.**

> Nothing in this book is true.

- Posts as: Kurt Vonnegut, Cat's Cradle (1963)
- Full citation on the page: Kurt Vonnegut, Cat's Cradle (1963)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**5.**

> Busy, busy, busy.

- Posts as: Kurt Vonnegut, Cat's Cradle (1963)
- Full citation on the page: Kurt Vonnegut, Cat's Cradle (1963)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**6.**

> Like all real heroes, Charley had a fatal flaw. He refused to believe that he had gonorrhea, whereas the truth was that he did.

- Posts as: Kurt Vonnegut, God Bless You, Mr. Rosewater (1965)
- Full citation on the page: Kurt Vonnegut, God Bless You, Mr. Rosewater (1965), On Charley Warmergran, the Fire Chief of Rosewater.
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**7.**

> You understand, of course, that everything I say is horseshit.

- Posts as: Kurt Vonnegut, Playboy interview (1973)
- Full citation on the page: Kurt Vonnegut, Playboy interview (1973)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**8.**

> Here is a lesson in creative writing. First rule: Do not use semicolons. They are transvestite hermaphrodites representing absolutely nothing. All they do is show you've been to college.

- Posts as: Kurt Vonnegut, A Man Without a Country (2005)
- Full citation on the page: Kurt Vonnegut, A Man Without a Country (2005), A close paraphrase of this (beginning "Do not use...") is repeated in a commencement address at Cloves Hall, 27 April 2007, as reprinted in Armageddo…
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor
- **owner check**: the semicolon joke leans on a phrase about people (metaphor only); check

**9.**

> We are here on Earth to fart around. Don't let anybody tell you any different.

- Posts as: Kurt Vonnegut, A Man Without a Country (2005)
- Full citation on the page: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**10.**

> Evolution is so creative. That's how we got giraffes.

- Posts as: Kurt Vonnegut, A Man Without a Country (2005)
- Full citation on the page: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**11.**

> If I should ever die, God forbid, I hope you will say, "Kurt is up in heaven now." That's my favorite joke.

- Posts as: Kurt Vonnegut, A Man Without a Country (2005)
- Full citation on the page: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**12.**

> It is almost always a mistake to mention Abraham Lincoln. He always steals the show.

- Posts as: Kurt Vonnegut, A Man Without a Country (2005)
- Full citation on the page: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**13.**

> Like most science-fiction writers, Trout knew almost nothing about science.

- Posts as: Kurt Vonnegut, Breakfast of Champions (1973)
- Full citation on the page: Kurt Vonnegut, Breakfast of Champions (1973)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**14.**

> How embarrassing to be human.

- Posts as: Kurt Vonnegut, Hocus Pocus (1990)
- Full citation on the page: Kurt Vonnegut, Hocus Pocus (1990)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**15.**

> Shrapnel was invented by an Englishman of the same name. Don't you wish you could have something named after you?

- Posts as: Kurt Vonnegut, I Love You, Madame Librarian (2004)
- Full citation on the page: Kurt Vonnegut, I Love You, Madame Librarian (2004)
- Wikiquote page: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor


## Groucho Marx (10)

**1.**

> A likely story — and probably true.

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, The Al Jolson Show repartee following a trite, scripted Al Jolson joke. (1949)
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**2.**

> Although it is generally known, I think it's about time to announce that I was born at a very early age.

- Posts as: Groucho Marx, Groucho and Me (1959)
- Full citation on the page: Groucho Marx, From his autobiography Groucho and Me (1959)
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**3.**

> I sent the club a wire stating, "PLEASE ACCEPT MY RESIGNATION. I DON'T WANT TO BELONG TO ANY CLUB THAT WILL ACCEPT PEOPLE LIKE ME AS A MEMBER".

- Posts as: Groucho Marx, Groucho and Me (1959)
- Full citation on the page: Groucho Marx, Telegram to the Friar's Club of Beverly Hills to which he belonged, as recounted in Groucho and Me (1959), p. 321
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**4.**

> From the moment I picked your book up until I laid it down I was convulsed with laughter. Someday I intend on reading it.

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, To S. J. Perelman about his book Dawn Ginsbergh’s Revenge (1929), as quoted in LIFE (9 February 1962)
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**5.**

> I never forget a face, but in your case I'll be glad to make an exception.

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, Quote by Leo Rosten in The Many Worlds of Leo Rosten (1964)
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**6.**

> My experience is that people are most likely to listen to reason when in bed.

- Posts as: Groucho Marx, An Evening With Groucho (1972)
- Full citation on the page: Groucho Marx, Liner notes of An Evening With Groucho (1972) the recording of his appearance at Carnegie Hall.
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**7.**

> I find television very educational. Every time someone switches it on I go into another room and read a good book.

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, As quoted in Halliwell’s Filmgoer’s Companion (1984) by Leslie Halliwell
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**8.**

> To write an autobiography of Groucho Marx would be as asinine as to read an autobiography of Groucho Marx.

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, Just after completing his second autobiography, as quoted in The Marx Brothers: A Bio-bibliography (1987) by Wes D. Gehring, p. 137
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**9.**

> Die, my dear? Why that's the last thing I'll do!

- Posts as: Groucho Marx
- Full citation on the page: Groucho Marx, Last words
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**10.**

> Years ago, I tried to top everybody, but I don't anymore. I realized it was killing conversation. When you're always trying for a topper you aren't really listening. It ruins communication.

- Posts as: Groucho Marx, The Groucho Phile (1976)
- Full citation on the page: Groucho Marx, The Groucho Phile (1976), p. 308
- Wikiquote page: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor


## George Carlin (10)

**1.**

> Have you ever noticed that anybody driving slower than you is an idiot, and anyone going faster than you is a maniac?

- Posts as: George Carlin, Carlin on Campus (1984)
- Full citation on the page: George Carlin, Carlin on Campus (1984)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**2.**

> "One thing leads to another"? Not always. Sometimes one thing leads to the same thing. Ask an addict.

- Posts as: George Carlin, Brain Droppings (1997)
- Full citation on the page: George Carlin, Books, Brain Droppings (1997)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**3.**

> When it comes to God's existence, I'm not an atheist and I'm not an agnostic- I'm an acrostic, the whole thing puzzles me.

- Posts as: George Carlin, Brain Droppings (1997)
- Full citation on the page: George Carlin, Books, Brain Droppings (1997)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**4.**

> I put a dollar in a change machine. Nothing changed.

- Posts as: George Carlin, Brain Droppings (1997)
- Full citation on the page: George Carlin, Books, Brain Droppings (1997)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**5.**

> Meow means "woof" in cat.

- Posts as: George Carlin, Brain Droppings (1997)
- Full citation on the page: George Carlin, Books, Brain Droppings (1997)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**6.**

> I think I am, therefore I am. I think.

- Posts as: George Carlin, Napalm and Silly Putty (2001)
- Full citation on the page: George Carlin, Books, Napalm and Silly Putty (2001)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**7.**

> "Undisputed heavyweight champion." Well, if it's undisputed, what's all the fighting about?

- Posts as: George Carlin, Napalm and Silly Putty (2001)
- Full citation on the page: George Carlin, Books, Napalm and Silly Putty (2001)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**8.**

> I had no shoes, and I felt sorry for myself until I met a man who had no feet. I took his shoes. Now I feel better.

- Posts as: George Carlin, When Will Jesus Bring the Pork Chops? (2004)
- Full citation on the page: George Carlin, Books, When Will Jesus Bring the Pork Chops? (2004)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark
- **owner check**: 'I took his shoes' is dark; could read as cruel

**9.**

> They say rather than cursing the darkness, one should light a candle. They don't mention anything about cursing a lack of candles.

- Posts as: George Carlin, When Will Jesus Bring the Pork Chops? (2004)
- Full citation on the page: George Carlin, Books, When Will Jesus Bring the Pork Chops? (2004)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**10.**

> I think people should be allowed to do anything they want. We haven't tried that for a while. Maybe this time it'll work.

- Posts as: George Carlin, Brain Droppings (1997)
- Full citation on the page: George Carlin, Books, Brain Droppings (1997)
- Wikiquote page: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark


## Mark Twain (10)

**1.**

> Reader, suppose you were an idiot. And suppose you were a member of Congress. But I repeat myself.

- Posts as: Mark Twain
- Full citation on the page: Mark Twain, Draft manuscript (c.1881), quoted by Albert Bigelow Paine in Mark Twain: A Biography (1912), p. 724
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**2.**

> I am opposed to millionaires, but it would be dangerous to offer me the position.

- Posts as: Mark Twain, The American Claimant (1892)
- Full citation on the page: Mark Twain, American Claimant (1892)
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**3.**

> Always do right. This will gratify some people, and astonish the rest.

- Posts as: Mark Twain
- Full citation on the page: Mark Twain, To the Young People's Society, Greenpoint Presbyterian Church, Brooklyn (16 February 1901)
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**4.**

> To create man was a fine and original idea; but to add the sheep was a tautology.

- Posts as: Mark Twain, St. Louis Post-Dispatch (1902)
- Full citation on the page: Mark Twain, St. Louis Post-Dispatch (30 May 1902); also in Mark Twain : A Life, p. 611
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**5.**

> Clothes make the man. Naked people have little or no influence on society.

- Posts as: Mark Twain, More Maxims of Mark (1927)
- Full citation on the page: Mark Twain, More Maxims of Mark (1927) edited by Merle Johnson
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**6.**

> Always acknowledge a fault frankly. This will throw those in authority off their guard and give you opportunity to commit more.

- Posts as: Mark Twain, More Maxims of Mark (1927)
- Full citation on the page: Mark Twain, More Maxims of Mark (1927) edited by Merle Johnson
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**7.**

> If you pick up a starving dog and make him prosperous, he will not bite you. This is the principal difference between a dog and a man.

- Posts as: Mark Twain, The Tragedy of Pudd'nhead Wilson (1894)
- Full citation on the page: Mark Twain, The Tragedy of Pudd'nhead Wilson (1894), p. 214.
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**8.**

> In German, a young lady has no sex, while a turnip has.

- Posts as: Mark Twain, A Tramp Abroad (1880)
- Full citation on the page: Mark Twain, A Tramp Abroad (1880)
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**9.**

> Persons attempting to find a motive in this narrative will be prosecuted; persons attempting to find a moral in it will be banished; persons attempting to find a plot in it will be shot.
> BY ORDER OF THE AUTHOR.

- Posts as: Mark Twain, Adventures of Huckleberry Finn (1885)
- Full citation on the page: Mark Twain, Adventures of Huckleberry Finn (1885), Notice
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**10.**

> What is the difference between a taxidermist & a tax-collector? The taxidermist only takes your skin.

- Posts as: Mark Twain, Mark Twain's Notebook (1935)
- Full citation on the page: Mark Twain, Mark Twain's Notebook (1935), p. 379
- Wikiquote page: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark


## Blackadder (10)

**1.**

> Blackadder: Right. Good morning, team. My name is Lord Blackadder. And I'm the new minister in charge of religious genocide. If you play fair by me, you'll find me a considerate employer. But cross me and you'll soon discover that under this playful, boyish exterior beats the heart of a ruthless, sadistic maniac.

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Head, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark
- **owner check**: 'religious genocide' as an office joke; dark satire

**2.**

> Blackadder: Baldrick! That Farrow bloke you executed today, you sure he's dead?
> Baldrick: I chopped his head off. That usually does the trick.
> Blackadder: Yes, don't get clever with me. I just thought you might've lopped off a leg or something by mistake.
> Baldrick: No, the thing I chopped off had a nose.

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Head, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**3.**

> Melchett: Potato?
> Blackadder: Thanks, I don't.

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Potato, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**4.**

> Percy: I intend to discover, this very afternoon, the secret of alchemy - the hidden art of turning base things into gold.
> Blackadder: I see. And the fact that this secret has eluded the most intelligent of men since the dawn of time doesn't dampen your spirits?
> Percy: Oh no. I like a challenge!

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**5.**

> Messenger: [enters again] My lord, the Queen does demand your urgent presence, on pain of death.
> Blackadder: You're not making any friends here. You do know that, don't you?

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**6.**

> Messenger: [enters] My lord-
> Blackadder: [sarcastic] Ah, messenger, thank God you came. Percy and I could not have waited another second without you.

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**7.**

> [At the Queen's party, she comes dressed as her father, King Henry VIII]
> Queen: [deep voice] Yo ho ho, off with their heads!
> Percy: Ma'am, it is brilliant! Your father is born again!
> Queen: [normal voice] Let's bally well hope not, or else I won't be queen anymore.

- Posts as: Blackadder II (1986)
- Full citation on the page: Blackadder II, Chains, Blackadder II (series 2)
- Wikiquote page: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**8.**

> [Baldrick has become the new MP for Dunny-On-The-Wold]
> Blackadder: We are reprieved. It is a triumph for stupidity over common sense.
> Baldrick: Thank you very much.
> Blackadder: As a reward, Baldrick, take a short holiday. Did you enjoy it? Right. Back to work.

- Posts as: Blackadder the Third (1987)
- Full citation on the page: Blackadder the Third, Dish and Dishonesty, Blackadder the Third (series 3)
- Wikiquote page: [Blackadder the Third (series 3)](https://en.wikiquote.org/wiki/Blackadder_the_Third_(series_3))
- Tags: humor, snark

**9.**

> Doctor Johnson: [reading Baldrick's 'novel'] "Once upon a time, there was a lovely little sausage called-" Sausage? Sausage?! Oh, blast your eyes! [crumples it up, throws it to the ground and storms out]
> Baldrick: Oh, I didn't think it was that bad.
> Blackadder: [checks the dictionary] I think you'll find he left 'sausage' out of his dictionary, Baldrick. [checks again] Oh, and 'aardvark'.

- Posts as: Blackadder the Third (1987)
- Full citation on the page: Blackadder the Third, Ink and Incapability, Blackadder the Third (series 3)
- Wikiquote page: [Blackadder the Third (series 3)](https://en.wikiquote.org/wiki/Blackadder_the_Third_(series_3))
- Tags: humor, snark

**10.**

> Blackadder: I spy with my bored little eye something beginning with 'T'.
> Baldrick: Breakfast!
> Blackadder: What?
> Baldrick: My breakfast always begins with tea. Then I have a little sausage, and then an egg with some little soldiers.
> Blackadder: Baldrick, when I said it begins with 'T', I was talking about a letter.
> Baldrick: No, it never begins with a letter. The postman don't come 'til ten thirty.

- Posts as: Blackadder Goes Forth (1989)
- Full citation on the page: Blackadder Goes Forth, Plan E: General Hospital, Blackadder Goes Forth (series 4)
- Wikiquote page: [Blackadder Goes Forth (series 4)](https://en.wikiquote.org/wiki/Blackadder_Goes_Forth_(series_4))
- Tags: humor, snark


## The Big Lebowski (10)

**1.**

> Well, sir, it's this rug I had. It really tied the room together.

- Posts as: Jeffrey "The Dude" Lebowski, The Big Lebowski (1998)
- Full citation on the page: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**2.**

> No I do mind. Uhh, The Dude minds. This will not stand. This aggression will not stand, man.

- Posts as: Jeffrey "The Dude" Lebowski, The Big Lebowski (1998)
- Full citation on the page: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**3.**

> You're not wrong Walter. You're just an asshole.

- Posts as: Jeffrey "The Dude" Lebowski, The Big Lebowski (1998)
- Full citation on the page: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**4.**

> Smokey, this is not Nam, this is Bowling, there are rules.

- Posts as: Walter Sobchak, The Big Lebowski (1998)
- Full citation on the page: Walter Sobchak, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**5.**

> Smokey, my friend. [pulls out a Colt M1911A1 from his bag] You're entering a world of pain.

- Posts as: Walter Sobchak, The Big Lebowski (1998)
- Full citation on the page: Walter Sobchak, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**6.**

> Eh, fuck it, Dude. Let's go bowling.

- Posts as: Walter Sobchak, The Big Lebowski (1998)
- Full citation on the page: Walter Sobchak, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**7.**

> A wiser man than myself once said, "Sometimes you eat the b'ar.… Sometimes the b'ar, well, he eats you."

- Posts as: The Stranger, The Big Lebowski (1998)
- Full citation on the page: The Stranger, The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**8.**

> Walter Sobchak: You know, Dude, I myself dabbled in pacifism at one point. Not in 'Nam of course.
> The Dude: And, you know, he's got emotional problems, man.
> Walter Sobchak: You mean beyond pacifism?

- Posts as: The Big Lebowski (1998)
- Full citation on the page: The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**9.**

> Maude Lebowski: What do you do for— for recreation?
> The Dude: Oh, the usual. I bowl. Drive around. The occasional acid flashback.

- Posts as: The Big Lebowski (1998)
- Full citation on the page: The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**10.**

> The Dude: Well, take care, man. Gotta get back.
> The Stranger: Sure. Take it easy, Dude.
> The Dude: Oh yeah!
> The Stranger: I know that you will.
> The Dude: Yeah, well, the Dude abides.
> The Stranger: "The Dude abides." I don't know about you, but I take comfort in that. It's good knowin' he's out there. The Dude. Takin' 'er easy for all us sinners. Shoosh. I sure hope he makes the finals.

- Posts as: The Big Lebowski (1998)
- Full citation on the page: The Big Lebowski
- Wikiquote page: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture


## Red Dwarf (10)

**1.**

> Holly: I am Holly, the ship's computer, with an IQ of 6000; the same IQ as 6000 PE teachers.

- Posts as: Red Dwarf (1988)
- Full citation on the page: Red Dwarf: Series I (1988), Future Echoes, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**2.**

> Lister: What time is it?
> Rimmer: (blearily crawls over to the clock on the bedside table) Saturday.
> Lister: That the best you can do?
> Rimmer: There are some numbers next to it, but they could be anything.

- Posts as: Red Dwarf (1988)
- Full citation on the page: Red Dwarf: Series II (1988), Thanks for the Memory, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**3.**

> Rimmer: I loved that little lemming. I built him a little wall he could hurl himself off of.

- Posts as: Red Dwarf (1988)
- Full citation on the page: Red Dwarf: Series II (1988), Stasis Leak, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**4.**

> Holly: [after being insulted about his temporarily reduced IQ]: 6? Do me a lemon! That's a poor IQ for a glass of water!

- Posts as: Red Dwarf (1988)
- Full citation on the page: Red Dwarf: Series II (1988), Queeg, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**5.**

> Kryten: I think there's something wrong with the gearbox. The thing is, I learned to drive in Starbug 2. I'm not used to the controls in Starbug 1.
> Rimmer: They're exactly the same.
> Kryten: Yes. That's the problem.

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), Backwards, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**6.**

> Holly: Abandon ship! Abandon ship! Black hole approaching! This is not a drill. This is a drill! [pneumatic drill sound] Abandon shi- Oh God, now the siren's bust.... Awooga! Awooga! Abandon ship!

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), Marooned, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**7.**

> Rimmer: So Holly managed to navigate through five black holes?
> Holly: As it 'appens, there weren't any black 'oles.
> Rimmer: But you saw them!
> Holly: They weren't black 'oles.
> Rimmer (resigned): What were they?
> Holly: Grit. Five specs of grit on the scanner scope. Y'see the thing about grit, is it's black. And the thing about the scanner scope...
> Rimmer: Ohhhh!

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), Marooned, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**8.**

> Kryten: "Pub." Ah, yes: a meeting place where people attempt to achieve advanced states of mental incompetence by the repeated consumption of fermented vegetable drinks.

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), Timeslides, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**9.**

> Kryten: [reading Hitler's diary] Things to remember: Stop milk, pay papers, invade Czechoslovakia!

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), Timeslides, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**10.**

> Rimmer: At least he gets 24 hours notice, that's more than most of us get. Most of us get "Mind that bus!" "What bus?" Splat!

- Posts as: Red Dwarf (1989)
- Full citation on the page: Red Dwarf: Series III (1989), The Last Day, Red Dwarf
- Wikiquote page: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor


## Futurama (10)

**1.**

> Professor Farnsworth: Time travel is impossible!
> Fry: But Professor, you time traveled yourself remember? When we went back to Roswell?
> Professor Farnsworth: That proves nothing! And furthermore, you'd think I could remember a thing like that; plus, who are you anyway?

- Posts as: Futurama: Bender's Big Score (2007)
- Full citation on the page: Futurama: Bender's Big Score
- Wikiquote page: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**2.**

> Fry: I don't get it. How can you say Lars is more mature than me?
> Leela: Well, for one thing his checkbook doesn't have The Hulk on it.

- Posts as: Futurama: Bender's Big Score (2007)
- Full citation on the page: Futurama: Bender's Big Score
- Wikiquote page: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**3.**

> [Nudar is ordering Bender to kill Fry]
> Nudar: You know what to do.
> Bender: You want me to concludify him, like some sort of dispatcherator?
> Nudar: Yes, and don't forget to terminate him.

- Posts as: Futurama: Bender's Big Score (2007)
- Full citation on the page: Futurama: Bender's Big Score
- Wikiquote page: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**4.**

> Nudar: Faster, faster!
> Professor Farnsworth: I’m sciencing as fast as I can

- Posts as: Futurama: Bender's Big Score (2007)
- Full citation on the page: Futurama: Bender's Big Score
- Wikiquote page: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**5.**

> [The Hypnotoad is shown on screen.]
> Bender: [Voice over.] On the count of three, you will awaken feeling refreshed, as if Futurama had never been cancelled by idiots and then brought back by bigger idiots. One... two... [Snaps fingers.]

- Posts as: Futurama (2010)
- Full citation on the page: Part 1, Rebirth, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent

**6.**

> Zapp Brannigan: My God, we're defenseless. Like fish in a barrel.
> Richard Nixon's Head: Options?
> Zapp Brannigan: My instinct is to hide in this barrel, like the wily fish.

- Posts as: Futurama (2010)
- Full citation on the page: Part 1, In-A-Gadda-Da-Leela, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent

**7.**

> Clerk: Okay, it's $500, you have no choice of carrier, the battery can't hold the charge and the reception isn't very…
> Fry: Shut up and take my money!

- Posts as: Futurama (2010)
- Full citation on the page: Part 1, Attack of the Killer App, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent

**8.**

> Fry: That was low, Bender, even by your standards.
> Bender: My what, now?
> Fry: Since when is the Internet about robbing people of their privacy?
> Bender: August 6, 1991.

- Posts as: Futurama (2010)
- Full citation on the page: Part 1, Attack of the Killer App, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent

**9.**

> Fry: I feel like a mindless zombie. I wish I knew how long we've been waiting.
> Dr. Ben Beeler: The new eyePhone has an app for that!
> Bender: Does it have an app for kissing my shiny metal ass?
> Dr. Ben Beeler: Several!

- Posts as: Futurama (2010)
- Full citation on the page: Part 1, Attack of the Killer App, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent

**10.**

> Prof. Farnsworth: Oh, God. We've opened Pandora's fly. They'll reproduce without limit, consuming all the matter in the world!
> Fry: Like the Kardashians!

- Posts as: Futurama (2010)
- Full citation on the page: Part 2, Benderama, Futurama/Season 6
- Wikiquote page: [Futurama/Season 6](https://en.wikiquote.org/wiki/Futurama/Season_6)
- Tags: humor, irreverent


## Office Space (10)

**1.**

> Michael, I did nothing. I did absolutely nothing, and it was everything I thought it could be.

- Posts as: Peter Gibbons, Office Space (1999)
- Full citation on the page: Peter Gibbons, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**2.**

> (after asking Peter to come in and work on Saturday) Ah, ah, I almost forgot... I'm also going to need you to go ahead and come in on Sunday, too. We, uhhh, lost some people this week and we sorta need to play catch-up. Thaaaaaanks.

- Posts as: Bill Lumbergh, Office Space (1999)
- Full citation on the page: Bill Lumbergh, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**3.**

> The ratio of cake to people is too big...

- Posts as: Milton Waddams, Office Space (1999)
- Full citation on the page: Milton Waddams, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**4.**

> I could set the building on fire...

- Posts as: Milton Waddams, Office Space (1999)
- Full citation on the page: Milton Waddams, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**5.**

> I believe you have my stapler...

- Posts as: Milton Waddams, Office Space (1999)
- Full citation on the page: Milton Waddams, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**6.**

> And yes, I won't be leaving a tip, 'cause I could... I could shut this whole resort down. Sir? I'll take my traveler's checks to a competing resort. I could write a letter to your board of tourism and I could have this place condemned. I could put... I could put... strychnine in the guacamole. There was salt on the glass, BIG grains of salt.

- Posts as: Milton Waddams, Office Space (1999)
- Full citation on the page: Milton Waddams, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**7.**

> What would ya say... ya do here?

- Posts as: Bob Slydell, Office Space (1999)
- Full citation on the page: Bob Slydell, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**8.**

> [frustrated with the malfunctioning printer] Why does it say "Paper Jam" when there is no paper jam?!

- Posts as: Samir Nagheenanajar, Office Space (1999)
- Full citation on the page: Samir Nagheenanajar, Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**9.**

> Peter Gibbons: Let me ask you something. When you come in on Monday and you're not feeling real well, does anyone ever say to you, "Sounds like someone has a case of the Mondays?"
> Lawrence: No. No, man. Shit, no, man. I believe you'd get your ass kicked sayin' something like that, man.

- Posts as: Office Space (1999)
- Full citation on the page: Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**10.**

> Bob Porter: Looks like you've been missing a lot of work lately.
> Peter Gibbons: Well, I wouldn't exactly say I've been missing it, Bob.

- Posts as: Office Space (1999)
- Full citation on the page: Office Space
- Wikiquote page: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture


## Revisions used

- The Hitchhiker's Guide to the Galaxy: revision 3883300
- Douglas Adams: revision 3931377
- Monty Python and the Holy Grail: revision 4001452
- Monty Python's Life of Brian: revision 4000732
- Monty Python's The Meaning of Life: revision 4000773
- Monty Python's Flying Circus: revision 4000743
- Jack Handey: revision 3492162
- Boxing: revision 3890387
- Sand: revision 3613595
- Embarrassment: revision 3694727
- Hunter S. Thompson: revision 3901461
- H. L. Mencken: revision 3962671
- Terry Pratchett: revision 3924187
- Fight Club (film): revision 4001607
- Dorothy Parker: revision 4012240
- Kurt Vonnegut: revision 3959939
- Groucho Marx: revision 4020043
- George Carlin: revision 3941591
- Mark Twain: revision 3992852
- Blackadder II (series 2): revision 3784336
- Blackadder the Third (series 3): revision 3754540
- Blackadder Goes Forth (series 4): revision 3803833
- The Big Lebowski: revision 4024202
- Red Dwarf: revision 4023807
- Futurama: Bender's Big Score: revision 4014780
- Futurama/Season 6: revision 4009887
- Office Space: revision 4015751
