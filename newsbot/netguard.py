"""Where an outbound request is allowed to go: public addresses only.

Anything that fetches a URL somebody else controls (a feed, a page of SHiFT codes, a raw
quotes file) can be told by that somebody to go somewhere else. If nothing looked first, "somewhere
else" could be the cloud metadata endpoint at `169.254.169.254` or a service only reachable from
inside this container's own network. Two checks, used by both `collectors/rss.py` and
`lounge/sources.py`, which follow redirects by hand so they can call the first one before every
request instead of after it (I used to check the final URL once httpx had followed the whole
chain, which is the bouncer checking your ID after you've walked through the dodgy alley).

Both raise `AddressRefused`, a `ValueError`, with a message that names the host and is safe to
show: no path, no query string, so no secret that rode along in a URL.
"""

from __future__ import annotations

import asyncio
import ipaddress

import httpx

# The host check's own DNS lookup gets this long. `getaddrinfo` has no timeout of its own,
# and a resolver that never answers would park the whole collection pass behind one feed.
DNS_TIMEOUT_S = 10.0


# NAT64 gateways turn an IPv6 address with an IPv4 address tucked into its last 32 bits into a
# request to that IPv4 address. The stdlib calls the whole well-known prefix global, so on a
# network with a gateway `64:ff9b::a9fe:a9fe` would be the metadata endpoint in a trench coat.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# The local-use prefix (RFC 8215) is for a network's own gateway: never public by definition, and
# where the IPv4 part sits depends on a prefix length I can't see, so the whole /48 is refused.
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class AddressRefused(ValueError):
    """A host, or the address a connection landed on, that isn't public."""


def _is_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether `addr` is somewhere this bot may talk to.

    `is_global` rather than a list of is_private/is_loopback/...: the
    list missed CGNAT space (100.64.0.0/10), and I'd rather let the
    stdlib keep the catalogue of non-public ranges than keep it myself.
    The one thing it gets wrong is NAT64 (see above), which is judged by the IPv4 address
    inside it.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _NAT64:
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF).is_global
        if addr in _NAT64_LOCAL:
            return False
    return addr.is_global


def host_name(url: httpx.URL) -> str:
    """The name the connection will use for `url`'s host: httpx's ASCII (IDNA 2008) form.

    Not `url.host`, which is the Unicode form: `socket.getaddrinfo` encodes that with the older
    IDNA 2003 rules, so `faß.example` would be looked up as `fass.example` while httpx connects
    to `xn--fa-hia.example`. A name an attacker controls could pass the check on one address and
    be requested on another. For an IP literal this is the address without its brackets.
    """
    return url.raw_host.decode("ascii")


async def reject_private_host(url: httpx.URL) -> None:
    """Raise `AddressRefused` if `url`'s host is, or resolves to, a non-public address.

    Call it before every request: the configured URL and each redirect target. A literal IP is
    judged as it stands; a name is resolved and every address it answers with has to pass.
    """
    host = host_name(url)
    if not host:
        return
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        # Not a literal IP: resolve it. A DNS failure here isn't this
        # function's problem to report; the request that follows will fail
        # with httpx's own connection error, which says it better.
        try:
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(DNS_TIMEOUT_S):
                infos = await loop.getaddrinfo(host, None)
        except TimeoutError:
            # Unlike a plain DNS failure, a resolver that stalls is a host nobody vetted, and a
            # stalled lookup is how a collection pass could hang. Refuse the source. (This
            # clause goes first because TimeoutError is an OSError, and the next one waves
            # those through.)
            raise AddressRefused(
                f"could not check where {host} points: DNS lookup timed out"
            ) from None
        except OSError:
            return
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addresses:
        if not _is_public(addr):
            raise AddressRefused(f"refusing a non-public host: {host} ({addr})")


def reject_private_peer(url: httpx.URL, response: httpx.Response) -> None:
    """Raise `AddressRefused` if the socket behind `response` is connected to a non-public address.

    `reject_private_host` looks a name up; the connection looks it up again on its own, and a
    name can answer differently the second time (a public address for us, a private one for
    the connection). This asks the connection where it actually went, before any body is read.
    What it catches: that request did go out, but nothing it returns comes back into the bot.
    What it doesn't: stop the request, which would mean pinning the connection to the address
    we checked, and that means rewriting how every feed connects (TLS SNI included). The owner
    decided against it. Callers run it on redirect responses too, since a redirect answered
    from a private address is just as wrong; it costs one dictionary lookup.

    A transport with no real socket behind it (`httpx.MockTransport`, or anything else that
    doesn't set the `network_stream` extension) has no address to report, and then this does
    nothing: the pre-request check still applied.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return
    server_addr = stream.get_extra_info("server_addr")
    if not isinstance(server_addr, tuple) or not server_addr:
        return
    try:
        peer = ipaddress.ip_address(server_addr[0])
    except ValueError:
        return
    if not _is_public(peer):
        raise AddressRefused(f"connected to a non-public address: {host_name(url)} ({peer})")
