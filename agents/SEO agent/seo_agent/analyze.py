"""Phase-1 orchestrator: keyword → SERP → filter → crawl → terms → topics →
persisted Benchmark. Fails loudly with the reason — no silent fallbacks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from seo_agent import store
from seo_agent.config import effective_config
from seo_agent.crawl import crawl_pages
from seo_agent.filters import filter_entries
from seo_agent.schemas import Benchmark
from seo_agent.serp import get_provider
from seo_agent.terms import build_term_targets, structure_ranges
from seo_agent.topics import group_topics


class AnalysisError(RuntimeError):
    """Raised when a benchmark cannot be honestly built; message says why."""


def _cfg() -> dict:
    return effective_config(store.load_config())


def run_analysis(
    keyword: str,
    location: str,
    brand: str,
    provider=None,
    fetcher=None,
    llm=None,
    progress: Callable[[str], None] | None = None,
) -> Benchmark:
    emit = progress or (lambda msg: None)
    cfg = _cfg()

    emit("Fetching live SERP…")
    try:
        serp_result = (provider or get_provider()).fetch(keyword, location)
    except Exception as exc:
        raise AnalysisError(f"SERP fetch failed: {exc}") from exc
    if not serp_result.entries:
        raise AnalysisError("SERP returned no organic results for this keyword")

    emit("Filtering non-comparable results…")
    kept, excluded = filter_entries(serp_result.entries, top_n=int(cfg["serp_top_n"]))

    emit(f"Crawling {len(kept)} ranking pages…")
    pages, failures = crawl_pages(kept, fetcher=fetcher)
    excluded = excluded + failures
    min_pages = int(cfg["min_pages"])
    if len(pages) < min_pages:
        raise AnalysisError(
            f"Crawl produced only {len(pages)} of {min_pages} required pages — "
            + "; ".join(d["reason"] for d in failures[:3])
        )

    emit("Extracting term statistics…")
    term_targets = build_term_targets(pages, keyword, cfg)
    ranges = structure_ranges(pages)

    emit("Grouping topics (AI pass)…")
    topics, questions, ai, fallback_reason = group_topics(
        term_targets, serp_result.paa_questions, keyword, llm=llm
    )

    benchmark = Benchmark(
        id=store.new_id(), keyword=keyword, location=location, brand=brand,
        created_at=datetime.now(timezone.utc).isoformat(),
        serp_fetched_at=serp_result.fetched_at,
        term_targets=term_targets, topics=topics, questions=questions,
        word_count_range=ranges["word_count_range"],
        heading_count_range=ranges["heading_count_range"],
        paa_questions=serp_result.paa_questions,
        source_pages=[
            {"url": p.url, "rank": p.rank, "word_count": p.word_count} for p in pages
        ],
        excluded=excluded, topics_ai=ai, topics_fallback_reason=fallback_reason,
    )
    store.save_benchmark(benchmark.model_dump())
    emit("Benchmark ready.")
    return benchmark
