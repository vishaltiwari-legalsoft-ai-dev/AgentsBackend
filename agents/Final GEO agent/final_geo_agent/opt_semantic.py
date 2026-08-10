"""Layer 4 — the semantic layer: chunking, embeddings, subtopics, coverage.

String matching sees 'steep time' and 'brewing duration' as unrelated; this
layer measures MEANING coverage. Competitor chunks are embedded and clustered
into subtopics; the draft is covered on a subtopic when one of its chunks
sits close enough to the centroid — with two honesty guards from the spec:
reciprocal-nearest evidence (the centroid must also rank the chunk) and a
hubness penalty (a generic intro that matches everything proves nothing).

Embeddings go through the ``Embedder`` protocol: ``OpenAIEmbedder`` in prod
(key via runtime_config, CredentialMissing when absent — never fake vectors),
any deterministic fake in tests. Vectors are normalized on ingest, so cosine
is a plain dot product.
"""
from __future__ import annotations

from collections import Counter
from typing import Protocol

import httpx
import numpy as np
from pydantic import BaseModel

from app.services import runtime_config
from seo_geo_agent.sources import CredentialMissing

from .opt_config import SemanticCfg, TermsCfg
from .opt_extract import CleanDoc
from . import opt_text

EMBED_TIMEOUT = 60
EMBED_BATCH = 96


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """Prod embedder. Missing key raises CredentialMissing so the pipeline
    degrades to lexical-only scoring with an honest confidence label."""

    def __init__(self, model: str):
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        key = runtime_config.get("openai_api_key")
        if not key:
            raise CredentialMissing(
                "OpenAI API key required for the semantic layer (Settings → Secrets)."
            )
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            resp = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.model, "input": batch},
                timeout=EMBED_TIMEOUT,
            )
            resp.raise_for_status()
            rows = sorted(resp.json()["data"], key=lambda r: r["index"])
            vectors.extend(r["embedding"] for r in rows)
        return vectors


class Chunk(BaseModel):
    doc_idx: int
    heading: str = ""
    text: str


class Subtopic(BaseModel):
    label: str
    suggested_heading: str
    doc_idxs: list[int]
    chunk_count: int
    centroid: list[float]
    member_terms: list[str] = []   # top lemmas inside the cluster — maps terms to subtopics


class SubtopicModel(BaseModel):
    subtopics: list[Subtopic]
    n_docs: int
    embed_model: str = ""


class SubtopicCoverage(BaseModel):
    label: str
    covered: bool
    best_sim: float = 0.0
    evidence: str = ""       # the draft chunk text that covered it


class CoverageResult(BaseModel):
    per_subtopic: list[SubtopicCoverage]
    score: float             # covered / total, 0..1
    gaps: list[tuple[str, str]]  # (label, suggested heading)


def _norm(vectors: list[list[float]]) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float64)
    if arr.ndim != 2 or not len(arr):
        return np.zeros((0, 1))
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _windows(sents: list[str], size: int, overlap: int) -> list[str]:
    step = max(1, size - overlap)
    return [" ".join(sents[i:i + size]) for i in range(0, len(sents), step)]


def chunk_doc(doc: CleanDoc, doc_idx: int, cfg: SemanticCfg) -> list[Chunk]:
    """Heading-bounded sections; long sections become overlapping sentence windows."""
    chunks: list[Chunk] = []
    for section in doc.sections or []:
        sents = opt_text.sentences(section.text)
        if not sents:
            continue
        for window in _windows(sents, cfg.chunk_max_sentences, cfg.chunk_overlap_sentences):
            prefix = f"{section.heading}. " if section.heading else ""
            chunks.append(Chunk(doc_idx=doc_idx, heading=section.heading, text=prefix + window))
    if not chunks and doc.text:
        for window in _windows(opt_text.sentences(doc.text), cfg.chunk_max_sentences, cfg.chunk_overlap_sentences):
            chunks.append(Chunk(doc_idx=doc_idx, text=window))
    return chunks


