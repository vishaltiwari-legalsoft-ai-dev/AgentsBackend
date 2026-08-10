"""Layer 3 text bridge — tokenization, n-grams, term counting, brand marking.

Feeds clean text into the pure math of ``opt_terms``. Design rules from the
spec: ratios/units/temperatures ('1:8', '24 hours', '200g') are first-class
tokens the tokenizer must never split; counting happens on light lemmas but
the UI shows the most frequent surface form; n-grams never cross sentence
boundaries or start/end on a stopword.

English-first: light suffix lemmatizer, per-language stopword hooks. Honest
limitation until a per-language model earns its Cloud Run weight.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .opt_config import TermsCfg
from . import opt_terms

# Special tokens first (ratios, temps, measurements), then plain words.
_TOKEN_RE = re.compile(
    r"\d+(?:[.,]\d+)?:\d+"                                  # 1:8
    r"|\d+(?:[.,]\d+)?\s?(?:°\s?[cf]|g|kg|ml|l|oz|lbs?|hours?|hrs?|minutes?|mins?|days?|weeks?|%)\b"
    r"|[a-z']+",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[.!?\n]+")

STOPWORDS_EN = frozenset("""
a about above after again all also am an and any are as at be because been
before being below between both but by can could did do does doing down during
each few for from further had has have having he her here hers him his how i
if in into is it its itself just me more most my no nor not now of off on once
only or other our ours out over own same she should so some such than that the
their theirs them then there these they this those through to too under until
up very was we were what when where which while who whom why will with you your
yours
""".split())

_IRREGULAR = {
    "made": "make", "making": "make", "brewed": "brew", "brewing": "brew",
    "brews": "brew", "steeped": "steep", "steeping": "steep", "steeps": "steep",
    "grounds": "ground", "better": "good", "best": "good", "leaves": "leave",
}


def lemma(token: str) -> str:
    """Light English lemmatizer: irregular map + conservative suffix rules.
    Special tokens (digits/ratios/units) pass through untouched."""
    t = token.lower().replace(" ", "")
    if any(ch.isdigit() for ch in t):
        return t
    if t in _IRREGULAR:
        return _IRREGULAR[t]
    if len(t) > 4:
        for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", "")):
            if t.endswith(suffix):
                stem = t[: -len(suffix)] + repl
                if len(stem) >= 3:
                    return stem
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


def _sentence_tokens(sentence: str) -> list[tuple[str, str]]:
    """(lemma, surface) pairs for one sentence."""
    out = []
    for m in _TOKEN_RE.finditer(sentence):
        surface = m.group()
        out.append((lemma(surface), surface.lower()))
    return out


def count_terms(
    text: str, cfg: TermsCfg
) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """N-gram (1..ngram_max) counts keyed by lemma form + surface-form tallies.

    Unigrams that are stopwords are dropped; n-grams may contain stopwords in
    the middle ('coffee to water ratio') but never at an edge."""
    counts: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for sent in sentences(text):
        toks = _sentence_tokens(sent)
        for n in range(1, cfg.ngram_max + 1):
            for i in range(len(toks) - n + 1):
                window = toks[i:i + n]
                first_l, last_l = window[0][0], window[-1][0]
                if first_l in STOPWORDS_EN or last_l in STOPWORDS_EN:
                    continue
                key = " ".join(l for l, _ in window)
                if len(key) < 3:
                    continue
                counts[key] += 1
                surfaces[key][" ".join(s for _, s in window)] += 1
    return counts, surfaces


def display_form(term: str, surfaces: dict[str, Counter[str]]) -> str:
    """Most frequent surface form for a lemma-keyed term."""
    tally = surfaces.get(term)
    return tally.most_common(1)[0][0] if tally else term


def term_profile(
    texts_by_rank: list[str],
    cfg: TermsCfg,
    draft_words: int | None = None,
    top_n: int = 40,
) -> list[dict]:
    """Corpus texts (rank order) -> ranked term list with importance + count
    range. Pure function over strings; the pipeline feeds it usable docs only."""
    per_doc: list[Counter[str]] = []
    surfaces_all: dict[str, Counter[str]] = defaultdict(Counter)
    for text in texts_by_rank:
        counts, surfaces = count_terms(text, cfg)
        per_doc.append(counts)
        for term, tally in surfaces.items():
            surfaces_all[term].update(tally)

    totals: Counter[str] = Counter()
    for counts in per_doc:
        totals.update(counts)
    kept = opt_terms.suppress_nested_ngrams(dict(totals), cfg)

    word_counts = [len(t.split()) for t in texts_by_rank]
    corpus_median = opt_terms.percentile([float(w) for w in word_counts], 50) if word_counts else None

    profile = []
    for term in kept:
        counts_by_rank = [c.get(term, 0) for c in per_doc]
        score = opt_terms.importance(counts_by_rank, cfg)
        if score <= 0:
            continue
        lo, hi, confidence = opt_terms.count_range(
            counts_by_rank, cfg, draft_words=draft_words, corpus_median_words=corpus_median
        )
        profile.append({
            "term": term,
            "display": display_form(term, surfaces_all),
            "importance": round(score, 4),
            "prevalence": round(opt_terms.prevalence(counts_by_rank), 3),
            "range": [lo, hi],
            "confidence": confidence,
        })
    profile.sort(key=lambda e: -e["importance"])
    return profile[:top_n]


# ------------------------------------------------------------- brand entities

def brand_terms(
    profile: list[dict], raw_texts: list[str], gazetteer: set[str] | None = None
) -> set[str]:
    """Terms that are ORG/PRODUCT-like -> opt-in 'brand mentions', never
    auto-recommended. Heuristic: gazetteer hit, or the surface form appears
    capitalized mid-sentence in most of its occurrences across the corpus."""
    gaz = {g.lower() for g in (gazetteer or set())}
    corpus = "\n".join(raw_texts)
    flagged: set[str] = set()
    for entry in profile:
        display = entry["display"]
        if display in gaz or entry["term"] in gaz:
            flagged.add(entry["term"])
            continue
        pattern = re.compile(r"(?<![.!?]\s)(?<!^)\b" + re.escape(display), re.IGNORECASE | re.MULTILINE)
        hits = [m.group() for m in pattern.finditer(corpus)]
        capitalized = sum(1 for h in hits if h[:1].isupper())
        if len(hits) >= 3 and capitalized / len(hits) > 0.7:
            flagged.add(entry["term"])
    return flagged
