"""External data adapters: Search Console, Serper.dev, page fetcher, LLM.

Every adapter degrades instead of failing the run: missing credentials (or
offline mode) raise ``CredentialMissing`` and the caller records a
plain-language degradation note or falls back to a heuristic.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from . import state

SERPER_ENDPOINT = "https://google.serper.dev/search"
FETCH_UA = "Mozilla/5.0 (compatible; AgentOS-SEO/1.0)"

# --- Deadlines -------------------------------------------------------------
# These run in sync handlers, i.e. on one of anyio's 40 worker threads, so a
# stalled report costs the service a slot rather than just this run.
# googleapiclient applies an implicit 60s socket timeout of its own; 45s is
# stated deliberately because a GSC query pulls up to 5000 rows and a GA
# batchRunReports covers three reports — slow is normal, forever is not.
GOOGLE_API_TIMEOUT_SECONDS = 45

# Read-only scopes, named explicitly. Passing our own transport (the only way
# to set a timeout) means googleapiclient no longer derives scopes from the
# discovery document — which is an improvement: it was requesting the write
# scopes (`analytics.edit`, `webmasters`) for a module that only ever reads.
GA_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def _timed_google_client(api: str, version: str, scope: str):
    """A Google API client whose transport carries an explicit socket deadline.

    A fresh ``httplib2.Http`` per client is deliberate: httplib2 is not
    thread-safe and these are used from FastAPI worker threads. ``build`` opens
    no socket — the discovery document ships inside google-api-python-client.
    """
    import google.auth
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=[scope])
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=GOOGLE_API_TIMEOUT_SECONDS)
    )
    return build(api, version, http=http, cache_discovery=False)


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


# ------------------------- Google Analytics (GA4) -------------------------

# Metric order is load-bearing: parsing reads metricValues by this position.
GA_TOTAL_METRICS = ["sessions", "totalUsers", "newUsers", "engagementRate",
                    "averageSessionDuration", "screenPageViews"]


def _ga_service(api: str):
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        return _timed_google_client(api, "v1beta", GA_READONLY_SCOPE)
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"Google Analytics auth unavailable: {exc}") from exc


def ga_discover_property(domain: str, service=None) -> dict:
    """Find the GA4 property the service account can see for this brand.

    Match order: brand domain root in the property name, else the single
    visible property. Anything else is ambiguous — the caller degrades and
    the brand can pin ``ga4_property`` explicitly.
    """
    svc = service or _ga_service("analyticsadmin")
    try:
        resp = svc.accountSummaries().list(pageSize=200).execute()
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — 403 = GA not shared with our SA
        raise CredentialMissing(f"Analytics admin rejected: {exc}") from exc
    props = [
        {"property": p.get("property", ""), "name": p.get("displayName", "")}
        for acct in resp.get("accountSummaries", [])
        for p in acct.get("propertySummaries", [])
    ]
    if not props:
        raise CredentialMissing("no GA property is shared with the service account")
    root = domain.split(".")[0].lower()
    for p in props:
        if root and root in p["name"].lower().replace(" ", ""):
            return p
    if len(props) == 1:
        return props[0]
    names = ", ".join(p["name"] for p in props[:5])
    raise CredentialMissing(
        f"{len(props)} GA properties visible ({names}) — none match {domain}"
    )


def _ga_ranges(start: date, end: date, prev_start: date, prev_end: date) -> list[dict]:
    return [
        {"startDate": start.isoformat(), "endDate": end.isoformat(), "name": "current"},
        {"startDate": prev_start.isoformat(), "endDate": prev_end.isoformat(), "name": "previous"},
    ]


def _ga_totals(row) -> dict:
    vals = [v.get("value", "0") for v in row.get("metricValues", [])]
    sessions, users, new_users, engagement, avg_dur, views = (vals + ["0"] * 6)[:6]
    return {
        "sessions": int(float(sessions)),
        "users": int(float(users)),
        "new_users": int(float(new_users)),
        "engagement_rate": float(engagement),
        "avg_session_sec": float(avg_dur),
        "pageviews": int(float(views)),
    }


def ga_fetch_overview(prop: str, start: date, end: date,
                      prev_start: date, prev_end: date, service=None) -> dict:
    """Traffic totals, top pages, channel split and key events for one property."""
    svc = service or _ga_service("analyticsdata")
    ranges = _ga_ranges(start, end, prev_start, prev_end)
    body = {"requests": [
        {"dateRanges": ranges, "metrics": [{"name": m} for m in GA_TOTAL_METRICS]},
        {"dateRanges": ranges[:1], "dimensions": [{"name": "pagePath"}],
         "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}],
         "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
         "limit": 10},
        {"dateRanges": ranges, "dimensions": [{"name": "sessionDefaultChannelGroup"}],
         "metrics": [{"name": "sessions"}]},
    ]}
    try:
        reports = svc.properties().batchRunReports(property=prop, body=body).execute().get("reports", [])
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — 403 = property not shared / wrong scopes
        raise CredentialMissing(f"Google Analytics rejected {prop}: {exc}") from exc
    while len(reports) < 3:
        reports.append({})

    empty = _ga_totals({})
    totals, prev_totals = dict(empty), dict(empty)
    for row in reports[0].get("rows", []):
        which = (row.get("dimensionValues") or [{}])[-1].get("value")
        if which == "current":
            totals = _ga_totals(row)
        elif which == "previous":
            prev_totals = _ga_totals(row)

    top_pages = [
        {
            "path": (row.get("dimensionValues") or [{}])[0].get("value", ""),
            "views": int(float(row["metricValues"][0]["value"])),
            "sessions": int(float(row["metricValues"][1]["value"])),
        }
        for row in reports[1].get("rows", [])
    ]

    by_channel: dict[str, dict] = {}
    for row in reports[2].get("rows", []):
        dims = row.get("dimensionValues") or []
        channel = dims[0].get("value", "") if dims else ""
        which = dims[-1].get("value") if len(dims) > 1 else "current"
        slot = by_channel.setdefault(channel, {"channel": channel, "sessions": 0, "prev_sessions": 0})
        key = "sessions" if which == "current" else "prev_sessions"
        slot[key] = int(float(row["metricValues"][0]["value"]))
    channels = sorted(by_channel.values(), key=lambda c: c["sessions"], reverse=True)

    # Key events ride separately: a site with none configured (or a metric the
    # property rejects) must never sink the traffic overview.
    key_events: list[dict] = []
    try:
        resp = svc.properties().runReport(property=prop, body={
            "dateRanges": ranges[:1], "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "keyEvents"}], "limit": 25,
        }).execute()
        key_events = [
            {"event": (row.get("dimensionValues") or [{}])[0].get("value", ""),
             "count": int(float(row["metricValues"][0]["value"]))}
            for row in resp.get("rows", [])
            if float(row["metricValues"][0]["value"]) > 0
        ]
        key_events.sort(key=lambda e: e["count"], reverse=True)
        key_events = key_events[:10]
    except Exception:  # noqa: BLE001
        key_events = []

    return {"totals": totals, "prev_totals": prev_totals, "top_pages": top_pages,
            "channels": channels, "key_events": key_events}


def ga_fetch_pages(prop: str, start: date, end: date, service=None, limit: int = 50) -> list[dict]:
    """Per-page GA traffic, ordered by views — feeds the page intelligence table."""
    svc = service or _ga_service("analyticsdata")
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}, {"name": "engagementRate"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": limit,
    }
    try:
        resp = svc.properties().runReport(property=prop, body=body).execute()
    except CredentialMissing:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"Google Analytics rejected {prop}: {exc}") from exc
    pages: list[dict] = []
    for r in resp.get("rows", []):
        try:
            pages.append({
                "path": (r.get("dimensionValues") or [{}])[0].get("value", ""),
                "views": int(float(r["metricValues"][0]["value"])),
                "sessions": int(float(r["metricValues"][1]["value"])),
                "engagement_rate": float(r["metricValues"][2]["value"]),
            })
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # one malformed row must not be a fatal (non-CredentialMissing) crash
    return pages


def _gsc_service():
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        return _timed_google_client("searchconsole", "v1", GSC_READONLY_SCOPE)
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


def _serper_key() -> str:
    """Env var first; in cloud mode fall back to the admin-managed app config
    (Firestore ``app_config/global`` → ``seo_serper_api_key``) so prod works
    without a Cloud Run env change. Offline mode never touches Firestore."""
    key = os.environ.get("SEO_SERPER_API_KEY", "")
    if key or not state.use_cloud():
        return key
    try:
        from app.services.firestore_repo import get_app_config

        return str(get_app_config().get("seo_serper_api_key") or "")
    except Exception:  # noqa: BLE001 — config unreachable = no key, honestly
        return ""


def serper_available() -> bool:
    return bool(_serper_key()) and state.use_cloud()


def serper_search(query: str, client: httpx.Client | None = None) -> dict:
    """One Google SERP via Serper: organic top-10, related searches, PAA, AIO flag."""
    key = _serper_key()
    if not key or not state.use_cloud():
        raise CredentialMissing("SEO_SERPER_API_KEY not set")
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

def llm_text(system: str, prompt: str, *, agent_id: str | None = None) -> str:
    """One fast-model completion returned as plain text. Raises ``CredentialMissing``
    when offline or the provider fails, so callers surface an honest message.
    ``agent_id`` routes the creator's per-agent model override (agent → global)."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=0.3, fast=True, agent_id=agent_id).invoke(
            [("system", system), ("user", prompt)]
        ).content
        return str(raw).strip()
    except Exception as exc:  # noqa: BLE001
        raise CredentialMissing(f"LLM unavailable: {exc}") from exc


