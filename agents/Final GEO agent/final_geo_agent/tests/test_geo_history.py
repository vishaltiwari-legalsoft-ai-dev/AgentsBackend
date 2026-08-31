"""GEO score + trend history. Math is pure; the store half runs on the
tmp-dir offline state the package conftest pins."""
import pytest

from final_geo_agent import geo_history, geo_poll


def block(mention=0.5, citation=0.4, sov_self=0.25, n_prompts=4):
    return {
        "mention": {"rate": mention, "stdev": 0.1, "n_prompts": n_prompts, "n_answers": 40},
        "citation": {"rate": citation, "n_answers_with_citations": 20, "cited_answers": 8},
        "sov": {"share": {"self": sov_self, "clio": 0.75}, "credit": {}, "unclaimed_answers": 0},
    }


ROLLUP = [{"self_rate": 0.6}, {"self_rate": 0.2}, {"self_rate": 0.0}, {"self_rate": 0.0}]


# -------------------------------------------------------------------- score


def test_score_blends_all_four_components():
    scored = geo_history.geo_score(block(), ROLLUP, has_competitors=True)
    # .40*0.50 + .25*0.40 + .25*0.25 + .10*0.50 = 0.4125
    assert scored["score"] == pytest.approx(41.2, abs=0.1)
    assert set(scored["components"]) == {"presence", "citation", "sov", "breadth"}
    assert sum(scored["weights"].values()) == pytest.approx(1.0)
    assert scored["missing"] == []


def test_unmeasurable_citation_is_dropped_not_scored_zero():
    no_cites = block()
    no_cites["citation"] = {"rate": None, "n_answers_with_citations": 0, "cited_answers": 0}
    scored = geo_history.geo_score(no_cites, ROLLUP, has_competitors=True)
    # weights renormalise over presence+sov+breadth (0.75), so the score is
    # NOT dragged down by an engine set that returns no citations
    expected = (0.40 * 0.5 + 0.25 * 0.25 + 0.10 * 0.5) / 0.75 * 100
    assert scored["score"] == pytest.approx(round(expected, 1), abs=0.1)
    assert scored["missing"] == ["citation"]
    assert "citation" not in scored["weights"]


def test_share_of_voice_excluded_when_nothing_is_tracked():
    scored = geo_history.geo_score(block(), ROLLUP, has_competitors=False)
    assert "sov" not in scored["components"]      # a one-horse race is not a 100%
    assert scored["missing"] == ["sov"]


def test_nothing_measurable_scores_none_not_zero():
    empty = {"mention": {"rate": None}, "citation": {"rate": None},
             "sov": {"share": {"self": None}}}
    scored = geo_history.geo_score(empty, [], has_competitors=True)
    assert scored["score"] is None
    assert scored["components"] == {}


# -------------------------------------------------------------------- point


def answers(n, named=0, engine="perplexity"):
    rows = []
    for i in range(n):
        rows.append({
            "prompt_id": f"p{i}", "prompt_text": f"q{i}", "run": 1, "engine": engine,
            "mentions": {"self": 1} if i < named else {"clio": 1},
            "citations": [{"domain": "legalsoft.com" if i < named else "g2.com"}],
            "brand_cited": i < named,
            "error": None,
        })
    return rows


def test_thin_sweep_earns_no_point():
    assert geo_history.build_point("20260819", answers(5, 2), ["self"], "legalsoft.com") is None


def test_point_carries_score_engines_and_rivals():
    point = geo_history.build_point(
        "20260819", answers(20, 10), ["self", "clio"], "legalsoft.com",
    )
    assert point["date"] == "20260819"
    assert point["source"] == "sweep"
    assert point["mention_rate"] == 0.5
    assert point["n_measured"] == 20
    assert point["engines"] == {"perplexity": 0.5}
    assert point["competitors"]["clio"] == 0.5
    assert 0 < point["score"] <= 100


def test_point_carries_the_three_states_as_counts_over_one_denominator():
    """The panel splits every stored answer into linked / named-only / absent.

    It cannot derive that from the two rates: `citation_rate` is measured over
    answers that carry citations at all, a smaller population than `n_measured`.
    Multiplying both by `n_measured` and subtracting produced a "named, no link"
    of MINUS NINE on a real brand. So the counts are counted here.
    """
    point = geo_history.build_point(
        "20260819", answers(20, 10), ["self", "clio"], "legalsoft.com",
    )
    assert point["n_named"] == 10
    assert point["n_named_cited"] == 10
    # the three regions are a partition of n_measured, in every direction
    assert point["n_named_cited"] <= point["n_named"] <= point["n_measured"]


