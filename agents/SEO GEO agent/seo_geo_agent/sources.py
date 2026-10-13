"""External data adapters: Search Console, Serper.dev, page fetcher, LLM.

Every adapter degrades instead of failing the run: missing credentials (or
offline mode) raise ``CredentialMissing`` and the caller records a
plain-language degradation note or falls back to a heuristic.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

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


# ------------------------------ plain notes ------------------------------
# A degraded note is ``"<Source>: <reason>"`` and the reason used to be the
# exception text — ``<HttpError 403 when requesting https://…>`` straight into
# the API payload. The reason a person reads is one of a few plain sentences;
# the raw text goes to the server log. Each sentence keeps the words
# ``app.services.issues`` classifies on ("permission", "not found", "expired",
# "not set") and the property in ``for property '…'`` form, so the Issues rail
# still files it under the right code.

_log = logging.getLogger("agentos.seo_geo.sources")

_RAW_NOTE = re.compile(r"[<>{}\[\]]|https?://|traceback|exception|\berror\b|\n", re.I)
_NOTE_PROPERTY = (re.compile(r"\brejected (\S+?):(?:\s|$)"),
                  re.compile(r"for (?:site|property) '([^']+)'"))
#: First match wins: the specific causes sit above the status codes a
#: wrapped error also carries ("LLM unavailable: Error code: 401 …" is the
#: model, not a Search Console permission).
_REASONS = (
    (re.compile(r"invalid_grant|expired or revoked|token has been expired|revoked", re.I),
     lambda thing: "the saved connection was revoked or expired — reconnect it."),
    (re.compile(r"llm unavailable|ai model", re.I),
     lambda thing: "the AI model wasn't available."),
    (re.compile(r"key missing|not set\b|no api key", re.I),
     lambda thing: "the API key is not set on this server."),
    (re.compile(r"auth unavailable|credentials", re.I),
     lambda thing: "this server has no Google credentials for it."),
    (re.compile(r"\b40[13]\b|permission|forbidden|unauthori[sz]ed|access denied|does not have access",
                re.I),
     lambda thing: f"no permission for {thing} — the account this server reads with hasn't been "
                   "given access."),
    (re.compile(r"\b404\b|not found", re.I),
     lambda thing: f"{thing} was not found in the account this server reads with."),
    (re.compile(r"\b429\b|quota|rate.?limit|credits?\b", re.I),
     lambda thing: "the service's usage limit or credits ran out."),
    (re.compile(r"timed? ?out|timeout|deadline", re.I),
     lambda thing: "the service didn't answer in time."),
    (re.compile(r"\b50[0234]\b|connecterror|connection (?:refused|reset|error|aborted)|"
                r"could not resolve|service unavailable", re.I),
     lambda thing: "the service couldn't be reached."),
)
GENERIC_NOTE_REASON = "it couldn't be read on this run."


def plain_reason(detail, *, log: bool = False) -> str:
    """The reason part of a note as one plain sentence. Our own plain
    sentences pass through; a recognised failure becomes its sentence; any
    other raw text becomes :data:`GENERIC_NOTE_REASON`. ``log``: write the raw
    text to the server log when it was replaced."""
    text = " ".join(str(detail or "").split())
    prop = next((m.group(1).rstrip(".,;") for rx in _NOTE_PROPERTY for m in [rx.search(text)] if m), "")
    thing = f"property '{prop}'" if prop else "this property"
    plain = next((say(thing) for rx, say in _REASONS if rx.search(text)), None)
    if plain is None:
        plain = GENERIC_NOTE_REASON if _RAW_NOTE.search(text) else text
    if log and plain != text:
        _log.warning("SEO degraded note made plain; raw: %s", text[:2000])
    return plain


def plain_note(note, *, log: bool = True) -> str:
    """``"<Source>: <reason>"`` with the reason made plain (:func:`plain_reason`).

    A note with no ``Label: `` prefix is kept when it is already a sentence
    and replaced by a generic one when it is raw. Idempotent. When the text
    changes, the raw original goes to the server log (``log=False`` on read
    paths that re-clean stored notes, so one bad note is logged once)."""
    text = str(note or "").strip()
    label, sep, detail = text.partition(": ")
    if sep and 0 < len(label) <= 60 and not _RAW_NOTE.search(label):
        plain = f"{label}: {plain_reason(detail)}"
    elif _RAW_NOTE.search(text):
        plain = "Part of this run couldn't be completed."
    else:
        plain = text
    if log and plain != text:
        _log.warning("SEO degraded note made plain; raw: %s", text[:2000])
    return plain


@dataclass
class QueryStat:
    query: str
    page: str
    clicks: int
    impressions: int
    ctr: float
    position: float


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


def norm_url(url: str) -> str:
    """One spelling per page: ``host/path`` — no scheme, ``www.``, query,
    fragment or trailing slash. Used wherever two mentions of a page must
    compare equal (evidence citations, stable to-do ids, template grouping)."""
    raw = (url or "").strip()
    if "//" not in raw:
        raw = "//" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}"


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

#: Offending image srcs kept per page — enough to name a template image or
#: point at the page's own culprits, never a dump of every asset.
MAX_ALT_SRCS = 20
MAX_SCHEMA_ERRORS = 5

_WH_WORDS = frozenset({"what", "how", "why", "when", "who", "which"})
_AUX_WORDS = frozenset({"can", "do", "does", "is", "are"})
# A wh-word only opens a question when an auxiliary or quantifier follows it:
# "How much does it cost" is one, "How it works" / "What we do" are section
# labels. Same for a leading auxiliary: "Is it worth it" vs "Do more with less".
_AFTER_WH = frozenset({"is", "are", "was", "were", "do", "does", "did", "can", "could",
                       "should", "would", "will", "much", "many", "long", "often", "if"})
_AFTER_AUX = frozenset({"i", "you", "we", "it", "they", "my", "your", "our", "a", "an", "the",
                        "this", "that", "these", "those", "there", "he", "she", "someone",
                        "anyone"})


def is_question_heading(text: str) -> bool:
    """True for a heading written as a question — the FAQ-fit signal."""
    t = " ".join((text or "").split())
    if not t:
        return False
    if t.endswith("?"):
        return True
    words = re.findall(r"[a-z']+", t.lower())
    if len(words) < 3:
        return False
    if words[0] in _WH_WORDS:
        return words[1] in _AFTER_WH
    if words[0] in _AUX_WORDS:
        return words[1] in _AFTER_AUX
    return False


#: Words a question a visitor would ask opens with. "Missing calls from leads?"
#: and "Ready to start earning?" end in a question mark but are marketing
#: hooks; "How do virtual paralegal services work?" is a question.
_INTERROGATIVES = _WH_WORDS | _AUX_WORDS | frozenset({
    "where", "whose", "whom", "should", "would", "will", "could", "did", "has", "have",
    "had", "was", "were", "may", "might", "must", "shall", "am",
})


def is_faq_question(text: str) -> bool:
    """A heading that reads as a visitor's question: question-shaped
    (:func:`is_question_heading`) AND opening with a question word or an
    auxiliary. Every real FAQ heading on legalsoft.com does; the CTA hooks on
    /testimonials ("Growing in a crowded market?") and the partner pages
    ("Ready to Start Earning?") do not."""
    if not is_question_heading(text):
        return False
    first = re.findall(r"[a-z']+", " ".join((text or "").split()).lower())
    return bool(first) and first[0].split("'")[0] in _INTERROGATIVES


#: An answer is a text block of at least this many words between a question
#: heading and the next heading.
FAQ_ANSWER_WORDS = 25
MAX_LINK_PATHS = 300
MAX_PATH_CHARS = 200
MAX_CONTACT_LINKS = 5
MAX_BOOKING_LINKS = 10

#: Form kinds that are not a way to reach the business.
NON_CONTACT_FORMS = frozenset({"search", "newsletter", "filter", "login"})
#: Embed kinds that are a calendar, not (only) a form.
BOOKING_EMBEDS = frozenset({"calendly", "hubspot-meetings"})

#: Known form / scheduling embeds: ``(kind, pattern)``. Matched against a
#: <script>/<iframe> src, the class and data- attributes of any element, and
#: inline-script text — HubSpot's v2 embed is a src on js.hsforms.net plus an
#: inline ``hbspt.forms.create(...)``, which is exactly how /contact-us ships it.
_EMBEDS = (
    ("hubspot-meetings", re.compile(r"meetings\.hubspot\.com|meetings-iframe-container", re.I)),
    ("hubspot", re.compile(r"hsforms\.(?:net|com)|hbspt\.forms|\bhbspt-form\b|\bhs-form\b", re.I)),
    ("typeform", re.compile(r"typeform\.com|\btypeform-widget\b|\bdata-tf-(?:widget|live|popup)\b", re.I)),
    ("calendly", re.compile(r"calendly\.com|\bcalendly-(?:inline|badge)-widget\b|Calendly\.init", re.I)),
    ("jotform", re.compile(r"jotform\.(?:com|us)|jotfor\.ms", re.I)),
    ("wpforms", re.compile(r"\bwpforms-(?:form|container)\b|/wpforms/", re.I)),
    ("gravity-forms", re.compile(r"\bgform_(?:wrapper|body)\b|gravityforms", re.I)),
    ("formstack", re.compile(r"formstack\.com", re.I)),
    ("google-forms", re.compile(r"docs\.google\.com/forms|forms\.gle", re.I)),
    ("cognito", re.compile(r"cognitoforms\.com|\bcognito-form\b", re.I)),
)
#: Scheduling hosts a booking link points at.
_BOOKING_HOSTS = ("calendly.com", "meetings.hubspot.com", "savvycal.com", "cal.com",
                  "acuityscheduling.com", "as.me", "youcanbook.me", "tidycal.com", "zcal.co",
                  "oncehub.com", "appointlet.com", "chilipiper.com", "setmore.com",
                  "simplybook.me", "vcita.com")
#: A same-site path segment that is a booking page: /book-a-meeting-…,
#: /schedule-a-call, /booking — never /bookkeeper.
_BOOKING_SEG = re.compile(r"^(?:book|schedule|booking|appointments?|calendar)(?:-|$)", re.I)


def _embed_kind(text: str) -> str | None:
    for kind, rx in _EMBEDS:
        if rx.search(text or ""):
            return kind
    return None


def _form_kind(a: dict) -> str:
    """What one <form> is for, from its own attributes."""
    blob = " ".join(str(a.get(k) or "") for k in (
        "id", "name", "data-name", "class", "action", "aria-label", "role")).lower()
    if (a.get("role") or "").lower() == "search" or "search" in blob:
        return "search"
    if "newsletter" in blob or "subscribe" in blob or "subscription" in blob:
        return "newsletter"
    if "filter" in blob or "fs-cmsfilter-element" in a:
        return "filter"
    if re.search(r"log-?in|sign-?in|password", blob):
        return "login"
    return "form"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def link_facts(page_url: str, hrefs: list[str]) -> dict:
    """From a page's raw hrefs: same-site link targets as normalised paths, the
    ``tel:`` and ``mailto:`` links, and booking links — the facts an "it has no
    X" claim is checked against."""
    own = _host(page_url)
    paths: list[str] = []
    tel: list[str] = []
    mail: list[str] = []
    booking: list[str] = []
    seen: set[str] = set()
    capped = False
    for raw in hrefs:
        href = (raw or "").strip()
        low = href.lower()
        if low.startswith("tel:"):
            if href not in tel and len(tel) < MAX_CONTACT_LINKS:
                tel.append(href[:60])
            continue
        if low.startswith("mailto:"):
            if href not in mail and len(mail) < MAX_CONTACT_LINKS:
                mail.append(href[:120])
            continue
        if not href or low.startswith(("javascript:", "#", "data:")):
            continue
        try:
            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/") or "/"
        first = next((s for s in path.split("/") if s), "")
        is_booking = (any(host == h or host.endswith("." + h) for h in _BOOKING_HOSTS)
                      or (host == own and _BOOKING_SEG.match(first) is not None))
        if is_booking and absolute not in booking and len(booking) < MAX_BOOKING_LINKS:
            booking.append(absolute.split("#", 1)[0][:300])
        if host == own and path not in seen:
            seen.add(path)
            if len(paths) < MAX_LINK_PATHS and len(path) <= MAX_PATH_CHARS:
                paths.append(path)
            else:
                capped = True
    return {"link_paths": paths, "link_paths_capped": capped, "tel_links": tel,
            "mailto_links": mail, "booking_links": booking}


