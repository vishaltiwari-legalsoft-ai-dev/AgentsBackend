"""GEO metrics — pure-math unit tests (no I/O, no network)."""
from final_geo_agent import geo_metrics


def ans(prompt_id, run, engine="perplexity", mentions=None, citations=None, error=None):
    return {
        "prompt_id": prompt_id,
        "run": run,
        "engine": engine,
        "mentions": mentions or {},
        "citations": citations or [],
        "error": error,
    }


def test_mention_rate_averages_per_prompt_across_runs():
    answers = [
        ans("p1", 1, mentions={"self": 1}),
        ans("p1", 2),  # flaky prompt: 1 of 2 runs
        ans("p2", 1, mentions={"self": 1}),
        ans("p2", 2, mentions={"self": 2}),
    ]
    stats = geo_metrics.mention_stats(answers)
    assert stats["rate"] == 0.75  # mean(0.5, 1.0)
    assert stats["n_prompts"] == 2
    assert stats["stdev"] > 0


def test_mention_stats_empty_is_none_not_zero():
    stats = geo_metrics.mention_stats([ans("p1", 1, error="HTTP 500")])
    assert stats["rate"] is None
    assert stats["n_prompts"] == 0


def test_share_of_voice_position_weighted():
    answers = [
        ans("p1", 1, mentions={"self": 1, "comp": 2}),  # self 1.0, comp 0.5
        ans("p2", 1, mentions={"comp": 1}),             # comp 1.0
    ]
    sov = geo_metrics.share_of_voice(answers, ["self", "comp"])
    assert sov["credit"]["self"] == 1.0
    assert sov["credit"]["comp"] == 1.5
    assert sov["share"]["self"] == 0.4
    assert sov["unclaimed_answers"] == 0


def test_share_of_voice_unclaimed_counted():
    sov = geo_metrics.share_of_voice([ans("p1", 1)], ["self"])
    assert sov["share"]["self"] is None
    assert sov["unclaimed_answers"] == 1


def test_citation_share_matches_domain_suffix():
    answers = [
        ans("p1", 1, citations=[{"domain": "www.legalsoft.com"}]),
        ans("p2", 1, citations=[{"domain": "g2.com"}]),
        ans("p3", 1),  # no citations → excluded from denominator
    ]
    cit = geo_metrics.citation_share(answers, "legalsoft.com")
    assert cit["rate"] == 0.5
    assert cit["n_answers_with_citations"] == 2


def test_source_gap_excludes_answers_where_we_are_cited():
    answers = [
        ans("p1", 1, citations=[{"domain": "g2.com"}, {"domain": "legalsoft.com"}]),
        ans("p2", 1, citations=[{"domain": "g2.com"}, {"domain": "reddit.com"}]),
        ans("p3", 1, citations=[{"domain": "g2.com"}]),
    ]
    gap = geo_metrics.source_gap(answers, "legalsoft.com")
    domains = {g["domain"]: g["count"] for g in gap}
    # p1 cites us → its g2 citation is NOT a gap
    assert domains == {"g2.com": 2, "reddit.com": 1}
    assert gap[0]["example_prompt_ids"] == ["p2", "p3"]


def test_engine_report_shapes_and_engine_split():
    answers = [
        ans("p1", 1, engine="perplexity", mentions={"self": 1}),
        ans("p1", 1, engine="gemini", error="no API key configured"),
    ]
    report = geo_metrics.engine_report(answers, ["self", "comp"], "legalsoft.com")
    assert set(report["engines"]) == {"perplexity", "gemini"}
    assert report["engines"]["gemini"]["n_errors"] == 1
    assert report["blended"]["mention"]["rate"] == 1.0
    assert "source_gap" in report and "competitors" in report
    assert report["competitors"]["comp"]["rate"] == 0.0


def test_prompt_rollup_appear_vs_missing():
    answers = [
        ans("p1", 1, mentions={"self": 1}),
        ans("p1", 2, mentions={"self": 2, "rival": 1}),
        ans("p2", 1, mentions={"rival": 1}),
        ans("p2", 2, mentions={"rival": 1, "other": 2}),
        ans("p3", 1),                                   # nobody named
        ans("p3", 2, error="HTTP 429"),                 # errors never count
    ]
    answers[0]["prompt_text"] = "best legal intake service"
    rollup = {r["prompt_id"]: r for r in geo_metrics.prompt_rollup(answers)}
    assert rollup["p1"]["self_rate"] == 1.0 and rollup["p1"]["n"] == 2
    assert rollup["p2"]["self_rate"] == 0.0
    assert rollup["p2"]["rivals"][0] == {"key": "rival", "count": 2}
    assert rollup["p3"]["n"] == 1                       # error row excluded
    assert rollup["p1"]["engines_hit"] == ["perplexity"]
