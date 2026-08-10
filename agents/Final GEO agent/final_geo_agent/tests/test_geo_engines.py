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
    assert all(geo_engines.available_engines().values())


def test_poll_engine_unknown_engine():
    ans = geo_engines.poll_engine("bing", "q")
    assert ans.error == "unknown engine: bing"


def test_answer_text_capped():
    ans = geo_engines.EngineAnswer(engine="perplexity", text="x" * 10000)
    assert len(ans.to_dict()["text"]) == geo_engines.ANSWER_TEXT_CAP
