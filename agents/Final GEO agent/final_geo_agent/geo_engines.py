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

import os
import time
from dataclasses import dataclass, field

import httpx

from app.services import runtime_config

from seo_geo_agent import state
from seo_geo_agent.sources import domain_of

# One place to change engine targets. Answer text AND citations are capped in
# to_dict so a day of poll docs stays under Firestore's 1 MB doc limit — an
# uncapped Gemini citation list (500-char redirect URLs) blew a prod day-doc
# past the limit on 2026-08-10.
ANSWER_TEXT_CAP = 4000
CITATIONS_CAP = 10
CITATION_URL_CAP = 400
CITATION_TITLE_CAP = 120
REQUEST_TIMEOUT = 45
PERPLEXITY_MODEL = "sonar"
GEMINI_MODEL = "gemini-flash-latest"  # alias survives model turnover; pinned 2.5 404s for new keys
OPENAI_MODEL = "gpt-4o-mini"

# engine id -> the model its NATIVE api runs (what a "native" chip promises)
NATIVE_ENGINE_MODELS: dict[str, str] = {
    "perplexity": PERPLEXITY_MODEL,
    "gemini": GEMINI_MODEL,
    "chatgpt": OPENAI_MODEL,
}

# engine id -> runtime_config/settings field holding its key
ENGINE_KEY_FIELDS: dict[str, str] = {
    "perplexity": "perplexity_api_key",
    "gemini": "gemini_api_key",
    "chatgpt": "openai_api_key",
}

# OpenRouter fallback: the platform's shared key polls mainstream models when
# an engine has no native key (or its native quota runs dry). Perplexity via
# OpenRouter hits the same Sonar backend (same surface); gemini/chatgpt get
# OpenRouter's web plugin — a search-grounded index, not the consumer app.
# The ``via`` field on every answer discloses which surface was measured.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_ENGINE_MODELS: dict[str, str] = {
    "perplexity": "perplexity/sonar",
    "gemini": "google/gemini-2.5-flash",
    "chatgpt": "openai/gpt-5-mini",
}
_NEEDS_WEB_PLUGIN = {"gemini", "chatgpt"}  # sonar searches on its own

# Google AI Overview via SerpAPI — the ONLY engine here measuring the real
# consumer SERP surface. Free plan is ~250 searches/month, so AIO polls once
# per prompt (SERP content, low variance) under a monthly credit guard in
# geo_poll. "No AI Overview shown" is a completed observation, never an error.
AIO_ENGINE = "aio"
SERPAPI_URL = "https://serpapi.com/search.json"

# every engine whose poll docs exist on disk — readers must scan THIS, not
# ENGINE_KEY_FIELDS (aio has no chat key field; forgetting it made stored AIO
# answers invisible to the whole product on 2026-08-11)
ALL_ENGINES: tuple[str, ...] = (*ENGINE_KEY_FIELDS, AIO_ENGINE)


@dataclass
class EngineAnswer:
    engine: str
    model: str = ""
    text: str = ""
    citations: list[dict] = field(default_factory=list)  # {url, domain, title}
    latency_ms: int = 0
    error: str | None = None
    via: str = ""  # "native" | "openrouter" | "serpapi" — the measurement surface
    no_aio: bool = False   # Google showed no AI Overview for this query (not an error)
    credits: int = 1       # provider credits this answer cost (page_token AIO = 2)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "model": self.model,
            "text": self.text[:ANSWER_TEXT_CAP],
            "citations": [
                {
                    "url": (c.get("url") or "")[:CITATION_URL_CAP],
                    "domain": c.get("domain", ""),
                    "title": (c.get("title") or "")[:CITATION_TITLE_CAP],
                }
                for c in self.citations[:CITATIONS_CAP]
            ],
            "latency_ms": self.latency_ms,
            "error": self.error,
            "via": self.via,
            "no_aio": self.no_aio,
        }


def engine_key(engine: str) -> str:
    field_name = ENGINE_KEY_FIELDS.get(engine, "")
    return runtime_config.get(field_name) if field_name else ""


def openrouter_key() -> str:
    return runtime_config.get("openrouter_api_key")


def serpapi_key() -> str:
    """Env first; in cloud mode fall back to admin app config (same pattern
    as the Serper key). Offline mode never touches Firestore."""
    key = os.environ.get("SERPAPI_API_KEY", "")
    if key or not state.use_cloud():
        return key
    try:
        from app.services.firestore_repo import get_app_config

        return str(get_app_config().get("serpapi_api_key") or "")
    except Exception:  # noqa: BLE001 — config unreachable = no key, honestly
        return ""


def available_engines() -> dict[str, bool]:
    """Which engines can answer right now: native key, or OpenRouter fallback.
    AIO is SERP data — SerpAPI key only, no chat fallback exists for it."""
    fallback = bool(openrouter_key())
    engines = {engine: bool(engine_key(engine)) or fallback for engine in ENGINE_KEY_FIELDS}
    engines[AIO_ENGINE] = bool(serpapi_key())
    return engines


# what each mode actually measures — shown verbatim in the UI so a chip can
# never imply "we asked the real product" when we asked a proxy of it
MODE_MEANING: dict[str, str] = {
    "native": "the engine's own API — this is the real product",
    "proxy": "a stand-in model routed through OpenRouter, NOT the consumer product",
    "serpapi": "Google's live SERP via SerpAPI — the real consumer surface",
    "off": "no key configured — nothing is measured",
}


