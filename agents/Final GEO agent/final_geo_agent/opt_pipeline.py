"""Pipeline — Layers 1-6 for one keyword, pinned to a reproducible snapshot.

``analyze`` runs the whole chain: SERP → fetch → extract → term profile →
subtopics → structure bands → (optional) draft score. Everything derived is
saved as one snapshot document, so ``rescore`` against the same analysis is
deterministic — same draft, same snapshot, same number. New SERP data only
on an explicit fresh ``analyze``.

Honesty rules carried throughout: warnings instead of fake precision (thin
corpus, low article share, volatile SERP), semantic layer degrades to
lexical-only with a visible label when no embedding key is configured, and
every report states that the score is a pattern-match to current winners.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from seo_geo_agent import state
from seo_geo_agent.sources import CredentialMissing

from . import opt_extract, opt_score, opt_semantic, opt_serp, opt_structure, opt_terms, opt_text
from .opt_config import OptimizerConfig, load_config

ANALYSIS_DOC = "optimizer_analysis__{aid}"
INDEX_DOC = "optimizer_index"
INDEX_CAP = 30
REPORT_TERMS = 20


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _analysis_id(keyword: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:40]
    return f"{slug}-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------ draft features

_MD_QUESTION = re.compile(
    r"^(how|what|why|when|where|which|who|can|could|should|do|does|did|is|are|will)\b",
    re.IGNORECASE,
)


def draft_features(md: str) -> dict[str, float]:
    """Only what markdown can honestly show — no link/table guessing games."""
    lines = md.splitlines()
    headings = [l.lstrip("#").strip() for l in lines if l.lstrip().startswith("#")]
    h2 = sum(1 for l in lines if re.match(r"^##\s", l.lstrip()))
    h3 = sum(1 for l in lines if re.match(r"^###\s", l.lstrip()))
    questions = sum(1 for h in headings if h.endswith("?") or _MD_QUESTION.match(h))
    images = md.count("![")
    list_lines = [l for l in lines if re.match(r"^\s*([-*+]|\d+\.)\s", l)]
    tables = sum(1 for i, l in enumerate(lines) if set(l.strip()) <= {"|", "-", ":", " "} and "|" in l)

    text = re.sub(r"[#*_`>\[\]()!]", " ", md)
    words = text.split()
    sents = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    paragraphs = [p for p in re.split(r"\n\s*\n", md) if p.strip()]
    numeric_hits = sum(1 for m in opt_extract._NUMERIC_RE.finditer(text) if m.group().strip())
    return {
        "word_count": float(len(words)),
        "h2_count": float(h2),
        "h3_count": float(h3),
        "paragraph_count": float(len(paragraphs)),
        "avg_sentence_len": (len(words) / len(sents)) if sents else 0.0,
        "image_count": float(images),
        "list_count": 1.0 if list_lines else 0.0,
        "table_count": float(min(tables, 5)),
        "question_headings": float(questions),
        "numeric_density": (numeric_hits / len(words) * 100) if words else 0.0,
    }


# ----------------------------------------------------------------- scoring

def _blend(term_cov: float, sem: float | None, struct: float, cfg: OptimizerConfig,
           penalty: float) -> int:
    w = cfg.score.weights
    if sem is None:  # semantic layer unavailable -> renormalize, never fake a 0
        raw = 100.0 * (w.term * term_cov + w.structure * struct) / (w.term + w.structure)
    else:
        raw = 100.0 * (w.term * term_cov + w.semantic * sem + w.structure * struct)
    return int(round(max(0.0, min(100.0, raw - penalty))))


def _scaled_range(lo: int, hi: int, scale: float) -> tuple[int, int]:
    lo_i = max(1, round(lo * scale))
    return lo_i, max(lo_i, round(hi * scale))


def _term_entries(profile: list[dict], counts, scale: float,
                  covered_terms: set[str]) -> list[opt_score.TermEntry]:
    entries = []
    for e in profile:
        lo, hi = _scaled_range(e["range"][0], e["range"][1], scale)
        entries.append(opt_score.TermEntry(
            term=e["term"], importance=e["importance"], draft_count=counts.get(e["term"], 0),
            lo=lo, hi=hi, subtopic_covered=e["term"] in covered_terms,
            brand_optin=bool(e.get("brand")),
        ))
    return entries


def _score_draft(doc: dict, draft: str, embedder, cfg: OptimizerConfig) -> dict:
    """Score a draft against a pinned snapshot. Pure over (snapshot, draft)."""
    counts, _ = opt_text.count_terms(draft, cfg.terms)
    feats = draft_features(draft)
    corpus_median = doc["corpus_median_words"] or 1.0
    scale = max(0.1, feats["word_count"] / corpus_median) if feats["word_count"] else 1.0

    subtopics = [opt_semantic.Subtopic(**s) for s in doc["subtopics"]]
    model = opt_semantic.SubtopicModel(subtopics=subtopics, n_docs=doc["meta"]["n_docs"])
    sem_score: float | None = None
    coverage = None
    degraded = list(doc["meta"].get("degraded", []))
    if subtopics:
        try:
            chunks = opt_semantic.chunk_draft(draft, cfg.semantic)
            vectors = embedder.embed([c.text for c in chunks]) if chunks else []
            coverage = opt_semantic.coverage(model, chunks, vectors, cfg.semantic)
            sem_score = coverage.score
        except CredentialMissing:
            degraded.append("semantic_unavailable_at_scoring")
    else:
        degraded.append("semantic_unavailable")

    covered_terms: set[str] = set()
    if coverage:
        for sub, cov in zip(subtopics, coverage.per_subtopic):
            if cov.covered:
                covered_terms.update(sub.member_terms)

    entries = _term_entries(doc["term_profile"], counts, scale, covered_terms)
    term_cov = opt_score.term_coverage(entries, cfg.score)
    penalty = opt_score.stuffing_penalty(
        entries, int(feats["word_count"]), doc.get("density_p90", {}), cfg.score
    )
    bands = {f: opt_structure.StructureBand(**b) for f, b in doc["structure_bands"].items()}
    struct = opt_score.structure_fit(bands, feats, cfg.score, semantic_coverage=sem_score or 0.0)
    total = _blend(term_cov, sem_score, struct, cfg, penalty)

    gaps = opt_score.gap_report(
        coverage.gaps if coverage else [], entries, bands, feats, cfg.score
    )
    return {
        "total": total,
        "term_coverage": round(term_cov, 3),
        "semantic_coverage": None if sem_score is None else round(sem_score, 3),
        "structure_fit": round(struct, 3),
        "stuffing_penalty": penalty,
        "winners_median": doc.get("winners_median_score"),
        "degraded": degraded,
        "gaps": [g.model_dump() for g in gaps[:12]],
        "draft_features": feats,
        "subtopic_coverage": [c.model_dump() for c in coverage.per_subtopic] if coverage else [],
        "draft_term_counts": {e["term"]: counts.get(e["term"], 0) for e in doc["term_profile"]},
    }


def _winner_median(profile: list[dict], subtopics: list[opt_semantic.Subtopic],
                   bands: dict, article_docs: list[opt_extract.CleanDoc],
                   corpus_median: float, cfg: OptimizerConfig) -> int | None:
    totals: list[float] = []
    n_sub = len(subtopics)
    for di, d in enumerate(article_docs):
        scale = max(0.1, d.word_count / corpus_median) if corpus_median else 1.0
        entries = []
        for e in profile:
            lo, hi = _scaled_range(e["range"][0], e["range"][1], scale)
            count = e["counts"][di] if di < len(e["counts"]) else 0
            entries.append(opt_score.TermEntry(
                term=e["term"], importance=e["importance"], draft_count=count,
                lo=lo, hi=hi, brand_optin=bool(e.get("brand")),
            ))
        term_cov = opt_score.term_coverage(entries, cfg.score)
        sem = (sum(1 for s in subtopics if di in s.doc_idxs) / n_sub) if n_sub else None
        struct = opt_score.structure_fit(bands, d.features, cfg.score, semantic_coverage=sem or 0.0)
        totals.append(_blend(term_cov, sem, struct, cfg, 0.0))
    return opt_score.winners_median_score(totals)


# ----------------------------------------------------------------- analyze

def analyze(keyword: str, locale: str = "en-US", draft: str = "", *,
            provider=None, embedder=None, own_domain: str = "",
            vertical: str | None = None) -> dict:
    cfg = load_config(vertical)
    provider = provider or opt_serp.SerperProvider()
    serp = provider.search(keyword, locale, cfg.serp.top_n)
    serp = opt_serp.prepare_results(serp, cfg.serp, own_domain)
    pages = opt_serp.gather_pages(provider, serp.results, cfg.serp)

    lang = (locale.partition("-")[0] or "en").lower()
    articles: list[opt_extract.CleanDoc] = []
    forums: list[opt_extract.CleanDoc] = []
    result_rows = []
    for r in serp.results:
        row = {"rank": r.rank, "url": r.url, "title": r.title,
               "page_type": r.page_type, "excluded": r.excluded, "flags": []}
        html = pages.get(r.url)
        if not r.excluded and html:
            doc = opt_extract.extract_document(html, cfg.extract, url=r.url, expected_lang=lang)
            row["flags"] = doc.flags
            if doc.usable and r.page_type == "article":
                articles.append(doc)
            elif doc.usable and r.page_type == "forum":
                forums.append(doc)   # vocabulary yes, structure no
        elif not r.excluded and not html:
            row["flags"] = ["fetch_failed"]
        result_rows.append(row)
    articles = [d for d in opt_extract.drop_duplicates(articles, cfg.extract) if d.usable]

    warnings: list[str] = []
    considered = [r for r in serp.results if not r.excluded]
    article_share = (len(articles) / len(considered)) if considered else 0.0
    if article_share < cfg.serp.article_share_min:
        warnings.append(
            "Low article share in this SERP — this query may not want an article. "
            "Targets below are low-confidence."
        )
    if len(articles) < cfg.structure.small_n:
        warnings.append(f"Only {len(articles)} usable winners — ranges are widened, treat as hints.")

    # Layer 3 — terms (articles first, forums appended = lowest rank weights)
    term_texts = [d.text for d in articles] + [d.text for d in forums]
    word_counts = [float(len(t.split())) for t in term_texts]
    corpus_median = opt_terms.percentile(word_counts, 50) if word_counts else 0.0
    profile = opt_text.term_profile(term_texts, cfg.terms, top_n=40)
    flagged = opt_text.brand_terms(profile, term_texts)
    for entry in profile:
        entry["brand"] = entry["term"] in flagged
    density_p90 = {}
    for e in profile:
        densities = [
            e["counts"][i] / wc * 100
            for i, wc in enumerate(word_counts[: len(articles)]) if wc > 0
        ]
        if densities:
            density_p90[e["term"]] = round(opt_terms.percentile(densities, 90), 3)

    # Layer 4 — subtopics from article chunks (doc_idx aligns with articles)
    embedder = embedder or opt_semantic.AutoEmbedder(cfg.semantic)
    subtopics: list[opt_semantic.Subtopic] = []
    degraded: list[str] = []
    try:
        chunks = []
        for di, d in enumerate(articles):
            chunks.extend(opt_semantic.chunk_doc(d, di, cfg.semantic))
        if chunks:
            vectors = embedder.embed([c.text for c in chunks])
            subtopics = opt_semantic.build_subtopics(chunks, vectors, cfg.semantic, cfg.terms).subtopics
    except CredentialMissing:
        degraded.append("semantic_unavailable")
        warnings.append("No embedding key configured — scoring is lexical-only (lower confidence).")

    # Layer 5 — structure (articles only)
    feature_values: dict[str, list[float]] = {}
    for d in articles:
        for f, v in d.features.items():
            if f in cfg.structure.features:
                feature_values.setdefault(f, []).append(v)
    bands = opt_structure.profile(feature_values, cfg.structure)

    winners_median = _winner_median(profile, subtopics, bands, articles, corpus_median, cfg)

    aid = _analysis_id(keyword)
    volatility = _volatility(keyword, locale, [r["url"] for r in result_rows], cfg)
    doc = {
        "meta": {
            "analysis_id": aid, "keyword": keyword, "locale": locale,
            "vertical": vertical, "created_at": _now_iso(), "provider": serp.provider,
            "aio_present": serp.aio_present, "paa": serp.paa[:10],
            "n_docs": len(articles), "article_share": round(article_share, 2),
            "warnings": warnings, "degraded": degraded, "volatility": volatility,
        },
        "results": result_rows,
        "term_profile": profile,
        "subtopics": [s.model_dump() for s in subtopics],
        "structure_bands": {f: b.model_dump() for f, b in bands.items()},
        "corpus_median_words": corpus_median,
        "density_p90": density_p90,
        "winners_median_score": winners_median,
        "disclaimer": opt_score.DISCLAIMER,
    }
    if draft.strip():
        doc["last_report"] = _score_draft(doc, draft, embedder, cfg)
    state.save(ANALYSIS_DOC.format(aid=aid), doc)
    _index_add(doc)
    return doc


def rescore(analysis_id: str, draft: str, embedder=None) -> dict:
    doc = state.load(ANALYSIS_DOC.format(aid=analysis_id))
    if not doc:
        raise KeyError(f"unknown analysis: {analysis_id}")
    cfg = load_config(doc["meta"].get("vertical"))
    embedder = embedder or opt_semantic.AutoEmbedder(cfg.semantic)
    report = _score_draft(doc, draft, embedder, cfg)
    doc["last_report"] = report
    state.save(ANALYSIS_DOC.format(aid=analysis_id), doc)
    _index_add(doc)
    return report


def get_analysis(analysis_id: str) -> dict | None:
    return state.load(ANALYSIS_DOC.format(aid=analysis_id))


def list_analyses() -> list[dict]:
    return (state.load(INDEX_DOC) or {}).get("analyses", [])


def _index_add(doc: dict) -> None:
    meta = doc["meta"]
    entry = {
        "id": meta["analysis_id"], "keyword": meta["keyword"], "locale": meta["locale"],
        "created_at": meta["created_at"], "n_docs": meta["n_docs"],
        "score": (doc.get("last_report") or {}).get("total"),
    }
    index = [e for e in list_analyses() if e["id"] != entry["id"]]
    index.insert(0, entry)
    state.save(INDEX_DOC, {"analyses": index[:INDEX_CAP]})


def _volatility(keyword: str, locale: str, urls: list[str], cfg: OptimizerConfig) -> str:
    """Overlap vs the previous snapshot of the same keyword — advice shelf life."""
    previous = next(
        (e for e in list_analyses() if e["keyword"] == keyword and e["locale"] == locale),
        None,
    )
    if not previous:
        return "first-analysis"
    old = state.load(ANALYSIS_DOC.format(aid=previous["id"])) or {}
    old_urls = {r["url"] for r in old.get("results", [])}
    if not old_urls or not urls:
        return "unknown"
    overlap = len(old_urls & set(urls)) / len(set(urls))
    return "volatile" if overlap < cfg.serp.volatility_overlap_low else "stable"
