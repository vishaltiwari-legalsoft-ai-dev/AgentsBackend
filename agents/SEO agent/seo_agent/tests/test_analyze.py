from pathlib import Path

import pytest

from seo_agent import store
from seo_agent.analyze import AnalysisError, run_analysis
from seo_agent.serp import FixtureProvider

FIXTURE = Path(__file__).parent / "fixtures" / "serp_legal_intake.json"
HTML = (Path(__file__).parent / "fixtures" / "page_intake_guide.html").read_text(encoding="utf-8")


class FakeLLM:
    def invoke(self, prompt):
        class R:
            content = ('{"topics": [{"name": "Role", "terms": ["intake"], "questions": []}],'
                       ' "questions": ["What does one do?"]}')
        return R()


def _fetcher(url: str) -> str:
    return HTML


def test_full_analysis_builds_and_persists_benchmark(monkeypatch):
    # fixture has 5 results, 2 blocklisted (reddit, youtube) → 3 pages; lower min_pages
    monkeypatch.setattr("seo_agent.analyze._cfg", lambda: {
        **__import__("seo_agent.config", fromlist=["DEFAULTS"]).DEFAULTS, "min_pages": 3,
    })
    events: list[str] = []
    benchmark = run_analysis(
        "legal intake specialist", "United States", "legalsoft",
        provider=FixtureProvider(FIXTURE), fetcher=_fetcher, llm=FakeLLM(),
        progress=events.append,
    )
    assert benchmark.keyword == "legal intake specialist"
    assert len(benchmark.source_pages) == 3
    assert any(d["reason"].startswith("blocklist:") for d in benchmark.excluded)
    assert benchmark.term_targets and benchmark.topics_ai is True
    assert benchmark.word_count_range[1] >= benchmark.word_count_range[0] > 0
    assert store.get_benchmark(benchmark.id) is not None      # persisted
    assert any("SERP" in e or "Crawl" in e for e in events)   # progress emitted


def test_too_few_pages_fails_with_reason():
    def broken_fetcher(url: str) -> str:
        raise RuntimeError("timeout")

    with pytest.raises(AnalysisError, match=r"only 0 of .* pages"):
        run_analysis("kw", "US", "b", provider=FixtureProvider(FIXTURE), fetcher=broken_fetcher)
