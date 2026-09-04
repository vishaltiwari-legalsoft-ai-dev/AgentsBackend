"""GEO run log — record, order, cap, and plan progress. Offline: the
local-file state adapter, real save/load functions on both sides."""
from concurrent.futures import ThreadPoolExecutor

from final_geo_agent import geo_runlog, geo_strategy
from seo_geo_agent import state

BRAND_ID = "legalsoft"


def test_record_run_assigns_an_id_and_returns_the_stored_entry():
    entry = geo_runlog.record_run(BRAND_ID, {"day": "20260830", "trigger": "manual"})

    assert entry["id"] and entry["recorded_at"]
    assert entry["day"] == "20260830"
    assert geo_runlog.recent_runs(BRAND_ID) == [entry]


def test_a_caller_supplied_id_is_kept():
    entry = geo_runlog.record_run(BRAND_ID, {"id": "run-1", "day": "20260830"})
    assert entry["id"] == "run-1"


def test_runs_come_back_newest_first_and_n_limits_them():
    for i in range(3):
        geo_runlog.record_run(BRAND_ID, {"id": f"r{i}", "day": f"2026082{i}"})

    assert [r["id"] for r in geo_runlog.recent_runs(BRAND_ID)] == ["r2", "r1", "r0"]
    assert [r["id"] for r in geo_runlog.recent_runs(BRAND_ID, n=2)] == ["r2", "r1"]
    assert geo_runlog.recent_runs(BRAND_ID, n=0) == []


def test_the_log_is_capped_at_max_runs_dropping_the_oldest():
    extra = 5
    for i in range(geo_runlog.MAX_RUNS + extra):
        geo_runlog.record_run(BRAND_ID, {"id": f"r{i}"})

    runs = geo_runlog.recent_runs(BRAND_ID, n=10_000)
    assert len(runs) == geo_runlog.MAX_RUNS
    assert runs[0]["id"] == f"r{geo_runlog.MAX_RUNS + extra - 1}"
    assert runs[-1]["id"] == f"r{extra}"


def test_re_recording_an_id_replaces_rather_than_duplicates():
    geo_runlog.record_run(BRAND_ID, {"id": "r1", "steps": 1})
    geo_runlog.record_run(BRAND_ID, {"id": "r1", "steps": 2})

    runs = geo_runlog.recent_runs(BRAND_ID)
    assert len(runs) == 1 and runs[0]["steps"] == 2


def test_recent_runs_is_empty_for_a_brand_never_swept():
    assert geo_runlog.recent_runs("nobody") == []


def test_the_doc_is_keyed_per_brand():
    geo_runlog.record_run("a", {"id": "ra"})
    geo_runlog.record_run("b", {"id": "rb"})
    assert [r["id"] for r in geo_runlog.recent_runs("a")] == ["ra"]
    assert [r["id"] for r in geo_runlog.recent_runs("b")] == ["rb"]
    assert state.load(geo_runlog.runlog_doc_id("a"))["brand_id"] == "a"


def test_concurrent_records_are_all_kept():
    with ThreadPoolExecutor(max_workers=8) as pool:
        for f in [
            pool.submit(geo_runlog.record_run, BRAND_ID, {"id": f"r{i}"})
            for i in range(20)
        ]:
            f.result()
    assert {r["id"] for r in geo_runlog.recent_runs(BRAND_ID)} == {f"r{i}" for i in range(20)}


# ---------------------------------------------------------- plan progress ----


# ------------------------------------------- sweeps inside a report window
# The report scales its per-engine `n_expected` (one sweep's worth) by this.
# Getting it wrong invents a shortfall, or hides one.


def test_sweep_days_counts_days_inside_the_window_only():
    for day in ("20260828", "20260830", "20260901"):
        geo_runlog.record_run(BRAND_ID, {"day": day, "trigger": "cron"})

    window = ["20260830", "20260831", "20260901"]
    assert geo_runlog.sweep_days(BRAND_ID, window) == {"20260830", "20260901"}


def test_two_sweeps_on_one_day_are_one_day_not_two():
    """A cron sweep truncated by the wall clock and continued by a manual check
    the same day writes TWO entries for ONE day's answers — a day-doc holds one
    record per (prompt, run) however many sweeps touched it. Counting entries
    would price that day twice and invent a shortfall out of arithmetic."""
    geo_runlog.record_run(BRAND_ID, {"day": "20260901", "trigger": "cron"})
    geo_runlog.record_run(BRAND_ID, {"day": "20260901", "trigger": "manual"})

    assert len(geo_runlog.recent_runs(BRAND_ID)) == 2
    assert geo_runlog.sweep_days(BRAND_ID, ["20260901"]) == {"20260901"}


def test_a_brand_with_no_sweeps_in_the_window_counts_zero():
    """Zero is a real answer — "no checks in this period" — and the panel says
    that rather than dividing by it."""
    geo_runlog.record_run(BRAND_ID, {"day": "20260701", "trigger": "cron"})

    assert geo_runlog.sweep_days(BRAND_ID, ["20260901", "20260902"]) == set()
    assert geo_runlog.sweep_days("never-swept", ["20260901"]) == set()


def test_an_entry_with_no_day_is_skipped_not_counted():
    """A malformed entry must not add a phantom check to the denominator."""
    geo_runlog.record_run(BRAND_ID, {"trigger": "cron"})
    geo_runlog.record_run(BRAND_ID, {"day": None, "trigger": "cron"})

    assert geo_runlog.sweep_days(BRAND_ID, ["20260901"]) == set()


def test_plan_progress_is_none_without_a_plan():
    assert geo_runlog.plan_progress(BRAND_ID) is None


def test_plan_progress_is_none_for_a_plan_doc_with_no_current_plan():
    state.save(geo_strategy.strategy_doc_id(BRAND_ID), {"brand_id": BRAND_ID, "history": []})
    assert geo_runlog.plan_progress(BRAND_ID) is None


def _plan() -> dict:
    return {
        "brand_id": BRAND_ID,
        "history": [],
        "current": {
            "generated_at": "2026-08-20T00:00:00+00:00",
            "summary": "measured",
            "waves": [
                {"weeks": "1-2", "title": "Quick wins", "objective": "", "why_evidence": "",
                 "actions": [
                     {"id": "a1", "title": "A", "status": "todo"},
                     {"id": "a2", "title": "B", "status": "todo"},
                 ]},
                {"weeks": "3-4", "title": "Mentions", "objective": "", "why_evidence": "",
                 "actions": [{"id": "a3", "title": "C", "status": "todo"}]},
            ],
        },
    }


def test_plan_progress_counts_done_actions_over_the_whole_plan():
    state.save(geo_strategy.strategy_doc_id(BRAND_ID), _plan())
    assert geo_runlog.plan_progress(BRAND_ID) == {"done": 0, "total": 3}

    geo_strategy.set_action_status(BRAND_ID, "a2", "done")
    geo_strategy.set_action_status(BRAND_ID, "a3", "skipped")   # not done, still counted

    assert geo_runlog.plan_progress(BRAND_ID) == {"done": 1, "total": 3}
