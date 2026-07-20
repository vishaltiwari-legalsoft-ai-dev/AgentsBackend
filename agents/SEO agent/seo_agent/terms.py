"""Term statistics from ranking pages — hand-rolled TF-IDF, position-weighted.

Ranks 1-3 count cfg["top3_weight"] times in the aggregate so targets reflect
what it takes to WIN, not to appear. Target counts are ranges drawn from the
per-page frequency distribution (never a single number).
"""

from __future__ import annotations

import math
import re
from collections import Counter

from seo_agent.schemas import PageDoc, TermTarget

_WORD_RE = re.compile(r"[a-z][a-z\-']{2,}")
STOPWORDS = frozenset(
    "the and for with that this from are was were you your our their has have had "
    "can will would should could not but they them there here what when where which "
    "who how why all any each more most other some such only own same too very just "
    "than then once during before after above below between into through about against".split()
)


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS]


def tokenize_with_bigrams(text: str) -> list[str]:
    """Unigrams + adjacent-pair bigrams ("legal intake") — the n-gram part of
    the spec's 'TF-IDF and n-gram analysis'. Bigram terms contain a space;
    scoring counts them as phrases."""
    words = tokenize(text)
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def _page_weight(rank: int, cfg: dict) -> float:
    return float(cfg["top3_weight"]) if rank <= 3 else 1.0


def _quartile_range(values: list[int]) -> list[int]:
    """[Q1, Q3] of the values — robust to a single outlier page."""
    vs = sorted(values)
    if not vs:
        return [0, 0]
    q1 = vs[max(0, (len(vs) - 1) // 4)]
    q3 = vs[min(len(vs) - 1, (3 * (len(vs) - 1)) // 4)]
    return [q1, max(q3, q1)]


def build_term_targets(pages: list[PageDoc], keyword: str, cfg: dict) -> list[TermTarget]:
    if not pages:
        return []
    page_tokens = {
        p.url: tokenize_with_bigrams(p.body_text + " " + " ".join(p.headings))
        for p in pages
    }
    doc_freq: Counter = Counter()
    weighted_tf: Counter = Counter()
    for page in pages:
        tokens = page_tokens[page.url]
        counts = Counter(tokens)
        w = _page_weight(page.rank, cfg)
        for term, n in counts.items():
            doc_freq[term] += 1
            weighted_tf[term] += w * n

    n_docs = len(pages)
    keyword_tokens = set(tokenize(keyword))
    scored: list[tuple[float, str]] = []
    for term, tf in weighted_tf.items():
        df = doc_freq[term]
        idf = math.log(1 + n_docs / df)
        score = tf * idf
        if term in keyword_tokens:      # the keyword's own words always matter
            score *= 1.5
        scored.append((score, term))
    scored.sort(reverse=True)

    targets: list[TermTarget] = []
    top_score = scored[0][0] if scored else 1.0
    for score, term in scored[: int(cfg["max_terms"])]:
        per_page = [Counter(page_tokens[p.url])[term] for p in pages]
        present = [c for c in per_page if c > 0]
        lo, hi = _quartile_range(present)
        targets.append(TermTarget(
            term=term,
            weight=round(score / top_score, 4),
            min_count=max(1, lo),
            max_count=max(hi, max(1, lo)),
        ))
    return targets


def structure_ranges(pages: list[PageDoc]) -> dict:
    return {
        "word_count_range": _quartile_range([p.word_count for p in pages]),
        "heading_count_range": _quartile_range([len(p.headings) for p in pages]),
    }
