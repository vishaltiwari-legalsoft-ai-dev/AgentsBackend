"""External data adapters: Google Search Console (ADC auth) + Serper.dev.

Every adapter degrades instead of failing the run: missing credentials raise
``CredentialMissing`` and the caller records a plain-language degradation note.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import httpx

from . import state

SERPER_ENDPOINT = "https://google.serper.dev/search"


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
