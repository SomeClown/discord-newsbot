"""What discord.py really sends for a nonce (qa review, fix 9: settling the comments).

Two reviewers disagreed about whether `enforce_nonce` goes out with the digest publisher's sends,
and the comments in `client.py` and `guild_digest.py` had to take a side. This pins the answer
in discord.py 2.7.1: `handle_message_parameters` adds `enforce_nonce: True` whenever a nonce is
passed, and `Messageable.send` always passes one (a random one if the caller gave none). So
Discord does dedupe our sends, within its own short memory for nonces. If an upgrade ever changes
that, this fails and the comments need another look.
"""

from __future__ import annotations

import inspect

from discord import abc, http


def test_a_nonce_goes_out_with_enforce_nonce_set():
    with http.handle_message_parameters(content="hello", nonce="abc123") as params:
        assert params.payload["nonce"] == "abc123"
        assert params.payload["enforce_nonce"] is True


def test_no_nonce_means_no_enforcement_at_this_layer():
    with http.handle_message_parameters(content="hello") as params:
        assert "nonce" not in params.payload and "enforce_nonce" not in params.payload


def test_send_always_mints_a_nonce_when_the_caller_passes_none():
    source = inspect.getsource(abc.Messageable.send)
    assert "if nonce is None:" in source and "nonce = secrets.randbits(64)" in source