def llm_json(system: str, prompt: str, *, agent_id: str | None = None):
    """One fast-model completion, parsed as JSON. Raises ``CredentialMissing``
    on any failure so callers fall back to their deterministic heuristic.
    ``agent_id`` routes the creator's per-agent model override (agent → global)."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    try:
        from app.services.openrouter import get_llm

        raw = get_llm(temperature=0.2, fast=True, agent_id=agent_id).invoke(
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


# --- Outbound fetch safety -------------------------------------------------
# These fetchers take a URL from a brand config, a sitemap or a SERP result,
# then hand the body, the status and the final URL back to the caller. Any of
# those can be attacker-influenced. Unrestricted that is
# an arbitrary GET from inside the VPC: 169.254.169.254 for metadata, a Redis
# or admin port for a scan oracle, and the response reflected to whoever asked.
# app/routers/canva.py::_fetch_bytes is the same idea with a fixed allowlist;
# here the whole public web is legitimate, so the check is on the address the
# name resolves to instead.
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECT_HOPS = 4


def _assert_public_address(url: str) -> None:
    """Raise an ``httpx.HTTPError`` unless ``url`` is a public http(s) address.

    An HTTPError specifically: every caller here already treats one as "that
    page is unreachable", so a blocked host degrades exactly like a dead one
    instead of becoming a new exception nobody catches.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise httpx.UnsupportedProtocol(
            f"{url[:200]!r} is not an http(s) address"
        )
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