def schema_local_name(raw: str) -> str:
    """``https://schema.org/Article`` / ``schema:Article`` / ``Article`` -> ``Article``."""
    name = str(raw or "").strip()
    for sep in ("/", "#", ":"):
        name = name.rsplit(sep, 1)[-1]
    return name


def jsonld_types(node) -> list[str]:
    """Every ``@type`` in a parsed JSON-LD value — nested objects, arrays and
    ``@graph`` included (a FAQPage under a WebPage's ``mainEntity`` is still a
    FAQPage on the page). Order of first appearance, no duplicates."""
    out: list[str] = []

    def walk(n) -> None:
        if isinstance(n, list):
            for item in n:
                walk(item)
        elif isinstance(n, dict):
            t = n.get("@type")
            for typ in t if isinstance(t, list) else [t]:
                name = schema_local_name(typ) if isinstance(typ, str) else ""
                if name and name not in out:
                    out.append(name)
            for key, value in n.items():
                if key != "@type" and isinstance(value, (dict, list)):
                    walk(value)

    walk(node)
    return out


_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"   # NANP
    r"|(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\d)"  # international
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][\w.'-]*\s+){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|"
    r"Place|Pl|Parkway|Pkwy|Highway|Hwy|Suite|Ste)\b\.?"
)
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_KEPT_HEADINGS = ("h1", "h2", "h3")


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
    schema_errors: list[str] = field(default_factory=list)  # JSON-LD blocks that failed to parse
    internal_links: list[str] = field(default_factory=list)
    images_total: int = 0           # images counted for alt (noscript/presentational skipped)
    images_no_alt: int = 0          # no alt ATTRIBUTE at all — the real defect
    images_decorative: int = 0      # alt="" — declared decorative, not "missing"
    images_no_alt_srcs: list[str] = field(default_factory=list)  # capped, de-duplicated
    q_headings: int = 0             # question-style headings, h1-h6
    #: Question headings that are not themselves links and are followed by an
    #: answer block (FAQ_ANSWER_WORDS+) before the next heading: real Q&A.
    faq_pairs: int = 0
    headings_total: int = 0         # h1-h6
    headings_linked: int = 0        # headings inside (or wrapping) a link: a listing's cards
    has_phone: bool = False
    has_address: bool = False       # a postal street address in the visible text
    address_text: str = ""          # the first one seen, so a claim about it can be checked
    #: What an "it has no form / phone / booking link / link to X" claim is
    #: checked against. ``forms`` holds kinds: an embed ("hubspot", …), "form"
    #: (an HTML form that is a way to reach the business), or one of
    #: NON_CONTACT_FORMS.
    forms: list[str] = field(default_factory=list)
    tel_links: list[str] = field(default_factory=list)
    mailto_links: list[str] = field(default_factory=list)
    booking_links: list[str] = field(default_factory=list)
    link_paths: list[str] = field(default_factory=list)   # same-site targets, normalised, capped
    #: True when same-site links were left out (the cap, an over-long path, or
    #: the corpus size budget): "it never links to X" cannot be proven then.
    link_paths_capped: bool = False
    word_count: int = 0
    text: str = ""  # body text, capped — feeds the site-brain content analysis

    @property
    def questions(self) -> list[str]:
        return [h for h in self.h2 + self.h3 if h.strip().endswith("?")]


