"""Exclude non-comparable SERP results so the benchmark is like-for-like.

Heuristic-only in slice 1 (domain blocklist + URL patterns) — no LLM call.
Excluded entries keep their reason so a benchmark is auditable.
"""

from __future__ import annotations

from urllib.parse import urlparse

from seo_agent.schemas import SerpEntry

# Forums, video, Q&A, social, directories/aggregators, job boards.
BLOCKLIST_DOMAINS: tuple[str, ...] = (
    "reddit.com", "youtube.com", "quora.com", "facebook.com", "x.com",
    "twitter.com", "linkedin.com", "pinterest.com", "tiktok.com",
    "yelp.com", "avvo.com", "justia.com", "findlaw.com/lawyers",
    "clutch.co", "g2.com", "capterra.com", "upwork.com", "fiverr.com",
    "indeed.com", "ziprecruiter.com", "glassdoor.com", "amazon.com",
)


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _block_reason(entry: SerpEntry) -> str | None:
    domain = _domain(entry.url)
    full = f"{domain}{urlparse(entry.url).path.lower()}"
    for blocked in BLOCKLIST_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked) or full.startswith(blocked):
            return f"blocklist:{blocked}"
    return None


def filter_entries(
    entries: list[SerpEntry], top_n: int
) -> tuple[list[SerpEntry], list[dict]]:
    kept: list[SerpEntry] = []
    excluded: list[dict] = []
    for entry in sorted(entries, key=lambda e: e.position):
        reason = _block_reason(entry)
        if reason:
            excluded.append({"url": entry.url, "reason": reason})
        elif len(kept) >= top_n:
            excluded.append({"url": entry.url, "reason": "beyond-top-n"})
        else:
            kept.append(entry)
    return kept, excluded
