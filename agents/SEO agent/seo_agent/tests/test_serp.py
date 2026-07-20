from pathlib import Path

import pytest

from seo_agent import serp

FIXTURE = Path(__file__).parent / "fixtures" / "serp_legal_intake.json"


def test_fixture_provider_parses_serpapi_shape():
    result = serp.FixtureProvider(FIXTURE).fetch("legal intake specialist", "United States")
    assert result.keyword == "legal intake specialist"
    assert len(result.entries) == 5
    assert result.entries[0].position == 1
    assert result.entries[0].url == "https://example-legal.com/intake-specialist"
    assert "What does a legal intake specialist do?" in result.paa_questions
    assert "first point of contact" in (result.ai_overview or "")
    assert result.ai_overview_sources == ["https://example-legal.com/intake-specialist"]


def test_get_provider_without_key_raises(monkeypatch):
    monkeypatch.setattr(serp, "_serpapi_key", lambda: "")
    with pytest.raises(RuntimeError, match="SerpAPI key"):
        serp.get_provider()


def test_serpapi_provider_builds_request(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            import json
            return json.loads(FIXTURE.read_text(encoding="utf-8"))
        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(serp, "_serpapi_key", lambda: "test-key")
    provider = serp.SerpApiProvider(client=FakeClient())
    result = provider.fetch("legal intake specialist", "United States")
    assert captured["url"] == "https://serpapi.com/search"
    assert captured["params"]["q"] == "legal intake specialist"
    assert captured["params"]["location"] == "United States"
    assert captured["params"]["api_key"] == "test-key"
    assert len(result.entries) == 5
