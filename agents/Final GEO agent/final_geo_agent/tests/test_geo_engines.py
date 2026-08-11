"""GEO engine adapters — response-shape parsing against recorded fixtures.

httpx is stubbed at the module seam; no network, no keys."""
import httpx
import pytest

from final_geo_agent import geo_engines


class FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or "err"

    def json(self):
        return self._payload


def patch_post(monkeypatch, resp: FakeResp):
    monkeypatch.setattr(geo_engines.httpx, "post", lambda *a, **k: resp)


PERPLEXITY_OK = {
    "choices": [{"message": {"content": "Legal Soft is a top provider."}}],
    "search_results": [
        {"url": "https://www.g2.com/products/x", "title": "G2 reviews"},
        {"url": "https://reddit.com/r/law/1", "title": "Reddit thread"},
    ],
}

GEMINI_OK = {
    "candidates": [{
        "content": {"parts": [{"text": "Here are the best options."}, {"text": "More detail."}]},
        "groundingMetadata": {
            "groundingChunks": [
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                         "title": "clio.com"}},
                {"web": {"uri": "https://example.org/page", "title": "An article title"}},
            ]
        },
    }]
}

OPENAI_OK = {
    "output": [
        {"content": [{"text": "ChatGPT-style answer.",
                      "annotations": [{"type": "url_citation",
                                       "url": "https://capterra.com/p/1", "title": "Capterra"}]}]},
    ]
}


def test_perplexity_parses_text_and_search_results(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, PERPLEXITY_OK))
    ans = geo_engines.poll_perplexity("best legal va", "pplx-test")
    assert ans.error is None
    assert "Legal Soft" in ans.text
    assert [c["domain"] for c in ans.citations] == ["g2.com", "reddit.com"]


def test_perplexity_falls_back_to_bare_citations(monkeypatch):
    payload = {"choices": [{"message": {"content": "hi"}}],
               "citations": ["https://www.example.com/a"]}
    patch_post(monkeypatch, FakeResp(200, payload))
    ans = geo_engines.poll_perplexity("q", "pplx-test")
    assert ans.citations == [{"url": "https://www.example.com/a",
                              "domain": "example.com", "title": ""}]


def test_gemini_joins_parts_and_trusts_domain_titles(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, GEMINI_OK))
    ans = geo_engines.poll_gemini("q", "AIza-test")
    assert ans.text == "Here are the best options.\nMore detail."
    assert ans.citations[0]["domain"] == "clio.com"          # title looked like a domain
    assert ans.citations[1]["domain"] == "example.org"       # fell back to the uri


def test_openai_collects_text_and_url_citations(monkeypatch):
    patch_post(monkeypatch, FakeResp(200, OPENAI_OK))
    ans = geo_engines.poll_openai("q", "sk-test")
    assert ans.text == "ChatGPT-style answer."
    assert ans.citations == [{"url": "https://capterra.com/p/1",
                              "domain": "capterra.com", "title": "Capterra"}]


def test_http_error_is_captured_not_raised(monkeypatch):
    patch_post(monkeypatch, FakeResp(401, text="unauthorized"))
    ans = geo_engines.poll_perplexity("q", "pplx-bad")
    assert ans.error and "HTTP 401" in ans.error
    assert ans.text == ""


