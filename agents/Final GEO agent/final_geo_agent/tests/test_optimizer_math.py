"""Content Optimizer math core — golden-fixture unit tests (no I/O, no network).

Golden numbers come straight from the theory doc's worked examples:
importance('ratio') ~ 0.293 with k=1.5, word-count band ~ 1400-2340 with the
11,800-word outlier ignored, blend(0.31, 0.44, 0.12) = 31/100.
"""
import json
import pathlib

import pytest

from final_geo_agent import opt_score, opt_structure, opt_terms
from final_geo_agent.opt_config import load_config

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "coldbrew_corpus.json").read_text()
)

CFG = load_config()


# ---------------------------------------------------------------- Layer 3

def test_golden_term_importance_ratio():
    counts = FIXTURE["ratio_counts_5doc"]          # [4, 3, 0, 5, 2] by rank
    assert opt_terms.prevalence(counts) == pytest.approx(0.80)
    cfg = CFG.terms.model_copy(update={"prevalence_exponent": 1.5})
    assert opt_terms.importance(counts, cfg) == pytest.approx(0.293, abs=0.005)


def test_golden_term_count_range_3_to_4():
    lo, hi, confidence = opt_terms.count_range(FIXTURE["ratio_counts_5doc"], CFG.terms)
    assert (lo, hi) == (3, 4)
    assert confidence == "high"


def test_count_range_scales_with_draft_length():
    # a draft half the corpus median length gets a halved target
    lo, hi, _ = opt_terms.count_range(
        [8, 6, 10, 8], CFG.terms, draft_words=500, corpus_median_words=1000
    )
    assert hi <= 5


def test_rank_weights_top_heavy_and_normalized():
    w = opt_terms.rank_weights(10, CFG.terms.rank_weight_offset)
    assert sum(w) == pytest.approx(1.0)
    assert w[0] / w[9] == pytest.approx(2.5, abs=0.3)   # rank 1 ~ 2.5x rank 10


def test_absent_term_importance_zero():
    assert opt_terms.importance([0, 0, 0], CFG.terms) == 0.0


def test_nested_ngram_suppression():
    totals = {"cold brew concentrate": 5, "brew concentrate": 5, "cold brew": 12}
    kept = opt_terms.suppress_nested_ngrams(totals, CFG.terms)
    assert "brew concentrate" not in kept          # fully covered by super-gram
    assert "cold brew" in kept                     # 5/12 < 0.8, survives
    assert "cold brew concentrate" in kept


# ---------------------------------------------------------------- Layer 5

def test_golden_wordcount_band_outlier_immune():
    b = opt_structure.band("word_count", FIXTURE["word_counts"], CFG.structure)
    assert b.kind == "range"
    assert b.median == 1775
    assert b.lo == pytest.approx(1400, abs=100)
    assert b.hi == pytest.approx(2340, abs=100)


def test_band_ignores_even_extreme_outlier():
    counts = FIXTURE["word_counts"][:-1] + [100000]
    b = opt_structure.band("word_count", counts, CFG.structure)
    assert b.median == 1775
    assert b.hi < 3000                             # outlier never drags the band


def test_small_n_widens_and_labels():
    b = opt_structure.band("word_count", [1000, 1200, 1400, 1600], CFG.structure)
    assert b.confidence == "low"
    assert "4 usable winners" in b.note


def test_bimodal_reports_modes_not_range():
    values = [0, 0, 0, 0, 0, 3, 3, 3, 3, 3]
    b = opt_structure.band("table_count", values, CFG.structure)
    assert b.kind == "modes"
    assert b.modes == [0, 3]
    assert b.mode_shares == [0.5, 0.5]


def test_unimodal_stays_range():
    b = opt_structure.band("h2_count", FIXTURE["h2_counts"], CFG.structure)
    assert b.kind == "range"


def test_non_whitelisted_feature_rejected():
    with pytest.raises(ValueError):
        opt_structure.band("dom_depth", [1, 2, 3], CFG.structure)


# ---------------------------------------------------------------- Layer 6

def test_golden_blend_31():
    s = FIXTURE["draft"]["sub_scores"]
    assert opt_score.blend(s["term"], s["semantic"], s["structure"], CFG.score) == 31