def test_a_brand_named_without_a_link_is_counted_in_the_middle_state():
    rows = answers(20, 10)
    for row in rows[:4]:
        row["brand_cited"] = False        # named, but the engine linked nobody
        row["citations"] = [{"domain": "g2.com"}]
    point = geo_history.build_point("20260819", rows, ["self"], "legalsoft.com")
    assert point["n_named"] == 10
    assert point["n_named_cited"] == 6
    assert point["n_named"] - point["n_named_cited"] == 4


def test_an_answer_that_links_you_without_naming_you_is_not_counted_as_named():
    """A citation without a mention is not "named and linked" — it is not named
    at all, and folding it in is how the middle band goes negative."""
    rows = answers(20, 0)
    rows[0]["brand_cited"] = True
    point = geo_history.build_point("20260819", rows, ["self"], "legalsoft.com")
    assert point["n_named"] == 0
    assert point["n_named_cited"] == 0


# ------------------------------------------------------------------- series


def test_merge_replaces_a_same_day_point_and_keeps_order():
    old = [{"date": "20260817", "score": 10.0}, {"date": "20260818", "score": 20.0}]
    merged = geo_history.merge_points(old, [{"date": "20260818", "score": 25.0},
                                            {"date": "20260816", "score": 5.0}])
    assert [(p["date"], p["score"]) for p in merged] == [
        ("20260816", 5.0), ("20260817", 10.0), ("20260818", 25.0)
    ]


def test_series_is_capped():
    many = [{"date": f"2026{i:04d}", "score": float(i)} for i in range(300)]
    assert len(geo_history.merge_points([], many)) == geo_history.MAX_POINTS


def test_thin_points_are_flagged_partial_not_dropped():
    points = geo_history.mark_partial([
        {"date": "a", "n_measured": 100}, {"date": "b", "n_measured": 20},
    ])
    assert [p["partial"] for p in points] == [False, True]


def test_trend_reports_first_measurement_honestly():
    one = geo_history.trend([{"date": "20260819", "score": 30.0}])
    assert one["since_last"]["change"] is None
    assert one["since_last"]["direction"] == "unknown"
    assert one["previous"] is None


def test_trend_measures_last_step_and_whole_window():
    points = [
        {"date": "20260815", "score": 30.0},
        {"date": "20260817", "score": 34.0},
        {"date": "20260819", "score": 41.0},
    ]
    result = geo_history.trend(points)
    assert result["since_last"] == {"change": 7.0, "direction": "up"}
    assert result["since_start"] == {"change": 11.0, "direction": "up"}
    assert result["current"]["date"] == "20260819"


def test_trend_ignores_points_that_could_not_be_scored():
    result = geo_history.trend([{"date": "a", "score": None}, {"date": "b", "score": 12.0}])
    assert result["current"]["score"] == 12.0
    assert result["n_points"] == 1


# ------------------------------------------------------------------- weekly
# Derived on read from the per-sweep points; nothing here is ever stored.


def point(date, score, mention=0.5, citation=0.2, partial=False):
    return {"date": date, "score": score, "mention_rate": mention,
            "citation_rate": citation, "partial": partial}


def test_weekly_buckets_by_iso_week_across_a_month_boundary():
    rows = geo_history.weekly_rollup([
        point("20260829", 30.0),               # Sat, ISO week 35
        point("20260831", 32.0),               # Mon, ISO week 36 — August
        point("20260902", 36.0),               # Wed, ISO week 36 — September
    ])
    assert [(r["week"], r["start"], r["n_sweeps"]) for r in rows] == [
        ("2026-W35", "2026-08-24", 1), ("2026-W36", "2026-08-31", 2),
    ]
    assert rows[1]["score"] == 34.0            # mean of the two September-week sweeps


def test_weekly_labels_a_january_sweep_by_its_iso_year_and_monday():
    # 1 Jan 2026 is a Thursday: ISO week 1 of 2026, whose Monday is still in 2025
    rows = geo_history.weekly_rollup([point("20260101", 10.0)])
    assert rows[0]["week"] == "2026-W01" and rows[0]["start"] == "2025-12-29"