class _PageParser(HTMLParser):
    """On-page facts, collected per ELEMENT.

    Text arrives in chunks — ``<h1>Legal<br>Virtual Assistants</h1>`` is two
    ``handle_data`` calls, a ``<span>`` inside is more — so a heading is
    buffered from its start tag to its end tag and recorded once. Recording
    per chunk is how one Webflow ``<h1>`` used to count as three.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.facts_title: list[str] = []
        self.meta_description = ""
        self.canonical = ""
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.q_headings = 0
        self.schema_raw: list[str] = []
        self.schema_types: list[str] = []   # microdata itemtype + RDFa typeof
        self.links: list[str] = []
        self.images_total = 0
        self.images_no_alt = 0
        self.images_decorative = 0
        self.no_alt_srcs: list[str] = []
        self.has_phone = False
        self.has_address = False
        self.address_text = ""
        self.forms: list[str] = []
        self.headings_total = 0
        self.headings_linked = 0
        self.faq_pairs = 0
        self.words = 0
        self.text_parts: list[str] = []
        self._text_len = 0
        self._skip: str | None = None        # inside <script>/<style>
        self._in_schema = False
        self._svg_depth = 0
        self._noscript_depth = 0
        self._title_state = "before"         # before -> open -> done
        self._heading: str | None = None
        self._heading_buf: list[str] = []
        self._heading_link = False
        self._anchors: list[bool] = []       # open <a> elements: has an href?
        self._qa: dict | None = None         # the last question heading, counting its answer
        self._form: int | None = None        # index in ``forms`` of the open <form>
        self._wform: list | None = None      # [tag, depth, saw_form] of an open Webflow w-form

    # -- helpers --------------------------------------------------------
    def _in_link(self) -> bool:
        return any(self._anchors)

    def _settle_qa(self) -> None:
        """The previous question heading's answer ends where a heading starts."""
        qa, self._qa = self._qa, None
        if qa and qa["words"] >= FAQ_ANSWER_WORDS:
            self.faq_pairs += 1

    def _close_heading(self) -> None:
        tag, self._heading = self._heading, None
        text = " ".join(" ".join(self._heading_buf).split())[:300]
        self._heading_buf = []
        linked, self._heading_link = self._heading_link, False
        if not tag or not text:
            return
        if tag in self.headings:
            self.headings[tag].append(text)
        self.headings_total += 1
        self.headings_linked += linked
        if is_question_heading(text):
            self.q_headings += 1
        if not linked and is_faq_question(text):
            self._qa = {"words": 0}

    def _add_form(self, kind: str) -> int:
        self.forms.append(kind)
        return len(self.forms) - 1

    def _embeds(self, *texts: str) -> None:
        for text in texts:
            kind = _embed_kind(text)
            if kind:
                self._add_form(kind)
                return

    def _add_types(self, raw: str | None) -> None:
        for token in (raw or "").split():
            name = schema_local_name(token)
            if name and name not in self.schema_types:
                self.schema_types.append(name)

    def _image(self, a: dict) -> None:
        if self._noscript_depth:
            return  # the no-JS duplicate of an image already counted (or a tracking pixel)
        if (a.get("role") or "").strip().lower() in ("presentation", "none"):
            return
        if (a.get("aria-hidden") or "").strip().lower() == "true":
            return
        self.images_total += 1
        if "alt" not in a:
            self.images_no_alt += 1
            src = (a.get("src") or a.get("data-src") or "").strip()
            if src.startswith("data:"):
                src = "(inline data: image)"
            if src and src not in self.no_alt_srcs and len(self.no_alt_srcs) < MAX_ALT_SRCS:
                self.no_alt_srcs.append(src[:500])
        elif not (a.get("alt") or "").strip():
            self.images_decorative += 1

    # -- HTMLParser hooks -----------------------------------------------
    def _track_wform(self, tag: str, a: dict) -> None:
        """A Webflow ``w-form`` wrapper counts as a form only when no <form>
        start tag appears inside it — the inner <form> is classified itself."""
        if self._wform is None:
            if "w-form" in (a.get("class") or "").split():
                self._wform = [tag, 1, False]
        elif tag == self._wform[0]:
            self._wform[1] += 1

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("itemtype"):
            self._add_types(a.get("itemtype"))
        if a.get("typeof"):
            self._add_types(a.get("typeof"))
        self._track_wform(tag, a)
        marker = " ".join(f"{k} {v or ''}" for k, v in a.items()
                          if k in ("class", "id") or k.startswith("data-"))
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "noscript":
            self._noscript_depth += 1
        elif tag == "title":
            if not self._svg_depth and self._title_state == "before":
                self._title_state = "open"
        elif tag in _HEADING_TAGS:
            self._close_heading()  # an unclosed heading ends where the next begins
            self._settle_qa()
            self._heading = tag
            self._heading_link = self._in_link()
        elif tag in ("script", "style"):
            kind = (a.get("type") or "").split(";")[0].strip().lower()
            self._in_schema = tag == "script" and kind == "application/ld+json"
            if self._in_schema:
                self.schema_raw.append("")
            elif tag == "script":
                self._embeds(a.get("src") or "", marker)
            self._skip = tag
        elif tag == "iframe":
            self._embeds(a.get("src") or a.get("data-src") or "", marker)
        elif tag == "form":
            if self._wform is not None:
                self._wform[2] = True
            self._form = self._add_form(_form_kind(a))
        elif tag == "input" and self._form is not None:
            if (a.get("type") or "").lower() == "search" or (a.get("name") or "") in ("q", "s"):
                self.forms[self._form] = "search"
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.meta_description = a.get("content") or ""
        elif tag == "link" and a.get("rel", "") == "canonical":
            self.canonical = a.get("href") or ""
        elif tag == "a":
            self._anchors.append(bool(a.get("href")))
            if a.get("href"):
                self.links.append(a["href"])
                if a["href"].strip().lower().startswith("tel:"):
                    self.has_phone = True
                if self._heading:
                    self._heading_link = True   # a heading that is a link to elsewhere
        elif tag == "img":
            self._image(a)
        if tag not in ("script", "iframe", "form") and marker and tag != "a":
            self._embeds(marker)

    def handle_endtag(self, tag):
        if self._wform is not None and tag == self._wform[0]:
            self._wform[1] -= 1
            if self._wform[1] <= 0:
                if not self._wform[2]:
                    self._add_form("form")
                self._wform = None
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        elif tag == "noscript":
            self._noscript_depth = max(0, self._noscript_depth - 1)
        elif tag == "title":
            if self._title_state == "open":
                self._title_state = "done"
        elif tag in _HEADING_TAGS:
            if self._heading:
                self._close_heading()
        elif tag in ("script", "style") and self._skip == tag:
            self._skip = None
            self._in_schema = False
        elif tag == "form":
            self._form = None
        elif tag == "a" and self._anchors:
            self._anchors.pop()

    def handle_data(self, data):
        if self._skip:
            if self._in_schema:
                self.schema_raw[-1] += data
            elif self._skip == "script":
                self._embeds(data)   # hbspt.forms.create(...), Calendly.initInlineWidget(...)
            return
        if self._svg_depth:
            return  # an icon's <title>/<desc> is not page text, a title, or a heading
        text = " ".join(data.split())
        if not text:
            return
        if self._title_state == "open":
            self.facts_title.append(text)
            return
        if self._heading:
            self._heading_buf.append(text)
            return
        if self._noscript_depth:
            return
        n = len(text.split())
        self.words += n
        if self._qa is not None:
            self._qa["words"] += n
        if not self.has_phone and _PHONE_RE.search(text):
            self.has_phone = True
        if not self.has_address:
            found = _ADDRESS_RE.search(text)
            if found:
                self.has_address = True
                tail = text[found.start(): found.start() + 120]
                zip_code = _ZIP_RE.search(tail, len(found.group(0)))  # after the street number
                self.address_text = (tail[: zip_code.end()] if zip_code else found.group(0)).strip()
        if self._text_len < 6000:
            self.text_parts.append(text)
            self._text_len += len(text) + 1

    def close(self):
        super().close()
        self._close_heading()
        self._settle_qa()