def test_band_score_graceful_decay_no_cliff():
    f = CFG.score.band_decay_factor
    assert opt_score.band_score(1500, 1400, 2340, f) == 1.0
    assert opt_score.band_score(1399, 1400, 2340, f) > 0.99      # no cliff
    just_out = opt_score.band_score(1100, 1400, 2340, f)
    assert 0 < just_out < 1
    assert opt_score.band_score(0, 1400, 2340, f) < 0.01         # ~zero far below
    assert opt_score.band_score(5000, 1400, 2340, f) == 0.0      # zero far above


def test_term_credit_hard_cap_and_floor():
    cfg = CFG.score
    inside = opt_score.TermEntry(term="ratio", importance=1, draft_count=3, lo=3, hi=4)
    stuffed = inside.model_copy(update={"draft_count": 40})
    partial = inside.model_copy(update={"draft_count": 1})
    absent = inside.model_copy(update={"draft_count": 0})
    covered = absent.model_copy(update={"subtopic_covered": True})
    assert opt_score.term_credit(inside, cfg) == 1.0
    assert opt_score.term_credit(stuffed, cfg) == 1.0            # cap: extra reps earn 0
    assert opt_score.term_credit(partial, cfg) == pytest.approx(1 / 3)
    assert opt_score.term_credit(absent, cfg) == 0.0
    assert opt_score.term_credit(covered, cfg) == cfg.semantic_floor_credit  # paraphrase floor


def test_brand_terms_never_scored():
    entries = [
        opt_score.TermEntry(term="starbucks", importance=5, draft_count=0, lo=2, hi=3, brand_optin=True),
        opt_score.TermEntry(term="ratio", importance=1, draft_count=3, lo=3, hi=4),
    ]
    assert opt_score.term_coverage(entries, CFG.score) == 1.0


def test_stuffing_subtracts_points():
    entries = [opt_score.TermEntry(term="cold brew", importance=1, draft_count=40, lo=3, hi=8)]
    p = opt_score.stuffing_penalty(entries, 400, {"cold brew": 2.0}, CFG.score)
    assert p == CFG.score.stuffing_penalty_points
    assert opt_score.blend(1.0, 1.0, 1.0, CFG.score, penalty_points=p) == 90


def test_shortness_capped_when_coverage_complete():
    bands = {
        "word_count": opt_structure.StructureBand(
            feature="word_count", n=18, lo=1400, hi=2340, median=1775
        )
    }
    short_complete = opt_score.structure_fit(bands, {"word_count": 400}, CFG.score, semantic_coverage=0.95)
    short_hollow = opt_score.structure_fit(bands, {"word_count": 400}, CFG.score, semantic_coverage=0.3)
    assert short_complete >= CFG.score.shortness_floor            # never "pad it"
    assert short_hollow < short_complete


def test_gap_report_ordering_and_content():
    entries = [
        opt_score.TermEntry(term="steep time", importance=0.9, draft_count=0, lo=2, hi=4),
        opt_score.TermEntry(term="ratio", importance=0.3, draft_count=0, lo=3, hi=4, subtopic_covered=True),
    ]
    bands = {
        "word_count": opt_structure.StructureBand(
            feature="word_count", n=18, lo=1400, hi=2340, median=1775
        )
    }
    gaps = opt_score.gap_report(
        [("storage", "How long does cold brew last in the fridge?")],
        entries, bands, {"word_count": 400}, CFG.score,
    )
    assert gaps[0].kind == "subtopic" and "storage" in gaps[0].message
    kinds = [g.kind for g in gaps]
    assert kinds.index("subtopic") < kinds.index("term") < kinds.index("structure")
    assert any("steep time" in g.message for g in gaps)


def test_report_carries_disclaimer_and_provenance():
    r = opt_score.ScoreReport(
        total=31, term_coverage=0.31, semantic_coverage=0.44, structure_fit=0.12,
        snapshot_date="2026-08-10", locale="en-US", n_docs=18,
    )
    assert "pattern-match" in r.disclaimer
    assert r.snapshot_date and r.locale and r.n_docs


# ---------------------------------------------------------------- config

def test_config_loads_and_validates():
    cfg = load_config()
    assert cfg.score.weights.term + cfg.score.weights.semantic + cfg.score.weights.structure == pytest.approx(1.0)
    assert 1.5 <= cfg.terms.prevalence_exponent <= 3.0


def test_unknown_vertical_raises():
    with pytest.raises(KeyError):
        load_config(vertical="astrology")