def test_weekly_averages_only_what_was_measured_and_never_invents_a_zero():
    rows = geo_history.weekly_rollup([
        point("20260825", None, mention=None, citation=None),
        point("20260827", 40.0, mention=0.4, citation=None),
        point("20260901", None, mention=None, citation=None),
    ])
    week_35, week_36 = rows
    assert week_35["score"] == 40.0 and week_35["mention_rate"] == 0.4
    assert week_35["citation_rate"] is None   # nobody measured it that week
    assert week_36["score"] is None and week_36["delta_score"] is None


def test_weekly_delta_is_against_the_previous_measured_week():
    rows = geo_history.weekly_rollup([
        point("20260817", 30.0),
        point("20260825", 34.0),
        point("20260831", None),               # a week that could not be scored
        point("20260908", 41.5),
    ])
    assert [r["delta_score"] for r in rows] == [None, 4.0, None, 7.5]


def test_weekly_flags_a_week_only_when_every_sweep_in_it_was_thin():
    rows = geo_history.weekly_rollup([
        point("20260825", 30.0, partial=True),
        point("20260827", 31.0, partial=False),
        point("20260901", 20.0, partial=True),
    ])
    assert [r["all_partial"] for r in rows] == [False, True]


def test_weekly_keeps_the_last_n_weeks_with_the_delta_over_the_cut():
    rows = geo_history.weekly_rollup([
        point("20260810", 10.0), point("20260817", 20.0), point("20260824", 25.0),
    ], weeks=2)
    assert [r["week"] for r in rows] == ["2026-W34", "2026-W35"]
    # the week before the cut is gone from the chart, not from the arithmetic
    assert rows[0]["delta_score"] == 10.0


def test_weekly_skips_points_whose_date_it_cannot_read():
    rows = geo_history.weekly_rollup([point("not-a-date", 5.0), point("20260825", 30.0)])
    assert [r["week"] for r in rows] == ["2026-W35"]
    assert geo_history.weekly_rollup([]) == []


# -------------------------------------------------------------------- store


def test_record_sweep_persists_and_is_idempotent_per_day():
    geo_history.record_sweep("b1", "20260819", answers(20, 10), ["self"], "legalsoft.com")
    geo_history.record_sweep("b1", "20260819", answers(20, 16), ["self"], "legalsoft.com")
    points = geo_history.load_points("b1")
    assert len(points) == 1                # one point per day, latest wins
    assert points[0]["mention_rate"] == 0.8


def test_backfill_runs_once_then_stamps_the_document():
    assert geo_history.needs_backfill("b2") is True
    days = {"20260817": answers(20, 4), "20260819": answers(20, 12)}
    points = geo_history.backfill("b2", days, ["self"], "legalsoft.com")
    assert [p["date"] for p in points] == ["20260817", "20260819"]
    assert all(p["source"] == "backfill" for p in points)
    assert geo_history.needs_backfill("b2") is False
    # a second call must not re-scan or duplicate
    assert len(geo_history.backfill("b2", days, ["self"], "legalsoft.com")) == 2


def test_backfill_stamps_even_when_there_is_nothing_to_recover():
    assert geo_history.backfill("b3", {}, ["self"], "legalsoft.com") == []
    assert geo_history.needs_backfill("b3") is False


def test_completed_sweep_records_a_point(monkeypatch):
    """The live path: poll_step banks the point when the day finishes."""
    brand = {"id": "b4", "name": "Legal Soft", "domain": "legalsoft.com"}
    cfg = {"competitors": [{"key": "clio", "name": "Clio", "aliases": ["Clio"]}]}
    day = "20260819"
    monkeypatch.setattr(geo_poll, "day_answers", lambda _b, _d: answers(20, 10))
    geo_poll._record_history(brand, cfg, day)
    points = geo_history.load_points("b4")
    assert len(points) == 1 and points[0]["date"] == day


def test_history_failure_never_breaks_a_paid_sweep(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(geo_history, "record_sweep", boom)
    geo_poll._record_history({"id": "b5", "domain": "x.com"}, {}, "20260819")
    assert "could not record history point" in caplog.text
