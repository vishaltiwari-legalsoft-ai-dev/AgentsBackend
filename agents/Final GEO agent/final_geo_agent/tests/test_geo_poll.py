"""GEO polling + prompt universe — offline, engines faked at the adapter seam."""
import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from final_geo_agent import geo_engines, geo_poll, geo_prompts
from final_geo_agent.geo_engines import EngineAnswer
from seo_geo_agent import state
from seo_geo_agent.sources import CredentialMissing

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}


def seed_prompts(n=3):
    prompts = [
        {"id": f"p{i}", "text": f"best legal va provider {i}", "intent": "category",
         "stage": "consideration", "enabled": True}
        for i in range(1, n + 1)
    ]
    geo_prompts.save_universe(BRAND["id"], prompts)
    return prompts


# --------------------------------------------------------------- mentions ----

def test_detect_mentions_ranks_by_first_occurrence():
    aliases = {"self": ["Legal Soft"], "comp": ["Acme Corp"]}
    text = "Acme Corp leads, but Legal Soft is a strong alternative."
    mentions = geo_poll.detect_mentions(text, aliases)
    assert mentions == {"comp": 1, "self": 2}


def test_detect_mentions_word_boundary_no_substring_hits():
    mentions = geo_poll.detect_mentions("rampant growth", {"self": ["Ramp"]})
    assert mentions == {}


def test_detect_mentions_matches_domain_alias():
    mentions = geo_poll.detect_mentions("see legalsoft.com for details",
                                        {"self": ["Legal Soft", "legalsoft.com"]})
    assert mentions == {"self": 1}


# ----------------------------------------------------------------- config ----

def test_ensure_config_seeds_self_aliases():
    cfg = geo_poll.ensure_config(BRAND)
    assert "Legal Soft" in cfg["aliases"]["self"]
    assert "legalsoft.com" in cfg["aliases"]["self"]
    assert cfg["daily_cap"] == geo_poll.DEFAULT_DAILY_CAP


def test_save_config_patches_only_known_keys():
    geo_poll.ensure_config(BRAND)
    cfg = geo_poll.save_config(BRAND["id"], {"daily_cap": 50, "bogus": 1})
    assert cfg["daily_cap"] == 50
    assert "bogus" not in cfg


# ------------------------------------------------------------------ polls ----

@pytest.fixture()
def fake_engine(monkeypatch):
    calls = []

    def scripted(engine, prompt):
        calls.append((engine, prompt))
        return EngineAnswer(
            engine=engine, model="fake", text=f"Legal Soft answers: {prompt}",
            citations=[{"url": "https://g2.com/x", "domain": "g2.com", "title": "G2"}],
        )

    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(geo_engines, "poll_engine", scripted)
    return calls


def test_poll_step_requires_prompts(fake_engine):
    with pytest.raises(ValueError):
        geo_poll.poll_step(BRAND)


def test_poll_step_requires_engine_keys(monkeypatch):
    seed_prompts()
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": False, "gemini": False, "chatgpt": False})
    with pytest.raises(CredentialMissing):
        geo_poll.poll_step(BRAND)


def test_poll_step_batches_resume_and_parse(fake_engine):
    seed_prompts(3)
    # 3 prompts x 2 runs x 1 engine = 6 tasks
    first = geo_poll.poll_step(BRAND, runs=2, batch_size=4)
    assert (first["done"], first["total"]) == (4, 6)
    assert first["capped"] is False
    second = geo_poll.poll_step(BRAND, runs=2, batch_size=10)
    assert (second["done"], second["total"]) == (6, 6)
    assert len(fake_engine) == 6  # no task re-run on resume

    answers = geo_poll.recent_answers(BRAND["id"], days=1)
    assert len(answers) == 6
    record = answers[0]
    assert record["brand_mentioned"] is True
    assert record["mentions"]["self"] == 1
    assert record["brand_cited"] is False  # g2.com is not our domain
    assert record["citations"][0]["domain"] == "g2.com"


