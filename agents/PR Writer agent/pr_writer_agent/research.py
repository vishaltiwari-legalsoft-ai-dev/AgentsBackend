"""Web research for a PR draft: search + fetch, no model.

Reuses Blog Writer's page gathering (``blog_writer_agent.research.gather_pages``)
and the SEO agent's Serper and safe page fetch. The one model call that turns
pages into evidence is ``steps.run_research_extract`` (all pages in one call),
not Blog Writer's per-page extraction, which would cost one call per page.

Failure is honest:
- no Serper key (or offline)         -> ``CredentialMissing``  (router: 424)
- Serper out of credit / unreachable -> ``SearchUnavailable``  (router: 424)
- a page that will not load is skipped; fewer than ``PARTIAL_BELOW`` pages
  is reported as ``partial``, never padded.
"""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from blog_writer_agent.research import gather_pages
from seo_geo_agent import sources
from seo_geo_agent.sources import CredentialMissing

from pr_writer_agent.steps import MAX_PAGES

PARTIAL_BELOW = 3
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'.-]*")


def default_search() -> Callable[[str], dict]:
    if not sources.serper_available():
        raise CredentialMissing("SEO_SERPER_API_KEY not set — PR research needs live search results")
    return sources.serper_search


def _domain(url_or_domain: str) -> str:
    raw = (url_or_domain or "").strip().lower()
    host = urlparse(raw).netloc if "://" in raw else raw.split("/")[0]
    return host.removeprefix("www.")


def _topic(news: str, limit: int = 12) -> str:
    return " ".join(_WORDS.findall(news or "")[:limit])


def queries(news: str, brand_name: str, domain: str) -> list[dict[str, str]]:
    """Three searches: the company's own pages about it, the news in the
    wider press, and industry context. Built from the brief only."""
    topic = _topic(news)
    out = []
    if domain:
        out.append({"angle": "newsroom", "q": f"site:{domain} {topic}"})
    out.append({"angle": "news", "q": f"{brand_name} {topic}".strip()})
    out.append({"angle": "context", "q": f"{topic} statistics industry report"})
    return out


def gather(news: str, brand: dict[str, Any], *, search=None, fetch=None) -> dict[str, Any]:
    """``{"pages": [{url, title, text}], "queries": [...], "partial": bool}``."""
    search = search or default_search()
    fetch = fetch or sources.fetch_page
    domain = _domain(brand.get("domain") or "")
    records, pages = gather_pages(queries(news, brand.get("name") or "", domain), MAX_PAGES,
                                  search=search, fetch=fetch)
    kept = []
    for page in pages:
        text = str(page.get("text") or "").strip()
        if not text:
            continue
        kept.append({"url": page.get("read_url") or page.get("url") or "",
                     "title": str(page.get("title") or ""), "text": text})
    return {"pages": kept, "queries": records, "partial": len(kept) < PARTIAL_BELOW}


def source_kind(url: str, brand_domain: str) -> str:
    dom, own = _domain(url), _domain(brand_domain)
    return "newsroom" if own and (dom == own or dom.endswith("." + own)) else "web"
