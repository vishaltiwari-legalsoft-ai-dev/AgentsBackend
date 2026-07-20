from seo_agent.briefs import build_brief
from seo_agent.schemas import Benchmark, TermTarget, Topic


def test_brief_contains_targets_topics_questions_and_honesty_flag():
    b = Benchmark(
        id="b1", keyword="legal intake specialist", created_at="t", serp_fetched_at="t",
        term_targets=[TermTarget(term="intake", weight=1.0, min_count=2, max_count=4)],
        topics=[Topic(name="Role", terms=["intake"], questions=["What does one do?"])],
        questions=["What does one do?"],
        word_count_range=[1800, 2400], heading_count_range=[8, 14],
        paa_questions=["What does one do?"],
        topics_ai=False, topics_fallback_reason="upstream 502",
    )
    brief = build_brief(b)
    assert brief["keyword"] == "legal intake specialist"
    assert brief["structure"]["word_count_range"] == [1800, 2400]
    assert brief["sections"][0]["name"] == "Role"
    assert {"term": "intake", "min_count": 2, "max_count": 4} in brief["terms"]
    assert brief["topics_ai"] is False            # honesty propagates to the brief
    assert brief["topics_fallback_reason"] == "upstream 502"
