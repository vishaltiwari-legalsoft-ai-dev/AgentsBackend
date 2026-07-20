"""Live-SERP access behind a provider seam.

SerpApiProvider is the paid, production path; FixtureProvider replays a saved
response for tests/offline. Nothing else in the package knows which is in use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from seo_agent.schemas import SerpEntry, SerpResult

_SERPAPI_URL = "https://serpapi.com/search"


def _serpapi_key() -> str:
    from app.services import runtime_config

    return runtime_config.get("serpapi_key")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_serpapi(payload: dict, keyword: str, location: str) -> SerpResult:
    entries = [
        SerpEntry(
            url=r.get("link", ""),
            title=r.get("title", ""),
            position=int(r.get("position", i + 1)),
            snippet=r.get("snippet", ""),
        )
        for i, r in enumerate(payload.get("organic_results", []))
        if r.get("link")
    ]
    paa = [q["question"] for q in payload.get("related_questions", []) if q.get("question")]
    overview = payload.get("ai_overview") or {}
    blocks = [b.get("snippet", "") for b in overview.get("text_blocks", [])]
    ai_text = " ".join(t for t in blocks if t).strip() or None
    ai_sources = [r["link"] for r in overview.get("references", []) if r.get("link")]
    return SerpResult(
        keyword=keyword, location=location, fetched_at=_now_iso(),
        entries=entries, paa_questions=paa,
        ai_overview=ai_text, ai_overview_sources=ai_sources,
    )


class SerpProvider(Protocol):
    def fetch(self, keyword: str, location: str) -> SerpResult: ...


class FixtureProvider:
    """Replays a saved SerpAPI JSON response (tests / offline dev)."""

    def __init__(self, path: Path):
        self._path = Path(path)

    def fetch(self, keyword: str, location: str) -> SerpResult:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return _parse_serpapi(payload, keyword, location)


class SerpApiProvider:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client()

    def fetch(self, keyword: str, location: str) -> SerpResult:
        params = {
            "engine": "google", "q": keyword, "location": location,
            "num": 20, "api_key": _serpapi_key(),
        }
        response = self._client.get(_SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        return _parse_serpapi(response.json(), keyword, location)


def get_provider() -> SerpProvider:
    if not _serpapi_key():
        raise RuntimeError("SerpAPI key is not configured (set serpapi_key in Secrets)")
    return SerpApiProvider()