def test_errored_runs_retry_and_replace_on_next_poll(monkeypatch):
    """A 429-style failed run must stay PENDING (not eat the day) and the
    retry must replace the stale error record, not pile up next to it."""
    seed_prompts(1)
    behavior = {"fail": True}

    def scripted(engine, prompt):
        if behavior["fail"]:
            return EngineAnswer(engine=engine, error="HTTP 429: quota exceeded")
        return EngineAnswer(engine=engine, model="fake", text="Legal Soft wins.")

    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(geo_engines, "poll_engine", scripted)

    first = geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    assert (first["done"], first["total"]) == (1, 1)
    assert geo_poll.recent_answers(BRAND["id"], days=1)[0]["error"]

    behavior["fail"] = False
    retry = geo_poll.poll_step(BRAND, runs=1, batch_size=10)   # quota back -> retried
    assert (retry["done"], retry["total"]) == (1, 1)
    answers = geo_poll.recent_answers(BRAND["id"], days=1)
    assert len(answers) == 1                       # error record replaced, not appended
    assert answers[0]["error"] is None
    assert answers[0]["brand_mentioned"] is True


def test_poll_step_honors_daily_cap(fake_engine):
    seed_prompts(3)
    geo_poll.ensure_config(BRAND)
    geo_poll.save_config(BRAND["id"], {"daily_cap": 4})
    first = geo_poll.poll_step(BRAND, runs=2, batch_size=10)
    assert first["done"] == 4  # budget clamped to the cap
    second = geo_poll.poll_step(BRAND, runs=2, batch_size=10)
    assert second["capped"] is True
    assert second["calls_used_today"] == 4
    assert len(fake_engine) == 4


# ------------------------------------------------------- terminal signal ----
# An errored run stays pending by design, so a dead key means `done` can never
# reach `total` — the UI loop only ever stopped when the daily budget ran out.
# These pin the exit: terminal + terminal_reason (a cross-agent contract with
# the console poll loop — do not rename or reshape).

def _engines(monkeypatch, **available):
    monkeypatch.setattr(
        geo_engines, "available_engines",
        lambda: {"perplexity": False, "gemini": False, "chatgpt": False} | available,
    )


def test_healthy_poll_carries_the_terminal_contract_and_is_not_terminal(fake_engine):
    seed_prompts(2)
    res = geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    assert res["terminal"] is False
    assert res["terminal_reason"] is None
    # nothing left pending: still non-terminal, and the keys still exist
    idle = geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    assert (idle["terminal"], idle["terminal_reason"]) == (False, None)


def test_fully_errored_batch_is_terminal_and_names_the_failure(monkeypatch):
    seed_prompts(3)
    _engines(monkeypatch, perplexity=True)
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(
            engine=engine, error="HTTP 401: invalid api key"),
    )
    res = geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    assert res["terminal"] is True
    # the reason must be actionable: which engine, what actually failed
    assert "perplexity" in res["terminal_reason"]
    assert "HTTP 401: invalid api key" in res["terminal_reason"]


def test_partial_failure_is_not_terminal(monkeypatch):
    """One engine down while another answers is a degraded poll, not a dead
    one — it still makes forward progress, so the loop keeps going."""
    seed_prompts(2)
    _engines(monkeypatch, perplexity=True, gemini=True)
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(engine=engine, error="HTTP 500: upstream")
        if engine == "perplexity"
        else EngineAnswer(engine=engine, model="fake", text="Legal Soft answers."),
    )
    res = geo_poll.poll_step(BRAND, runs=1, batch_size=3)
    assert res["terminal"] is False
    assert res["terminal_reason"] is None


def test_consecutive_engine_failures_terminate(monkeypatch):
    """A batch that keeps making SOME progress never trips the all-failed rule,
    so a permanently dead engine needs the streak rule to stop the burn."""
    seed_prompts(4)
    _engines(monkeypatch, perplexity=True, gemini=True)
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(engine=engine, error="HTTP 503: engine down")
        if engine == "perplexity"
        else EngineAnswer(engine=engine, model="fake", text="Legal Soft answers."),
    )
    # 4 perplexity tasks (always fail, always pending) + 1 gemini task per step
    results = [
        geo_poll.poll_step(BRAND, runs=1, batch_size=5)
        for _ in range(geo_poll.FAIL_STREAK_LIMIT)
    ]
    assert [r["terminal"] for r in results[:-1]] == [False] * (
        geo_poll.FAIL_STREAK_LIMIT - 1
    )
    last = results[-1]
    assert last["terminal"] is True
    assert "perplexity" in last["terminal_reason"]
    assert f"{geo_poll.FAIL_STREAK_LIMIT} consecutive" in last["terminal_reason"]
    assert "gemini" not in last["terminal_reason"]   # the healthy engine is not blamed


