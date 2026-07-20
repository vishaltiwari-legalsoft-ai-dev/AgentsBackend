"""Ready-to-use content brief derived from a Benchmark — the writer-facing
checklist the editor and the /briefs endpoint both serve."""

from __future__ import annotations

from seo_agent.schemas import Benchmark


def build_brief(benchmark: Benchmark) -> dict:
    return {
        "keyword": benchmark.keyword,
        "location": benchmark.location,
        "brand": benchmark.brand,
        "serp_fetched_at": benchmark.serp_fetched_at,
        "structure": {
            "word_count_range": benchmark.word_count_range,
            "heading_count_range": benchmark.heading_count_range,
            "include_faq": bool(benchmark.paa_questions),
        },
        "sections": [
            {"name": t.name, "terms": t.terms, "questions": t.questions}
            for t in benchmark.topics
        ],
        "questions": benchmark.questions,
        "terms": [
            {"term": t.term, "min_count": t.min_count, "max_count": t.max_count}
            for t in benchmark.term_targets
        ],
        "topics_ai": benchmark.topics_ai,
        "topics_fallback_reason": benchmark.topics_fallback_reason,
    }
