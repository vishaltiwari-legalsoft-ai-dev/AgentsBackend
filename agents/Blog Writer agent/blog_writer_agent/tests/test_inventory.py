"""Inventory scan: sitemap → the brand's published blog list, honestly capped."""
from __future__ import annotations

from dataclasses import dataclass

from blog_writer_agent import inventory, state

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com"}


@dataclass
class _Page:
    url: str
    title: str = ""
    status: int = 200


def _fetch(url, client=None):
    return _Page(url=url, title=f"Title of {url.rsplit('/', 2)[-2]}")


def test_scan_keeps_blog_urls_and_drops_the_rest():
    urls = [
        "https://legalsoft.com/",
        "https://legalsoft.com/pricing",
        "https://legalsoft.com/blog/hiring-a-virtual-assistant/",
        "https://legalsoft.com/insights/intake-checklist/",
        "https://legalsoft.com/2026/05/answering-service-guide/",
    ]
    result = inventory.scan(BRAND, sitemap=lambda d, client=None: urls, fetch=_fetch)
    kept = [p["url"] for p in result["posts"]]
    assert "https://legalsoft.com/blog/hiring-a-virtual-assistant/" in kept
    assert "https://legalsoft.com/insights/intake-checklist/" in kept
    assert "https://legalsoft.com/2026/05/answering-service-guide/" in kept
    assert "https://legalsoft.com/pricing" not in kept
    assert result["counts"] == {"sitemap_urls": 5, "blog_urls": 3, "titled": 3}


def test_scan_caps_titles_with_an_honest_note():
    urls = [f"https://legalsoft.com/blog/post-{i}/" for i in range(inventory.TITLE_CAP + 20)]
    result = inventory.scan(BRAND, sitemap=lambda d, client=None: urls, fetch=_fetch)
    assert len(result["posts"]) == inventory.TITLE_CAP
    assert result["counts"]["blog_urls"] == inventory.TITLE_CAP + 20
    assert any(str(inventory.TITLE_CAP) in n for n in result["notes"])


def test_scan_survives_a_fetch_error_with_path_title():
    def flaky(url, client=None):
        if "broken" in url:
            raise OSError("boom")
        return _fetch(url)

    urls = [
        "https://legalsoft.com/blog/good-post/",
        "https://legalsoft.com/blog/broken-post/",
    ]
    result = inventory.scan(BRAND, sitemap=lambda d, client=None: urls, fetch=flaky)
    by_url = {p["url"]: p["title"] for p in result["posts"]}
    assert by_url["https://legalsoft.com/blog/broken-post/"] == "Broken Post"
    assert any("broken-post" in n or "1 page" in n for n in result["notes"])


def test_scan_persists_and_latest_reads_back():
    urls = ["https://legalsoft.com/blog/one/"]
    result = inventory.scan(BRAND, sitemap=lambda d, client=None: urls, fetch=_fetch)
    assert state.load("inventory-legalsoft") == result
    assert inventory.latest("legalsoft") == result
    assert inventory.latest("unknown-brand") is None
