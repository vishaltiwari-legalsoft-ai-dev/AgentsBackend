"""GEO agent — AI answer-engine adapters.

Each adapter asks one engine one prompt and returns an :class:`EngineAnswer`
with the answer text and the engine's OWN citation metadata. This is why we
call the providers directly instead of OpenRouter: citations (Perplexity
``search_results``, Gemini ``groundingMetadata``, OpenAI ``url_citation``
annotations) are only exposed by the native APIs.

Adapters never raise — any HTTP/parse failure comes back as
``EngineAnswer(error=...)`` so one bad engine degrades instead of killing a
poll sweep. Keys resolve through ``runtime_config`` (Settings → Secrets
override, else env); a missing key just makes the engine unavailable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from app.services import runtime_config

from seo_geo_agent.sources import domain_of

# One place to change engine targets. Answer text is capped so a day of poll
# docs stays far under Firestore's 1 MB doc limit.
ANSWER_TEXT_CAP = 4000
REQUEST_TIMEOUT = 45
PERPLEXITY_MODEL = "sonar"
GEMINI_MODEL = "gemini-flash-latest"  # alias survives model turnover; pinned 2.5 404s for new keys
OPENAI_MODEL = "gpt-4o-mini"

# engine id -> runtime_config/settings field holding its key
ENGINE_KEY_FIELDS: dict[str, str] = {
    "perplexity": "perplexity_api_key",
    "gemini": "gemini_api_key",
    "chatgpt": "openai_api_key",
}


@dataclass
class EngineAnswer:
    engine: str
    model: str = ""
    text: str = ""
    citations: list[dict] = field(default_factory=list)  # {url, domain, title}
    latency_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "model": self.model,
            "text": self.text[:ANSWER_TEXT_CAP],
            "citations": self.citations,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def engine_key(engine: str) -> str:
    field_name = ENGINE_KEY_FIELDS.get(engine, "")
    return runtime_config.get(field_name) if field_name else ""


def available_engines() -> dict[str, bool]:
    """Which engines have a usable key right now (env or admin override)."""
    return {engine: bool(engine_key(engine)) for engine in ENGINE_KEY_FIELDS}


def _citation(url: str, title: str = "") -> dict:
    return {"url": url, "domain": domain_of(url), "title": title}


def poll_perplexity(prompt: str, key: str) -> EngineAnswer:
    started = time.monotonic()
    answer = EngineAnswer(engine="perplexity", model=PERPLEXITY_MODEL)
    try:
        resp = httpx.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": PERPLEXITY_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        data = resp.json()
        answer.text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        # Newer responses carry search_results [{title,url,date}]; older ones a
        # bare citations list of URLs. Accept both.
        for item in data.get("search_results") or []:
            if item.get("url"):
                answer.citations.append(_citation(item["url"], item.get("title", "")))
        if not answer.citations:
            for url in data.get("citations") or []:
                if isinstance(url, str) and url:
                    answer.citations.append(_citation(url))
    except Exception as exc:  # noqa: BLE001 — adapters must degrade, never raise
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        answer.error = f"{type(exc).__name__}: {exc}"
    return answer


def poll_gemini(prompt: str, key: str) -> EngineAnswer:
    started = time.monotonic()
    answer = EngineAnswer(engine="gemini", model=GEMINI_MODEL)
    try:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "tools": [{"google_search": {}}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        data = resp.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts") or []
        answer.text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
        # groundingChunks[].web = {uri (redirect URL), title (usually the real
        # domain)}. The uri is a vertexaisearch redirect, so when the title
        # looks like a domain we trust it for the domain field instead.
        grounding = candidate.get("groundingMetadata") or {}
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri, title = web.get("uri", ""), web.get("title", "")
            if not uri:
                continue
            cit = _citation(uri, title)
            if title and "." in title and " " not in title:
                cit["domain"] = title.lower().removeprefix("www.")
            answer.citations.append(cit)
    except Exception as exc:  # noqa: BLE001
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        answer.error = f"{type(exc).__name__}: {exc}"
    return answer


def poll_openai(prompt: str, key: str) -> EngineAnswer:
    started = time.monotonic()
    answer = EngineAnswer(engine="chatgpt", model=OPENAI_MODEL)
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": OPENAI_MODEL,
                "input": prompt,
                "tools": [{"type": "web_search"}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        data = resp.json()
        texts: list[str] = []
        for item in data.get("output") or []:
            for content in item.get("content") or []:
                if content.get("text"):
                    texts.append(content["text"])
                for note in content.get("annotations") or []:
                    if note.get("type") == "url_citation" and note.get("url"):
                        answer.citations.append(
                            _citation(note["url"], note.get("title", ""))
                        )
        answer.text = "\n".join(texts)
    except Exception as exc:  # noqa: BLE001
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        answer.error = f"{type(exc).__name__}: {exc}"
    return answer


ENGINES = {
    "perplexity": poll_perplexity,
    "gemini": poll_gemini,
    "chatgpt": poll_openai,
}


def poll_engine(engine: str, prompt: str) -> EngineAnswer:
    """Poll one engine once. Missing key → EngineAnswer(error=...), no raise."""
    fn = ENGINES.get(engine)
    if fn is None:
        return EngineAnswer(engine=engine, error=f"unknown engine: {engine}")
    key = engine_key(engine)
    if not key:
        return EngineAnswer(engine=engine, error="no API key configured")
    return fn(prompt, key)
