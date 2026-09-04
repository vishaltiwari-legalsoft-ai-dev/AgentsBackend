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


def test_prompt_rollup_rows_carry_the_persona_off_the_answer_record():
    answers = [
        dict(ans("p1", 1, mentions={"self": 1}), persona="solo practitioner"),
        ans("p2", 1),                                   # polled before personas existed
    ]
    rollup = {r["prompt_id"]: r for r in geo_metrics.prompt_rollup(answers)}
    assert rollup["p1"]["persona"] == "solo practitioner"
    assert rollup["p2"]["persona"] == ""


# ---------------------------------------------------------------- personas
# The same per-prompt-then-mean discipline as mention_stats, cut by who asks.


def test_persona_rollup_averages_per_prompt_then_across_prompts():
    answers = [
        dict(ans("p1", 1, mentions={"self": 1}), persona="solo", brand_cited=True),
        dict(ans("p1", 2), persona="solo", brand_cited=False),      # flaky: 1 of 2 runs
        dict(ans("p2", 1, mentions={"self": 1}), persona="solo", brand_cited=False),
        dict(ans("p3", 1), persona="firm admin"),
        dict(ans("p3", 2, error="HTTP 500"), persona="firm admin"),  # errors never count
    ]
    rows = {r["persona"]: r for r in geo_metrics.persona_rollup(answers)}
    solo = rows["solo"]
    assert solo["n_prompts"] == 2 and solo["n_answers"] == 3
    assert solo["mention_rate"] == 0.75           # mean(0.5, 1.0), not 2/3
    assert solo["cited_rate"] == 0.25             # mean(0.5, 0.0)
    admin = rows["firm admin"]
    assert (admin["n_prompts"], admin["n_answers"]) == (1, 1)
    assert admin["mention_rate"] == 0.0 and admin["cited_rate"] == 0.0


def test_persona_rollup_keeps_unassigned_answers_visible_and_last():
    answers = [
        ans("p1", 1, mentions={"self": 1}),                          # no persona at all
        dict(ans("p2", 1), persona="solo"),
        dict(ans("p3", 1), persona="agency"),
    ]
    rows = geo_metrics.persona_rollup(answers)
    assert [r["persona"] for r in rows] == ["agency", "solo", ""]
    assert rows[-1]["mention_rate"] == 1.0


def test_persona_rollup_is_empty_when_nothing_was_measured():
    assert geo_metrics.persona_rollup([ans("p1", 1, error="HTTP 429")]) == []


def test_engine_report_carries_the_persona_rollup():
    answers = [dict(ans("p1", 1, mentions={"self": 1}), persona="solo")]
    report = geo_metrics.engine_report(answers, ["self"], "legalsoft.com")
    assert report["persona_rollup"] == [{
        "persona": "solo", "n_prompts": 1, "n_answers": 1,
        "mention_rate": 1.0, "cited_rate": 0.0,
    }]


def test_no_aio_rows_stay_out_of_rate_denominators():
    answers = [
        ans("p1", 1, mentions={"self": 1}),
        dict(ans("p1", 2), no_aio=True),     # Google showed no AIO — not a miss
    ]
    stats = geo_metrics.mention_stats(answers)
    assert stats["rate"] == 1.0
    assert stats["n_answers"] == 1


# ---------------------------------------------------------------- provenance
# Each rate must carry the surface it was measured on, so the panel can label
# "23% named" as native-measured or proxy-measured instead of implying the former.


def _answer(engine: str, via: str, **extra) -> dict:
    return {"engine": engine, "via": via, "prompt_id": extra.pop("pid", "p1"),
            "run": 1, "mentions": {}, "citations": [], **extra}


def test_via_mix_counts_each_measurement_surface():
    answers = [
        _answer("perplexity", "openrouter"),
        _answer("perplexity", "openrouter", pid="p2"),
        _answer("gemini", "native"),
    ]

    assert geo_metrics.via_mix(answers) == {"openrouter": 2, "native": 1}


def test_via_mix_excludes_errored_and_missing_answers():
    answers = [
        _answer("gemini", "native"),
        _answer("gemini", "native", error="HTTP 429", pid="p2"),
        _answer("aio", "dataforseo", no_aio=True, pid="p3"),
    ]

    # an errored run and a "no AI Overview shown" observation are already out of
    # every rate denominator — provenance must use the same denominator
    assert geo_metrics.via_mix(answers) == {"native": 1}


def test_via_mix_labels_legacy_answers_unknown():
    # answers polled before `via` was stored must read as unknown, never native
    assert geo_metrics.via_mix([{"engine": "chatgpt", "prompt_id": "p1", "run": 1}]) == {
        "unknown": 1
    }


def test_engine_report_blocks_carry_via_mix():
    answers = [
        _answer("perplexity", "openrouter", mentions={"self": 1}),
        _answer("gemini", "native", pid="p2"),
    ]

    report = geo_metrics.engine_report(answers, ["self"], "example.com")

    assert report["engines"]["perplexity"]["via_mix"] == {"openrouter": 1}
    assert report["engines"]["gemini"]["via_mix"] == {"native": 1}
    assert report["blended"]["via_mix"] == {"openrouter": 1, "native": 1}