def test_transport_exception_is_captured(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectTimeout("timeout")
    monkeypatch.setattr(geo_engines.httpx, "post", boom)
    ans = geo_engines.poll_gemini("q", "k")
    assert ans.error and "ConnectTimeout" in ans.error


def test_poll_engine_without_any_key_reports_unavailable(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    ans = geo_engines.poll_engine("perplexity", "q")
    assert ans.error == "no API key configured"


OPENROUTER_OK = {
    "choices": [{"message": {
        "content": "Legal Soft leads this space.",
        "annotations": [
            {"type": "url_citation",
             "url_citation": {"url": "https://clutch.co/profile/x", "title": "Clutch"}},
        ],
    }}],
}


def test_openrouter_fallback_when_no_native_key(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    patch_post(monkeypatch, FakeResp(200, OPENROUTER_OK))
    ans = geo_engines.poll_engine("chatgpt", "best legal va")
    assert ans.error is None and ans.via == "openrouter"
    assert ans.model == geo_engines.OPENROUTER_ENGINE_MODELS["chatgpt"]
    assert [c["domain"] for c in ans.citations] == ["clutch.co"]


def test_native_quota_error_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "native-key")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "openrouter" in url:
            return FakeResp(200, OPENROUTER_OK)
        return FakeResp(429, text="quota exceeded")

    monkeypatch.setattr(geo_engines.httpx, "post", post)
    ans = geo_engines.poll_engine("gemini", "q")
    assert ans.via == "openrouter" and ans.error is None
    assert len(calls) == 2                       # native tried first, then fallback


def test_available_engines_lights_up_with_openrouter_only(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "or-key")
    monkeypatch.setattr(geo_engines, "serpapi_key", lambda: "")
    engines = geo_engines.available_engines()
    assert engines["perplexity"] and engines["gemini"] and engines["chatgpt"]
    assert engines["aio"] is False               # AIO needs SerpAPI, chat fallback can't fake a SERP


def test_poll_engine_unknown_engine():
    ans = geo_engines.poll_engine("bing", "q")
    assert ans.error == "unknown engine: bing"


def test_answer_text_capped():
    ans = geo_engines.EngineAnswer(engine="perplexity", text="x" * 10000)
    assert len(ans.to_dict()["text"]) == geo_engines.ANSWER_TEXT_CAP


def test_to_dict_caps_citations_and_lengths():
    ans = geo_engines.EngineAnswer(
        engine="gemini", text="x" * 9000,
        citations=[{"url": "https://vertexaisearch.example/" + "r" * 900,
                    "domain": f"site{i}.com", "title": "t" * 500} for i in range(30)],
    )
    d = ans.to_dict()
    assert len(d["text"]) == geo_engines.ANSWER_TEXT_CAP
    assert len(d["citations"]) == geo_engines.CITATIONS_CAP        # 1MB day-doc guard
    assert len(d["citations"][0]["url"]) == geo_engines.CITATION_URL_CAP
    assert len(d["citations"][0]["title"]) == geo_engines.CITATION_TITLE_CAP


# ------------------------------------------------------- Google AIO engine ----

AIO_OK = {
    "ai_overview": {
        "text_blocks": [
            {"type": "paragraph", "snippet": "Legal Soft and Ruby lead the intake space."},
            {"type": "list", "list": [
                {"title": "Answer calls 24/7", "snippet": "after-hours coverage"},
            ]},
        ],
        "references": [
            {"title": "Lawyerist review", "link": "https://lawyerist.com/reviews/x"},
        ],
    }
}


def patch_get(monkeypatch, resp):
    monkeypatch.setattr(geo_engines.httpx, "get", lambda *a, **k: resp)


def test_poll_aio_parses_blocks_and_references(monkeypatch):
    patch_get(monkeypatch, FakeResp(200, AIO_OK))
    ans = geo_engines.poll_aio("best legal intake", "serp-key")
    assert ans.error is None and not ans.no_aio and ans.via == "serpapi"
    assert "Legal Soft and Ruby" in ans.text and "Answer calls 24/7" in ans.text
    assert ans.citations[0]["domain"] == "lawyerist.com"
    assert ans.credits == 1


def test_poll_aio_no_overview_is_completed_not_error(monkeypatch):
    patch_get(monkeypatch, FakeResp(200, {"organic": []}))
    ans = geo_engines.poll_aio("some query", "serp-key")
    assert ans.no_aio is True and ans.error is None
    assert ans.to_dict()["no_aio"] is True


def test_poll_aio_page_token_second_call_costs_two(monkeypatch):
    calls = []

    def get(url, params=None, **kw):
        calls.append(params.get("engine"))
        if params.get("engine") == "google":
            return FakeResp(200, {"ai_overview": {"page_token": "tok123"}})
        return FakeResp(200, AIO_OK)

    monkeypatch.setattr(geo_engines.httpx, "get", get)
    ans = geo_engines.poll_aio("q", "serp-key")
    assert calls == ["google", "google_ai_overview"]
    assert ans.credits == 2 and "Legal Soft" in ans.text


def test_available_engines_includes_aio_with_serpapi_key(monkeypatch):
    monkeypatch.setattr(geo_engines, "engine_key", lambda e: "")
    monkeypatch.setattr(geo_engines, "openrouter_key", lambda: "")
    monkeypatch.setattr(geo_engines, "serpapi_key", lambda: "serp-key")
    engines = geo_engines.available_engines()
    assert engines["aio"] is True
    assert engines["perplexity"] is False
