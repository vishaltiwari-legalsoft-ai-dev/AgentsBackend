"""Per-brand blog inventory: sitemap scan → the posts the site already publishes.

Grounds two behaviours downstream: never draft a topic the brand already
covers, and offer internal-link candidates. Results persist per brand under
``inventory-{brand_id}`` so the catalogue loads instantly.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from seo_geo_agent import sources

from blog_writer_agent import state

# URL path fragments that mark editorial content on the brands we manage.
BLOG_PATH_HINTS = ("/blog", "/post", "/article", "/insights", "/news", "/resources", "/guides")
_DATE_PATH = re.compile(r"/20\d{2}/\d{1,2}/")
TITLE_CAP = 150  # pages actually fetched for a real <title>; the rest wait for a rescan


def _is_post(url: str) -> bool:
    path = re.sub(r"^https?://[^/]+", "", url.lower())
    if path in ("", "/"):
        return False
    return path.startswith(BLOG_PATH_HINTS) or any(h + "/" in path for h in BLOG_PATH_HINTS) or bool(
        _DATE_PATH.search(path)
    )


def _title_from_path(url: str) -> str:
    slug = [seg for seg in url.rstrip("/").split("/") if seg][-1]
    return re.sub(r"[-_]+", " ", slug).strip().title()


def scan(brand: dict, *, sitemap=None, fetch=None) -> dict:
    """Live scan; ``sitemap``/``fetch`` are injectable for offline tests."""
    sitemap = sitemap or sources.fetch_sitemap
    fetch = fetch or sources.fetch_page
    urls = sitemap(brand["domain"])
    blog_urls = [u for u in urls if _is_post(u)]

    notes: list[str] = []
    posts: list[dict] = []
    failed = 0
    for url in blog_urls[:TITLE_CAP]:
        try:
            page = fetch(url)
            title = (page.title or "").strip() or _title_from_path(url)
        except Exception:  # noqa: BLE001 — one dead page must not kill the scan
            title = _title_from_path(url)
            failed += 1
        posts.append({"url": url, "title": title})
    if failed:
        notes.append(f"{failed} page(s) would not load — titles derived from their URL")
    if len(blog_urls) > TITLE_CAP:
        notes.append(f"titled first {TITLE_CAP} of {len(blog_urls)} blog URLs")

    result = {
        "domain": brand["domain"],
        "scanned": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
        "counts": {"sitemap_urls": len(urls), "blog_urls": len(blog_urls), "titled": len(posts)},
        "notes": notes,
    }
    state.save(f"inventory-{brand['id']}", result)
    return state.load(f"inventory-{brand['id']}") or result


def latest(brand_id: str) -> dict | None:
    return state.load(f"inventory-{brand_id}")
