"""Layer 3 — lexical term importance. Pure math, no I/O, no tokenization.

Implements the spec exactly:
    tf_sub        = 1 + ln(tf)                       (sublinear TF)
    idf           = ln(N / df)                       (mini-corpus IDF)
    prevalence(t) = df / N                           (consensus signal)
    w(r)          = 1 / log2(r + offset), normalized (rank weighting)
    importance(t) = weighted_mean_tfidf x prevalence^k

Inputs are per-document counts ordered by SERP rank (index 0 = rank 1).
Tokenization/lemmatization live in Layer 2/3 extraction code (Phase 2);
this module only does the arithmetic so it can be golden-tested.
"""
from __future__ import annotations

import math

from .opt_config import TermsCfg


def sublinear_tf(count: int) -> float:
    return 0.0 if count <= 0 else 1.0 + math.log(count)


def rank_weights(n: int, offset: float) -> list[float]:
    """Normalized DCG-style discount: rank 1 teaches the model the most."""
    raw = [1.0 / math.log2(r + offset) for r in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def prevalence(counts_by_rank: list[int]) -> float:
    if not counts_by_rank:
        return 0.0
    return sum(1 for c in counts_by_rank if c > 0) / len(counts_by_rank)


def importance(counts_by_rank: list[int], cfg: TermsCfg) -> float:
    """importance(t) = rank-weighted mean sublinear TF-IDF x prevalence^k."""
    n = len(counts_by_rank)
    df = sum(1 for c in counts_by_rank if c > 0)
    if n == 0 or df == 0:
        return 0.0
    idf = math.log(n / df)
    weights = rank_weights(n, cfg.rank_weight_offset)
    weighted_tfidf = sum(w * sublinear_tf(c) for w, c in zip(weights, counts_by_rank)) * idf
    return weighted_tfidf * prevalence(counts_by_rank) ** cfg.prevalence_exponent


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy default). p in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty list")
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * (p / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def count_range(
    counts_by_rank: list[int],
    cfg: TermsCfg,
    draft_words: int | None = None,
    corpus_median_words: float | None = None,
) -> tuple[int, int, str]:
    """Recommended usage range for a term: P25-P75 among docs that USE it,
    scaled by draft length vs corpus median, rounded inward.

    Returns (lo, hi, confidence). Confidence drops (and the band widens by
    range_widen_factor) when quartile dispersion is high — never fake precision.
    """
    used = [c for c in counts_by_rank if c > 0]
    if not used:
        return (0, 0, "low")
    p_lo, p_hi = cfg.count_range_percentiles
    lo = percentile(used, p_lo)
    hi = percentile(used, p_hi)

    scale = 1.0
    if draft_words and corpus_median_words:
        scale = max(0.1, draft_words / corpus_median_words)
    lo, hi = lo * scale, hi * scale

    confidence = "high"
    cqd = (hi - lo) / (hi + lo) if (hi + lo) > 0 else 0.0  # quartile coefficient of dispersion
    if cqd > cfg.high_dispersion_cqd or len(used) < 3:
        confidence = "low"
        half = (hi - lo) / 2 or 0.5
        mid = (hi + lo) / 2
        lo = mid - half * cfg.range_widen_factor
        hi = mid + half * cfg.range_widen_factor

    lo_i = max(1, math.ceil(lo - 1e-9))
    hi_i = max(lo_i, math.floor(hi + 1e-9))
    return (lo_i, hi_i, confidence)


def suppress_nested_ngrams(
    totals: dict[str, int], cfg: TermsCfg
) -> dict[str, int]:
    """Drop a sub-gram when a super-gram covers >= nested_ngram_overlap of its
    occurrences ('cold brew' inside 'cold brew concentrate'). Operates on
    surface strings; word-boundary containment only."""
    def contains(sup: str, sub: str) -> bool:
        return sub != sup and f" {sub} " in f" {sup} "

    keep: dict[str, int] = {}
    for term, count in totals.items():
        covered = max(
            (sup_count for sup, sup_count in totals.items() if contains(sup, term)),
            default=0,
        )
        if count > 0 and covered / count >= cfg.nested_ngram_overlap:
            continue
        keep[term] = count
    return keep
