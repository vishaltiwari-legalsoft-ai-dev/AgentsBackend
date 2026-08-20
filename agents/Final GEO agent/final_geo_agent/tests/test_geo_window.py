"""The measured window — and the READ COST of the paths that use it.

Output tests already cover what these numbers mean. What was never pinned is
what they cost to produce, and every regression this module exists to prevent
was a cost regression that produced perfectly correct output:

* ``generate_strategy`` opening the same week twice per request,
* ``rescan_mentions`` staying on one round trip per ``(day, engine)`` while its
  three sibling readers were batched,
* the brand listing fetching a week of answer text per brand to take a ``len``.

So these assert fetch counts, not values. Each one has been mutation-checked
against the pre-window code: revert the batching and the test fails.
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from final_geo_agent import (
    geo_engines, geo_history, geo_metrics, geo_poll, geo_strategy, geo_venues,
    geo_window,
)
from final_geo_agent.geo_engines import EngineAnswer
from seo_geo_agent import state

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}

CLIO = {"key": "clio", "name": "Clio", "aliases": ["Clio"], "domain": "clio.com"}


# --------------------------------------------------------------- harness ----


@pytest.fixture()
def reads(monkeypatch):
    """Record every batched fetch, and every single-doc read outside a batch.

    The distinction is the whole point. Both shapes read the same documents;
    only the batched one does it in a single round trip, so counting
    ``state.load`` alone cannot tell the old code from the new. ``solo`` is
    therefore "round trips nobody batched" — the number that used to be 120.
    """
    batches: list[list[str]] = []
    solo: list[str] = []
    depth = SimpleNamespace(n=0)
    real_load_many, real_load = geo_window.load_many, state.load

    def counting_load_many(doc_ids):
        batches.append(list(doc_ids))
        depth.n += 1
        try:
            return real_load_many(doc_ids)
        finally:
            depth.n -= 1

    def counting_load(doc_id):
        if depth.n == 0:  # top level is single-threaded, so this is safe
            solo.append(doc_id)
        return real_load(doc_id)

    monkeypatch.setattr(geo_window, "load_many", counting_load_many)
    monkeypatch.setattr(state, "load", counting_load)

    log = SimpleNamespace(
        batches=batches, solo=solo,
        poll_batches=lambda: [b for b in batches if _is_poll(b[0])],
        solo_polls=lambda: [d for d in solo if _is_poll(d)],
        fetched=lambda: sum(len(b) for b in batches if _is_poll(b[0])),
        reset=lambda: (batches.clear(), solo.clear()),
    )
    return log


def _is_poll(doc_id: str) -> bool:
    return doc_id.startswith("geo-poll-")


@pytest.fixture()
def fake_engine(monkeypatch):
    monkeypatch.setattr(
        geo_engines, "available_engines",
        lambda: {"perplexity": True, "gemini": False, "chatgpt": False, "aio": False},
    )
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, model="fake",
            text=f"Legal Soft and Clio both answer: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        ),
    )


def seed(n_prompts=3, runs=1):
    """One completed sweep of ``n_prompts x runs`` answers on today's day-doc."""
    from final_geo_agent import geo_prompts

    geo_prompts.save_universe(BRAND["id"], [
        {"id": f"p{i}", "text": f"buyer question {i}", "intent": "category",
         "stage": "consideration", "enabled": True}
        for i in range(n_prompts)
    ])
    geo_poll.poll_step(BRAND, runs=runs, batch_size=100)


# ---------------------------------------------------- the window contract ----


def test_readers_scan_every_engine_including_aio():
    """The scar at geo_engines:74-77 is a comment; this is the assertion.

    A reader that scans ``ENGINE_KEY_FIELDS`` loses every stored AI Overview
    answer — it shipped once. The window names the constant so no reader has to
    choose, and this pins the one it names.
    """
    assert geo_window.ENGINES == geo_engines.ALL_ENGINES
    assert geo_engines.AIO_ENGINE in geo_window.ENGINES


def test_the_day_doc_id_shape_is_written_down_once():
    """Every reader and the writer must agree on the id, so there is one copy.

    A second ``f"geo-poll-..."`` anywhere is how a reader ends up looking for
    documents no writer makes — cheap to type, invisible until a panel is empty.
    """
    package = pathlib.Path(geo_window.__file__).parent
    owners = sorted(
        path.name
        for path in package.glob("*.py")
        if 'f"geo-poll-' in path.read_text(encoding="utf-8")
    )
    assert owners == ["geo_window.py"]


def test_clamp_owns_the_window_bounds():
    assert geo_window.clamp_days(0) == geo_window.MIN_DAYS
    assert geo_window.clamp_days(9999) == geo_window.MAX_DAYS
    assert geo_window.clamp_days(7) == 7
    # a query string is not a promise of a number
    assert geo_window.clamp_days("nonsense") == geo_window.DEFAULT_DAYS
    assert geo_window.clamp_days(None) == geo_window.DEFAULT_DAYS


