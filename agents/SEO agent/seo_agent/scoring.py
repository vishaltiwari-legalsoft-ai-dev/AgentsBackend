"""The live scoring hot path: pure function over (draft, Benchmark, cfg).

NO I/O, NO LLM, NO network — this is what makes per-keystroke scoring viable.
Weights and tapers come from cfg (seo_agent.config), nothing hardcoded here.
"""

from __future__ import annotations

import re
from collections import Counter

from seo_agent.schemas import Benchmark, ScoreReport
from seo_agent.terms import tokenize

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", flags=re.MULTILINE)


def _term_credit(used: int, lo: int, hi: int, zero_multiple: float) -> float:
    """1.0 inside [lo, hi]; partial below; tapers to 0 at hi*zero_multiple."""
    if used <= 0:
        return 0.0
    if used < lo:
        return used / lo
    if used <= hi:
        return 1.0
    ceiling = hi * zero_multiple
    if used >= ceiling:
        return 0.0
    return 1.0 - (used - hi) / (ceiling - hi)


def _range_credit(value: int, lo: int, hi: int) -> float:
    """1.0 inside [lo, hi]; linear taper to 0 at half/double the range edge."""
    if lo <= value <= hi:
        return 1.0
    if value < lo:
        return max(0.0, value / lo) if lo else 1.0
    return max(0.0, 1.0 - (value - hi) / max(hi, 1))


def _term_count(term: str, counts: Counter, joined: str) -> int:
    """Occurrences of a target term in the draft. Bigram targets (contain a
    space) are counted as phrases in the space-padded token stream."""
    if " " in term:
        return joined.count(f" {term} ")
    return counts[term]


def _question_answered(question: str, text_lower: str, headings_lower: list[str]) -> bool:
    q_tokens = set(tokenize(question))
    if not q_tokens:
        return True
    if any(q_tokens <= set(tokenize(h)) for h in headings_lower):
        return True
    # ≥70% of question terms somewhere in the body
    hit = sum(1 for t in q_tokens if t in text_lower)
    return hit / len(q_tokens) >= 0.7


def score_draft(
    draft_text: str,
    draft_headings: list[str] | None,
    benchmark: Benchmark,
    cfg: dict,
) -> ScoreReport:
    headings = draft_headings if draft_headings is not None else _HEADING_RE.findall(draft_text)
    body = _HEADING_RE.sub(" ", draft_text)
    tokens = tokenize(draft_text)
    counts = Counter(tokens)
    joined = " " + " ".join(tokens) + " "
    text_lower = draft_text.lower()
    headings_lower = [h.lower() for h in headings]
    zero_multiple = float(cfg["stuffing_zero_multiple"])

    # --- term coverage (weighted by term importance) ---
    missing_terms: list[dict] = []
    total_weight = sum(t.weight for t in benchmark.term_targets) or 1.0
    earned = 0.0
    for target in benchmark.term_targets:
        used = _term_count(target.term, counts, joined)
        credit = _term_credit(used, target.min_count, target.max_count, zero_multiple)
        earned += target.weight * credit
        if credit < 1.0 and used < target.min_count:
            missing_terms.append({
                "term": target.term, "used": used,
                "min_count": target.min_count, "max_count": target.max_count,
            })
    term_coverage = earned / total_weight

    # --- topical completeness (topic hit = ≥half its terms present) ---
    questions_unanswered = [
        q for q in benchmark.questions
        if not _question_answered(q, text_lower, headings_lower)
    ]
    topic_scores: list[float] = []
    for topic in benchmark.topics:
        t_terms = [t for t in topic.terms if t]
        term_hit = (
            sum(1 for t in t_terms if _term_count(t, counts, joined) > 0) / len(t_terms)
            if t_terms else 0.0
        )
        topic_scores.append(term_hit)
    q_total = len(benchmark.questions)
    q_score = (q_total - len(questions_unanswered)) / q_total if q_total else 1.0
    parts = topic_scores + [q_score]
    topical_completeness = sum(parts) / len(parts) if parts else 0.0

    # --- structure fit ---
    word_count = len(body.split())
    wc_lo, wc_hi = benchmark.word_count_range
    h_lo, h_hi = benchmark.heading_count_range
    structure_parts = [
        _range_credit(word_count, wc_lo, wc_hi),
        _range_credit(len(headings), h_lo, h_hi),
    ]
    structure_notes: list[str] = []
    if word_count < wc_lo:
        structure_notes.append(f"Add ~{wc_lo - word_count} words (have {word_count}, target {wc_lo}-{wc_hi})")
    elif word_count > wc_hi:
        structure_notes.append(f"Trim toward {wc_lo}-{wc_hi} words (have {word_count})")
    if len(headings) < h_lo:
        structure_notes.append(f"Add headings (have {len(headings)}, target {h_lo}-{h_hi})")
    if benchmark.paa_questions and not any(
        "faq" in h or "question" in h for h in headings_lower
    ):
        structure_parts.append(0.0)
        structure_notes.append("Add an FAQ section — Google shows People-Also-Ask for this keyword")
    else:
        structure_parts.append(1.0)
    structure_fit = sum(structure_parts) / len(structure_parts)

    # --- semantic depth (local proxies only) ---
    unique_ratio = len(set(tokens)) / len(tokens) if tokens else 0.0
    # depth proxy stays unigram-based; phrase targets would deflate it unfairly
    benchmark_terms = {t.term for t in benchmark.term_targets if " " not in t.term}
    per_section_hits: list[float] = []
    sections = re.split(r"^#{1,6}\s+.+$", draft_text, flags=re.MULTILINE)
    for section in sections:
        s_tokens = set(tokenize(section))
        if s_tokens:
            per_section_hits.append(len(s_tokens & benchmark_terms) / max(len(benchmark_terms), 1))
    spread = sum(per_section_hits) / len(per_section_hits) if per_section_hits else 0.0
    semantic_depth = min(1.0, 0.5 * unique_ratio + 0.5 * min(1.0, spread * 3))

    score = 100 * (
        float(cfg["w_term_coverage"]) * term_coverage
        + float(cfg["w_topics"]) * topical_completeness
        + float(cfg["w_structure"]) * structure_fit
        + float(cfg["w_depth"]) * semantic_depth
    )
    return ScoreReport(
        score=round(score, 1),
        term_coverage=round(term_coverage, 4),
        topical_completeness=round(topical_completeness, 4),
        structure_fit=round(structure_fit, 4),
        semantic_depth=round(semantic_depth, 4),
        missing_terms=missing_terms,
        questions_unanswered=questions_unanswered,
        structure_notes=structure_notes,
    )
