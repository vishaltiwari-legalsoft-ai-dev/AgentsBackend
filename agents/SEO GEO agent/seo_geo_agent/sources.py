"""External data adapters: Search Console, Serper.dev, page fetcher, LLM.

Every adapter degrades instead of failing the run: missing credentials (or
offline mode) raise ``CredentialMissing`` and the caller records a
plain-language degradation note or falls back to a heuristic.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser

import httpx

from . import state

SERPER_ENDPOINT = "https://google.serper.dev/search"
FETCH_UA = "Mozilla/5.0 (compatible; AgentOS-SEO/1.0)"


class CredentialMissing(Exception):
    """A data source has no usable credentials — caller degrades, never crashes."""


@dataclass
class QueryStat:
    query: str
    page: str
    clicks: int
    impressions: int
    ctr: float
    position: float


def gsc_available() -> bool:
    return state.use_cloud()


def _gsc_service():
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from googleapiclient.discovery import build

        return build("searchconsole", "v1", cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"Search Console auth unavailable: {exc}") from exc


def gsc_fetch(prop: str, start: date, end: date, service=None) -> list[QueryStat]:
    """Query+page rows for one Search Console property over [start, end]."""
    svc = service or _gsc_service()
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query", "page"],
        "rowLimit": 5000,
    }
    try:
        data = svc.searchanalytics().query(siteUrl=prop, body=body).execute()
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — 403 = property not shared with our SA
        raise CredentialMissing(f"Search Console rejected {prop}: {exc}") from exc
    return [
        QueryStat(
            query=r["keys"][0],
            page=r["keys"][1],
            clicks=int(r.get("clicks", 0)),
            impressions=int(r.get("impressions", 0)),
            ctr=float(r.get("ctr", 0.0)),
            position=float(r.get("position", 0.0)),
        )
        for r in data.get("rows", [])
    ]


def serper_available() -> bool:
    return bool(os.environ.get("SEO_SERPER_API_KEY")) and state.use_cloud()


def serper_search(query: str, client: httpx.Client | None = None) -> dict:
    """One Google SERP via Serper: organic top-10, related searches, PAA, AIO flag."""
    if not serper_available():
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
    key = os.environ["SEO_SERPER_API_KEY"]
    own = client is None
    cli = client or httpx.Client(timeout=20)
    try:
        resp = cli.post(
            SERPER_ENDPOINT,
            json={"q": query, "num": 10},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own:
            cli.close()
    return {
        "organic": [
            {"link": r.get("link", ""), "title": r.get("title", ""), "position": r.get("position", i + 1)}
            for i, r in enumerate(data.get("organic", [])[:10])
        ],
        "related": [r.get("query", "") for r in data.get("relatedSearches", []) if r.get("query")],
        "paa": [q.get("question", "") for q in data.get("peopleAlsoAsk", []) if q.get("question")],
        "aio_present": bool((data.get("aiOverview") or {}).get("text")),
    }


def domain_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


# ------------------------------- LLM adapter -------------------------------

def llm_text(system: str, prompt: str) -> str:
    """One fast-model completion returned as plain text. Raises ``CredentialMissing``
    when offline or the provider fails, so callers surface an honest message."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=0.3, fast=True).invoke(
            [("system", system), ("user", prompt)]
        ).content
        return str(raw).strip()
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"LLM unavailable: {exc}") from exc


