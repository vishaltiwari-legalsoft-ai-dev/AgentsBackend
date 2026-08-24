"""The server-side fetchers must not become a proxy into our own network.

``fetch_page`` / ``fetch_text`` / ``fetch_sitemap`` take a URL from a brand
config, a sitemap or a SERP result, and hand the body, the status and the final
URL back to the caller. The old check was ``url.startswith(("http://","https://"))``
and nothing else, so ``http://169.254.169.254/`` was a valid request and a 302
was a way around any check made before it.

No DNS here, let alone a socket: every address is a literal, and the one client
that answers is a fake.
"""
from __future__ import annotations

import httpx
import pytest

from seo_geo_agent import sources


class _FakeResponse:
    def __init__(self, status_code=200, text="ok", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = httpx.URL(url)


class _RecordingClient:
    """Answers whatever it is asked; records every URL it was asked for."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked: list[str] = []

    def get(self, url):
        self.asked.append(url)
        return self.replies.pop(0) if self.replies else _FakeResponse(url=url)

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# The address check
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",            # loopback
        "http://localhost:6379/",            # loopback by name
        "http://10.0.0.5:6379/",             # private
        "http://192.168.1.1/",               # private
        "http://169.254.169.254/latest/",    # link-local: cloud metadata
        "http://[::1]/",                     # loopback, v6
        "http://0.0.0.0/",                   # unspecified
        "http://224.0.0.1/",                 # multicast
    ],
)
def test_internal_addresses_are_refused(url):
    with pytest.raises(httpx.HTTPError):
        sources._assert_public_address(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/f"])
def test_non_web_schemes_are_refused(url):
    with pytest.raises(httpx.HTTPError):
        sources._assert_public_address(url)


def test_a_public_address_is_allowed():
    sources._assert_public_address("http://93.184.216.34/index.html")  # no raise


def test_the_refusal_is_an_httpx_error_so_callers_degrade_as_before():
    """Callers already catch httpx.HTTPError and report "unreachable"; a
    blocked host must land there rather than become a new exception type."""
    with pytest.raises(httpx.HTTPError):
        sources._assert_public_address("http://169.254.169.254/")


# --------------------------------------------------------------------------- #
# Every redirect hop, not just the first
# --------------------------------------------------------------------------- #

def test_a_redirect_into_the_private_range_is_refused():
    """The whole reason redirects are followed by hand: httpx would only ever
    have checked the address we asked for."""
    cli = _RecordingClient(
        _FakeResponse(302, headers={"location": "http://169.254.169.254/latest/"},
                      url="http://93.184.216.34/")
    )
    with pytest.raises(httpx.HTTPError):
        sources._safe_get(cli, "http://93.184.216.34/")
    assert cli.asked == ["http://93.184.216.34/"]     # the second hop never went


def test_a_public_redirect_is_followed():
    cli = _RecordingClient(
        _FakeResponse(301, headers={"location": "http://93.184.216.35/moved"},
                      url="http://93.184.216.34/"),
        _FakeResponse(200, text="arrived", url="http://93.184.216.35/moved"),
    )
    resp = sources._safe_get(cli, "http://93.184.216.34/")
    assert resp.text == "arrived"
    assert cli.asked == ["http://93.184.216.34/", "http://93.184.216.35/moved"]


def test_a_redirect_loop_stops_instead_of_spinning():
    cli = _RecordingClient(*[
        _FakeResponse(302, headers={"location": "http://93.184.216.34/again"},
                      url="http://93.184.216.34/")
        for _ in range(sources.MAX_REDIRECT_HOPS + 2)
    ])
    with pytest.raises(httpx.HTTPError):
        sources._safe_get(cli, "http://93.184.216.34/")
    assert len(cli.asked) == sources.MAX_REDIRECT_HOPS + 1


# --------------------------------------------------------------------------- #
# The fetchers are actually wired to it
# --------------------------------------------------------------------------- #

def test_fetch_text_reports_a_blocked_host_as_unreachable(monkeypatch):
    """Same shape as a dead host: status 0, empty text — not a raise."""
    monkeypatch.setattr(sources.state, "use_cloud", lambda: True)
    out = sources.fetch_text("http://169.254.169.254/latest/", client=_RecordingClient())
    assert out == {"status": 0, "text": "", "final_url": "http://169.254.169.254/latest/"}


def test_fetch_page_reports_a_blocked_host_as_unreachable(monkeypatch):
    monkeypatch.setattr(sources.state, "use_cloud", lambda: True)
    facts = sources.fetch_page("http://127.0.0.1:8080/", client=_RecordingClient())
    assert facts.status == 0


def test_fetch_sitemap_refuses_a_private_child_sitemap(monkeypatch):
    """A hostile sitemap index names the next URL; each one is re-checked."""
    monkeypatch.setattr(sources.state, "use_cloud", lambda: True)
    cli = _RecordingClient(
        _FakeResponse(200, text="<loc>http://10.0.0.5/secret.xml</loc>",
                      url="https://example.com/sitemap.xml"),
    )
    assert sources.fetch_sitemap("93.184.216.34", client=cli) == []
    assert cli.asked == ["https://93.184.216.34/sitemap.xml"]


def test_the_fetch_clients_never_auto_follow_redirects():
    """Guard on the source: follow_redirects=True on any of these three would
    silently return the hop-by-hop check to httpx, which does not do it."""
    source = open(sources.__file__, encoding="utf-8").read()
    tail = source.split("def fetch_page")[1]
    assert "follow_redirects=True" not in tail
    assert tail.count("follow_redirects=False") == 3