def chunk_draft(text: str, cfg: SemanticCfg) -> list[Chunk]:
    """Draft chunking: markdown headings bound sections when present."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            sections.append((stripped.lstrip("# ").strip(), []))
        elif stripped:
            sections[-1][1].append(stripped)
    chunks: list[Chunk] = []
    for heading, lines in sections:
        sents = opt_text.sentences(" ".join(lines))
        for window in _windows(sents, cfg.chunk_max_sentences, cfg.chunk_overlap_sentences):
            prefix = f"{heading}. " if heading else ""
            chunks.append(Chunk(doc_idx=-1, heading=heading, text=prefix + window))
    return chunks


def _greedy_clusters(vectors: np.ndarray, cfg: SemanticCfg) -> list[list[int]]:
    """Leader clustering with centroid updates + a merge pass. Corpus chunk
    counts are a few hundred at most, so O(n·k) is instant."""
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for idx in range(len(vectors)):
        v = vectors[idx]
        sims = [float(v @ c) for c in centroids]
        best = int(np.argmax(sims)) if sims else -1
        if best >= 0 and sims[best] >= cfg.cluster_merge_threshold:
            clusters[best].append(idx)
            centroid = vectors[clusters[best]].mean(axis=0)
            centroids[best] = centroid / (np.linalg.norm(centroid) or 1.0)
        else:
            clusters.append([idx])
            centroids.append(v)
    # merge clusters that converged onto the same meaning
    merged = True
    while merged:
        merged = False
        for i in range(len(centroids)):
            for j in range(len(centroids) - 1, i, -1):
                if float(centroids[i] @ centroids[j]) >= cfg.cluster_merge_threshold:
                    clusters[i].extend(clusters[j])
                    del clusters[j], centroids[j]
                    centroid = vectors[clusters[i]].mean(axis=0)
                    centroids[i] = centroid / (np.linalg.norm(centroid) or 1.0)
                    merged = True
    return clusters


def _label(member_texts: list[str], other_texts: list[str], terms_cfg: TermsCfg) -> tuple[str, list[str]]:
    """Contrastive top terms (label) + the cluster's own vocabulary (mapping)."""
    inside, _ = opt_text.count_terms(" ".join(member_texts), terms_cfg)
    outside, _ = opt_text.count_terms(" ".join(other_texts), terms_cfg)
    scored = {
        term: count / (1 + outside.get(term, 0))
        for term, count in inside.items() if count >= 2
    }
    top = [t for t, _ in Counter(scored).most_common(3)]
    member_terms = [t for t, _ in inside.most_common(30)]
    return (", ".join(top) if top else "subtopic"), member_terms


def build_subtopics(
    chunks: list[Chunk],
    vectors: list[list[float]],
    cfg: SemanticCfg,
    terms_cfg: TermsCfg,
) -> SubtopicModel:
    arr = _norm(vectors)
    n_docs = len({c.doc_idx for c in chunks})
    subtopics: list[Subtopic] = []
    for members in _greedy_clusters(arr, cfg):
        doc_idxs = sorted({chunks[i].doc_idx for i in members})
        if len(doc_idxs) < cfg.min_cluster_docs:
            continue  # one obsessive author is style, not a subtopic
        member_texts = [chunks[i].text for i in members]
        other_texts = [c.text for i, c in enumerate(chunks) if i not in set(members)]
        headings = [chunks[i].heading for i in members if chunks[i].heading]
        question_headings = [h for h in headings if h.endswith("?")]
        common = Counter(question_headings or headings).most_common(1)
        label, member_terms = _label(member_texts, other_texts, terms_cfg)
        centroid = arr[members].mean(axis=0)
        centroid /= (np.linalg.norm(centroid) or 1.0)
        subtopics.append(Subtopic(
            label=label,
            suggested_heading=common[0][0] if common else f"Add a section about: {label}",
            doc_idxs=doc_idxs,
            chunk_count=len(members),
            centroid=centroid.tolist(),
            member_terms=member_terms,
        ))
    subtopics.sort(key=lambda s: (-len(s.doc_idxs), -s.chunk_count))
    return SubtopicModel(subtopics=subtopics, n_docs=n_docs, embed_model=cfg.embed_model)


def coverage(
    model: SubtopicModel,
    draft_chunks: list[Chunk],
    draft_vectors: list[list[float]],
    cfg: SemanticCfg,
) -> CoverageResult:
    if not model.subtopics:
        return CoverageResult(per_subtopic=[], score=0.0, gaps=[])
    centroids = _norm([s.centroid for s in model.subtopics])
    if not len(draft_chunks):
        per = [SubtopicCoverage(label=s.label, covered=False) for s in model.subtopics]
        return CoverageResult(
            per_subtopic=per, score=0.0,
            gaps=[(s.label, s.suggested_heading) for s in model.subtopics],
        )
    drafts = _norm(draft_vectors)
    sims = drafts @ centroids.T                      # (n_chunks, n_subtopics)

    # hubness guard: a chunk near everything is generic and proves nothing
    matches_per_chunk = (sims >= cfg.similarity_threshold).sum(axis=1)
    hub = matches_per_chunk > cfg.hubness_max_matches
    # reciprocal evidence: centroid must be in that chunk's top-K neighbours
    top_k = np.argsort(-sims, axis=1)[:, :cfg.reciprocal_top_k]

    per: list[SubtopicCoverage] = []
    gaps: list[tuple[str, str]] = []
    for s_idx, subtopic in enumerate(model.subtopics):
        best_sim, best_chunk = 0.0, -1
        for c_idx in range(len(draft_chunks)):
            if hub[c_idx] or s_idx not in top_k[c_idx]:
                continue
            sim = float(sims[c_idx, s_idx])
            if sim >= cfg.similarity_threshold and sim > best_sim:
                best_sim, best_chunk = sim, c_idx
        covered = best_chunk >= 0
        per.append(SubtopicCoverage(
            label=subtopic.label, covered=covered, best_sim=round(best_sim, 3),
            evidence=draft_chunks[best_chunk].text[:160] if covered else "",
        ))
        if not covered:
            gaps.append((subtopic.label, subtopic.suggested_heading))
    score = sum(1 for p in per if p.covered) / len(per)
    return CoverageResult(per_subtopic=per, score=score, gaps=gaps)