def _safe_get(cli: httpx.Client, url: str, *, max_hops: int = MAX_REDIRECT_HOPS):
    """``cli.get(url)`` with the address check applied to every hop.

    Redirects are followed here rather than by httpx because httpx would only
    ever check what we asked for: a 302 to http://169.254.169.254/ walks past
    any check made before the request. Hence ``follow_redirects=False`` on the
    clients below — re-checking each Location is the entire point.
    """
    target = url
    for _ in range(max_hops + 1):
        _assert_public_address(target)
        resp = cli.get(target)
        if resp.status_code not in _REDIRECT_CODES:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        target = str(resp.url.join(location))
    raise httpx.TooManyRedirects(f"more than {max_hops} redirects from {url[:200]}")


def fetch_page(url: str, client: httpx.Client | None = None) -> PageFacts:
    """Fetch one page and extract the on-page facts audits and briefs need."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    own = client is None
    cli = client or httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": FETCH_UA})
    facts = PageFacts(url=url)
    try:
        resp = _safe_get(cli, url)
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


def fetch_html(url: str, *, max_bytes: int = 2_000_000, timeout: float = 20.0,
               client: httpx.Client | None = None) -> tuple[str, str]:
    """One user-supplied URL -> ``(final_url, html)``, address-checked on every hop.

    The only fetcher here that hands the raw document back, for callers that
    run their own extractor (the Content Optimizer's page check). Two outcomes:
    the page, or ``ValueError`` saying in plain words why not — a scheme that
    is not http(s), a host that resolves to a non-public address on the first
    request or on any redirect, a transport failure, a non-200 status, a
    non-HTML body, or a body over ``max_bytes``. Wrapping the httpx errors is
    deliberate: a URL a user typed has one failure mode from their side
    ("could not fetch this"), so one exception type carries all of them and
    the message says which. ``CredentialMissing`` offline, like every fetcher
    above.
    """
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{url[:200]!r} is not an http(s) address")
    own = client is None
    cli = client or httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": FETCH_UA})
    try:
        try:
            resp = _safe_get(cli, url)
        except httpx.HTTPError as exc:
            raise ValueError(f"could not fetch {url[:200]}: {exc}") from exc
        if resp.status_code != 200:
            raise ValueError(f"{url[:200]} returned HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "html")
        if "html" not in content_type:
            raise ValueError(f"{url[:200]} is not an HTML page ({content_type})")
        if len(resp.content) > max_bytes:
            raise ValueError(f"{url[:200]} is larger than {max_bytes} bytes")
        return str(resp.url), resp.text
    finally:
        if own:
            cli.close()


def fetch_text(url: str, client: httpx.Client | None = None) -> dict:
    """Raw text fetch (robots.txt, redirect checks): {status, text, final_url}."""
    if not state.use_cloud():
        raise CredentialMissing("offline mode")
    own = client is None
    cli = client or httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": FETCH_UA})
    try:
        resp = _safe_get(cli, url)
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
    cli = client or httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": FETCH_UA})

    def locs(url: str) -> list[str]:
        try:
            resp = _safe_get(cli, url)
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
