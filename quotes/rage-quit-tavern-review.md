# Rage Quit Tavern quotes: review sheet

Total: **317** quotes in `quotes/rage-quit-tavern.txt` (the brief asked for about 400). **9** are flagged "owner check".

## Counts and shortfalls

| Source | Target | Taken | Shortfall |
|---|---|---|---|
| Douglas Adams (the Hitchhiker's books) | 65 | 65 |  |
| Monty Python | 55 | 13 | 42 |
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
| Jack Handey (Deep Thoughts) | 50 | 9 | 41 |
| **Total** | 400 | 317 | 83 |

The targets are the owner's, after the 2026-10-05 changes (Jack Handey added at 50; Thompson and Mencken 35, Pratchett 25, and Twain, Carlin, Groucho, Blackadder, Lebowski, Red Dwarf and Futurama 10 each).

**Why the two big shortfalls.** I took only what the bot's own Wikiquote parser (`newsbot/lounge/wikiquote.py`) accepts, and didn't backfill from anywhere the owner didn't approve.

- *Monty Python (13 of 55).* *The Holy Grail* page yields 2 quotes and 11 more were dropped by the parser as too long (over 400 characters); *Life of Brian* yields 12 (10 taken) and 11 dropped as too long; *Flying Circus* yields 9, of which 7 are cast names and 1 is a cut-off fragment ("It's..."), so 1 was usable; *The Meaning of Life* yields none at all, because the page isn't classed as a "work" page (no "Cast" or "Dialogue" heading), so the parser throws its dialogue away.
- *Jack Handey (9 of 50).* The "Jack Handey" page has nine sourced quotes. There's no dedicated Deep Thoughts page on Wikiquote (I tried the likely titles). Handey quotes also sit on theme pages (Boxing, Key, War, Embarrassment), which weren't on the approved list, so I left them.
- *Douglas Adams.* The four later Hitchhiker's books don't have their own Wikiquote pages. Everything for the five books lives on the single "The Hitchhiker's Guide to the Galaxy" page, section by book, so I took 53 from it and 12 from the "Douglas Adams" author page (Dirk Gently, *The Salmon of Doubt*, a few quotes about technology and writing, and five *Meaning of Liff* entries). The 2009 sequel *And Another Thing...* is listed on the same page but was written by Eoin Colfer, so none of it is used.

## How the selection was made

1. Fetched each page from Wikiquote's API with `fetch_page` from the repo and parsed it with `parse_page` (same code the bot runs), with the bot's User-Agent, one request at a time and a two-second pause. Revisions used are listed per page at the bottom.
2. Read every parsed quote. Kept the ones with humor, irreverent humor, snark or an irreverent pop-culture reference. Mencken is kept whatever the tone (his exception), so his are tagged "Mencken: any tone".
3. Dropped anything built on a slur or on mocking people for who they are, anything cruel rather than funny, quotes that need the scene around them, entries whose citation says they are only "credited" or a variation of someone else's line, and quotes with a trailing page number in the text (the text is verbatim, and I only strip `[1]`-style markers). Some Thompson lines are partisan rants about presidents; I took the few that are funny and flagged the sharpest.
4. Dialogue keeps every speaker's name on its own line, as the page gives it. Single lines from film and TV pages get the speaker in the attribution where the page gives one.
5. Attribution is the parser's own string. Two small changes: the Hitchhiker's page's attributions don't name the author, so I put "Douglas Adams, " in front (the "Douglas Adams" author page already says it), and on the first novel I dropped the doubled title ("The Hitchhiker's Guide to the Galaxy, The Hitchhiker's Guide to the Galaxy (1979 novel)" becomes the title once).
6. Checked: no duplicates (by the lounge's own `quote_hash`), every rendered message fits Discord's 2,000 units.

Tags are per source (a few of the Thompson, Carlin and Twain lines are more snark than humor, and the other way round; I didn't hand-label each one).

**Link line.** The Wikiquote source posts a "From Wikiquote: <link>" line under each quote. A plain file can't, because `esc()` puts a zero-width space after `https:` and escapes underscores, which kills the link. So these post as text, then the `~ ` attribution line, and nothing else. See `quotes/README.md`.


## Douglas Adams (the Hitchhiker's books) (65)

**1.**

> Far out in the uncharted backwaters of the unfashionable end of the western spiral arm of the Galaxy lies a small unregarded yellow sun. Orbiting this at a distance of roughly ninety-two million miles is an utterly insignificant little blue-green planet whose ape-descended life forms are so amazingly primitive that they still think digital watches are a pretty neat idea.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Introduction
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**2.**

> "Time is an illusion. Lunchtime doubly so."
> "Very deep," said Arthur, "you should send that in to the Reader's Digest. They've got a page for people like you."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 2
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**3.**

> "This must be Thursday," said Arthur to himself, sinking low over his beer, "I never could get the hang of Thursdays."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 2
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**4.**

> The ships hung in the sky in much the same way that bricks don't.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 3
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**5.**

> "If I asked you where the hell we were," said Arthur weakly, "would I regret it?"
> Ford stood up. "We're safe," he said.
> "Oh good," said Arthur.
> "We're in a small galley cabin," said Ford, "in one of the spaceships of the Vogon Constructor Fleet."
> "Ah," said Arthur, "this is obviously some strange usage of the word safe that I wasn't previously aware of."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 5
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**6.**

> "You know," said Arthur, "it's at times like this, when I'm trapped in a Vogon airlock with a man from Betelgeuse, and about to die of asphyxiation in deep space that I really wish I'd listened to what my mother told me when I was young."
> "Why, what did she tell you?"
> "I don't know, I didn't listen."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 7
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**7.**

> "This is terrific," Arthur thought to himself, "Nelson's Column has gone, McDonald's have gone, all that's left is me and the words Mostly harmless. Any second now all that will be left is Mostly harmless. And yesterday the planet seemed to be going so well."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 7
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**8.**

> "Space," it says, "is big. Really big. You just won't believe how vastly, hugely, mindbogglingly big it is. I mean, you may think it's a long way down the road to the chemist, but that's just peanuts to space. Listen..." and so on.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 8
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**9.**

> The fabulously beautiful planet Bethselamin is now so worried about the cumulative erosion by ten billion visiting tourists a year that any net imbalance between the amount you eat and the amount you excrete while on the planet is surgically removed from your body weight when you leave: so every time you go to the lavatory there it is vitally important to get a receipt.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 8
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**10.**

> Arthur looked up. "Ford!" he said, "there's an infinite number of monkeys outside who want to talk to us about this script for Hamlet they've worked out."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**11.**

> "Ford," he said, "you're turning into a penguin. Stop it."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**12.**

> He reached out and pressed an invitingly large red button on a nearby panel. The panel lit up with the words Please do not press this button again.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**13.**

> "Come on," he droned, "I've been ordered to take you down to the bridge. Here I am, brain the size of a planet and they ask me to take you down to the bridge. Call that job satisfaction? 'Cos I don't."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**14.**

> "Sorry, did I say something wrong?" said Marvin, dragging himself on regardless. "Pardon me for breathing, which I never do anyway so I don't know why I bother to say it, oh God I'm so depressed. Here's another one of those self-satisfied doors. Life! Don't talk to me about life."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 11
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**15.**

> "If there's anything more important than my ego around, I want it caught and shot now."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 12
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**16.**

> He had found a Nutri-Matic machine which had provided him with a plastic cup filled with a liquid that was almost, but not quite, entirely unlike tea.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 17
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**17.**

> Curiously enough, the only thing that went through the mind of the bowl of petunias as it fell was Oh no, not again. Many people have speculated that if we knew exactly why the bowl of petunias had thought that we would know a lot more about the nature of the Universe than we do now.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**18.**

> "For instance, on the planet Earth, man had always assumed that he was more intelligent than dolphins because he had achieved so much—the wheel, New York, wars, and so on—whilst all the dolphins had ever done was muck about in the water having a good time. But conversely, the dolphins had always believed that they were far more intelligent than man... for precisely the same reasons."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 23
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**19.**

> "I'd far rather be happy than right any day."
> "And are you?"
> "No, that's where it all falls down, of course."
> "Pity," said Arthur with sympathy. "It sounded like quite a good lifestyle otherwise."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 30
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**20.**

> It said: "The History of every major Galactic Civilization tends to pass through three distinct and recognizable phases, those of Survival, Inquiry and Sophistication, otherwise known as the How, Why and Where phases.
> "For instance, the first phase is characterized by the question How can we eat? the second by the question Why do we eat? and the third by the question Where shall we have lunch?"

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy (1979 novel), Chapter 35
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**21.**

> The story so far:
> In the beginning the Universe was created.
> This has made a lot of people very angry and been widely regarded as a bad move.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 1
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**22.**

> "Share and Enjoy" is the company motto of the hugely successful Sirius Cybernetics Corporation Complaints division, which now covers the major land masses of three medium sized planets and is the only part of the Corporation to have shown a consistent profit in recent years.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 2
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
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

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 3
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**24.**

> "Well sir," snapped the fragile little creature, "if you could be a little cool about it..."
> "Look," said Zaphod. "I'm up to here with cool, okay? I'm so amazingly cool you could keep a side of meat in me for a month. I'm so hip I have trouble seeing over my pelvis. Now will you move before I blow it?"

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**25.**

> "Mr. Beeblebrox, sir," said the insect in awed wonder, "you're so weird you should be in movies."
> "Yeah," said Zaphod patting the thing on a glittering pink wing, "and you, baby, should be in real life."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
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

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 6
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**27.**

> "Have another drink," said Trillian. "Enjoy yourself."
> "Which?" said Arthur. "The two are mutually exclusive."
> "Poor Arthur, you're really not cut out for this life are you?"
> "You call this life?"
> "You're starting to sound like Marvin."
> "Marvin is the clearest thinker I know."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 16
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**28.**

> "The first ten million years were the worst," said Marvin, "and the second ten million years, they were the worst too. The third ten million years I didn't enjoy at all. After that I went into a bit of a decline."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**29.**

> "The best conversation I had was over forty million years ago," continued Marvin. ..."And that was with a coffee machine."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**30.**

> "Well, I wish you'd just tell me rather than try to engage my enthusiasm," said Marvin, "because I haven't got one."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**31.**

> "Er..." [Zarquon] said, "hello. Er, look, I'm sorry I'm a bit late. I've had the most ghastly time, all sorts of things cropping up at the last moment."
> He seemed nervous of the expectant awed hush. He cleared his throat.
> "Er, how are we for time?" he said, "have I just got a min—"
> And so the Universe ended.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**32.**

> "I wonder who this ship belongs to anyway," said Arthur.
> "Me," said Zaphod.
> "No. Who it really belongs to."
> "Really me," insisted Zaphod, "look, property is theft, right? Therefore theft is property. Therefore this ship is mine, OK?"
> "Tell the ship that," said Arthur.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, The Restaurant at the End of the Universe (1980), Chapter 20
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**33.**

> For a moment or two the old man didn't reply. He was staring at the instruments with the air of one who is trying to convert Fahrenheit to centigrade in his head while his house is burning down.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 4
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**34.**

> "My doctor says that I have a malformed public-duty gland and a natural deficiency in moral fibre," Ford muttered to himself, "and that I am therefore excused from saving Universes."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 6
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**35.**

> "My capacity for happiness," he added, "you could fit into a matchbox without taking out the matches first." —Marvin

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 7
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**36.**

> "Voon," [the mattress] wurfed at last, "and was it a magnificent occasion?"
> "Reasonably magnificent. The entire thousand-mile-long bridge spontaneously folded up its glittering spans and sank weeping into the mire, taking everybody with it."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 7
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**37.**

> [The Guide] had some advice to offer on drunkenness.
> "Go to it," it said, "and good luck."
> It was cross-referenced to the entry concerning the size of the Universe and the ways of coping with that.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**38.**

> There is an art, it says, or rather, a knack to flying. The knack lies in learning how to throw yourself at the ground and miss. ... Clearly, it is this second part, the missing, which presents the difficulties.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**39.**

> [Zaphod] sat up sharply and started to pull clothes on. He decided that there must be someone in the Universe feeling more wretched, miserable and forsaken than himself, and he determined to set out and find him.
> Halfway to the bridge it occurred to him that it might be Marvin, and he returned to bed.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**40.**

> He hoped and prayed that there wasn't an afterlife. Then he realized there was a contradiction involved here and merely hoped that there wasn't an afterlife.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Life, the Universe and Everything (1982), Chapter 33
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**41.**

> The storm had now definitely abated, and what thunder there was now grumbled over more distant hills, like a man saying "And another thing..." twenty minutes after admitting he's lost the argument.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 3
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**42.**

> The moon was out in a watery way. It looked like a ball of paper from the back pocket of jeans that have just come out of the washing machine, which only time and ironing would tell if it was an old shopping list or a five pound note.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 7
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**43.**

> He paused and maneuvered his thoughts. It was like watching oil tankers doing three-point turns in the English Channel.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 9
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**44.**

> Ford: "Life," he said, "is like a grapefruit."
> Creature: "Er, how so?"
> Ford: "Well, it's sort of orangey-yellow and dimpled on the outside, wet and squidgy in the middle. It's got pips inside, too. Oh, and some people have half a one for breakfast."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 23
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**45.**

> The Hitchhiker's Guide to the Galaxy ... says of the Sirius Cybernetics Corporation products that "it is very easy to be blinded to the essential uselessness of them by the sense of achievement you get from getting them to work at all."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 35
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**46.**

> "I come in peace," [the silver robot] said, adding after a long moment of further grinding, "take me to your Lizard."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, So Long, and Thanks for All the Fish (1984), Chapter 36
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**47.**

> One of the problems has to do with the speed of light and the difficulties involved in trying to exceed it. You can't. Nothing travels faster than the speed of light with the possible exception of bad news, which obeys its own special laws.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 1
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**48.**

> The thing they wouldn't be expecting him to do was to be there in the first place. Only an absolute idiot would be sitting where he was, so he was winning already. A common mistake that people make when trying to design something completely foolproof is to underestimate the ingenuity of complete fools.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 12
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**49.**

> The major difference between a thing that might go wrong and a thing that cannot possibly go wrong is that when a thing that cannot possibly go wrong goes wrong it usually turns out to be impossible to get at or repair.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 12
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**50.**

> "The insurance business is completely screwy now. You know they've reintroduced the death penalty for insurance company directors?"
> "Really?" said Arthur. "No, I didn't. For what offense?"
> Trillian frowned.
> "What do you mean, offense?"
> "I see."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 13
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**51.**

> "I leaped out of a high-rise office window."
> This cheered Arthur up. "Oh!" he said. "Why don't you do it again?"
> "I did."
> "Hmmm," said Arthur, disappointed. "Obviously no good came of it."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**52.**

> "What was the self-sacrifice?"
> "I jettisoned half of a much-loved and I think irreplaceable pair of shoes."
> "Why was that self-sacrifice?"
> "Because they were mine!" said Ford, crossly.
> "I think we have different value systems."
> "Well, mine's better."

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, Mostly Harmless (1992), Chapter 18
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**53.**

> Humans are not proud of their ancestors, and rarely invite them round to dinner.

- Attribution: Douglas Adams, The Hitchhiker's Guide to the Galaxy, TV Series, Episode 1
- Wikiquote: [The Hitchhiker's Guide to the Galaxy](https://en.wikiquote.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy)
- Tags: humor, irreverent

**54.**

> Driving a Porsche in London is like bringing a Ming vase to a football game.

- Attribution: Douglas Adams, As quoted in Don't Panic: The Official Hitchhikers Guide to the Galaxy Companion (1988) by Neil Gaiman
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**55.**

> A learning experience is one of those things that say, "You know that thing you just did? Don't do that."

- Attribution: Douglas Adams, Interview in The Daily Nexus (5 April 2000), reprinted in The Salmon of Doubt
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**56.**

> SHOEBURYNESS (abs.n.) The vague uncomfortable feeling you get when sitting on a seat which is still warm from somebody else's bottom

- Attribution: Douglas Adams, The Meaning of Liff (1983)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**57.**

> WOKING (vb.) To enter the kitchen with the precise determination to perform something only to forget what it is just before you do it.

- Attribution: Douglas Adams, The Meaning of Liff (1983)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**58.**

> The seat received him in a loose and distant kind of way, like an aunt who disapproves of the last fifteen years of your life and will therefore furnish you with a basic sherry, but refuses to catch your eye.

- Attribution: Douglas Adams, Dirk Gently's Holistic Detective Agency (1987)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**59.**

> Thor was the God of Thunder and, frankly, acted like it.

- Attribution: Douglas Adams, The Long Dark Tea-Time of the Soul (1988), Ch. 7
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**60.**

> The Great Zaganza said: "You are very fat and stupid and persistently wear a ridiculous hat which you should be ashamed of."

- Attribution: Douglas Adams, The Long Dark Tea-Time of the Soul (1988), Ch. 35
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**61.**

> "Stotting" is jumping upward with all four legs simultaneously. My advice: do not die until you've seen a large black poodle stotting in the snow.

- Attribution: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**62.**

> Anything that is in the world when you're born is normal and ordinary and is just a natural part of the way the world works. Anything that's invented between when you're fifteen and thirty-five is new and exciting and revolutionary and you can probably get a career in it. Anything invented after you're thirty-five is against the natural order of things.

- Attribution: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**63.**

> The hotel shop only had two decent books, and I'd written both of them.

- Attribution: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**64.**

> I love deadlines. I love the whooshing noise they make as they go by.

- Attribution: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent

**65.**

> My favourite piece of information is that Branwell Brontë, brother of Emily and Charlotte, died standing up leaning against a mantelpiece, in order to prove it could be done. This is not quite true, in fact. My absolute favourite piece of information is the fact that young sloths are so inept that they frequently grab their own arms and legs instead of tree limbs, and fall out of trees.

- Attribution: Douglas Adams, The Salmon of Doubt (2002)
- Wikiquote: [Douglas Adams](https://en.wikiquote.org/wiki/Douglas_Adams)
- Tags: humor, irreverent


## Monty Python (13)

**1.**

> [Arthur and Patsy "ride" through the village]
> Large Man: Who's that then?
> Dead Collector: I dunno. Must be a king.
> Large Man: Why?
> Dead Collector: He hasn't got shit all over him.

- Attribution: Must be a king, Monty Python and the Holy Grail
- Wikiquote: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**2.**

> Head Knight: The Knights Who Say Ni demand a sacrifice!
> King Arthur: Knights of Ni, we are but simple travelers who seek the enchanter who lives beyond these woods--
> Knights who say Ni: Ni! Ni! Ni! Ni!
> King Arthur: Oh, ow!
> Head Knight: We shall say "Ni" again to you, if you do not appease us.
> King Arthur: Well, what do you want?
> Head Knight: We want... a shrubbery!! [jarring chord]

- Attribution: Knights who say Ni, Monty Python and the Holy Grail
- Wikiquote: [Monty Python and the Holy Grail](https://en.wikiquote.org/wiki/Monty_Python_and_the_Holy_Grail)
- Tags: irreverent

**3.**

> What Jesus blatantly fails to appreciate is that it's the meek who are the problem.

- Attribution: Reg, Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**4.**

> Now, you listen here: 'e's not the Messiah, 'e's a very naughty boy! Now go away!

- Attribution: Mandy, Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**5.**

> ... And the beast shall be huge and black, and the eyes thereof red with the blood of living creatures, and the whore of Babylon shall ride forth on a three-headed serpent, and throughout the lands, there'll be a great rubbing of parts. Yeeah...

- Attribution: Blood and Thunder Prophet, Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**6.**

> Oh, what I wouldn't give to be spat at in the face. I sometimes hang awake at night dreaming of being spat at in the face.

- Attribution: Prisoner, Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**7.**

> You lucky bastards! You lucky, jammy bastards!

- Attribution: Prisoner, Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**8.**

> Mandy: So you're astrologers, are you? Well what is he then?
> Wise man: Mmmm?
> Mandy: What star sign is he?
> Wise man: Well, Capricorn.
> Mandy: Ehh, Capricorn, eh? What are they like?
> Wise men: He is the son of God, our Messiah. King of the Jews.
> Mandy: And that's Capricorn, is it?
> Wise man: No, no, no. That's just him.
> Mandy: Ohh, I was going to say, 'Otherwise, there'd be a lot of them.'

- Attribution: Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**9.**

> [The audience members at the back of the crowd are having trouble hearing the Sermon on the Mount]
> Man: I think it was, "Blessed are the cheesemakers"!
> Gregory's wife: What's so special about the cheesemakers?
> Gregory: Well, obviously it's not meant to be taken literally. It refers to any manufacturer of...dairy products.

- Attribution: Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**10.**

> Brian: There's no pleasing some people.
> Ex-leper: That's just what Jesus said, sir.

- Attribution: Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**11.**

> Reg: All right, but apart from the sanitation, the medicine, education, wine, public order, irrigation, roads, the fresh-water system, and public health, what have the Romans ever done for us?
> PFJ Member: Brought peace?
> Reg: Oh, peace? SHUT UP!

- Attribution: Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**12.**

> Brian: Look, you've got it all wrong! You don't need to follow me. You don't need to follow anybody! You've got to think for yourselves! You're all individuals!
> Crowd: [in unison] Yes! We're all individuals!
> Brian: You're all different!
> Crowd: [in unison] Yes, we are all different!
> Man in crowd: I'm not...
> Crowd: Shhh!

- Attribution: Monty Python's Life of Brian
- Wikiquote: [Monty Python's Life of Brian](https://en.wikiquote.org/wiki/Monty_Python's_Life_of_Brian)
- Tags: irreverent

**13.**

> Announcer: And now for something completely different.

- Attribution: Monty Python's Flying Circus, Recurring lines
- Wikiquote: [Monty Python's Flying Circus](https://en.wikiquote.org/wiki/Monty_Python's_Flying_Circus)
- Tags: irreverent


## Hunter S. Thompson (35)

**1.**

> When the going gets weird, the weird turn pro.

- Attribution: Hunter S. Thompson, 1970s, "Fear and Loathing at the Super Bowl" (Rolling Stone #155, (28 February 1974); republished in Gonzo Papers, Vol. 1: The Great Shark Hunt: Strange Tal…
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**2.**

> To Richard Milhous Nixon, who never let me down.

- Attribution: Hunter S. Thompson, 1970s, epigraph to Gonzo Papers, Vol I : The Great Shark Hunt: Strange Tales from a Strange Time (1979), p. 7
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**3.**

> No point mentioning those bats, I thought. The poor bastard will see them soon enough.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing in Las Vegas (1971)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**4.**

> The kids are turned off from politics, they say. Most of 'em don't even want to hear about it. All they want to do these days is lie around on waterbeds and smoke that goddamn marrywanna... yeah, and just between you and me Fred thats probably all for the best.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**5.**

> A nervous blonde nymphet who thought that politics was some kind of game played by old people, like bridge.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**6.**

> So much for Objective Journalism. Don't bother to look for it here — not under any byline of mine; or anyone else I can think of. With the possible exception of things like box scores, race results, and stock market tabulations, there is no such thing as Objective Journalism. The phrase itself is a pompous contradiction in terms.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**7.**

> The massive, frustrated energies of a mainly young, disillusioned electorate that has long since abandoned the idea that we all have a duty to vote. This is like being told you have a duty to buy a new car, but you have to choose immediately between a Ford and a Chevy.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**8.**

> Jesus man! You don't look for acid! Acid finds you when it thinks you're ready.

- Attribution: Hunter S. Thompson, 1970s, Fear and Loathing: On the Campaign Trail '72 (1973)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**9.**

> The TV business is uglier than most things. It is normally perceived as some kind of cruel and shallow money trench through the heart of the journalism industry, a long plastic hallway where thieves and pimps run free and good men die like dogs, for no good reason.

- Attribution: Hunter S. Thompson, 1980s, Generation of Swine (1988), Originally published in the San Francisco Examiner (4 November 1985), this is often quoted as concluding with the statement "There's also a negative…
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**10.**

> He could shake your hand and stab you in the back at the same time.

- Attribution: Hunter S. Thompson, 1990s, He Was A Crook (1994)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**11.**

> Ah, fortune and fame shall follow me...and I shall dwell in the world of the chosen for a few moments of fleeting ecstasy; ere the seven burly lads turn into creditors and hustle me off to debtors' prison at last.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Porter Bibb III (6 February 1957), p. 44
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**12.**

> But fie on these unanswered queries and fie on those who pose them. There are stories to be written, drinks to be drunk, women to be ravished, and … alas, money to be made. We shall ride the bouncing ball and fight gamely to avoid being on the bottom when it bounces. … that is all ye know and all ye need to know. Amen.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Lieutenant Colonel Frank Campbell (6 January 1958), p. 96
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**13.**

> Events of the past two years have virtually decreed that I shall wrestle with the literary muse for the rest of my days. And so, having tasted the poverty of one end of the scale, I have no choice but to direct my energies toward the acquisition of fame and fortune. Frankly, I have no taste for either poverty or honest labor, so writing is the only recourse left me.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Arch Gerhart (29 January 1958), p. 106
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**14.**

> I may sound a little black, but I'm really pretty well adjusted.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Kay Menyers (17 March 1958), p. 109
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**15.**

> Sacrificing good men to journalism is like sending William Faulkner to work for Time magazine.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Jerome H. Walker (7 December 1958), p. 142
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**16.**

> Once I establish credit, I may be able to function. A man needs credit. Especially when he has no money.

- Attribution: Hunter S. Thompson, 1990s, The Proud Highway : The Fear and Loathing Letters Volume I (1997), Letter to Dwight Martin (21 February 1964), p. 440
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**17.**

> Disgusting as he usually was, on rare occasions he showed flashes of stagnant intelligence. But his brain was so rotted with drink and dissolute living that whenever he put it to work it behaved like an old engine that had gone haywire from being dipped in lard.

- Attribution: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**18.**

> What passed for society was a loud, giddy whirl of thieves and pretentious hustlers, a dull sideshow full of quacks and clowns and philistines with gimp mentalities.

- Attribution: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**19.**

> It was the kind of town that made you feel like Humphrey Bogart: you came in on a bumpy little plane, and, for some mysterious reason, got a private room with a balcony overlooking the town and the harbor; then you sat there and drank until something happened. I felt a tremendous distance between me and everything real.

- Attribution: Hunter S. Thompson, 1990s, The Rum Diary (1998)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**20.**

> For myself, I would much prefer to be stuck with Kentucky in the NCAA Tournament, than stuck with George Bush in the White House. It is the difference between losing your wallet at a cock fight and losing all your credit cards forever, along with your job and your house and your ability to earn enough money to pay off your sports-gambling debts or even a six-pack on game day. . .

- Attribution: Hunter S. Thompson, 2000s, "What's Better Than the Tournament?' (18 March 2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark
- **owner check**: partisan (names a sitting president of the day) and crude; funny, but your call

**21.**

> There was no time for scholarly details, and, besides, I have always believed that a man can fairly be judged by the standards and taste of his choices in matters of high-level plagiarism.

- Attribution: Hunter S. Thompson, 2000s, "Prisoner of Denver", in Vanity Fair (June 2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**22.**

> If you're going to be crazy, you have to get paid for it or else you're going to be locked up.

- Attribution: Hunter S. Thompson, 2000s, BankRate.com Interview (1 November 2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**23.**

> Richard Nixon could tell us a lot about peaking too early. He was a master of it, because it beat him every time. He never learned and neither did Bush the Elder.

- Attribution: Hunter S. Thompson, 2000s, Welcome to the Big Darkness (2003)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**24.**

> Paranoia is just another word for ignorance.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**25.**

> I shit on the chest of Fun.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**26.**

> We shit on the chest of Weird.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**27.**

> I have a theory that the truth is never told during the nine-to-five hours.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**28.**

> The only difference between the Sane and the Insane, is IN and yet within this world, the Sane have the power to have the Insane locked up.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**29.**

> All political power comes from the barrel of either guns, pussy, or opium pipes, and people seem to like it that way.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark
- **owner check**: crude line about power; your call

**30.**

> We are like pygmies lost in a maze of haze. We are not at war, we are having a nervous breakdown,again.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**31.**

> I understand that fear is my friend, but not always. Never turn your back on fear. It should always be in front of you, like a thing that might have to be killed.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**32.**

> The only ones left with any confidence at all are the New Dumb. It is the beginning of the end of our world as we knew it. Doom is the operative ethic.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**33.**

> I was also drunk, crazy and heavily armed at all times. People trembled and cursed when I came into a public room and started screaming in German.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**34.**

> I knew a Buddhist once, and I've hated myself ever since. The whole thing was a failure.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark

**35.**

> Music has always been a matter of energy to me, a question of fuel. Sentimental people call it inspiration, but what they really mean is fuel. I have always needed fuel. I am a serious consumer. On some nights I still believe that a car with the gas needle on empty can run about fifty more miles if you have the right music very loud on the radio.

- Attribution: Hunter S. Thompson, 2000s, Kingdom of Fear: Loathsome Secrets of a Star-crossed Child in the Final Days of the American Century (2004)
- Wikiquote: [Hunter S. Thompson](https://en.wikiquote.org/wiki/Hunter_S._Thompson)
- Tags: snark


## H. L. Mencken (35)

**1.**

> An idealist is one who, on noticing that a rose smells better than a cabbage, concludes that it is also more nourishing.

- Attribution: H. L. Mencken, 1910s, Smart Set (1910s), "A Few Pages of Notes," The Smart Set (January 1915); later published in A Little Book in C Major (1916)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**2.**

> Democracy is the theory that the common people know what they want, and deserve to get it good and hard.

- Attribution: H. L. Mencken, 1910s, Smart Set (1910s), "A Few Pages of Notes," The Smart Set (January 1915); later published in A Little Book in C major (1916), and A Mencken Crestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**3.**

> Progress: The process whereby the human race has got rid of whiskers, the vermiform appendix and God.

- Attribution: H. L. Mencken, 1910s, A Book of Burlesques (1916), A Book of Burlesques (1916)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**4.**

> Socialism is the theory that the desire of one man to get something he hasn't got is more pleasing to a just God than the desire of some other man to keep what he has got.

- Attribution: H. L. Mencken, 1910s, A Little Book in C Major (1916), A Little Book in C Major, New York, NY, John Lane Company (1916) p. 51
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**5.**

> The objection to Puritans is not that they try to make us think as they do, but that they try to make us do as they think.

- Attribution: H. L. Mencken, 1910s, A Little Book in C Major (1916), A Little Book in C Major, New York, NY, John Lane Company (1916) p. 53
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**6.**

> It is the dull man who is always sure, and the sure man who is always dull.

- Attribution: H. L. Mencken, 1920s, Prejudices, Second Series (1920) Ch. 1
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**7.**

> When I mount the scaffold at last these will be my farewell words to the sheriff: Say what you will against me when I am gone, but don't forget to add, in common justice, that I was never converted to anything.

- Attribution: H. L. Mencken, 1920s, Baltimore Evening Sun (12 June 1922)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**8.**

> What is any political campaign save a concerted effort to turn out a set of politicians who are admittedly bad and put in a set who are thought to be better. The former assumption, I believe is always sound; the latter is just as certainly false. For if experience teaches us anything at all it teaches us this: that a good politician, under democracy, is quite as unthinkable as an honest burglar.

- Attribution: H. L. Mencken, 1920s, Prejudices, Fourth Series (1924)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**9.**

> The basic fact about human existence is not that it is a tragedy, but that it is a bore. It is not so much a war as an endless standing in line. The objection to it is not that it is predominantly painful, but that it is lacking in sense.

- Attribution: H. L. Mencken, 1920s, Baltimore Evening Sun (9 August 1926)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**10.**

> No one in this world, so far as I know—and I have researched the records for years, and employed agents to help me—has ever lost money by underestimating the intelligence of the great masses of the plain people. Nor has anyone ever lost public office thereby.

- Attribution: H. L. Mencken, 1920s, 'Notes On Journalism' in the Chicago Tribune (19 September 1926)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**11.**

> Shave a gorilla and it would be almost impossible, at twenty paces, to distinguish him from a heavyweight champion of the world. Skin a chimpanzee, and it would take an autopsy to prove he was not a theologian.

- Attribution: H. L. Mencken, 1920s, Baltimore Evening Sun (4 April 1927)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**12.**

> The older I grow the more I distrust the familiar doctrine that age brings wisdom.

- Attribution: H. L. Mencken, 1920s, Prejudices, Third Series (1922), Ch. 3
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**13.**

> Faith may be defined briefly as an illogical belief in the occurrence of the improbable.

- Attribution: H. L. Mencken, 1920s, Prejudices, Third Series (1922), Ch. 14 "Types of Men" - 3 : The Believer
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**14.**

> When A annoys or injures B on the pretense of saving or improving X, A is a scoundrel.

- Attribution: H. L. Mencken, 1940s–present, Newspaper Days: 1899-1906 (1941)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**15.**

> In the present case it is a little inaccurate to say I hate everything. I am strongly in favor of common sense, common honesty and common decency. This makes me forever ineligible to any public office of trust or profit in the Republic. But I do not repine, for I am a subject of it only by force of arms.

- Attribution: H. L. Mencken, 1940s–present, As quoted in LIFE magazine, Vol. 21, No. 6, (5 August 1946), p. 52; this has also been paraphrased as "It is inaccurate to say I hate everything. I a…
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**16.**

> Nature abhors a moron.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**17.**

> Conscience is the inner voice that warns us somebody may be looking.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**18.**

> A celebrity is one who is known to many persons he is glad he doesn't know.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**19.**

> Platitude — An idea (a) that is admitted to be true by everyone, and (b) that is not true.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**20.**

> Remorse — Regret that one waited so long to do it.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**21.**

> Self-respect — The secure feeling that no one, as yet, is suspicious.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**22.**

> Before a man speaks it is always safe to assume that he is a fool. After he speaks, it is seldom necessary to assume it.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**23.**

> Democracy is the art and science of running the circus from the monkey cage.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**24.**

> Lawyer — One who protects us against robbers by taking away the temptation.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**25.**

> Theology — An effort to explain the unknowable by putting it into terms of the not worth knowing.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**26.**

> Creator — A comedian whose audience is afraid to laugh.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**27.**

> Sunday — A day given over by Americans to wishing that they themselves were dead and in Heaven, and that their neighbors were dead and in Hell.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**28.**

> A newspaper is a device for making the ignorant more ignorant and the crazy crazier.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**29.**

> Puritanism: The haunting fear that someone, somewhere, may be happy.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949), Sententiæ: The Citizen and the State, p. 624
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**30.**

> If x is the population of the United States and y is the degree of imbecility of the average American, then democracy is the theory that x × y is less than y.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949), Sententiæ: The Citizen and the State
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**31.**

> We must respect the other fellow's religion, but only in the sense and to the extent that we respect his theory that his wife is beautiful and his children smart.

- Attribution: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 1
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**32.**

> Human life is basically a comedy. Even its tragedies often seem comic to the spectator, and not infrequently they actually have comic touches to the victim. Happiness probably consists largely in the capacity to detect and relish them. A man who can laugh, if only at himself, is never really miserable.

- Attribution: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 15
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**33.**

> It is impossible to imagine the universe run by a wise, just and omnipotent God, but it is quite easy to imagine it run by a board of gods. If such a board actually exists it operates precisely like the board of a corporation that is losing money.

- Attribution: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 79
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**34.**

> The kind of man who wants the government to adopt and enforce his ideas is always the kind of man whose ideas are idiotic.

- Attribution: H. L. Mencken, 1940s–present, Minority Report : H.L. Mencken's Notebooks (1956), 323
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone

**35.**

> In this world of sin and sorrow there is always something to be thankful for. As for me, I rejoice that I am not a Republican.

- Attribution: H. L. Mencken, 1940s–present, A Mencken Chrestomathy (1949)
- Wikiquote: [H. L. Mencken](https://en.wikiquote.org/wiki/H._L._Mencken)
- Tags: Mencken: any tone
- **owner check**: partisan jab; kept under the Mencken rule


## Terry Pratchett (25)

**1.**

> Only in our dreams are we free. The rest of the time we need wages.

- Attribution: Terry Pratchett, General sources, Cited in Power Quotes: For Life, Business, and Leadership (2018) by Danai Krokou, ISBN 1-63157-750-6
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**2.**

> Eight years involved with the nuclear industry have taught me that when nothing can possibly go wrong and every avenue has been covered, then is the time to buy a house on the next continent.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**3.**

> As they say in Discworld, we are trying to unravel the Mighty Infinite using a language which was designed to tell one another where the fresh fruit was.

- Attribution: Terry Pratchett, General sources, Relatively Einstein episode 3, "Fantasy Physics" (18 January 2005); the Discworld version of this statement appears in Night Watch (2002)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**4.**

> Wikipedia, eh? Must be accurate then!

- Attribution: Terry Pratchett, General sources, The age interview (17 Feb 2007)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**5.**

> Nerds are the only people who know how to operate the video recorder.

- Attribution: Terry Pratchett, General sources, Desert Island Discs (1997)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**6.**

> Never trust any complicated cocktail that remains perfectly clear until the last ingredient goes in, and then immediately clouds.

- Attribution: Terry Pratchett, Usenet, alt.fan.pratchett (22 November 1993)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**7.**

> "Educational" refers to the process, not the object. Although, come to think of it, some of my teachers could easily have been replaced by a cheeseburger.

- Attribution: Terry Pratchett, Usenet, In response to a comment that if television is educational because watching it can teach you a lot about society, then a cheeseburger is also educati…
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**8.**

> I don't like the place at all. It's all wrong. An imposition on the Landscape. I reckon that Stonehenge was build by the contemporary equivalent of Microsoft, whereas Avebury was definitely an Apple circle.

- Attribution: Terry Pratchett, Usenet, On Stonehenge, at alt.fan.pratchett (8 June 1997)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**9.**

> Over the centuries, mankind has tried many ways of combatting the forces of evil... prayer, fasting, good works and so on. Up until Doom, no one seemed to have thought about the double-barrel shotgun. Eat leaden death, demon...

- Attribution: Terry Pratchett, Usenet, alt.fan.pratchett (30 May 1998)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**10.**

> 'They can ta'k our lives but they can never ta'k our freedom!' Now there's a battle cry not designed by a clear thinker...

- Attribution: Terry Pratchett, Usenet, Referring to a statement in the movie Braveheart, at alt.fan.pratchett (11 January 1999)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**11.**

> It's amazing how fast gold works.

- Attribution: Terry Pratchett, Usenet, On building the clacks, at alt.fan.pratchett (18 June 2002)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**12.**

> Go on, prove me wrong. Destroy the fabric of the universe. See if I care.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**13.**

> This isn't life in the fast lane, it's life in the oncoming traffic.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**14.**

> I mean, I wouldn't pay more than a couple of quid to see me, and I'm me.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**15.**

> Death isn't online. If he was, there would be a sudden drop in the death rate. Although it'd be interesting to see if he'd post things like: DON'T YOU THINK I SOUND LIKE JAMES EARL JONES?

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**16.**

> Too many people want to have written.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**17.**

> Up until now I'd always thought RSI meant 'I hate my damn job'.

- Attribution: Terry Pratchett, Usenet
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**18.**

> When they're standing right in front of you, kings are a kind of speech impediment.

- Attribution: Terry Pratchett, The Carpet People (1971; 1992)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**19.**

> Most armies are in fact run by their sergeants — the officers are there just to give things a bit of tone and prevent warfare from becoming a mere lower-class brawl.

- Attribution: Terry Pratchett, The Carpet People (1971; 1992)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**20.**

> It is well known that a vital ingredient of success is not knowing that what you're attempting can't be done.

- Attribution: Terry Pratchett, Equal Rites (1987)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**21.**

> Everyone's heard of Erwin Schrodinger's famous thought experiment. You put a cat in a box with a bottle of poison, which many people would suggest is about as far as you need to go.

- Attribution: Terry Pratchett, The Unadulterated Cat (1989)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**22.**

> The trouble with having an open mind, of course, is that people will insist on coming along and trying to put things in it.

- Attribution: Terry Pratchett, The Nome Trilogy (1989 - 1990), Diggers (1990)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**23.**

> Everything makes sense a bit at a time. But when you try to think of it all at once, it comes out wrong.

- Attribution: Terry Pratchett, Only You Can Save Mankind (1992)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**24.**

> I asked a teacher what the opposite of a miracle was and she, without thinking, I assume, said it was an act of God.
> You shouldn't say something like that to the kind of kid who will grow up to be a writer; we have long memories.

- Attribution: Terry Pratchett, "I create gods all the time - now I think one might exist" (2008)
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor

**25.**

> Tolkien's dead. J. K. Rowling said no. Philip Pullman couldn't make it. Hi, I'm Terry Pratchett.

- Attribution: Terry Pratchett, Misc, t-shirt worn by Pratchett at conventions
- Wikiquote: [Terry Pratchett](https://en.wikiquote.org/wiki/Terry_Pratchett)
- Tags: humor


## Fight Club (20)

**1.**

> I am Jack's... complete lack of surprise.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**2.**

> On a long enough time line, the survival rate for everyone drops to zero.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**3.**

> I am Jack's wasted life.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**4.**

> When you have insomnia, you're never really asleep... and you're never really awake.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**5.**

> With insomnia, nothing's real. Everything's far away. Everything's a copy of a copy of a copy.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**6.**

> When you have a gun in your mouth, you can only speak in vowels.

- Attribution: The Narrator, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**7.**

> Self-improvement is masturbation. Now, self-destruction...

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**8.**

> It's only after we've lost everything that we're free to do anything.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**9.**

> You are not your job. You're not how much money you have in the bank. You're not the car you drive. You're not the contents of your wallet. You're not your fucking khakis. You're the all-singing, all-dancing crap of the world.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**10.**

> The things you own end up owning you.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**11.**

> You have to consider the possibility that God does not like you, never wanted you, in all probability he hates you. It's not the worst thing that could happen.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent
- **owner check**: 'God hates you', irreverent about religion

**12.**

> Sticking feathers up your butt does not make you a chicken.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**13.**

> First you've gotta know - not fear, know - that someday you're gonna die.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**14.**

> We're consumers. We are the byproducts of a lifestyle obsession. Murder, crime, poverty, these things don't concern me. What concerns me are celebrity magazines, television with 500 channels, some guy's name on my underwear. Rogaine, Viagra, Olestra.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**15.**

> Listen up, maggots! You are not special. You are not a beautiful or unique snowflake. You are the same decaying organic matter as everything else. We are the all-singing, all-dancing crap of the world.

- Attribution: Tyler Durden, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**16.**

> A condom is the glass slipper of our generation. You slip one on when you meet a stranger. You dance all night, and then you throw it away. The condom, I mean, not the stranger.

- Attribution: Marla Singer, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent
- **owner check**: condom joke; mild, but check

**17.**

> It's a bridesmaid's dress. I got it at a second-hand store. It was loved intensely for one night.. then cast aside.

- Attribution: Marla Singer, Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**18.**

> Narrator: When people think you're dying, they really, really listen to you, instead of just …
> Marla Singer: … instead of just waiting for their turn to speak?
> Narrator: Yeah. Yeah.

- Attribution: Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**19.**

> Tyler Durden: You decide your level of involvement!
> Narrator: I will! I want to know certain things first.
> Everyone: The first rule of Project-- (Narrator: SHUT. UP.)

- Attribution: Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent

**20.**

> Narrator: I know it seems like I have more than one side sometimes...
> Marla Singer: More than one side? You're Dr. Jekyll and Mr. Jackass!

- Attribution: Fight Club (film)
- Wikiquote: [Fight Club (film)](https://en.wikiquote.org/wiki/Fight_Club_(film))
- Tags: pop-culture, irreverent


## Dorothy Parker (20)

**1.**

> And she had It. It, hell; she had Those.

- Attribution: Dorothy Parker, Regarding a character in Elinor Glyn's novel It; in her review, "Madame Glyn Lectures on 'It,' with Illustrations" in The New Yorker (November 26, 19…
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**2.**

> Salary is no object: I want only enough to keep body and soul apart.

- Attribution: Dorothy Parker, The New Yorker (February 4, 1928)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**3.**

> And it is that word 'hummy,' my darlings, that marks the first place in The House at Pooh Corner at which Tonstant Weader fwowed up.

- Attribution: Dorothy Parker, Her "Constant Reader" book review of The House at Pooh Corner by A. A. Milne, in The New Yorker (October 20, 1928)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**4.**

> That would be a good thing for them to cut on my tombstone: Wherever she went, including here, it was against her better judgment.

- Attribution: Dorothy Parker, "But the One on the Right" in The New Yorker (1929)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**5.**

> The House Beautiful is, for me, the play lousy.

- Attribution: Dorothy Parker, Review of "The House Beautiful" by Channing Pollock, The New Yorker (March 21, 1931)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**6.**

> Drink and dance and laugh and lie,
> Love, the reeling midnight through,
> For tomorrow we shall die!
> (But, alas, we never do.)

- Attribution: Dorothy Parker, "The Flaw in Paganism" in Death and Taxes (1931)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**7.**

> The ones I like ... are "cheque" and "enclosed."

- Attribution: Dorothy Parker, On the most beautiful words in the English language, as quoted in The New York Herald Tribune (December 12, 1932)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**8.**

> I might repeat to myself, slowly and soothingly, a list of quotations beautiful from minds profound; if I can remember any of the damn things.

- Attribution: Dorothy Parker, "The Little Hours" in Here Lies (1939)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**9.**

> I'm never going to accomplish anything; that's perfectly clear to me. I'm never going to be famous. My name will never be writ large on the roster of Those Who Do Things. I don't do anything. Not one single thing. I used to bite my nails, but I don't even do that any more.

- Attribution: Dorothy Parker, "The Little Hours" in Here Lies (1939)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**10.**

> One more drink and I'd have been under the host.

- Attribution: Dorothy Parker, As quoted in Try and Stop Me by Bennett Cerf (1944)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**11.**

> There's a hell of a distance between wise-cracking and wit. Wit has truth in it; wise-cracking is simply calisthenics with words.

- Attribution: Dorothy Parker, Interview, The Paris Review (Summer 1956)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**12.**

> It's not the tragedies that kill us; it's the messes.

- Attribution: Dorothy Parker, Interview, The Paris Review (Summer 1956)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**13.**

> All those writers who write about their own childhood! Gentle God, if I wrote about mine you wouldn't sit in the same room with me.

- Attribution: Dorothy Parker, Interview in The Paris Review, Issue #13 (Summer 1956)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**14.**

> [On being told of Calvin Coolidge's death] How do they know? (Coolidge was known as a man who said very few words.)

- Attribution: Dorothy Parker, Quoted in Writers at Work 1st Series by Malcolm Cowley (1958)
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**15.**

> Too fucking busy, and vice versa.

- Attribution: Dorothy Parker, Response to an editor pressuring her for overdue work, as quoted in The Unimportance of Being Oscar (1968) by Oscar Levant, p. 89
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**16.**

> You can lead a horticulture, but you can't make her think.

- Attribution: Dorothy Parker, Parker's answer when asked to use the word horticulture during a game of Can-You-Give-Me-A-Sentence?, as quoted in You Might as well Live by John Kea…
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**17.**

> What fresh hell can this be?

- Attribution: Dorothy Parker, "If the doorbell rang in her apartment, she would say, 'What fresh hell can this be?' — and it wasn't funny; she meant it." You might as well live: t…
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**18.**

> If you have any young friends who aspire to become writers, the second greatest favor you can do them is to present them with copies of The Elements of Style. The first greatest, of course, is to shoot them now, while they're happy.

- Attribution: Dorothy Parker, From a review of the revised edition of The Elements of Style by William Strunk Jr. and E. B. White published in Esquire (November 1959).
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**19.**

> Brevity is the soul of lingerie.

- Attribution: Dorothy Parker, "Our Mrs Parker" (1934), Caption written for Vogue 1916
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor

**20.**

> Katharine Hepburn delivered a striking performance that ran the gamut of emotions, from A to B.

- Attribution: Dorothy Parker, "Our Mrs Parker" (1934), Woollcott writes in While Rome Burns that Parker had "recently...achieved an equal compression in reporting on The Lake, Miss Hepburn, it seems, had…
- Wikiquote: [Dorothy Parker](https://en.wikiquote.org/wiki/Dorothy_Parker)
- Tags: snark, humor


## Kurt Vonnegut (15)

**1.**

> This speech will be very short. After all, you asked me to come here — I didn't ask you.

- Attribution: Kurt Vonnegut, Various quotations
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**2.**

> The thing I just wrote [Slaughterhouse Five] is my masterpiece. The rest of it is going to be crap from now on.

- Attribution: Kurt Vonnegut, Various quotations
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**3.**

> Science fiction is like other writing. It is just novels and short stories with machines.

- Attribution: Kurt Vonnegut, Various quotations
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**4.**

> Nothing in this book is true.

- Attribution: Kurt Vonnegut, Cat's Cradle (1963)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**5.**

> Busy, busy, busy.

- Attribution: Kurt Vonnegut, Cat's Cradle (1963)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**6.**

> Like all real heroes, Charley had a fatal flaw. He refused to believe that he had gonorrhea, whereas the truth was that he did.

- Attribution: Kurt Vonnegut, God Bless You, Mr. Rosewater (1965), On Charley Warmergran, the Fire Chief of Rosewater.
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**7.**

> You understand, of course, that everything I say is horseshit.

- Attribution: Kurt Vonnegut, Playboy interview (1973)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**8.**

> Here is a lesson in creative writing. First rule: Do not use semicolons. They are transvestite hermaphrodites representing absolutely nothing. All they do is show you've been to college.

- Attribution: Kurt Vonnegut, A Man Without a Country (2005), A close paraphrase of this (beginning "Do not use...") is repeated in a commencement address at Cloves Hall, 27 April 2007, as reprinted in Armageddo…
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor
- **owner check**: the semicolon joke uses a phrase about people; metaphor only, but check

**9.**

> We are here on Earth to fart around. Don't let anybody tell you any different.

- Attribution: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**10.**

> Evolution is so creative. That's how we got giraffes.

- Attribution: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**11.**

> If I should ever die, God forbid, I hope you will say, "Kurt is up in heaven now." That's my favorite joke.

- Attribution: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**12.**

> It is almost always a mistake to mention Abraham Lincoln. He always steals the show.

- Attribution: Kurt Vonnegut, A Man Without a Country (2005)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**13.**

> Like most science-fiction writers, Trout knew almost nothing about science.

- Attribution: Kurt Vonnegut, Breakfast of Champions (1973)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**14.**

> How embarrassing to be human.

- Attribution: Kurt Vonnegut, Hocus Pocus (1990)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor

**15.**

> Shrapnel was invented by an Englishman of the same name. Don't you wish you could have something named after you?

- Attribution: Kurt Vonnegut, I Love You, Madame Librarian (2004)
- Wikiquote: [Kurt Vonnegut](https://en.wikiquote.org/wiki/Kurt_Vonnegut)
- Tags: humor


## Groucho Marx (10)

**1.**

> A likely story — and probably true.

- Attribution: Groucho Marx, The Al Jolson Show repartee following a trite, scripted Al Jolson joke. (1949)
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**2.**

> Although it is generally known, I think it's about time to announce that I was born at a very early age.

- Attribution: Groucho Marx, From his autobiography Groucho and Me (1959)
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**3.**

> I sent the club a wire stating, "PLEASE ACCEPT MY RESIGNATION. I DON'T WANT TO BELONG TO ANY CLUB THAT WILL ACCEPT PEOPLE LIKE ME AS A MEMBER".

- Attribution: Groucho Marx, Telegram to the Friar's Club of Beverly Hills to which he belonged, as recounted in Groucho and Me (1959), p. 321
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**4.**

> From the moment I picked your book up until I laid it down I was convulsed with laughter. Someday I intend on reading it.

- Attribution: Groucho Marx, To S. J. Perelman about his book Dawn Ginsbergh’s Revenge (1929), as quoted in LIFE (9 February 1962)
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**5.**

> I never forget a face, but in your case I'll be glad to make an exception.

- Attribution: Groucho Marx, Quote by Leo Rosten in The Many Worlds of Leo Rosten (1964)
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**6.**

> My experience is that people are most likely to listen to reason when in bed.

- Attribution: Groucho Marx, Liner notes of An Evening With Groucho (1972) the recording of his appearance at Carnegie Hall.
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**7.**

> I find television very educational. Every time someone switches it on I go into another room and read a good book.

- Attribution: Groucho Marx, As quoted in Halliwell’s Filmgoer’s Companion (1984) by Leslie Halliwell
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**8.**

> To write an autobiography of Groucho Marx would be as asinine as to read an autobiography of Groucho Marx.

- Attribution: Groucho Marx, Just after completing his second autobiography, as quoted in The Marx Brothers: A Bio-bibliography (1987) by Wes D. Gehring, p. 137
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**9.**

> Die, my dear? Why that's the last thing I'll do!

- Attribution: Groucho Marx, Last words
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor

**10.**

> Years ago, I tried to top everybody, but I don't anymore. I realized it was killing conversation. When you're always trying for a topper you aren't really listening. It ruins communication.

- Attribution: Groucho Marx, The Groucho Phile (1976), p. 308
- Wikiquote: [Groucho Marx](https://en.wikiquote.org/wiki/Groucho_Marx)
- Tags: humor


## George Carlin (10)

**1.**

> Have you ever noticed that anybody driving slower than you is an idiot, and anyone going faster than you is a maniac?

- Attribution: George Carlin, Carlin on Campus (1984)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**2.**

> "One thing leads to another"? Not always. Sometimes one thing leads to the same thing. Ask an addict.

- Attribution: George Carlin, Books, Brain Droppings (1997)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**3.**

> When it comes to God's existence, I'm not an atheist and I'm not an agnostic- I'm an acrostic, the whole thing puzzles me.

- Attribution: George Carlin, Books, Brain Droppings (1997)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**4.**

> I put a dollar in a change machine. Nothing changed.

- Attribution: George Carlin, Books, Brain Droppings (1997)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**5.**

> Meow means "woof" in cat.

- Attribution: George Carlin, Books, Brain Droppings (1997)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**6.**

> I think I am, therefore I am. I think.

- Attribution: George Carlin, Books, Napalm and Silly Putty (2001)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**7.**

> "Undisputed heavyweight champion." Well, if it's undisputed, what's all the fighting about?

- Attribution: George Carlin, Books, Napalm and Silly Putty (2001)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**8.**

> I had no shoes, and I felt sorry for myself until I met a man who had no feet. I took his shoes. Now I feel better.

- Attribution: George Carlin, Books, When Will Jesus Bring the Pork Chops? (2004)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark
- **owner check**: 'I took his shoes' is dark; could read as cruel

**9.**

> They say rather than cursing the darkness, one should light a candle. They don't mention anything about cursing a lack of candles.

- Attribution: George Carlin, Books, When Will Jesus Bring the Pork Chops? (2004)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark

**10.**

> I think people should be allowed to do anything they want. We haven't tried that for a while. Maybe this time it'll work.

- Attribution: George Carlin, Books, Brain Droppings (1997)
- Wikiquote: [George Carlin](https://en.wikiquote.org/wiki/George_Carlin)
- Tags: humor, snark


## Mark Twain (10)

**1.**

> Reader, suppose you were an idiot. And suppose you were a member of Congress. But I repeat myself.

- Attribution: Mark Twain, Draft manuscript (c.1881), quoted by Albert Bigelow Paine in Mark Twain: A Biography (1912), p. 724
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**2.**

> I am opposed to millionaires, but it would be dangerous to offer me the position.

- Attribution: Mark Twain, American Claimant (1892)
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**3.**

> Always do right. This will gratify some people, and astonish the rest.

- Attribution: Mark Twain, To the Young People's Society, Greenpoint Presbyterian Church, Brooklyn (16 February 1901)
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**4.**

> To create man was a fine and original idea; but to add the sheep was a tautology.

- Attribution: Mark Twain, St. Louis Post-Dispatch (30 May 1902); also in Mark Twain : A Life, p. 611
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**5.**

> Clothes make the man. Naked people have little or no influence on society.

- Attribution: Mark Twain, More Maxims of Mark (1927) edited by Merle Johnson
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**6.**

> Always acknowledge a fault frankly. This will throw those in authority off their guard and give you opportunity to commit more.

- Attribution: Mark Twain, More Maxims of Mark (1927) edited by Merle Johnson
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**7.**

> If you pick up a starving dog and make him prosperous, he will not bite you. This is the principal difference between a dog and a man.

- Attribution: Mark Twain, The Tragedy of Pudd'nhead Wilson (1894), p. 214.
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**8.**

> In German, a young lady has no sex, while a turnip has.

- Attribution: Mark Twain, A Tramp Abroad (1880)
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**9.**

> Persons attempting to find a motive in this narrative will be prosecuted; persons attempting to find a moral in it will be banished; persons attempting to find a plot in it will be shot.
> BY ORDER OF THE AUTHOR.

- Attribution: Mark Twain, Adventures of Huckleberry Finn (1885), Notice
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark

**10.**

> What is the difference between a taxidermist & a tax-collector? The taxidermist only takes your skin.

- Attribution: Mark Twain, Mark Twain's Notebook (1935), p. 379
- Wikiquote: [Mark Twain](https://en.wikiquote.org/wiki/Mark_Twain)
- Tags: humor, snark


## Blackadder (10)

**1.**

> Blackadder: Right. Good morning, team. My name is Lord Blackadder. And I'm the new minister in charge of religious genocide. If you play fair by me, you'll find me a considerate employer. But cross me and you'll soon discover that under this playful, boyish exterior beats the heart of a ruthless, sadistic maniac.

- Attribution: Blackadder II, Head, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark
- **owner check**: 'religious genocide' as an office joke; dark satire

**2.**

> Blackadder: Baldrick! That Farrow bloke you executed today, you sure he's dead?
> Baldrick: I chopped his head off. That usually does the trick.
> Blackadder: Yes, don't get clever with me. I just thought you might've lopped off a leg or something by mistake.
> Baldrick: No, the thing I chopped off had a nose.

- Attribution: Blackadder II, Head, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**3.**

> Melchett: Potato?
> Blackadder: Thanks, I don't.

- Attribution: Blackadder II, Potato, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**4.**

> Percy: I intend to discover, this very afternoon, the secret of alchemy - the hidden art of turning base things into gold.
> Blackadder: I see. And the fact that this secret has eluded the most intelligent of men since the dawn of time doesn't dampen your spirits?
> Percy: Oh no. I like a challenge!

- Attribution: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**5.**

> Messenger: [enters again] My lord, the Queen does demand your urgent presence, on pain of death.
> Blackadder: You're not making any friends here. You do know that, don't you?

- Attribution: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**6.**

> Messenger: [enters] My lord-
> Blackadder: [sarcastic] Ah, messenger, thank God you came. Percy and I could not have waited another second without you.

- Attribution: Blackadder II, Money, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**7.**

> [At the Queen's party, she comes dressed as her father, King Henry VIII]
> Queen: [deep voice] Yo ho ho, off with their heads!
> Percy: Ma'am, it is brilliant! Your father is born again!
> Queen: [normal voice] Let's bally well hope not, or else I won't be queen anymore.

- Attribution: Blackadder II, Chains, Blackadder II (series 2)
- Wikiquote: [Blackadder II (series 2)](https://en.wikiquote.org/wiki/Blackadder_II_(series_2))
- Tags: humor, snark

**8.**

> [Baldrick has become the new MP for Dunny-On-The-Wold]
> Blackadder: We are reprieved. It is a triumph for stupidity over common sense.
> Baldrick: Thank you very much.
> Blackadder: As a reward, Baldrick, take a short holiday. Did you enjoy it? Right. Back to work.

- Attribution: Blackadder the Third, Dish and Dishonesty, Blackadder the Third (series 3)
- Wikiquote: [Blackadder the Third (series 3)](https://en.wikiquote.org/wiki/Blackadder_the_Third_(series_3))
- Tags: humor, snark

**9.**

> Doctor Johnson: [reading Baldrick's 'novel'] "Once upon a time, there was a lovely little sausage called-" Sausage? Sausage?! Oh, blast your eyes! [crumples it up, throws it to the ground and storms out]
> Baldrick: Oh, I didn't think it was that bad.
> Blackadder: [checks the dictionary] I think you'll find he left 'sausage' out of his dictionary, Baldrick. [checks again] Oh, and 'aardvark'.

- Attribution: Blackadder the Third, Ink and Incapability, Blackadder the Third (series 3)
- Wikiquote: [Blackadder the Third (series 3)](https://en.wikiquote.org/wiki/Blackadder_the_Third_(series_3))
- Tags: humor, snark

**10.**

> Blackadder: I spy with my bored little eye something beginning with 'T'.
> Baldrick: Breakfast!
> Blackadder: What?
> Baldrick: My breakfast always begins with tea. Then I have a little sausage, and then an egg with some little soldiers.
> Blackadder: Baldrick, when I said it begins with 'T', I was talking about a letter.
> Baldrick: No, it never begins with a letter. The postman don't come 'til ten thirty.

- Attribution: Blackadder Goes Forth, Plan E: General Hospital, Blackadder Goes Forth (series 4)
- Wikiquote: [Blackadder Goes Forth (series 4)](https://en.wikiquote.org/wiki/Blackadder_Goes_Forth_(series_4))
- Tags: humor, snark


## The Big Lebowski (10)

**1.**

> Well, sir, it's this rug I had. It really tied the room together.

- Attribution: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**2.**

> No I do mind. Uhh, The Dude minds. This will not stand. This aggression will not stand, man.

- Attribution: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**3.**

> You're not wrong Walter. You're just an asshole.

- Attribution: Jeffrey "The Dude" Lebowski, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**4.**

> Smokey, this is not Nam, this is Bowling, there are rules.

- Attribution: Walter Sobchak, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**5.**

> Smokey, my friend. [pulls out a Colt M1911A1 from his bag] You're entering a world of pain.

- Attribution: Walter Sobchak, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**6.**

> Eh, fuck it, Dude. Let's go bowling.

- Attribution: Walter Sobchak, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**7.**

> A wiser man than myself once said, "Sometimes you eat the b'ar.… Sometimes the b'ar, well, he eats you."

- Attribution: The Stranger, The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**8.**

> Walter Sobchak: You know, Dude, I myself dabbled in pacifism at one point. Not in 'Nam of course.
> The Dude: And, you know, he's got emotional problems, man.
> Walter Sobchak: You mean beyond pacifism?

- Attribution: The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**9.**

> Maude Lebowski: What do you do for— for recreation?
> The Dude: Oh, the usual. I bowl. Drive around. The occasional acid flashback.

- Attribution: The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture

**10.**

> The Dude: Well, take care, man. Gotta get back.
> The Stranger: Sure. Take it easy, Dude.
> The Dude: Oh yeah!
> The Stranger: I know that you will.
> The Dude: Yeah, well, the Dude abides.
> The Stranger: "The Dude abides." I don't know about you, but I take comfort in that. It's good knowin' he's out there. The Dude. Takin' 'er easy for all us sinners. Shoosh. I sure hope he makes the finals.

- Attribution: The Big Lebowski
- Wikiquote: [The Big Lebowski](https://en.wikiquote.org/wiki/The_Big_Lebowski)
- Tags: humor, pop-culture


## Red Dwarf (10)

**1.**

> Holly: I am Holly, the ship's computer, with an IQ of 6000; the same IQ as 6000 PE teachers.

- Attribution: Red Dwarf: Series I (1988), Future Echoes, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**2.**

> Lister: What time is it?
> Rimmer: (blearily crawls over to the clock on the bedside table) Saturday.
> Lister: That the best you can do?
> Rimmer: There are some numbers next to it, but they could be anything.

- Attribution: Red Dwarf: Series II (1988), Thanks for the Memory, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**3.**

> Rimmer: I loved that little lemming. I built him a little wall he could hurl himself off of.

- Attribution: Red Dwarf: Series II (1988), Stasis Leak, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**4.**

> Holly: [after being insulted about his temporarily reduced IQ]: 6? Do me a lemon! That's a poor IQ for a glass of water!

- Attribution: Red Dwarf: Series II (1988), Queeg, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**5.**

> Kryten: I think there's something wrong with the gearbox. The thing is, I learned to drive in Starbug 2. I'm not used to the controls in Starbug 1.
> Rimmer: They're exactly the same.
> Kryten: Yes. That's the problem.

- Attribution: Red Dwarf: Series III (1989), Backwards, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**6.**

> Holly: Abandon ship! Abandon ship! Black hole approaching! This is not a drill. This is a drill! [pneumatic drill sound] Abandon shi- Oh God, now the siren's bust.... Awooga! Awooga! Abandon ship!

- Attribution: Red Dwarf: Series III (1989), Marooned, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**7.**

> Rimmer: So Holly managed to navigate through five black holes?
> Holly: As it 'appens, there weren't any black 'oles.
> Rimmer: But you saw them!
> Holly: They weren't black 'oles.
> Rimmer (resigned): What were they?
> Holly: Grit. Five specs of grit on the scanner scope. Y'see the thing about grit, is it's black. And the thing about the scanner scope...
> Rimmer: Ohhhh!

- Attribution: Red Dwarf: Series III (1989), Marooned, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**8.**

> Kryten: "Pub." Ah, yes: a meeting place where people attempt to achieve advanced states of mental incompetence by the repeated consumption of fermented vegetable drinks.

- Attribution: Red Dwarf: Series III (1989), Timeslides, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**9.**

> Kryten: [reading Hitler's diary] Things to remember: Stop milk, pay papers, invade Czechoslovakia!

- Attribution: Red Dwarf: Series III (1989), Timeslides, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor

**10.**

> Rimmer: At least he gets 24 hours notice, that's more than most of us get. Most of us get "Mind that bus!" "What bus?" Splat!

- Attribution: Red Dwarf: Series III (1989), The Last Day, Red Dwarf
- Wikiquote: [Red Dwarf](https://en.wikiquote.org/wiki/Red_Dwarf)
- Tags: humor


## Futurama (10)

**1.**

> Professor Farnsworth: Time travel is impossible!
> Fry: But Professor, you time traveled yourself remember? When we went back to Roswell?
> Professor Farnsworth: That proves nothing! And furthermore, you'd think I could remember a thing like that; plus, who are you anyway?

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**2.**

> Fry: I don't get it. How can you say Lars is more mature than me?
> Leela: Well, for one thing his checkbook doesn't have The Hulk on it.

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**3.**

> [Nudar is ordering Bender to kill Fry]
> Nudar: You know what to do.
> Bender: You want me to concludify him, like some sort of dispatcherator?
> Nudar: Yes, and don't forget to terminate him.

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**4.**

> [Nudar is telling Bender how to steal the Sphero-Boom from the professor.]
> Nudar: You'll need jeweller's tools and foot cup silencers.
> Bender: Hey, I don't tell you how to tell me what to do, so don't tell me how to do what you tell me to do!

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**5.**

> Nudar: Faster, faster!
> Professor Farnsworth: I’m sciencing as fast as I can

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**6.**

> [Fry is recounting how he survived his trip to the past.]
> Fry: Oh, it's an astonishing tale of incredibleness. It all began went I went back in time.
> Professor Farnsworth: Duh!

- Attribution: Futurama: Bender's Big Score
- Wikiquote: [Futurama: Bender's Big Score](https://en.wikiquote.org/wiki/Futurama:_Bender's_Big_Score)
- Tags: humor, irreverent

**7.**

> Bender: I feel great and it's all thanks to Calculon. His visit really inspired me. I finally know what I want to be when I grow up.
> Hermes: You want to co-star in his TV show, like that time you already did that?
> Bender: No. I'm going to be a stalker.
> Leela: That's not a career, more of a felony.

- Attribution: Futurama: The Beast with a Billion Backs
- Wikiquote: [Futurama: The Beast with a Billion Backs](https://en.wikiquote.org/wiki/Futurama:_The_Beast_with_a_Billion_Backs)
- Tags: humor, irreverent

**8.**

> Hermes: It got Zoidberg!
> Professor Farnsworth: Oh, I never knew how much I'd miss him until he was gone! Not that much, as it turns out.

- Attribution: Futurama: The Beast with a Billion Backs
- Wikiquote: [Futurama: The Beast with a Billion Backs](https://en.wikiquote.org/wiki/Futurama:_The_Beast_with_a_Billion_Backs)
- Tags: humor, irreverent

**9.**

> Wernstrom: I volunteer to lead the expedition. I have a squad of graduate students eager to risk their lives for a letter of recommendation.
> Professor Farnsworth: Your squad sucks bosons! My team is twice as qualified and three times as expendable!
> Planet Express Crew: Yeah!

- Attribution: Futurama: The Beast with a Billion Backs
- Wikiquote: [Futurama: The Beast with a Billion Backs](https://en.wikiquote.org/wiki/Futurama:_The_Beast_with_a_Billion_Backs)
- Tags: humor, irreverent

**10.**

> The Grand Priestess/Funeral Director: I am the Grand Funeral Director!
> Dr. Zoidberg: Do you validate parking?

- Attribution: Futurama: The Beast with a Billion Backs
- Wikiquote: [Futurama: The Beast with a Billion Backs](https://en.wikiquote.org/wiki/Futurama:_The_Beast_with_a_Billion_Backs)
- Tags: humor, irreverent


## Office Space (10)

**1.**

> Michael, I did nothing. I did absolutely nothing, and it was everything I thought it could be.

- Attribution: Peter Gibbons, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**2.**

> (after asking Peter to come in and work on Saturday) Ah, ah, I almost forgot... I'm also going to need you to go ahead and come in on Sunday, too. We, uhhh, lost some people this week and we sorta need to play catch-up. Thaaaaaanks.

- Attribution: Bill Lumbergh, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**3.**

> The ratio of cake to people is too big...

- Attribution: Milton Waddams, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**4.**

> I could set the building on fire...

- Attribution: Milton Waddams, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**5.**

> I believe you have my stapler...

- Attribution: Milton Waddams, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**6.**

> And yes, I won't be leaving a tip, 'cause I could... I could shut this whole resort down. Sir? I'll take my traveler's checks to a competing resort. I could write a letter to your board of tourism and I could have this place condemned. I could put... I could put... strychnine in the guacamole. There was salt on the glass, BIG grains of salt.

- Attribution: Milton Waddams, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**7.**

> What would ya say... ya do here?

- Attribution: Bob Slydell, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**8.**

> [frustrated with the malfunctioning printer] Why does it say "Paper Jam" when there is no paper jam?!

- Attribution: Samir Nagheenanajar, Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**9.**

> Peter Gibbons: Let me ask you something. When you come in on Monday and you're not feeling real well, does anyone ever say to you, "Sounds like someone has a case of the Mondays?"
> Lawrence: No. No, man. Shit, no, man. I believe you'd get your ass kicked sayin' something like that, man.

- Attribution: Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture

**10.**

> Bob Porter: Looks like you've been missing a lot of work lately.
> Peter Gibbons: Well, I wouldn't exactly say I've been missing it, Bob.

- Attribution: Office Space
- Wikiquote: [Office Space](https://en.wikiquote.org/wiki/Office_Space)
- Tags: humor, pop-culture


## Jack Handey (Deep Thoughts) (9)

**1.**

> If trees could scream, would we be so cavalier about cutting them down? We might, if they screamed all the time, for no good reason.

- Attribution: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**2.**

> I can picture in my mind a world without war, a world without hate. And I can picture us attacking that world because they'd never expect it.

- Attribution: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**3.**

> If a kid asks where rain comes from, I think a cute thing to tell him is "God is crying." And if he asks why God is crying, another cute thing to tell him is "Probably because of something you did."

- Attribution: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor
- **owner check**: 'God is crying ... because of something you did'; dark for a kid joke

**4.**

> It takes a big man to cry, but it takes an even bigger man to laugh at that man.

- Attribution: Jack Handey, Deep Thoughts: Inspiration for the Uninspired (1992), Berkley Books, ISBN 0-425-13365-6
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**5.**

> IF YOU ever drop your keys into a river of molten lava, let 'em go, because man, they're gone.

- Attribution: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**6.**

> TO ME, it's a good idea to always carry two sacks of something when you walk around. That way, if anybody says, "Hey, can you give me a hand?" you can say, "Sorry, got these sacks."

- Attribution: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**7.**

> IF YOU GO through a lot of hammers each month, I don't think it necessarily means you're a hard worker. It may just mean that you have a lot to learn about proper hammer maintenance.

- Attribution: Jack Handey, Deeper Thoughts : All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**8.**

> MAYBE in order to understand mankind, we have to look at the word itself. Basically, it's made up of two separate words — "mank" and "ind." What do these words mean? It's a mystery, and that's why so is mankind.

- Attribution: Jack Handey, Deeper Thoughts: All New, All Crispy (1993), Hachette Books, ISBN 1-56282-840-1
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor

**9.**

> I guess we were kinda poor when we were kids, but we didn't know it. That's because my dad always refused to let us look at the family's financial records.

- Attribution: Jack Handey, Fuzzy Memories (1996), Andrews McMeel Publishing, ISBN 0-8362-1040-9
- Wikiquote: [Jack Handey](https://en.wikiquote.org/wiki/Jack_Handey)
- Tags: irreverent, humor


## Revisions used

- Blackadder Goes Forth (series 4): revision 3803833
- Blackadder II (series 2): revision 3784336
- Blackadder the Third (series 3): revision 3754540
- Dorothy Parker: revision 4012240
- Douglas Adams: revision 3931377
- Fight Club (film): revision 4001607
- Futurama: Bender's Big Score: revision 4014780
- Futurama: The Beast with a Billion Backs: revision 4020346
- George Carlin: revision 3941591
- Groucho Marx: revision 4020043
- H. L. Mencken: revision 3962671
- Hunter S. Thompson: revision 3901461
- Jack Handey: revision 3492162
- Kurt Vonnegut: revision 3959939
- Mark Twain: revision 3992852
- Monty Python and the Holy Grail: revision 4001452
- Monty Python's Flying Circus: revision 4000743
- Monty Python's Life of Brian: revision 4000732
- Office Space: revision 4015751
- Red Dwarf: revision 4023807
- Terry Pratchett: revision 3924187
- The Big Lebowski: revision 4024202
- The Hitchhiker's Guide to the Galaxy: revision 3883300
