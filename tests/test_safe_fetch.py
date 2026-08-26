"""The shared SSRF guard, and the MR fetcher that now sits behind it.

The guard's whole job is to refuse an address, so the tests that matter are the
refusals. They are written against ``getaddrinfo`` rather than a live network:
the question is "what does this do when a name resolves to 169.254.169.254",
and the honest way to ask it is to make a name resolve there.
"""
from __future__ import annotations

import socket

import httpx
import pytest

from app.services import safe_fetch


def _resolves_to(monkeypatch, address: str) -> None:
    """Force every hostname lookup to return *address*."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # the cloud metadata server — the one that matters
        "127.0.0.1",        # loopback
        "10.0.0.5",         # RFC1918
        "192.168.1.10",     # RFC1918
        "172.16.4.4",       # RFC1918
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "::1",              # IPv6 loopback
        "fe80::1",          # IPv6 link-local
    ],
)
def test_non_public_addresses_are_refused(monkeypatch, address: str) -> None:
    _resolves_to(monkeypatch, address)
    with pytest.raises(httpx.ConnectError) as exc:
        safe_fetch.assert_public_address("https://totally-innocent.example/x")
    assert "non-public address" in str(exc.value)


def test_a_public_address_is_allowed(monkeypatch) -> None:
    _resolves_to(monkeypatch, "93.184.216.34")
    safe_fetch.assert_public_address("https://example.com/page")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "ftp://h/f"])
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(httpx.UnsupportedProtocol):
        safe_fetch.assert_public_address(url)


def test_a_name_that_does_not_resolve_is_refused(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", boom)
    with pytest.raises(httpx.ConnectError):
        safe_fetch.assert_public_address("https://nowhere.example/")


def test_a_redirect_into_private_space_is_refused(monkeypatch) -> None:
    """The hop is what this guard exists for.

    A check applied once, before the request, passes a public first URL and is
    then walked straight past by a 302 into the metadata server. ``safe_get``
    re-checks each ``Location``, so the second address is refused even though
    the first was fine.
    """
    resolutions = {"public.example": "93.184.216.34", "evil.example": "169.254.169.254"}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        addr = resolutions[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port))]

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://evil.example/token"})
        return httpx.Response(200, text="secret")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(httpx.ConnectError) as exc:
        safe_fetch.safe_get(client, "https://public.example/start")
    assert "169.254.169.254" in str(exc.value)


def test_a_redirect_loop_is_bounded(monkeypatch) -> None:
    _resolves_to(monkeypatch, "93.184.216.34")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(httpx.TooManyRedirects):
        safe_fetch.safe_get(client, "https://example.com/start")


def test_mr_web_source_refuses_the_metadata_server(monkeypatch) -> None:
    """The regression this guard was added for.

    ``web_source._default_fetcher`` used to call ``httpx.get(url,
    follow_redirects=True)`` with no address check at all. It is reachable only
    from ``competitor_intel``, which nothing wires up today — so this is a latent
    hole rather than a live one, and the test is here to make sure wiring it up
    later does not reopen it.
    """
    from marketing_research_agent.sources import web_source

    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(httpx.ConnectError):
        web_source.fetch("http://metadata.example/computeMetadata/v1/")
