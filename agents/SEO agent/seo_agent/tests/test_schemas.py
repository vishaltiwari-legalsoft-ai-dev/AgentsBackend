from seo_agent import config as seo_config
from seo_agent.schemas import (
    Benchmark, GeoAnswer, GeoRun, PageDoc, ScoreReport, SerpEntry, SerpResult,
    TermTarget, Topic,
)


def test_benchmark_roundtrip():
    b = Benchmark(
        id="abc123", keyword="legal intake specialist", location="United States",
        brand="legalsoft", created_at="2026-07-20T00:00:00Z",
        serp_fetched_at="2026-07-20T00:00:00Z",
        term_targets=[TermTarget(term="intake", weight=1.0, min_count=2, max_count=4)],
        topics=[Topic(name="Hiring", terms=["salary"], questions=["What does it cost?"])],
        questions=["What is a legal intake specialist?"],
        word_count_range=[1800, 2400], heading_count_range=[8, 14],
        paa_questions=["What does an intake specialist do?"],
        source_pages=[{"url": "https://x.com/a", "rank": 1, "word_count": 2100}],
        excluded=[{"url": "https://reddit.com/r/law", "reason": "blocklist:reddit.com"}],
        topics_ai=True, topics_fallback_reason=None,
    )
    assert Benchmark.model_validate(b.model_dump()) == b


def test_score_report_shape():
    r = ScoreReport(
        score=72.5, term_coverage=0.8, topical_completeness=0.6,
        structure_fit=0.7, semantic_depth=0.5,
        missing_terms=[{"term": "intake", "used": 0, "min_count": 2, "max_count": 4}],
        questions_unanswered=["What does it cost?"],
        structure_notes=["Add 2-4 more headings (have 6, target 8-14)"],
    )
    assert 0 <= r.score <= 100


def test_geo_run_roundtrip():
    run = GeoRun(
        id="g1", brand="legalsoft", week="2026-W30",
        captured_at="2026-07-20T00:00:00Z",
        answers=[GeoAnswer(
            engine="perplexity", question="best legal intake outsourcing companies",
            answer_text="LegalSoft is a provider...", citations=["https://legalsoft.com/x"],
            mentioned=True, cited=True, accuracy=1.0, accuracy_notes=[], error=None,
        )],
        score=8.2,
        components={"mention": 1.0, "citation": 1.0, "accuracy": 1.0, "sov": 0.4},
        engine_scores={"perplexity": 8.2},
        no_data_engines=["gemini"],
    )
    assert GeoRun.model_validate(run.model_dump()) == run


def test_effective_config_merges_overrides():
    cfg = seo_config.effective_config({"w_term_coverage": 0.5})
    assert cfg["w_term_coverage"] == 0.5
    assert cfg["w_topics"] == seo_config.DEFAULTS["w_topics"]  # untouched
    # weights and thresholds all present
    for key in ("w_term_coverage", "w_topics", "w_structure", "w_depth",
                "stuffing_zero_multiple", "min_pages", "top3_weight",
                "g_mention", "g_citation", "g_accuracy", "g_sov",
                "geo_engines", "serp_top_n"):
        assert key in cfg