# ------------------------------------------------- honest denominators
# A rate and the count printed beside it must describe the same population.
# "0% named in 40 answers" read as "absent from 40 AI Overviews" when Google
# had published no AI Overview on 38 of those queries at all.


def test_block_separates_answers_seen_from_answers_a_brand_could_appear_in():
    answers = [
        _answer("aio", "dataforseo", mentions={"self": 1}),
        _answer("aio", "dataforseo", no_aio=True, pid="p2"),
        _answer("aio", "dataforseo", no_aio=True, pid="p3"),
        _answer("aio", "dataforseo", error="HTTP 429", pid="p4"),
    ]

    block = geo_metrics.engine_report(answers, ["self"], "example.com")["engines"]["aio"]

    assert block["n_answers"] == 4      # rows we stored
    assert block["n_measured"] == 1     # rows a mention could have appeared in
    assert block["n_no_aio"] == 2       # Google published no overview here
    assert block["n_errors"] == 1
    # the rate is over n_measured, so a panel printing n_answers beside it lies
    assert block["mention"]["rate"] == 1.0


def test_an_engine_that_only_ever_showed_empty_slots_reports_no_rate():
    answers = [_answer("aio", "dataforseo", no_aio=True, pid=f"p{i}") for i in range(3)]

    block = geo_metrics.engine_report(answers, ["self"], "example.com")["engines"]["aio"]

    # None, not 0.0 — nobody was named because there was nothing to be named in
    assert block["mention"]["rate"] is None
    assert (block["n_answers"], block["n_measured"], block["n_no_aio"]) == (3, 0, 3)


# --------------------------------------------- what an engine OWES this brand
# "Different engines return different numbers of answers" was filed as a bug.
# It is the design — a chat engine is sampled three times per question, a billed
# Google engine is fetched once and only on the questions that do not already
# name the brand — but nothing in the report said so, so the numbers looked
# broken. `n_expected` is the missing denominator.


def test_a_block_says_how_many_answers_that_engine_owes_a_sweep():
    answers = [
        _answer("chatgpt", "openrouter", mentions={"self": 1}),
        _answer("chatgpt", "openrouter", pid="p2"),
        _answer("aio", "dataforseo", pid="p1"),
    ]

    report = geo_metrics.engine_report(
        answers, ["self"], "example.com", expected={"chatgpt": 60, "aio": 12},
    )

    assert report["engines"]["chatgpt"]["n_expected"] == 60
    assert report["engines"]["aio"]["n_expected"] == 12
    # and the two are DIFFERENT numbers, which is the whole point: a panel that
    # printed one expectation for every engine would restate the bug report
    assert (
        report["engines"]["chatgpt"]["n_expected"]
        != report["engines"]["aio"]["n_expected"]
    )
    # blended owes one full sweep of the engines that actually measured
    assert report["blended"]["n_expected"] == 72


def test_the_expectation_is_per_sweep_not_per_window():
    """Five days of stored answers do not multiply what one sweep owes.

    `n_answers` is per window and `n_expected` is per sweep, so the ratio is
    what the panel reads. Scaling the expectation by the window would have to
    scale it by sweeps that RAN, and a paused engine ran none — its expectation
    would collapse to zero and hide exactly the hole this exists to show.
    """
    one_day = [_answer("aio", "dataforseo", pid=f"p{i}") for i in range(3)]
    five_days = one_day * 5

    def owed(answers):
        report = geo_metrics.engine_report(
            answers, ["self"], "example.com", expected={"aio": 3},
        )
        return report["engines"]["aio"]

    assert owed(one_day)["n_expected"] == owed(five_days)["n_expected"] == 3
    assert owed(five_days)["n_answers"] == 15


def test_an_engine_nobody_priced_reports_none_rather_than_zero():
    """A retired engine's stored answers, and the `unknown` bucket, have no
    entry in the question list. Zero would read as "owed nothing, delivered
    40" — a fabricated denominator is worse than an absent one."""
    answers = [_answer("aio", "dataforseo"), {"prompt_id": "p9", "run": 1}]

    report = geo_metrics.engine_report(
        answers, ["self"], "example.com", expected={"chatgpt": 60},
    )

    assert report["engines"]["aio"]["n_expected"] is None
    assert report["engines"]["unknown"]["n_expected"] is None
    # nothing measured here was priced, so the blend has no expectation either
    assert report["blended"]["n_expected"] == 0


def test_a_caller_that_passes_no_question_list_gets_none_not_a_guess():
    """`geo_history` scores stored answers with no prompt universe in hand."""
    report = geo_metrics.engine_report(
        [_answer("chatgpt", "openrouter")], ["self"], "example.com",
    )

    assert report["engines"]["chatgpt"]["n_expected"] is None
    assert report["blended"]["n_expected"] is None
