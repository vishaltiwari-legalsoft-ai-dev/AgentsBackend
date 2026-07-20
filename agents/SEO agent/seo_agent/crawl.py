"""Fetch and extract ranking pages into PageDoc structures.

The HTTP fetch is injectable so tests replay saved HTML. Per-page failures are
collected (not raised) — the analyze orchestrator decides whether enough pages
survived (min_pages) to build an honest benchmark.
"""

from __future__ import annotations

from typing import Callable

import httpx
from bs4 import BeautifulSoup

from seo_agent.schemas import PageDoc, SerpEntry

_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "iframe")
_UA = "Mozilla/5.0 (compatible; AgentOS-SEO/1.0)"
_FAQ_HEADING_TOKENS = ("faq", "frequently asked", "questions")


def _default_fetcher(url: str) -> str:
    response = httpx.get(
        url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True
    )
    response.raise_for_status()
    return response.text


def extract(url: str, rank: int, html: str) -> PageDoc:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    headings = [
        h.get_text(" ", strip=True)
        for h in soup.find_all(["h1", "h2", "h3"])
        if h.get_text(strip=True)
    ]

    # FAQ text: paragraphs under an FAQ-ish heading until the next heading.
    faq_texts: list[str] = []
    for h in soup.find_all(["h2", "h3"]):
        if any(tok in h.get_text().lower() for tok in _FAQ_HEADING_TOKENS):
            for sib in h.find_next_siblings():
                if sib.name in ("h1", "h2", "h3"):
                    break
                text = sib.get_text(" ", strip=True)
                if text:
                    faq_texts.append(text)

    body_text = " ".join(
        (soup.body or soup).get_text(" ", strip=True).split()
    )
    return PageDoc(
        url=url, rank=rank, title=title, body_text=body_text,
        headings=headings, word_count=len(body_text.split()),
        faq_texts=faq_texts,
    )


def crawl_pages(
    entries: list[SerpEntry],
    fetcher: Callable[[str], str] | None = None,
) -> tuple[list[PageDoc], list[dict]]:
    fetch = fetcher or _default_fetcher
    pages: list[PageDoc] = []
    failures: list[dict] = []
    for entry in entries:
        try:
            html = fetch(entry.url)
            pages.append(extract(entry.url, entry.position, html))
        except Exception as exc:
            failures.append({"url": entry.url, "reason": f"fetch-failed: {exc}"})
    return pages, failures