def test_a_success_clears_the_failure_streak(monkeypatch):
    seed_prompts(2)
    _engines(monkeypatch, perplexity=True)
    behavior = {"fail": True}
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(engine=engine, error="HTTP 429: slow down")
        if behavior["fail"]
        else EngineAnswer(engine=engine, model="fake", text="Legal Soft answers."),
    )
    geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    behavior["fail"] = False
    geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    cfg = geo_poll.ensure_config(BRAND)
    assert cfg["poll_health"]["streaks"]["perplexity"] == 0


def test_hitting_the_daily_cap_is_terminal(fake_engine):
    seed_prompts(3)
    geo_poll.ensure_config(BRAND)
    geo_poll.save_config(BRAND["id"], {"daily_cap": 4})
    geo_poll.poll_step(BRAND, runs=2, batch_size=10)
    capped = geo_poll.poll_step(BRAND, runs=2, batch_size=10)
    assert (capped["capped"], capped["terminal"]) == (True, True)
    assert "4 of 4" in capped["terminal_reason"]


# ------------------------------------------------------------- atomicity ----
# The cap is the only thing between a signed-in user and the engine bill, and
# it used to be a read-modify-write race: two overlapping steps both read 1990
# against a 2000 cap, both fire ten paid calls, both write 2000.

def test_concurrent_reservations_never_oversell_the_cap():
    geo_poll.ensure_config(BRAND)
    cfg = geo_poll.ensure_config(BRAND)
    cfg["daily_cap"] = 2000
    cfg.setdefault("counters", {})[geo_poll._today()] = 1990
    state.save(geo_poll.config_doc_id(BRAND["id"]), cfg)

    day = geo_poll._today()
    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = [
            f.result()[0] for f in [
                pool.submit(geo_poll._reserve_calls, BRAND["id"], day, 10)
                for _ in range(8)
            ]
        ]
    assert sum(granted) == 10          # 10 head-room, 80 wanted, 10 handed out
    after = geo_poll.ensure_config(BRAND)
    assert after["counters"][day] == 2000


def test_concurrent_poll_steps_stay_inside_the_daily_cap(monkeypatch):
    seed_prompts(6)
    calls = []
    lock = threading.Lock()

    def slow(engine, prompt):
        with lock:
            calls.append(prompt)
        time.sleep(0.01)          # widen the window the old race needed
        return EngineAnswer(engine=engine, model="fake", text="Legal Soft answers.")

    _engines(monkeypatch, perplexity=True)
    monkeypatch.setattr(geo_engines, "poll_engine", slow)
    geo_poll.ensure_config(BRAND)
    geo_poll.save_config(BRAND["id"], {"daily_cap": 5})

    with ThreadPoolExecutor(max_workers=3) as pool:
        for f in [
            pool.submit(geo_poll.poll_step, BRAND, None, 1, 10) for _ in range(3)
        ]:
            f.result()
    assert len(calls) == 5                                   # cap held exactly
    cfg = geo_poll.ensure_config(BRAND)
    assert cfg["counters"][geo_poll._today()] == 5           # and every call counted


def test_concurrent_answer_writes_do_not_lose_records():
    day = geo_poll._today()

    def write(i):
        geo_poll._merge_answers(BRAND["id"], "perplexity", day, [
            {"prompt_id": f"p{i}", "run": 1, "engine": "perplexity",
             "text": "answer", "error": None},
        ])

    with ThreadPoolExecutor(max_workers=8) as pool:
        for f in [pool.submit(write, i) for i in range(20)]:
            f.result()
    doc = state.load(geo_poll.poll_doc_id(BRAND["id"], "perplexity", day))
    assert len(doc["answers"]) == 20
    assert {a["prompt_id"] for a in doc["answers"]} == {f"p{i}" for i in range(20)}


# ---------------------------------------------------------------- prompts ----