def _apply_parsed(facts: PageFacts, parser: _PageParser) -> PageFacts:
    """Copy what ``_PageParser`` collected onto ``facts`` — JSON-LD parsed here.

    A JSON-LD block that does not parse is recorded in ``schema_errors``, never
    skipped: Google ignores it too, so "you have schema" would be a false
    comfort and "you have none" a false alarm.
    """
    facts.title = " ".join(parser.facts_title)[:300]
    facts.meta_description = parser.meta_description[:500]
    facts.canonical = parser.canonical
    facts.h1, facts.h2, facts.h3 = parser.headings["h1"], parser.headings["h2"], parser.headings["h3"]
    facts.q_headings = parser.q_headings
    facts.faq_pairs = parser.faq_pairs
    facts.headings_total, facts.headings_linked = parser.headings_total, parser.headings_linked
    facts.internal_links = parser.links[:400]
    facts.images_total = parser.images_total
    facts.images_no_alt = parser.images_no_alt
    facts.images_decorative = parser.images_decorative
    facts.images_no_alt_srcs = list(parser.no_alt_srcs)
    facts.has_phone, facts.has_address = parser.has_phone, parser.has_address
    facts.address_text = parser.address_text
    facts.forms = list(dict.fromkeys(parser.forms))
    facts.word_count = parser.words
    facts.text = " ".join(parser.text_parts)[:6000]
    types: list[str] = list(parser.schema_types)
    for i, raw in enumerate(parser.schema_raw, start=1):
        body = raw.strip()
        if not body:
            continue  # an empty block claims nothing
        try:
            node = json.loads(body)
        except ValueError as exc:
            if len(facts.schema_errors) < MAX_SCHEMA_ERRORS:
                facts.schema_errors.append(f"JSON-LD block {i}: {exc}"[:200])
            continue
        for typ in jsonld_types(node):
            if typ not in types:
                types.append(typ)
    facts.schema_types = types
    return facts


def parse_page(url: str, html_text: str, status: int = 200) -> PageFacts:
    """Parse one HTML document into ``PageFacts`` — no network."""
    facts = PageFacts(url=url, status=status)
    parser = _PageParser()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — real-world HTML; keep what parsed
        pass
    try:
        parser.close()
    except Exception:  # noqa: BLE001
        parser._close_heading()
    _apply_parsed(facts, parser)
    # Resolve srcs so the same header logo reads the same on every page.
    facts.images_no_alt_srcs = [
        s if s.startswith("(") else urljoin(url, s).split("#", 1)[0]
        for s in facts.images_no_alt_srcs
    ]
    links = link_facts(url, parser.links)
    facts.link_paths = links["link_paths"]
    facts.link_paths_capped = links["link_paths_capped"]
    facts.tel_links = links["tel_links"]
    facts.mailto_links = links["mailto_links"]
    facts.booking_links = links["booking_links"]
    return facts


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
        return parse_page(url, resp.text, status=resp.status_code)
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