def engine_status() -> dict[str, dict]:
    """Per-engine truth for the UI: connected AND *how*.

    ``available_engines`` collapses native and proxy into one boolean, which
    let a green "Perplexity" chip stand for an OpenRouter-routed stand-in.
    This returns the mode and the exact model, so the panel can say which
    surface a number was actually measured on.
    """
    or_key = bool(openrouter_key())
    status: dict[str, dict] = {}
    for engine in ENGINE_KEY_FIELDS:
        if engine_key(engine):
            mode, model = "native", NATIVE_ENGINE_MODELS[engine]
        elif or_key:
            mode, model = "proxy", OPENROUTER_ENGINE_MODELS[engine]
        else:
            mode, model = "off", ""
        status[engine] = {
            "connected": mode != "off",
            "mode": mode,
            "model": model,
            "means": MODE_MEANING[mode],
        }
    aio_on = bool(serpapi_key())
    status[AIO_ENGINE] = {
        "connected": aio_on,
        "mode": "serpapi" if aio_on else "off",
        "model": "google_ai_overview" if aio_on else "",
        "means": MODE_MEANING["serpapi" if aio_on else "off"],
    }
    return status


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


def _aio_flatten(blocks: list) -> list[str]:
    """SerpAPI ai_overview text_blocks → plain lines (paragraphs, lists,
    headings, nested blocks — coded defensively against shape drift)."""
    lines: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("snippet"):
            lines.append(str(block["snippet"]))
        for item in block.get("list") or []:
            if isinstance(item, dict):
                text = " — ".join(filter(None, [item.get("title", ""), item.get("snippet", "")]))
                if text:
                    lines.append(f"- {text}")
        if block.get("text_blocks"):
            lines.extend(_aio_flatten(block["text_blocks"]))
    return lines


def poll_aio(prompt: str, key: str) -> EngineAnswer:
    """One Google AI Overview snapshot via SerpAPI. Two-step when Google
    defers the content behind a page_token (costs a second credit)."""
    started = time.monotonic()
    answer = EngineAnswer(engine=AIO_ENGINE, model="google-ai-overview", via="serpapi")
    try:
        resp = httpx.get(
            SERPAPI_URL,
            params={"engine": "google", "q": prompt, "hl": "en", "gl": "us", "api_key": key},
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        aio = resp.json().get("ai_overview") or {}
        if aio.get("page_token") and not aio.get("text_blocks"):
            resp2 = httpx.get(
                SERPAPI_URL,
                params={"engine": "google_ai_overview", "page_token": aio["page_token"], "api_key": key},
                timeout=REQUEST_TIMEOUT,
            )
            answer.credits = 2
            answer.latency_ms = int((time.monotonic() - started) * 1000)
            if resp2.status_code != 200:
                answer.error = f"HTTP {resp2.status_code} on AIO page fetch: {resp2.text[:150]}"
                return answer
            aio = resp2.json().get("ai_overview") or {}
        lines = _aio_flatten(aio.get("text_blocks") or [])
        if not lines:
            answer.no_aio = True   # honest observation: the AIO slot is empty here
            return answer
        answer.text = "\n".join(lines)
        for ref in aio.get("references") or []:
            if ref.get("link"):
                answer.citations.append(_citation(ref["link"], ref.get("title", "")))
    except Exception as exc:  # noqa: BLE001 — adapters must degrade, never raise
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        answer.error = f"{type(exc).__name__}: {exc}"
    return answer


def poll_openrouter(engine: str, prompt: str, key: str) -> EngineAnswer:
    """One engine-equivalent answer via OpenRouter — mainstream model + web
    search where the model has none of its own. Citations come back as
    url_citation annotations (and Sonar's citations list), passed through."""
    model = OPENROUTER_ENGINE_MODELS[engine]
    started = time.monotonic()
    answer = EngineAnswer(engine=engine, model=model, via="openrouter")
    body: dict = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if engine in _NEEDS_WEB_PLUGIN:
        body["plugins"] = [{"id": "web"}]
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {})
        answer.text = message.get("content", "") or ""
        for note in message.get("annotations") or []:
            cite = note.get("url_citation") or {}
            url = cite.get("url") or (note.get("url") if note.get("type") == "url_citation" else "")
            if url:
                answer.citations.append(_citation(url, cite.get("title", note.get("title", ""))))
        if not answer.citations:
            for url in data.get("citations") or []:  # Sonar passthrough
                if isinstance(url, str) and url:
                    answer.citations.append(_citation(url))
    except Exception as exc:  # noqa: BLE001 — adapters must degrade, never raise
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        answer.error = f"{type(exc).__name__}: {exc}"
    return answer


ENGINES = {
    "perplexity": poll_perplexity,
    "gemini": poll_gemini,
    "chatgpt": poll_openai,
}


def poll_engine(engine: str, prompt: str) -> EngineAnswer:
    """Poll one engine once. Native API when its key exists; OpenRouter as
    fallback when there is no native key OR the native call fails (quota,
    outage). Missing everything → EngineAnswer(error=...), never a raise."""
    if engine == AIO_ENGINE:
        key = serpapi_key()
        if not key:
            return EngineAnswer(engine=engine, error="no API key configured")
        return poll_aio(prompt, key)
    fn = ENGINES.get(engine)
    if fn is None:
        return EngineAnswer(engine=engine, error=f"unknown engine: {engine}")
    native_key = engine_key(engine)
    answer = None
    if native_key:
        answer = fn(prompt, native_key)
        answer.via = "native"
        if not answer.error:
            return answer
    or_key = openrouter_key()
    if or_key:
        return poll_openrouter(engine, prompt, or_key)
    return answer or EngineAnswer(engine=engine, error="no API key configured")