def test_generate_universe_cleans_and_persists(monkeypatch):
    raw = {
        "prompts": [
            {"text": "Best legal VA service?", "intent": "category", "stage": "purchase"},
            {"text": "Best legal VA service?", "intent": "category", "stage": "purchase"},  # dupe
            {"text": "How to cut law-firm admin costs", "intent": "weird", "stage": "nope"},
            {"text": ""},
        ]
    }
    monkeypatch.setattr(geo_prompts, "llm_json", lambda *a, **k: raw)
    doc = geo_prompts.generate_universe(BRAND)
    texts = [p["text"] for p in doc["prompts"]]
    assert len(texts) == 2 and len(set(texts)) == 2
    weird = doc["prompts"][1]
    assert weird["intent"] == "category" and weird["stage"] == "consideration"
    assert all(p["enabled"] and p["id"] for p in doc["prompts"])
    assert geo_prompts.enabled_prompts(BRAND["id"]) == doc["prompts"]


def test_generate_universe_empty_llm_raises(monkeypatch):
    monkeypatch.setattr(geo_prompts, "llm_json", lambda *a, **k: {"prompts": []})
    with pytest.raises(ValueError):
        geo_prompts.generate_universe(BRAND)


# ---------------------------------------------------------- custom prompts ----

def test_add_custom_prompt_and_duplicate_rejected():
    geo_prompts.add_custom_prompt(BRAND["id"], "Which intake service handles Spanish-speaking clients?")
    universe = geo_prompts.load_universe(BRAND["id"])
    assert universe["prompts"][-1]["source"] == "custom"
    with pytest.raises(ValueError):
        geo_prompts.add_custom_prompt(BRAND["id"], "which intake service handles spanish-speaking clients?")
    with pytest.raises(ValueError):
        geo_prompts.add_custom_prompt(BRAND["id"], "hey")          # too short


def test_regenerate_preserves_custom_prompts(monkeypatch):
    geo_prompts.add_custom_prompt(BRAND["id"], "Can a virtual receptionist do conflict checks?")
    monkeypatch.setattr(geo_prompts, "llm_json", lambda *a, **k: {"prompts": [
        {"text": "best legal intake service", "intent": "category", "stage": "consideration"},
        {"text": "can a virtual receptionist do conflict checks?", "intent": "category", "stage": "purchase"},
    ]})
    universe = geo_prompts.generate_universe(BRAND)
    texts = [p["text"] for p in universe["prompts"]]
    sources = {p["text"]: p["source"] for p in universe["prompts"]}
    assert "Can a virtual receptionist do conflict checks?" in texts     # custom survived
    assert sources["Can a virtual receptionist do conflict checks?"] == "custom"
    assert texts.count("can a virtual receptionist do conflict checks?") == 0  # AI dupe suppressed
    assert sources["best legal intake service"] == "ai"


# ------------------------------------------------------------- AIO polling ----

@pytest.fixture()
def aio_engine(monkeypatch):
    calls = []

    def scripted(engine, prompt):
        calls.append((engine, prompt))
        if engine == "aio":
            return EngineAnswer(engine="aio", model="google-ai-overview", via="serpapi",
                                text="Legal Soft appears in the AI Overview.", credits=1)
        return EngineAnswer(engine=engine, model="fake", text="Legal Soft answers.")

    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False, "aio": True})
    monkeypatch.setattr(geo_engines, "poll_engine", scripted)
    return calls


def test_aio_polls_single_run_and_counts_credits(aio_engine):
    seed_prompts(2)
    res = geo_poll.poll_step(BRAND, runs=3, batch_size=50)
    # perplexity 2x3 + aio 2x1 = 8 tasks, not 12
    assert (res["done"], res["total"]) == (8, 8)
    assert res["aio_credits_month"] == 2
    aio_calls = [c for c in aio_engine if c[0] == "aio"]
    assert len(aio_calls) == 2


def test_aio_monthly_cap_drops_aio_not_the_poll(aio_engine):
    seed_prompts(2)
    geo_poll.ensure_config(BRAND)
    geo_poll.save_config(BRAND["id"], {"aio_monthly_cap": 1})
    cfg = geo_poll.ensure_config(BRAND)
    cfg.setdefault("counters_aio", {})[geo_poll._month()] = 1   # budget already spent
    geo_poll.save_config(BRAND["id"], {})
    from seo_geo_agent import state as _state
    _state.save(geo_poll.config_doc_id(BRAND["id"]), cfg)

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=50)
    assert res["aio_capped"] is True
    assert "aio" not in res["engines"]                          # chat engines still polled
    assert (res["done"], res["total"]) == (2, 2)


