"""Layer 4 semantic — golden paraphrase-coverage behavior with a deterministic
fake embedder (no network, no keys).

Golden requirements from the spec: a draft paragraph paraphrasing a subtopic
with no exact-term overlap ('one part coffee to eight parts water' vs the
'ratio' subtopic) is marked covered; a genuinely absent subtopic ('storage')
comes out as a gap with a suggested section heading.
"""
import re

import pytest

from final_geo_agent import opt_semantic
from final_geo_agent.opt_config import load_config
from final_geo_agent.opt_extract import CleanDoc, Section

FULL_CFG = load_config()
CFG = FULL_CFG.semantic
TERMS_CFG = FULL_CFG.terms

CONCEPTS = {
    "ratio": ["ratio", "1:8", "part", "parts", "proportion", "measure"],
    "steep": ["steep", "steeping", "hours", "overnight", "twelve", "time"],
    "storage": ["store", "storage", "fridge", "refrigerator", "last", "keep", "airtight", "week"],
    "equipment": ["jar", "filter", "grinder", "cheesecloth", "scale"],
    "generic": ["coffee", "cold", "brew", "water", "make"],
}


class FakeEmbedder:
    """Concept-lexicon vectors: deterministic, meaning-shaped, key-free."""

    def embed(self, texts):
        out = []
        for t in texts:
            words = set(re.findall(r"[a-z0-9:]+", t.lower()))
            vec = [float(sum(1 for w in triggers if w in words)) for triggers in CONCEPTS.values()]
            out.append(vec if any(vec) else [0.0] * (len(CONCEPTS) - 1) + [1.0])
        return out


def corpus_docs() -> list[CleanDoc]:
    docs = []
    for i in range(4):
        docs.append(CleanDoc(sections=[
            Section(heading="What ratio should I use?",
                    text="The golden measure is a 1:8 ratio of coffee to water. Check the proportion with a scale every single batch."),
            Section(heading="How long should it steep?",
                    text="Steep the coffee overnight, twelve hours is the sweet spot. Steeping much past a full day turns the batch woody."),
            Section(heading="How long does cold brew last in the fridge?",
                    text="Store the finished brew in an airtight bottle in the fridge. It will keep for about a week before it goes flat."),
        ]))
    # one obsessive author's personal aside — must NOT become a subtopic
    docs[0].sections.append(Section(heading="My grandmother's story",
                                    text="My grandmother served this every summer on the porch with lemon cake and stories."))
    return docs


def build_model():
    chunks = []
    for idx, doc in enumerate(corpus_docs()):
        chunks.extend(opt_semantic.chunk_doc(doc, idx, CFG))
    vectors = FakeEmbedder().embed([c.text for c in chunks])
    return opt_semantic.build_subtopics(chunks, vectors, CFG, TERMS_CFG)


DRAFT = """# My easy cold brew
Cold brew is simple to make at home.

## The right mix
Use one part coffee to eight parts water. A kitchen scale keeps things honest.

## Waiting it out
Let the jar sit overnight. Twelve hours works for most beans.
"""


def test_consensus_subtopics_found_and_style_filtered():
    model = build_model()
    assert len(model.subtopics) == 3              # ratio, steep, storage
    assert all(len(s.doc_idxs) == 4 for s in model.subtopics)
    assert not any("grandmother" in s.label for s in model.subtopics)


def test_golden_paraphrase_covered_and_storage_gap():
    model = build_model()
    draft_chunks = opt_semantic.chunk_draft(DRAFT, CFG)
    draft_vectors = FakeEmbedder().embed([c.text for c in draft_chunks])
    result = opt_semantic.coverage(model, draft_chunks, draft_vectors, CFG)

    by_heading = {s.suggested_heading: c for s, c in zip(model.subtopics, result.per_subtopic)}
    ratio_cov = by_heading["What ratio should I use?"]
    assert ratio_cov.covered                       # zero lexical overlap with 'ratio'
    assert "one part coffee" in ratio_cov.evidence
    assert by_heading["How long should it steep?"].covered

    storage_cov = by_heading["How long does cold brew last in the fridge?"]
    assert not storage_cov.covered
    assert any("fridge" in heading for _, heading in result.gaps)
    assert result.score == pytest.approx(2 / 3)


def test_question_headings_become_suggestions():
    model = build_model()
    assert all(s.suggested_heading.endswith("?") for s in model.subtopics)


def test_empty_draft_all_gaps():
    model = build_model()
    result = opt_semantic.coverage(model, [], [], CFG)
    assert result.score == 0.0
    assert len(result.gaps) == 3


def test_hubness_generic_chunk_is_no_evidence():
    subtopics = [
        opt_semantic.Subtopic(label=f"s{i}", suggested_heading=f"S{i}?", doc_idxs=[0, 1, 2],
                              chunk_count=3, centroid=[1.0 if j == i else 0.0 for j in range(5)])
        for i in range(5)
    ]
    model = opt_semantic.SubtopicModel(subtopics=subtopics, n_docs=3)
    cfg = CFG.model_copy(update={"similarity_threshold": 0.4, "hubness_max_matches": 3,
                                 "reciprocal_top_k": 5})
    generic = opt_semantic.Chunk(doc_idx=-1, text="generic intro")
    specific = opt_semantic.Chunk(doc_idx=-1, text="specific point")
    vectors = [[1.0] * 5, [1.0, 0.0, 0.0, 0.0, 0.0]]   # hub vs one-hot
    result = opt_semantic.coverage(model, [generic, specific], vectors, cfg)
    covered = [c.label for c in result.per_subtopic if c.covered]
    assert covered == ["s0"]                       # hub chunk proved nothing


def test_reciprocal_rank_required():
    subtopics = [
        opt_semantic.Subtopic(label=f"s{i}", suggested_heading=f"S{i}?", doc_idxs=[0, 1, 2],
                              chunk_count=3, centroid=[1.0 if j == i else 0.0 for j in range(3)])
        for i in range(3)
    ]
    model = opt_semantic.SubtopicModel(subtopics=subtopics, n_docs=3)
    cfg = CFG.model_copy(update={"similarity_threshold": 0.45, "reciprocal_top_k": 2})
    chunk = opt_semantic.Chunk(doc_idx=-1, text="x")
    result = opt_semantic.coverage(model, [chunk], [[1.0, 0.9, 0.7]], cfg)
    covered = {c.label: c.covered for c in result.per_subtopic}
    # s2's sim (0.46) clears the threshold but ranks 3rd for the chunk -> not covered
    assert covered == {"s0": True, "s1": True, "s2": False}


def test_openai_embedder_needs_key(monkeypatch):
    from seo_geo_agent.sources import CredentialMissing
    monkeypatch.setattr(opt_semantic.runtime_config, "get", lambda *a, **k: "")
    with pytest.raises(CredentialMissing):
        opt_semantic.OpenAIEmbedder("text-embedding-3-small").embed(["hello"])
