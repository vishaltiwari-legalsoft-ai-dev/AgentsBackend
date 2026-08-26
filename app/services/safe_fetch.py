"""Outbound HTTP that cannot be pointed at our own infrastructure.

Cloud Run reaches a metadata server on 169.254.169.254 that will hand out an
access token for the service account the container runs as, plus whatever else
sits inside the VPC. So any fetch whose URL is influenced by stored or
user-supplied data is an SSRF primitive unless the address is checked first.

This module is the shared home for that check. It exists because the check was
written once, correctly, inside ``seo_geo_agent/sources.py`` — and the Marketing
Research fetcher, written later in a different agent folder, had no guard at
all. One agent knowing a rule the next one has never heard of is exactly how
this workspace grew two different answers to the same question, so the rule
lives here now and agents import it.

Two things make this more than a blocklist:

* **The address is checked, not the hostname.** ``getaddrinfo`` is resolved and
  every returned address is tested, so ``localtest.me`` and friends — public
  names that resolve to private space — are refused like any other.
* **Every redirect hop is re-checked.** A guard applied once before the request
  is walked straight past by a 302 to the metadata server, which is why
  :func:`safe_get` follows redirects itself with ``follow_redirects=False`` on
  the client.

Residual risk, stated honestly: this resolves the name, then hands the URL to
httpx, which resolves it again. A DNS record with a very low TTL can differ
between the two lookups (classic rebinding). Closing that means connecting to
the validated address with the ``Host`` header preserved, which is a larger
change than this module makes today.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

#: Status codes that carry a ``Location`` we would otherwise follow blindly.
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: Hops allowed before we call it a loop. Four is what the SEO fetcher has used
#: in production since it was written.
MAX_REDIRECT_HOPS = 4


def assert_public_address(url: str) -> None:
    """Raise unless *url* is http(s) and every resolved address is public.

    Raises ``httpx.UnsupportedProtocol`` for a non-http(s) scheme (``file://``,
    ``gopher://`` and the rest) and ``httpx.ConnectError`` for a name that will
    not resolve or that resolves anywhere we should not be reaching. Both are
    ``httpx.HTTPError`` subclasses, so callers that already handle fetch failure
    handle a refusal too — a blocked SSRF attempt looks like an unreachable
    page, which is what it is.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise httpx.UnsupportedProtocol(f"{url[:200]!r} is not an http(s) address")

    host = parsed.hostname
    if not host:
        raise httpx.ConnectError(f"{url[:200]!r} has no host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise httpx.ConnectError(f"could not resolve {host}") from exc

    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # drop any IPv6 scope id
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError as exc:  # pragma: no cover - getaddrinfo shouldn't
            raise httpx.ConnectError(f"could not resolve {host}") from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise httpx.ConnectError(
                f"{host} resolves to a non-public address ({addr}) — refusing"
            )


def safe_get(
    client: httpx.Client, url: str, *, max_hops: int = MAX_REDIRECT_HOPS
) -> httpx.Response:
    """``client.get(url)`` with :func:`assert_public_address` on every hop.

    The client passed in **must** have ``follow_redirects=False``; following
    them here is the entire point, because httpx would only ever check the
    address we asked for and not the one we were sent to.
    """
    target = url
    for _ in range(max_hops + 1):
        assert_public_address(target)
        resp = client.get(target)
        if resp.status_code not in REDIRECT_CODES:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        target = str(resp.url.join(location))
    raise httpx.TooManyRedirects(f"more than {max_hops} redirects from {url[:200]}")