def test_recent_answers_includes_aio_docs(aio_engine):
    seed_prompts(1)
    geo_poll.poll_step(BRAND, runs=1, batch_size=10)
    engines_seen = {a["engine"] for a in geo_poll.recent_answers(BRAND["id"], days=1)}
    assert "aio" in engines_seen          # stored AIO answers must be readable everywhere
    assert "perplexity" in engines_seen


# ------------------------------------------------------- concurrency ----
# ~400 sequential engine calls at ~5s each is the half hour that made polling
# unusable. Overlapping them must not disturb the order the caller settles
# budget and failure streaks in.


def test_batch_polls_concurrently_but_returns_in_submission_order(monkeypatch):
    seed_prompts(4)
    started = threading.Barrier(geo_poll.POLL_CONCURRENCY, timeout=5)

    def scripted(engine, prompt):
        # deadlocks unless POLL_CONCURRENCY calls are genuinely in flight
        started.wait()
        return EngineAnswer(engine=engine, model="fake", text=f"Legal Soft: {prompt}")

    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(geo_engines, "poll_engine", scripted)

    result = geo_poll.poll_step(BRAND, runs=2, batch_size=geo_poll.POLL_CONCURRENCY)

    assert result["done"] == geo_poll.POLL_CONCURRENCY


def test_answers_come_back_in_submission_order_not_completion_order(monkeypatch):
    # slowest task first: a completion-ordered result would reverse these, and
    # per-engine failure streaks would then depend on provider latency
    delays = {"slow": 0.05, "fast": 0.0}

    def scripted(engine, prompt):
        time.sleep(delays[prompt])
        return EngineAnswer(engine=engine, model="fake", text=prompt)

    monkeypatch.setattr(geo_engines, "poll_engine", scripted)
    batch = [("perplexity", {"text": "slow"}, 1), ("perplexity", {"text": "fast"}, 1)]

    assert [a.text for a in geo_poll._answers_for(batch)] == ["slow", "fast"]


# ---------------------------------------------------------- schedule ----
# The cron fires daily; being "due" is decided here, off the last COMPLETED
# sweep, so a truncated run is resumed rather than skipped for two days.


def test_brand_never_polled_is_due_immediately():
    due, reason = geo_poll.poll_due({})
    assert due is True
    assert reason == "never polled"
    assert geo_poll.next_due_at({}) is None


def test_brand_is_not_due_before_its_interval_elapses():
    cfg = {"last_poll_completed_at": "2026-08-18T02:00:00+00:00", "poll_interval_days": 2}
    now = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.timezone.utc)

    due, reason = geo_poll.poll_due(cfg, now=now)

    assert due is False
    assert "2026-08-20" in reason


def test_brand_is_due_once_the_interval_has_elapsed():
    cfg = {"last_poll_completed_at": "2026-08-18T02:00:00+00:00", "poll_interval_days": 2}
    now = dt.datetime(2026, 8, 20, 2, 0, tzinfo=dt.timezone.utc)

    due, _ = geo_poll.poll_due(cfg, now=now)

    assert due is True
    assert geo_poll.next_due_at(cfg).startswith("2026-08-20")


def test_month_boundary_keeps_a_true_two_day_gap():
    # the reason this is an interval and not a `*/2` day-of-month cron step:
    # that expression fires on the 31st and again on the 1st
    cfg = {"last_poll_completed_at": "2026-08-31T02:00:00+00:00", "poll_interval_days": 2}

    assert geo_poll.poll_due(cfg, now=dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc))[0] is False
    assert geo_poll.poll_due(cfg, now=dt.datetime(2026, 9, 2, 2, tzinfo=dt.timezone.utc))[0] is True


def test_auto_poll_off_is_never_due():
    cfg = {"auto_poll": False, "last_poll_completed_at": "2020-01-01T00:00:00+00:00"}

    due, reason = geo_poll.poll_due(cfg)

    assert due is False
    assert "auto-poll is off" in reason


