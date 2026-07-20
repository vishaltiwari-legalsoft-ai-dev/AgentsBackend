"""Pydantic models shared across the SEO + GEO agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SerpEntry(BaseModel):
    url: str
    title: str = ""
    position: int
    snippet: str = ""


class SerpResult(BaseModel):
    keyword: str
    location: str = ""
    fetched_at: str  # ISO timestamp
    entries: list[SerpEntry] = Field(default_factory=list)
    paa_questions: list[str] = Field(default_factory=list)
    ai_overview: str | None = None
    ai_overview_sources: list[str] = Field(default_factory=list)


class PageDoc(BaseModel):
    url: str
    rank: int
    title: str = ""
    body_text: str = ""
    headings: list[str] = Field(default_factory=list)
    word_count: int = 0
    faq_texts: list[str] = Field(default_factory=list)


class TermTarget(BaseModel):
    term: str
    weight: float = 1.0          # position-weighted importance
    min_count: int = 1           # target frequency range in the draft
    max_count: int = 3


class Topic(BaseModel):
    name: str
    terms: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class Benchmark(BaseModel):
    id: str
    keyword: str
    location: str = ""
    brand: str = ""
    created_at: str
    serp_fetched_at: str
    term_targets: list[TermTarget] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    word_count_range: list[int] = Field(default_factory=lambda: [0, 0])   # [lo, hi]
    heading_count_range: list[int] = Field(default_factory=lambda: [0, 0])
    paa_questions: list[str] = Field(default_factory=list)
    source_pages: list[dict] = Field(default_factory=list)  # {url, rank, word_count}
    excluded: list[dict] = Field(default_factory=list)      # {url, reason}
    topics_ai: bool = False               # honesty flag: True only if the LLM pass ran
    topics_fallback_reason: str | None = None


class TermReportRow(BaseModel):
    term: str
    weight: float = 1.0
    min_count: int = 1
    max_count: int = 3
    used: int = 0
    status: str = "missing"  # "missing" | "low" | "ok" | "overused"


class TopicCoverage(BaseModel):
    name: str
    terms_present: list[str] = Field(default_factory=list)
    terms_missing: list[str] = Field(default_factory=list)
    questions_unanswered: list[str] = Field(default_factory=list)


class StructureStatus(BaseModel):
    word_count: int = 0
    word_count_range: list[int] = Field(default_factory=lambda: [0, 0])
    heading_count: int = 0
    heading_count_range: list[int] = Field(default_factory=lambda: [0, 0])
    faq_needed: bool = False
    faq_present: bool = False


class ScoreReport(BaseModel):
    score: float                      # 0-100
    term_coverage: float              # each subscore 0-1
    topical_completeness: float
    structure_fit: float
    semantic_depth: float
    missing_terms: list[dict] = Field(default_factory=list)  # {term, used, min_count, max_count}
    questions_unanswered: list[str] = Field(default_factory=list)
    structure_notes: list[str] = Field(default_factory=list)
    term_report: list[TermReportRow] = Field(default_factory=list)   # every target, weight desc
    topic_coverage: list[TopicCoverage] = Field(default_factory=list)
    structure: StructureStatus | None = None


class GeoAnswer(BaseModel):
    engine: str                       # "gpt" | "gemini" | "perplexity" | "ai_overview"
    question: str
    answer_text: str = ""
    citations: list[str] = Field(default_factory=list)
    mentioned: bool = False
    cited: bool = False
    accuracy: float | None = None     # 0-1; None = not evaluated (e.g. not mentioned)
    accuracy_notes: list[str] = Field(default_factory=list)
    error: str | None = None          # set when the engine call failed


class GeoRun(BaseModel):
    id: str
    brand: str
    week: str                         # ISO week, e.g. "2026-W30"
    captured_at: str
    answers: list[GeoAnswer] = Field(default_factory=list)
    score: float = 0.0                # 0-10, averaged over engines WITH data
    components: dict = Field(default_factory=dict)     # {mention, citation, accuracy, sov} 0-1
    engine_scores: dict = Field(default_factory=dict)  # engine -> 0-10
    no_data_engines: list[str] = Field(default_factory=list)