def test_backfill_window_fits_inside_the_readable_window():
    """A trend backfill wider than the window it reads would quietly measure
    less than it claims."""
    assert geo_history.BACKFILL_DAYS <= geo_window.MAX_DAYS


def test_window_reports_the_clamped_days_it_actually_measured():
    window = geo_window.open_window(BRAND, 9999)
    assert window.days == geo_window.MAX_DAYS
    assert len(window.day_ids) == geo_window.MAX_DAYS


def test_opening_a_window_fetches_nothing_until_it_is_read(reads, fake_engine):
    """/history with its points already banked must not pay for answers."""
    seed(2)
    reads.reset()
    window = geo_window.open_window(BRAND, 7)
    assert reads.batches == []          # construction is free
    assert window.answers               # ... and this is what pays
    assert len(reads.poll_batches()) == 1


def test_a_window_is_fetched_and_scored_at_most_once(reads, fake_engine):
    seed(3)
    reads.reset()
    window = geo_window.open_window(BRAND, 7)
    for _ in range(3):
        assert window.answers is window.answers
        assert window.report is window.report
    assert len(reads.poll_batches()) == 1


# ------------------------------------------------------ (a) one window per
#                                                        POST /strategy/generate


def test_strategy_generate_opens_the_window_once(reads, fake_engine, monkeypatch):
    """One request, one window.

    ``generate_strategy`` called ``collect_baseline`` (which fetched the week
    and built the report) and then fetched the identical week and rebuilt the
    identical report itself: 2 x (7 days x 4 engines) = 56 day-doc fetches and
    two full ``engine_report`` runs over byte-identical data.
    """
    seed(n_prompts=7, runs=3)
    monkeypatch.setattr(geo_venues, "discover", lambda *a, **k: {
        "category": "legal va", "venues": [], "counts": {}, "searched": 0,
        "errors": [], "complete": True,
    })
    # one ON-SITE action (no venue), so the plan survives ``_clean`` without
    # this test having to care about venue verification — proven elsewhere
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda system, prompt: {
        "summary": "measured", "expectations": "weeks for retrieval",
        "monitoring": {"cadence": "weekly", "review_ritual": "read 3 numbers",
                       "leading_indicators": ["gap replies"]},
        "waves": [{
            "weeks": "1-2", "title": "Quick wins", "objective": "close gaps",
            "why_evidence": "g2.com cited where we are absent",
            "actions": [{
                "title": "Add answer blocks to /intake-services", "venue": "",
                "deliverable": "three 80-word answer blocks",
                "steps": ["Pick the three missing questions", "Draft the blocks",
                          "Publish"],
                "detail": "Answer-shaped, above the fold.",
                "owner_role": "content", "effort": "low", "impact": "high",
                "kpi": "mention_rate", "target": "30%",
                "why_evidence": "named in 0 of these answers",
            }],
        }],
    })
    reports = []
    real_report = geo_metrics.engine_report
    monkeypatch.setattr(geo_metrics, "engine_report",
                        lambda *a: (reports.append(1), real_report(*a))[1])

    reads.reset()
    geo_strategy.generate_strategy(BRAND)

    expected = geo_window.DEFAULT_DAYS * len(geo_window.ENGINES)
    assert len(reads.poll_batches()) == 1, "the measured week is fetched twice"
    assert reads.fetched() == expected == 28
    assert len(reports) == 1, "engine_report recomputed over identical answers"


# ------------------------------------------ (c)/(#6) rescan reads in one batch


def test_rescan_reads_the_whole_window_in_one_batch(reads, fake_engine):
    """30 days used to be 120 SERIAL probes before a single transaction ran.

    It was the one fan-out of four that never got the batched fetch, because the
    pattern was an idiom rather than a module.
    """
    seed(2)
    geo_poll.save_config(BRAND["id"], {"competitors": [CLIO]})
    reads.reset()

    result = geo_poll.rescan_mentions(BRAND, days=30)

    assert result["answers_updated"] == 2
    assert len(reads.poll_batches()) == 1, "day-docs are not fetched in one batch"
    assert reads.fetched() == 30 * len(geo_window.ENGINES) == 120
    # one solo read per doc actually rewritten (the transaction's own), and
    # nothing else: the 120 pre-flight probes are gone
    assert len(reads.solo_polls()) == 1


def test_rescan_writes_only_the_docs_that_changed(reads, fake_engine, monkeypatch):
    """A doc whose scoring already matches must not be rewritten byte for byte.

    After tracking one rival that is only named on some days, most of the window
    is unchanged — and a transaction per (day, engine) to prove it is exactly
    the cost this path was paying.
    """
    seed(2)
    geo_poll.save_config(BRAND["id"], {"competitors": [CLIO]})

    written: list[str] = []
    real_mutate = geo_poll._mutate

    def counting_mutate(doc_id, change):
        if _is_poll(doc_id):
            written.append(doc_id)
        return real_mutate(doc_id, change)

    monkeypatch.setattr(geo_poll, "_mutate", counting_mutate)
    geo_poll.rescan_mentions(BRAND, days=30)
    assert len(written) == 1, "wrote day-docs that had nothing to change"

    # ... and running it again writes nothing at all, because nothing moved
    written.clear()
    second = geo_poll.rescan_mentions(BRAND, days=30)
    assert written == []
    assert second["answers_updated"] == 0
    assert second["answers_scanned"] == 2   # still counted, just not rewritten


