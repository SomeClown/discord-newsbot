"""Lounge welcomes and the daily quote (design.md §14).

Everything that decides what gets said in the lounge channel lives here, and
nothing in this package talks to the Discord gateway; the bot hands in
callables and gets plain values back, the same arrangement the SHiFT
package has.

This file deliberately imports nothing. `config.py` reaches into
`lounge.welcome` and `lounge.default_sources`, and later modules reach back
out through `bot.format` to `config`, so an eager import here would turn the
package into a circle. I found that out on paper, which is the cheap way.
"""