def test_unparseable_timestamp_makes_the_brand_due_rather_than_stuck():
    # a corrupt stamp must fail towards polling; failing the other way would
    # silently stop a brand forever
    assert geo_poll.poll_due({"last_poll_completed_at": "not-a-date"})[0] is True


def test_interval_is_clamped_to_a_sane_range():
    assert geo_poll._interval_days({"poll_interval_days": 0}) == 1
    assert geo_poll._interval_days({"poll_interval_days": 999}) == 30
    assert geo_poll._interval_days({"poll_interval_days": "junk"}) == geo_poll.DEFAULT_POLL_INTERVAL_DAYS
    assert geo_poll._interval_days({}) == geo_poll.DEFAULT_POLL_INTERVAL_DAYS


# ------------------------------------------------------ unattended run ----


def test_poll_until_done_finishes_the_sweep_and_stamps_the_schedule(fake_engine):
    seed_prompts(3)

    result = geo_poll.poll_until_done(BRAND, runs=2, batch_size=4)

    assert (result["done"], result["total"]) == (6, 6)
    assert result["completed"] is True
    assert result["steps"] == 2                      # 4 + 2, no browser involved
    assert len(fake_engine) == 6                     # nothing polled twice
    cfg = geo_poll.ensure_config(BRAND)
    assert cfg["last_poll_completed_at"]
    assert geo_poll.poll_due(cfg)[0] is False        # not due again immediately


def test_poll_until_done_stops_on_budget_and_leaves_the_rest_pending(fake_engine):
    seed_prompts(3)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0])          # one step, then out of time

    result = geo_poll.poll_until_done(
        BRAND, runs=2, batch_size=2, budget_seconds=1.0, clock=lambda: next(ticks)
    )

    assert result["completed"] is False
    assert result["done"] < result["total"]
    assert "budget exhausted" in result["stopped_because"]
    # a truncated sweep must stay due, or two days of data go missing
    assert geo_poll.poll_due(geo_poll.ensure_config(BRAND))[0] is True


def test_poll_until_done_stops_on_a_dead_engine_without_stamping(monkeypatch):
    seed_prompts(2)

    def dead(engine, prompt):
        return EngineAnswer(engine=engine, model="fake", error="HTTP 401: bad key")

    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(geo_engines, "poll_engine", dead)

    result = geo_poll.poll_until_done(BRAND, runs=1, batch_size=2)

    assert result["completed"] is False
    assert result["terminal_reason"]
    assert "last_poll_completed_at" not in geo_poll.ensure_config(BRAND)


def test_poll_status_reports_progress_and_next_due(fake_engine):
    seed_prompts(3)
    geo_poll.poll_step(BRAND, runs=1, batch_size=2)

    status = geo_poll.poll_status(BRAND, runs=1)

    assert (status["done"], status["total"], status["pending"]) == (2, 3, 1)
    assert status["due_now"] is True          # sweep unfinished, never completed
    assert status["interval_days"] == geo_poll.DEFAULT_POLL_INTERVAL_DAYS
    assert status["next_due_at"] is None


# ------------------------------------------------------- engine last seen
# AIO runs once per prompt where chat engines run three times, so it ages out
# of the report window first. Vanishing silently reads as "this engine is
# broken"; the stamp lets the panel say when it was last measured instead.


def test_a_successful_poll_stamps_when_each_engine_was_last_measured(fake_engine):
    seed_prompts(2)

    geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    seen = geo_poll.ensure_config(BRAND).get("engine_last_seen") or {}
    assert "perplexity" in seen and seen["perplexity"]


def test_an_engine_that_only_errored_is_not_stamped_as_measured(monkeypatch):
    seed_prompts(2)
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False})
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="fake", error="HTTP 401"))

    geo_poll.poll_step(BRAND, runs=1, batch_size=4)

    assert not (geo_poll.ensure_config(BRAND).get("engine_last_seen") or {})


def test_no_aio_counts_as_measured_because_the_slot_was_genuinely_checked(monkeypatch):
    seed_prompts(2)
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": False, "gemini": False, "chatgpt": False, "aio": True})
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="fake", no_aio=True))

    geo_poll.poll_step(BRAND, runs=1, batch_size=4)

    # Google showing no overview is an observation, not a failure to observe
    assert "aio" in (geo_poll.ensure_config(BRAND).get("engine_last_seen") or {})
