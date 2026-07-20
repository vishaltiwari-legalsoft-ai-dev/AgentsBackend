from seo_agent.schemas import Benchmark, TermTarget, Topic
from seo_agent.scoring import score_draft

CFG = {
    "w_term_coverage": 0.40, "w_topics": 0.25, "w_structure": 0.20,
    "w_depth": 0.15, "stuffing_zero_multiple": 2.0,
}


def _benchmark(**kw) -> Benchmark:
    base = dict(
        id="b1", keyword="legal intake specialist", created_at="t", serp_fetched_at="t",
        term_targets=[
            TermTarget(term="intake", weight=1.0, min_count=2, max_count=4),
            TermTarget(term="salary", weight=0.5, min_count=1, max_count=2),
        ],
        topics=[Topic(name="Role", terms=["intake"], questions=["What does an intake specialist do?"])],
        questions=["What does an intake specialist do?"],
        word_count_range=[20, 60], heading_count_range=[2, 4],
        topics_ai=True,
    )
    base.update(kw)
    return Benchmark(**base)


def _draft(words: int, headings: int, extra: str = "") -> str:
    body = ("# H\n" * headings) + ("filler " * max(0, words - headings)) + extra
    return body


def test_empty_draft_scores_near_zero_and_lists_all_missing():
    report = score_draft("", None, _benchmark(), CFG)
    assert report.score < 10
    missing = {m["term"] for m in report.missing_terms}
    assert missing == {"intake", "salary"}
    assert report.questions_unanswered == ["What does an intake specialist do?"]


def test_good_draft_scores_high():
    text = (
        "# What does an intake specialist do?\n# Salary\n"
        "intake intake intake salary specialist qualifies leads "
        + "filler " * 40
    )
    report = score_draft(text, None, _benchmark(), CFG)
    assert report.score > 70
    assert report.missing_terms == []


def test_keyword_stuffing_earns_zero_term_credit():
    ok = score_draft("intake intake intake " + "filler " * 40, None, _benchmark(), CFG)
    stuffed = score_draft("intake " * 30 + "filler " * 20, None, _benchmark(), CFG)
    # 3 uses is inside [2,4] → credit; 30 uses is past 4*2.0 → zero for that term
    assert ok.term_coverage > stuffed.term_coverage


def test_structure_range_full_credit_inside_taper_outside():
    inside = score_draft(_draft(40, 3), None, _benchmark(), CFG)
    below = score_draft(_draft(5, 0), None, _benchmark(), CFG)
    assert inside.structure_fit > below.structure_fit
    assert inside.structure_fit == 1.0


def test_question_answered_detection_via_heading_or_sentence():
    text = "## What does an intake specialist do?\nThey answer calls. " + "filler " * 30
    report = score_draft(text, None, _benchmark(), CFG)
    assert report.questions_unanswered == []


def test_score_is_weighted_blend_0_100():
    report = score_draft(_draft(40, 3, " intake intake salary"), None, _benchmark(), CFG)
    expected = 100 * (
        0.40 * report.term_coverage + 0.25 * report.topical_completeness
        + 0.20 * report.structure_fit + 0.15 * report.semantic_depth
    )
    # report.score is rounded to 0.1 and subscores to 4dp — allow that slack
    assert abs(report.score - expected) < 0.06
    assert 0 <= report.score <= 100


def test_term_report_covers_all_targets_with_statuses():
    text = "intake intake intake " + "salary " * 10 + "filler " * 30
    # intake=3 → ok in [2,4]; salary=10 → past 2*2.0 ceiling → overused
    report = score_draft(text, None, _benchmark(), CFG)
    rows = {r.term: r for r in report.term_report}
    assert set(rows) == {"intake", "salary"}
    assert rows["intake"].status == "ok" and rows["intake"].used == 3
    assert rows["salary"].status == "overused" and rows["salary"].used == 10
    assert [r.term for r in report.term_report] == ["intake", "salary"]  # weight desc


def test_term_report_missing_and_low_statuses():
    report = score_draft("intake " + "filler " * 30, None, _benchmark(), CFG)
    rows = {r.term: r for r in report.term_report}
    assert rows["intake"].status == "low"       # 1 < min_count 2
    assert rows["salary"].status == "missing"   # 0 uses


def test_topic_coverage_lists_present_missing_and_questions():
    report = score_draft("filler " * 30, None, _benchmark(), CFG)
    tc = report.topic_coverage[0]
    assert tc.name == "Role"
    assert tc.terms_present == [] and tc.terms_missing == ["intake"]
    assert tc.questions_unanswered == ["What does an intake specialist do?"]
    covered = score_draft(
        "intake specialist answers calls. What does an intake specialist do? " + "filler " * 30,
        None, _benchmark(), CFG)
    tc2 = covered.topic_coverage[0]
    assert tc2.terms_present == ["intake"] and tc2.terms_missing == []
    assert tc2.questions_unanswered == []


def test_structure_block_always_present():
    report = score_draft(_draft(40, 3), None, _benchmark(), CFG)
    s = report.structure
    assert s is not None
    assert s.word_count_range == [20, 60] and s.heading_count_range == [2, 4]
    assert s.heading_count == 3
    assert s.faq_needed is False


def test_structure_faq_flags():
    b = _benchmark(paa_questions=["How much does it cost?"])
    no_faq = score_draft(_draft(40, 3), None, b, CFG)
    assert no_faq.structure.faq_needed is True and no_faq.structure.faq_present is False
    with_faq = score_draft("# FAQ\n" + _draft(40, 2), None, b, CFG)
    assert with_faq.structure.faq_needed is True and with_faq.structure.faq_present is True
