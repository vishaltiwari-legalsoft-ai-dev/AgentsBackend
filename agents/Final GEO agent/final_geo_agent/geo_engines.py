"""GEO agent — AI answer-engine adapters.

Each adapter asks one engine one prompt and returns an :class:`EngineAnswer`
with the answer text and the engine's OWN citation metadata. This is why we
call the providers directly instead of OpenRouter: citations (Perplexity
``search_results``, Gemini ``groundingMetadata``, OpenAI ``url_citation``
annotations) are only exposed by the native APIs.

Two kinds of engine live behind one interface, :class:`EngineSpec`:

* **chat** engines (Perplexity, Gemini, ChatGPT) are sampled — the same prompt
  asked several times, native key first, OpenRouter stand-in as fallback;
* **serp** engines (Google AI Overview, Google AI Mode) are snapshots of the
  live Google surface, one call per prompt, billed per call, with a vendor
  seam *below* the engine id: DataForSEO when its credentials exist, SerpAPI
  for AI Overview when only that key exists. The engine id is what the product
  measures; the vendor is how it was fetched, disclosed on ``via``.

Adapters never raise — any HTTP/parse failure comes back as
``EngineAnswer(error=...)`` so one bad engine degrades instead of killing a
poll sweep. Keys resolve through ``runtime_config`` (Settings → Secrets
override, else env); a missing key just makes the engine unavailable.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Callable

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

# engine id -> runtime_config/settings field holding its key. Chat engines
# only: the SERP engines are keyed per vendor (``serp_vendor``), not per engine.
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

# The SERP engines: the only ones here measuring the real consumer Google
# surface. SERP content is a snapshot with low variance and every call is
# billed, so they run once per prompt under the joint monthly guard in
# geo_poll. "No AI Overview shown" is a completed observation, never an error.
AIO_ENGINE = "aio"
AI_MODE_ENGINE = "ai_mode"
SERPAPI_URL = "https://serpapi.com/search.json"
DATAFORSEO_URL = "https://api.dataforseo.com/v3"
DATAFORSEO_LOCATION_CODE = 2840  # United States
DATAFORSEO_LANGUAGE_CODE = "en"
# Which prompts a billed SERP call is spent on. Brand-intent prompts ("is Legal
# Soft good for personal injury firms") are the weakest use of one: the buyer
# already has the name. Discovery happens on category and problem questions.
SERP_INTENTS: tuple[str, ...] = ("category", "problem")
# Sample size for a chat engine — the caller's default, which the poll request
# may override; a SERP engine is pinned to its own spec.
CHAT_RUNS_PER_PROMPT = 3
SERP_RUNS_PER_PROMPT = 1


@dataclass
class EngineAnswer:
    engine: str
    model: str = ""
    text: str = ""
    citations: list[dict] = field(default_factory=list)  # {url, domain, title}
    latency_ms: int = 0
    error: str | None = None
    via: str = ""  # "native" | "openrouter" | "serpapi" | "dataforseo" — the measurement surface
    no_aio: bool = False   # Google showed no AI answer for this query (not an error)
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


@dataclass(frozen=True)
class EngineSpec:
    """One engine as the poll planner and the status panel see it.

    ``poll`` and ``status`` are the whole behavioural surface: the planner
    never asks *which* engine it is holding, only what the spec says about
    it. ``intents`` of ``None`` means every prompt; ``runs_per_prompt`` is the
    sample size a SERP engine is pinned to and the default a chat engine
    offers the caller.
    """

    id: str
    label: str
    kind: str                             # "chat" | "serp"
    runs_per_prompt: int
    intents: tuple[str, ...] | None
    poll: Callable[[str], EngineAnswer]
    status: Callable[[], dict]           # {"connected", "mode", "model", "means"}


def engine_key(engine: str) -> str:
    field_name = ENGINE_KEY_FIELDS.get(engine, "")
    return runtime_config.get(field_name) if field_name else ""


def openrouter_key() -> str:
    return runtime_config.get("openrouter_api_key")


def _cloud_app_config() -> dict:
    """Admin app config, only ever read in cloud mode; offline never touches
    Firestore, and an unreachable config is honestly ``{}``."""
    if not state.use_cloud():
        return {}
    try:
        from app.services.firestore_repo import get_app_config

        return get_app_config() or {}
    except Exception:  # noqa: BLE001 — config unreachable = no key, honestly
        return {}


def serpapi_key() -> str:
    """Env first; in cloud mode fall back to admin app config (same pattern
    as the Serper key)."""
    key = os.environ.get("SERPAPI_API_KEY", "")
    if key:
        return key
    return str(_cloud_app_config().get("serpapi_api_key") or "")


def dataforseo_creds() -> tuple[str, str]:
    """``(login, password)`` for DataForSEO, or ``("", "")`` when either half
    is missing — a login without a password is not half a credential."""
    login = os.environ.get("DATAFORSEO_LOGIN", "")
    password = os.environ.get("DATAFORSEO_PASSWORD", "")
    if not (login and password):
        cfg = _cloud_app_config()
        login = login or str(cfg.get("dataforseo_login") or "")
        password = password or str(cfg.get("dataforseo_password") or "")
    return (login, password) if login and password else ("", "")


# what each mode actually measures — shown verbatim in the UI so a chip can
# never imply "we asked the real product" when we asked a proxy of it
MODE_MEANING: dict[str, str] = {
    "native": "the engine's own official API",
    "proxy": "measured with a similar model through OpenRouter — tracks the official engine closely; add the engine's own key for exact readings",
    "serpapi": "Google's live SERP via SerpAPI — the real consumer surface",
    "dataforseo": "Google's live SERP via DataForSEO — the real consumer surface",
    "off": "no key configured — nothing is measured",
}


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


# DataForSEO: engine id -> (endpoint under /serp/google, task body). Both
# endpoints answer in the same envelope and both put the AI answer in an item
# whose ``type`` is the literal "ai_overview" — the AI Mode endpoint included.
_DATAFORSEO_MODELS: dict[str, str] = {
    AIO_ENGINE: "google-ai-overview",
    AI_MODE_ENGINE: "google-ai-mode",
}


def _dataforseo_task(engine: str, prompt: str) -> tuple[str, dict]:
    task = {
        "keyword": prompt,
        "location_code": DATAFORSEO_LOCATION_CODE,
        "language_code": DATAFORSEO_LANGUAGE_CODE,
    }
    if engine == AI_MODE_ENGINE:
        return f"{DATAFORSEO_URL}/serp/google/ai_mode/live/advanced", task
    return f"{DATAFORSEO_URL}/serp/google/organic/live/advanced", task | {"device": "desktop"}


def _dataforseo_reference(ref: dict) -> dict | None:
    """A DataForSEO reference → citation. The vendor's ``domain`` is trusted
    over one parsed from the URL, because the URL may be a redirect."""
    url = ref.get("url") or ref.get("link") or (
        f"https://{ref['domain']}" if ref.get("domain") else ""
    )
    if not url:
        return None
    cit = _citation(url, ref.get("title", ""))
    if ref.get("domain"):
        cit["domain"] = str(ref["domain"]).lower().removeprefix("www.")
    return cit


def poll_dataforseo(engine: str, prompt: str, login: str, password: str) -> EngineAnswer:
    """One live Google snapshot via DataForSEO — AI Overview off the organic
    SERP, or the AI Mode answer.

    Google sometimes defers the overview: the item is present with
    ``asynchronous_ai_overview: true`` and an empty ``markdown``. That is the
    same observation as no overview at all — the slot was empty when we looked
    — and is reported as ``no_aio``, never as an empty answer with zero
    citations and never as an error.
    """
    url, task = _dataforseo_task(engine, prompt)
    started = time.monotonic()
    answer = EngineAnswer(engine=engine, model=_DATAFORSEO_MODELS[engine], via="dataforseo")
    try:
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        resp = httpx.post(
            url,
            headers={"Authorization": f"Basic {token}"},
            json=[task],
            timeout=REQUEST_TIMEOUT,
        )
        answer.latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            answer.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return answer
        data = resp.json() or {}
        if data.get("status_code") != 20000:
            answer.error = f"DataForSEO {data.get('status_code')}: {data.get('status_message', '')}"
            return answer
        job = (data.get("tasks") or [{}])[0] or {}
        if job.get("status_code") != 20000:
            answer.error = f"DataForSEO {job.get('status_code')}: {job.get('status_message', '')}"
            return answer
        result = (job.get("result") or [{}])[0] or {}
        block = next(
            (i for i in result.get("items") or [] if isinstance(i, dict) and i.get("type") == "ai_overview"),
            None,
        )
        markdown = str((block or {}).get("markdown") or "").strip()
        if not markdown:
            answer.no_aio = True
            return answer
        answer.text = markdown
        for ref in (block or {}).get("references") or []:
            if isinstance(ref, dict) and (cit := _dataforseo_reference(ref)):
                answer.citations.append(cit)
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


NATIVE_POLLERS: dict[str, Callable[[str, str], EngineAnswer]] = {
    "perplexity": poll_perplexity,
    "gemini": poll_gemini,
    "chatgpt": poll_openai,
}


# ------------------------------------------------------------- the specs ----

def _poll_chat(engine: str, prompt: str) -> EngineAnswer:
    """Native API when its key exists; OpenRouter as fallback when there is no
    native key OR the native call fails (quota, outage). Missing everything →
    EngineAnswer(error=...), never a raise."""
    native_key = engine_key(engine)
    answer = None
    if native_key:
        answer = NATIVE_POLLERS[engine](prompt, native_key)
        answer.via = "native"
        if not answer.error:
            return answer
    or_key = openrouter_key()
    if or_key:
        return poll_openrouter(engine, prompt, or_key)
    return answer or EngineAnswer(engine=engine, error="no API key configured")


def _chat_status(engine: str) -> dict:
    if engine_key(engine):
        mode, model = "native", NATIVE_ENGINE_MODELS[engine]
    elif openrouter_key():
        mode, model = "proxy", OPENROUTER_ENGINE_MODELS[engine]
    else:
        mode, model = "off", ""
    return {
        "connected": mode != "off",
        "mode": mode,
        "model": model,
        "means": MODE_MEANING[mode],
    }


def serp_vendor(engine: str) -> str:
    """Which vendor fetches this SERP engine right now: ``"dataforseo"`` |
    ``"serpapi"`` | ``"off"``. DataForSEO serves both engines and wins when
    configured; SerpAPI has no AI Mode endpoint, so it only ever backs AIO."""
    if dataforseo_creds() != ("", ""):
        return "dataforseo"
    if engine == AIO_ENGINE and serpapi_key():
        return "serpapi"
    return "off"


def _poll_serp(engine: str, prompt: str) -> EngineAnswer:
    vendor = serp_vendor(engine)
    if vendor == "dataforseo":
        login, password = dataforseo_creds()
        return poll_dataforseo(engine, prompt, login, password)
    if vendor == "serpapi":
        return poll_aio(prompt, serpapi_key())
    return EngineAnswer(engine=engine, error="no API key configured")


def _serp_status(engine: str) -> dict:
    vendor = serp_vendor(engine)
    return {
        "connected": vendor != "off",
        "mode": vendor,
        "model": _DATAFORSEO_MODELS[engine] if vendor != "off" else "",
        "means": MODE_MEANING[vendor],
    }


def _chat_spec(engine: str, label: str) -> EngineSpec:
    return EngineSpec(
        id=engine, label=label, kind="chat", runs_per_prompt=CHAT_RUNS_PER_PROMPT,
        intents=None, poll=partial(_poll_chat, engine), status=partial(_chat_status, engine),
    )


def _serp_spec(engine: str, label: str) -> EngineSpec:
    return EngineSpec(
        id=engine, label=label, kind="serp", runs_per_prompt=SERP_RUNS_PER_PROMPT,
        intents=SERP_INTENTS, poll=partial(_poll_serp, engine), status=partial(_serp_status, engine),
    )


# Order is the order the panel lists engines in and the order the poll planner
# interleaves them.
ENGINE_SPECS: dict[str, EngineSpec] = {
    spec.id: spec
    for spec in (
        _chat_spec("perplexity", "Perplexity"),
        _chat_spec("gemini", "Gemini"),
        _chat_spec("chatgpt", "ChatGPT"),
        _serp_spec(AIO_ENGINE, "Google AI Overview"),
        _serp_spec(AI_MODE_ENGINE, "Google AI Mode"),
    )
}

# every engine whose poll docs exist on disk — readers must scan THIS, not
# ENGINE_KEY_FIELDS (the SERP engines have no chat key field; forgetting aio
# made stored AIO answers invisible to the whole product on 2026-08-11)
ALL_ENGINES: tuple[str, ...] = tuple(ENGINE_SPECS)
SERP_ENGINES: tuple[str, ...] = tuple(e for e, s in ENGINE_SPECS.items() if s.kind == "serp")
ENGINE_LABELS: dict[str, str] = {e: s.label for e, s in ENGINE_SPECS.items()}


def engine_status() -> dict[str, dict]:
    """Per-engine truth for the UI: connected AND *how*.

    ``available_engines`` collapses native and proxy into one boolean, which
    let a green "Perplexity" chip stand for an OpenRouter-routed stand-in.
    This returns the mode and the exact model, so the panel can say which
    surface a number was actually measured on.
    """
    return {engine: spec.status() for engine, spec in ENGINE_SPECS.items()}


def available_engines() -> dict[str, bool]:
    """Which engines can answer right now: native key, OpenRouter fallback,
    or a SERP vendor. A chat fallback cannot fake a SERP."""
    return {engine: status["connected"] for engine, status in engine_status().items()}


def poll_engine(engine: str, prompt: str) -> EngineAnswer:
    """Poll one engine once, through its spec. Unknown id →
    EngineAnswer(error=...), never a raise."""
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        return EngineAnswer(engine=engine, error=f"unknown engine: {engine}")
    return spec.poll(prompt)
