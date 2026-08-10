"""Layer 1 — SERP acquisition. The corpus IS the answer sheet; get it right.

Providers sit behind one interface: ``SerperProvider`` (live Google SERP via
Serper, same key path a2/a9 already use) and ``FixtureProvider`` (frozen SERP
+ HTML for offline dev/tests). Page fetching is async httpx with bounded
concurrency, per-domain politeness delays, retries with backoff+jitter, and
robots.txt respect. Page-type heuristics keep videos/products/forums from
poisoning an article profile; forums may still contribute vocabulary.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Protocol
from urllib import robotparser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from seo_geo_agent import sources, state

from .opt_config import SerpCfg

# Browser-presenting UA — many winners 403 unknown agents; robots.txt is still
# checked and per-domain delays still apply, which is where the compliance lives.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerpResult(BaseModel):
    rank: int
    url: str
    title: str = ""
    snippet: str = ""
    page_type: str = "article"     # article | forum | video | product
    excluded: str = ""             # non-empty reason -> not fetched/profiled


class SerpData(BaseModel):
    keyword: str
    locale: str
    provider: str
    results: list[SerpResult]
    paa: list[str] = []
    related: list[str] = []
    aio_present: bool = False


class SerpProvider(Protocol):
    name: str

    def search(self, keyword: str, locale: str, n: int) -> SerpData: ...
    def page_html(self, url: str) -> str | None: ...   # fixtures serve pages; live returns None


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def classify_page_type(url: str, cfg: SerpCfg) -> str:
    domain, path = _domain(url), urlparse(url).path.lower()
    if any(domain == v or domain.endswith("." + v) for v in cfg.video_domains):
        return "video"
    if any(domain == f or domain.endswith("." + f) for f in cfg.forum_domains) or "forum" in domain:
        return "forum"
    if any(domain.startswith(p) or p in domain for p in cfg.product_domains):
        return "product"
    if any(tok in path for tok in cfg.product_path_tokens):
        return "product"
    return "article"


class SerperProvider:
    name = "serper"

    def search(self, keyword: str, locale: str, n: int) -> SerpData:
        key = sources._serper_key()  # single source of key truth (env → app config)
        if not key or not state.use_cloud():
            raise sources.CredentialMissing("SEO_SERPER_API_KEY not set")
        lang, _, country = locale.partition("-")
        resp = httpx.post(
            SERPER_ENDPOINT,
            json={"q": keyword, "num": n, "hl": lang or "en", "gl": (country or "us").lower()},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            SerpResult(
                rank=int(r.get("position", i + 1)),
                url=r.get("link", ""),
                title=r.get("title", ""),
                snippet=r.get("snippet", ""),
            )
            for i, r in enumerate(data.get("organic", [])[:n])
        ]
        return SerpData(
            keyword=keyword, locale=locale, provider=self.name, results=results,
            paa=[q.get("question", "") for q in data.get("peopleAlsoAsk", []) if q.get("question")],
            related=[r.get("query", "") for r in data.get("relatedSearches", []) if r.get("query")],
            aio_present=bool((data.get("aiOverview") or {}).get("text")),
        )

    def page_html(self, url: str) -> str | None:
        return None


class FixtureProvider:
    """Frozen SERP + pages for offline dev/tests. ``pages`` maps url -> html."""

    name = "fixture"
    offline = True   # never fall back to live fetching for missing pages

    def __init__(self, serp: SerpData, pages: dict[str, str]):
        self._serp = serp
        self._pages = pages

    def search(self, keyword: str, locale: str, n: int) -> SerpData:
        return self._serp.model_copy(update={"results": self._serp.results[:n]})

    def page_html(self, url: str) -> str | None:
        return self._pages.get(url)


def prepare_results(serp: SerpData, cfg: SerpCfg, own_domain: str = "") -> SerpData:
    """Tag page types and mark exclusions: videos (no article HTML) and the
    user's own page (a draft must never be scored against itself)."""
    for r in serp.results:
        r.page_type = classify_page_type(r.url, cfg)
        if own_domain and _domain(r.url) == own_domain.lower().removeprefix("www."):
            r.excluded = "own domain"
        elif r.page_type == "video":
            r.excluded = "video result"
    return serp


# ------------------------------------------------------------- page fetching

async def _allowed_by_robots(
    client: httpx.AsyncClient, url: str, cache: dict[str, robotparser.RobotFileParser]
) -> bool:
    domain = _domain(url)
    if domain not in cache:
        parser = robotparser.RobotFileParser()
        try:
            resp = await client.get(f"https://{domain}/robots.txt", timeout=8)
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except Exception:  # noqa: BLE001 — unreachable robots = assume allowed
            parser.parse([])
        cache[domain] = parser
    return cache[domain].can_fetch(USER_AGENT, url)


async def fetch_pages(urls: list[str], cfg: SerpCfg) -> dict[str, str]:
    """url -> html for every reachable, robots-allowed page. Failures are
    simply absent from the result — the pipeline reports them, never fakes."""
    sem = asyncio.Semaphore(cfg.fetch_concurrency)
    last_hit: dict[str, float] = {}
    robots_cache: dict[str, robotparser.RobotFileParser] = {}
    out: dict[str, str] = {}

    async with httpx.AsyncClient(
        headers=FETCH_HEADERS, follow_redirects=True,
        timeout=cfg.fetch_timeout_s,
    ) as client:

        async def grab(url: str) -> None:
            async with sem:
                if not await _allowed_by_robots(client, url, robots_cache):
                    return
                domain = _domain(url)
                wait = last_hit.get(domain, 0) + cfg.per_domain_delay_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                last_hit[domain] = time.monotonic()
                for attempt in range(cfg.fetch_retries + 1):
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200 and "html" in resp.headers.get("content-type", "html"):
                            out[url] = resp.text
                        return
                    except Exception:  # noqa: BLE001 — retry then give up honestly
                        if attempt == cfg.fetch_retries:
                            return
                        await asyncio.sleep((2 ** attempt) + random.random())

        await asyncio.gather(*(grab(u) for u in urls))
    return out


def gather_pages(provider: SerpProvider, results: list[SerpResult], cfg: SerpCfg) -> dict[str, str]:
    """Fixture pages when the provider carries them; live fetch otherwise."""
    pages: dict[str, str] = {}
    missing: list[str] = []
    for r in results:
        if r.excluded:
            continue
        html = provider.page_html(r.url)
        if html is not None:
            pages[r.url] = html
        else:
            missing.append(r.url)
    if missing and not getattr(provider, "offline", False):
        pages.update(asyncio.run(fetch_pages(missing, cfg)))
    return pages
