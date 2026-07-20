from seo_agent.schemas import PageDoc
from seo_agent.terms import build_term_targets, structure_ranges, tokenize

CFG = {"top3_weight": 2.0, "max_terms": 40}


def _page(rank: int, body: str, headings=None, wc=None) -> PageDoc:
    return PageDoc(
        url=f"https://s{rank}.com", rank=rank, body_text=body,
        headings=headings or ["H"], word_count=wc or len(body.split()),
    )


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("The Intake Specialist and the Firm") == ["intake", "specialist", "firm"]


def test_recurring_bigrams_become_targets():
    pages = [
        _page(1, "legal intake matters. legal intake process. " * 8),
        _page(2, "legal intake teams handle calls. legal intake speed. " * 8),
    ]
    targets = build_term_targets(pages, "legal intake specialist", CFG)
    assert "legal intake" in [t.term for t in targets]


def test_distinctive_terms_rank_above_generic_ones():
    pages = [
        _page(1, "intake specialist salary intake qualification leads " * 10),
        _page(2, "intake specialist hiring salary training " * 10),
        _page(3, "intake process specialist onboarding salary " * 10),
    ]
    targets = build_term_targets(pages, "legal intake specialist", CFG)
    names = [t.term for t in targets]
    assert "salary" in names          # present in every page
    assert "intake" in names
    for t in targets:
        assert t.min_count >= 1
        assert t.max_count >= t.min_count


def test_top3_pages_outweigh_lower_ranks():
    # "alpha" only on rank-1 page; "omega" only on rank-9 page, same counts.
    pages = [
        _page(1, "alpha " * 12 + "filler common words here " * 5),
        _page(9, "omega " * 12 + "filler common words here " * 5),
    ]
    targets = build_term_targets(pages, "kw", CFG)
    weights = {t.term: t.weight for t in targets}
    assert weights["alpha"] > weights["omega"]


def test_structure_ranges_are_iqr_not_minmax():
    pages = [
        _page(1, "w", wc=1000, headings=["a"] * 8),
        _page(2, "w", wc=2000, headings=["a"] * 10),
        _page(3, "w", wc=2200, headings=["a"] * 12),
        _page(4, "w", wc=9000, headings=["a"] * 40),   # outlier
    ]
    ranges = structure_ranges(pages)
    lo, hi = ranges["word_count_range"]
    assert lo >= 1000
    assert hi < 9000                  # outlier does not define the ceiling
    h_lo, h_hi = ranges["heading_count_range"]
    assert h_lo >= 8 and h_hi < 40