def test_rescan_rebuilds_the_trend_without_refetching_the_day(reads, fake_engine):
    """The history rebuild is fed from what the rescan just wrote.

    Re-reading each touched day would put one fetch per engine per touched day
    back on top of the batch already paid for — up to 120 more at 30 days.
    """
    seed(10)
    geo_poll.save_config(BRAND["id"], {"competitors": [CLIO]})
    reads.reset()

    geo_poll.rescan_mentions(BRAND, days=30)

    assert len(reads.poll_batches()) == 1, "the touched day was fetched again"
    points = geo_history.load_points(BRAND["id"])
    assert points and points[0]["competitors"]["clio"] == 1.0


# --------------------------------------- (b)/(#5) counting without hydrating


def test_recent_answer_count_is_exactly_the_corpus_length(fake_engine):
    """The cheap count and the expensive one must never disagree — the whole
    licence for this optimisation is that it is not an approximation."""
    seed(3, runs=2)
    cfg = geo_poll.ensure_config(BRAND)
    assert geo_poll.recent_answer_count(BRAND, cfg) == 6
    assert geo_poll.recent_answer_count(BRAND, cfg) == len(
        geo_poll.recent_answers(BRAND["id"], days=7)
    )


def test_counting_a_polled_brand_reads_no_day_docs(reads, fake_engine):
    seed(3)
    cfg = geo_poll.ensure_config(BRAND)
    geo_poll.recent_answer_count(BRAND, cfg)     # first call reconstructs
    cfg = geo_poll.ensure_config(BRAND)
    reads.reset()

    assert geo_poll.recent_answer_count(BRAND, cfg) == 3
    assert reads.poll_batches() == [], "hydrated the corpus to take a len()"
    assert reads.solo_polls() == []


def test_a_brand_polled_before_the_counter_existed_is_counted_once(reads, fake_engine):
    """The un-backfilled case, stated exactly.

    A brand with a month of stored answers and no counter must not read as "not
    polled yet" — the panel renders 0 as exactly that. So the counter is
    reconstructed from the day-docs on the first listing (one batched window
    read, the same one-time move ``geo_history.backfill`` makes), stamped, and
    never paid for again. There is no state in which the listing shows a
    confident wrong number.
    """
    seed(3)
    # simulate a brand whose answers predate the counter entirely
    cfg = geo_poll.ensure_config(BRAND)
    cfg.pop("answer_counts", None)
    cfg.pop("answer_counts_at", None)
    state.save(geo_poll.config_doc_id(BRAND["id"]), cfg)
    reads.reset()

    assert geo_poll.recent_answer_count(BRAND, cfg) == 3      # honest, not 0
    assert len(reads.poll_batches()) == 1
    assert reads.fetched() == geo_window.MAX_DAYS * len(geo_window.ENGINES)

    stamped = geo_poll.ensure_config(BRAND)
    assert stamped["answer_counts_at"]
    reads.reset()
    assert geo_poll.recent_answer_count(BRAND, stamped) == 3
    assert reads.poll_batches() == []            # reconstructed once, not again


def test_counting_falls_back_to_the_window_when_the_config_is_unreadable(fake_engine):
    """``cfg=None`` is "we know nothing about this brand right now". Counting the
    window is expensive; inventing the number is worse."""
    seed(2)
    assert geo_poll.recent_answer_count(BRAND, None) == 2


def test_a_never_polled_brand_counts_zero(fake_engine):
    cfg = geo_poll.ensure_config(BRAND)
    assert geo_poll.recent_answer_count(BRAND, cfg) == 0


def test_the_counter_survives_a_retry_that_replaces_an_error_record(monkeypatch):
    """The count is the day-doc's true length, so a retry that REPLACES a failed
    record must not make it drift upwards."""
    from final_geo_agent import geo_prompts

    geo_prompts.save_universe(BRAND["id"], [
        {"id": "p1", "text": "q", "intent": "category", "stage": "consideration",
         "enabled": True},
    ])
    monkeypatch.setattr(
        geo_engines, "available_engines",
        lambda: {"perplexity": True, "gemini": False, "chatgpt": False, "aio": False},
    )
    failing = {"on": True}

    def flaky(engine, prompt):
        if failing["on"]:
            return EngineAnswer(engine=engine, model="fake", error="429 slow down")
        return EngineAnswer(engine=engine, model="fake", text="Legal Soft answers")

    monkeypatch.setattr(geo_engines, "poll_engine", flaky)
    geo_poll.poll_step(BRAND, runs=1, batch_size=5)
    failing["on"] = False
    geo_poll.poll_step(BRAND, runs=1, batch_size=5)

    cfg = geo_poll.ensure_config(BRAND)
    assert geo_poll.recent_answer_count(BRAND, cfg) == 1
    assert len(geo_poll.recent_answers(BRAND["id"], days=7)) == 1