def llm_json(system: str, prompt: str):
    """One fast-model completion, parsed as JSON. Raises ``CredentialMissing``
    on any failure so callers fall back to their deterministic heuristic."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=0.2, fast=True).invoke(
            [("system", system), ("user", prompt)]
        ).content
        text = str(raw).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001 — bad JSON, no key, provider down: all degrade
        raise CredentialMissing(f"LLM unavailable: {exc}") from exc


# ------------------------------ page fetcher ------------------------------

@dataclass
class PageFacts:
    url: str
    status: int = 0
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    h3: list[str] = field(default_factory=list)
    schema_types: list[str] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)
    images_no_alt: int = 0
    word_count: int = 0
    text: str = ""  # body text, capped — feeds the site-brain content analysis

    @property
    def questions(self) -> list[str]:
        return [h for h in self.h2 + self.h3 if h.strip().endswith("?")]


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.facts_title: list[str] = []
        self.meta_description = ""
        self.canonical = ""
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.schema_raw: list[str] = []
        self.links: list[str] = []
        self.images_no_alt = 0
        self.words = 0
        self.text_parts: list[str] = []
        self._text_len = 0
        self._stack: list[str] = []
        self._in_schema = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("title", "h1", "h2", "h3"):
            self._stack.append(tag)
        elif tag in ("script", "style"):
            self._in_schema = tag == "script" and a.get("type", "") == "application/ld+json"
            if self._in_schema:
                self.schema_raw.append("")
            self._stack.append("skip")
        elif tag == "meta" and a.get("name", "").lower() == "description":
            self.meta_description = a.get("content", "")
        elif tag == "link" and a.get("rel", "") == "canonical":
            self.canonical = a.get("href", "")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img" and not (a.get("alt") or "").strip():
            self.images_no_alt += 1

    def handle_endtag(self, tag):
        if self._stack and (tag in ("title", "h1", "h2", "h3", "script", "style")):
            self._stack.pop()
            self._in_schema = False

    def handle_data(self, data):
        top = self._stack[-1] if self._stack else ""
        text = " ".join(data.split())
        if not text:
            return
        if top == "title":
            self.facts_title.append(text)
        elif top in ("h1", "h2", "h3"):
            self.headings[top].append(text)
        elif top == "skip":
            if self._in_schema:
                self.schema_raw[-1] += data
        else:
            self.words += len(text.split())
            if self._text_len < 6000:
                self.text_parts.append(text)
                self._text_len += len(text) + 1


def fetch_page(url: str, client: httpx.Client | None = None) -> PageFacts:
    """Fetch one page and extract the on-page facts audits and briefs need."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    own = client is None
    cli = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": FETCH_UA})
    facts = PageFacts(url=url)
    try:
        resp = cli.get(url)
        facts.status = resp.status_code
        if resp.status_code != 200 or len(resp.content) > 2_000_000:
            return facts
        parser = _PageParser()
        try:
            parser.feed(resp.text)
        except Exception:  # noqa: BLE001 — real-world HTML; keep what parsed
            pass
        facts.title = " ".join(parser.facts_title)[:300]
        facts.meta_description = parser.meta_description[:500]
        facts.canonical = parser.canonical
        facts.h1, facts.h2, facts.h3 = parser.headings["h1"], parser.headings["h2"], parser.headings["h3"]
        facts.internal_links = parser.links[:400]
        facts.images_no_alt = parser.images_no_alt
        facts.word_count = parser.words
        facts.text = " ".join(parser.text_parts)[:6000]
        for raw in parser.schema_raw:
            try:
                node = json.loads(raw)
                nodes = node if isinstance(node, list) else node.get("@graph", [node])
                for n in nodes:
                    t = n.get("@type") if isinstance(n, dict) else None
                    for typ in t if isinstance(t, list) else [t]:
                        if typ:
                            facts.schema_types.append(str(typ))
            except Exception:  # noqa: BLE001
                continue
        return facts
    except httpx.HTTPError:
        return facts  # status stays 0 -> "unreachable"
    finally:
        if own:
            cli.close()


def fetch_text(url: str, client: httpx.Client | None = None) -> dict:
    """Raw text fetch (robots.txt, redirect checks): {status, text, final_url}."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    own = client is None
    cli = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": FETCH_UA})
    try:
        resp = cli.get(url)
        return {"status": resp.status_code, "text": resp.text[:20_000], "final_url": str(resp.url)}
    except httpx.HTTPError:
        return {"status": 0, "text": "", "final_url": url}
    finally:
        if own:
            cli.close()


def fetch_sitemap(domain: str, client: httpx.Client | None = None, cap: int = 500) -> list[str]:
    """URL list from /sitemap.xml (one level of sitemap-index recursion)."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    own = client is None
    cli = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": FETCH_UA})

    def locs(url: str) -> list[str]:
        try:
            resp = cli.get(url)
            if resp.status_code != 200:
                return []
            return re.findall(r"<loc>\s*(.*?)\s*</loc>", resp.text)[:cap]
        except httpx.HTTPError:
            return []

    try:
        found = locs(f"https://{domain}/sitemap.xml")
        if found and all(".xml" in u for u in found[:5]):  # sitemap index
            urls: list[str] = []
            for child in found[:10]:
                urls.extend(locs(child))
                if len(urls) >= cap:
                    break
            return urls[:cap]
        return found[:cap]
    finally:
        if own:
            cli.close()
