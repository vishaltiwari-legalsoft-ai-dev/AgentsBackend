"""GEO polling + prompt universe — offline, engines faked at the adapter seam."""
import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from final_geo_agent import geo_engines, geo_history, geo_poll, geo_prompts, geo_runlog
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
    # enough prompts that the HEALTHY engine still has work after three steps —
    # otherwise the queue empties down to the dead engine and the all-failed
    # rule fires first, which is not the rule under test here
    seed_prompts(12)
    _engines(monkeypatch, perplexity=True, gemini=True)
    monkeypatch.setattr(
        geo_engines, "poll_engine",
        lambda engine, prompt: EngineAnswer(engine=engine, error="HTTP 503: engine down")
        if engine == "perplexity"
        else EngineAnswer(engine=engine, model="fake", text="Legal Soft answers."),
    )
    # every step mixes both engines, so some progress is always made
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


def test_saving_config_cannot_roll_back_a_reserved_counter(monkeypatch):
    """``geo-config-{brand}`` carries the settings AND the spend counters.

    Saving the config used to be a plain read-modify-write of that whole
    document, so a save whose read happened before a poll's reservation wrote
    the pre-reservation ``counters`` map straight back — handing out the same
    budget twice, against the only cap that stops a dead key burning 2,000
    paid calls."""
    geo_poll.ensure_config(BRAND)
    day = geo_poll._today()
    doc_id = geo_poll.config_doc_id(BRAND["id"])

    entered, proceed, reserved = threading.Event(), threading.Event(), threading.Event()
    real_load = state.load
    widened = []

    def load(doc_id_arg):
        # Widen the config save's read->write window exactly once — the read
        # happens first, so what the save later writes is a pre-reservation
        # snapshot, which is precisely the shape of the bug.
        if doc_id_arg == doc_id and not widened:
            widened.append(True)
            snapshot = real_load(doc_id_arg)
            entered.set()
            proceed.wait(5)
            return snapshot
        return real_load(doc_id_arg)

    monkeypatch.setattr(state, "load", load)

    def _save():
        geo_poll.save_config(BRAND["id"], {"daily_cap": 500})

    def _reserve():
        geo_poll._reserve_calls(BRAND["id"], day, 12)
        reserved.set()

    saver = threading.Thread(target=_save)
    saver.start()
    assert entered.wait(5), "the config save never read the document"
    reserver = threading.Thread(target=_reserve)
    reserver.start()
    reserved.wait(0.5)  # a transactional save makes the reservation wait its turn
    proceed.set()
    saver.join(5)
    reserver.join(5)

    after = real_load(doc_id)
    assert (after.get("counters") or {}).get(day) == 12, "a config save gave back reserved calls"
    assert after["daily_cap"] == 500  # ...and the save itself still landed


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
    # enough prompts to FILL the pool whatever POLL_CONCURRENCY is tuned to —
    # with fewer tasks than parties the barrier can never trip and the test
    # fails for its own arithmetic rather than for the behaviour under test
    seed_prompts(geo_poll.POLL_CONCURRENCY)
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


# ------------------------- competitor alias derivation -------------------------
# Regression: a rival typed "smith ai" with domain smith.ai was detected in 0 of
# 200 stored answers that write it "Smith.ai". Aliases came from the typed name
# only, while the brand's own have always used name + domain + stem.


def test_competitor_aliases_cover_the_domain_and_its_stem():
    assert geo_poll.competitor_aliases("smith ai", "smith.ai") == [
        "smith ai", "smith.ai", "smith",
    ]


def test_competitor_aliases_strip_scheme_and_www():
    # the stem is already the name here, so it dedupes away rather than
    # doubling the work of every mention scan
    assert geo_poll.competitor_aliases("Clio", "https://www.Clio.com/pricing") == [
        "Clio", "clio.com",
    ]
    assert geo_poll.competitor_aliases("Ruby", "http://ruby.com/") == ["Ruby", "ruby.com"]


def test_competitor_aliases_keep_what_the_team_typed_first():
    aliases = geo_poll.competitor_aliases(
        "Smith.ai", "smith.ai", ["Smith dot AI", "smith.ai"],
    )
    assert aliases[0] == "Smith dot AI"
    assert aliases.count("smith.ai") == 1   # deduped case-insensitively


def test_competitor_aliases_skip_a_stem_too_short_to_mean_anything():
    assert geo_poll.competitor_aliases("G2", "g2.com") == ["G2", "g2.com"]


def test_competitor_with_no_domain_still_matches_its_name():
    assert geo_poll.competitor_aliases("Smokeball", "") == ["Smokeball"]


def test_alias_map_derives_on_read_so_old_configs_need_no_migration():
    """The competitor was saved before the derivation existed — it carries only
    the typed name, and must still be matched on its domain forms."""
    cfg = {
        "aliases": {"self": ["Legal Soft"]},
        "competitors": [{"key": "smith-ai", "name": "smith ai",
                         "domain": "smith.ai", "aliases": ["smith ai"]}],
    }
    assert geo_poll.alias_map(cfg)["smith-ai"] == ["smith ai", "smith.ai", "smith"]


def test_derived_aliases_find_the_rival_the_typed_name_missed():
    cfg = {
        "aliases": {"self": ["Legal Soft"]},
        "competitors": [{"key": "smith-ai", "name": "smith ai", "domain": "smith.ai"}],
    }
    text = "For law firm intake, Smith.ai and Legal Soft are the usual shortlist."
    mentions = geo_poll.detect_mentions(text, geo_poll.alias_map(cfg))
    assert mentions == {"self": 2, "smith-ai": 1}


# ------------------------- task ordering / engine fairness -------------------------
# Regression: tasks were queued engine by engine and a step bills tasks[:granted].
# A full sweep (~410 calls) has never fitted in the cron's wall clock, so the
# engine at the END of that queue was never called. That engine was Google AIO:
# a working SerpAPI key, and 0 of 41 AIO answers collected, every single day.


def test_pending_tasks_interleave_so_no_engine_is_starved():
    prompts = [{"id": f"p{i}"} for i in range(4)]
    docs = {e: {"answers": []} for e in ("perplexity", "gemini", "aio")}

    tasks = geo_poll._pending_tasks(prompts, docs, runs=3)

    # every engine appears inside the first handful of calls, not after
    # a hundred belonging to somebody else
    assert {engine for engine, _p, _r in tasks[:3]} == {"perplexity", "gemini", "aio"}
    assert len(tasks) == 4 * 3 + 4 * 3 + 4      # nothing dropped: aio runs once


def test_a_truncated_sweep_still_measures_every_engine():
    prompts = [{"id": f"p{i}"} for i in range(40)]
    docs = {e: {"answers": []} for e in ("perplexity", "gemini", "chatgpt", "aio")}

    tasks = geo_poll._pending_tasks(prompts, docs, runs=3)
    budget = tasks[:250]        # what one cron fire actually affords
    got: dict[str, int] = {}
    for engine, _p, _r in budget:
        got[engine] = got.get(engine, 0) + 1

    assert got["aio"] == 40                     # was 0 before: fully measured now
    assert min(got[e] for e in ("perplexity", "gemini", "chatgpt")) > 60


def test_already_answered_tasks_are_still_skipped_when_interleaved():
    prompts = [{"id": "p0"}, {"id": "p1"}]
    docs = {
        "perplexity": {"answers": [{"prompt_id": "p0", "run": 1}]},
        "aio": {"answers": []},
    }

    tasks = geo_poll._pending_tasks(prompts, docs, runs=1)

    assert ("perplexity", {"id": "p0"}, 1) not in tasks
    assert sorted((e, p["id"]) for e, p, _r in tasks) == [
        ("aio", "p0"), ("aio", "p1"), ("perplexity", "p1"),
    ]


# ------------------------- AIO spends a small allowance -------------------------


def test_aio_polls_only_the_discovery_prompts():
    prompts = [
        {"id": "b1", "intent": "brand"},
        {"id": "c1", "intent": "category"},
        {"id": "p1", "intent": "problem"},
        {"id": "u1"},                       # no intent recorded -> category
    ]
    assert [p["id"] for p in geo_poll.aio_prompts(prompts)] == ["c1", "p1", "u1"]

    docs = {e: {"answers": []} for e in ("perplexity", "aio")}
    tasks = geo_poll._pending_tasks(prompts, docs, runs=1)
    aio = sorted(p["id"] for e, p, _r in tasks if e == "aio")
    chat = sorted(p["id"] for e, p, _r in tasks if e == "perplexity")

    assert aio == ["c1", "p1", "u1"]        # brand prompt costs no credit
    assert chat == ["b1", "c1", "p1", "u1"]  # chat engines still ask everything


def test_total_tasks_agrees_with_the_queue_or_a_sweep_can_never_finish():
    prompts = [{"id": f"b{i}", "intent": "brand"} for i in range(3)] + [
        {"id": f"c{i}", "intent": "category"} for i in range(5)
    ]
    usable = ["perplexity", "gemini", "aio", "ai_mode"]
    docs = {e: {"answers": []} for e in usable}

    assert geo_poll._total_tasks(prompts, usable, runs=3) == len(
        geo_poll._pending_tasks(prompts, docs, runs=3)
    )


# ------------------------- SERP engines are specs, not identity checks -------------------------
# Google AI Mode joins AI Overview as a second SERP engine. The planner must not
# know its name: one run per prompt, discovery prompts only, and the round-robin
# — all read off the spec.


def test_ai_mode_plans_like_aio_one_run_over_discovery_prompts_only():
    prompts = [
        {"id": "b1", "intent": "brand"},
        {"id": "c1", "intent": "category"},
        {"id": "p1", "intent": "problem"},
    ]
    docs = {e: {"answers": []} for e in ("perplexity", "aio", "ai_mode")}

    tasks = geo_poll._pending_tasks(prompts, docs, runs=3)

    ai_mode = [(p["id"], r) for e, p, r in tasks if e == "ai_mode"]
    assert ai_mode == [("c1", 1), ("p1", 1)]                # no brand prompt, no run 2
    assert {e for e, _p, _r in tasks[:3]} == {"perplexity", "aio", "ai_mode"}   # interleaved
    assert geo_poll._total_tasks(prompts, list(docs), runs=3) == len(tasks) == 9 + 2 + 2


def test_the_caller_sample_size_never_multiplies_a_serp_engine():
    prompts = [{"id": "c1", "intent": "category"}]
    for engine in geo_engines.SERP_ENGINES:
        assert geo_poll._engine_runs(engine, 5) == 1
    assert geo_poll._engine_runs("perplexity", 5) == 5
    assert geo_poll._total_tasks(prompts, ["chatgpt", "aio", "ai_mode"], runs=5) == 5 + 1 + 1


@pytest.fixture()
def serp_engines(monkeypatch):
    calls = []

    def scripted(engine, prompt):
        calls.append((engine, prompt))
        if engine in geo_engines.SERP_ENGINES:
            return EngineAnswer(engine=engine, model=f"google-{engine}", via="dataforseo",
                                text="Legal Soft appears in the AI answer.", credits=1)
        return EngineAnswer(engine=engine, model="fake", text="Legal Soft answers.")

    monkeypatch.setattr(geo_engines, "available_engines", lambda: {
        "perplexity": True, "gemini": False, "chatgpt": False, "aio": True, "ai_mode": True,
    })
    monkeypatch.setattr(geo_engines, "poll_engine", scripted)
    return calls


def test_every_serp_engine_spends_the_joint_monthly_counter(serp_engines):
    seed_prompts(2)

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=50)

    assert (res["done"], res["total"]) == (6, 6)          # 2 chat + 2 aio + 2 ai_mode
    assert res["aio_credits_month"] == 4                  # both SERP engines, not the chat one
    assert sorted(e for e, _p in serp_engines if e in geo_engines.SERP_ENGINES) == [
        "ai_mode", "ai_mode", "aio", "aio",
    ]


def test_the_serp_monthly_cap_is_joint_and_drops_every_serp_engine(serp_engines):
    seed_prompts(2)
    geo_poll.ensure_config(BRAND)
    geo_poll.save_config(BRAND["id"], {"aio_monthly_cap": 3})     # override key unchanged
    cfg = geo_poll.ensure_config(BRAND)
    cfg.setdefault("counters_aio", {})[geo_poll._month()] = 3   # counter key unchanged
    state.save(geo_poll.config_doc_id(BRAND["id"]), cfg)

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=50)

    assert res["aio_capped"] is True
    assert "aio" not in res["engines"] and "ai_mode" not in res["engines"]
    assert res["engines"] == ["perplexity"]                     # chat engines still polled
    assert (res["done"], res["total"]) == (2, 2)
    assert geo_poll.SERP_MONTHLY_CAP == geo_poll.AIO_MONTHLY_CAP == 2000


def test_only_serp_engines_available_and_capped_is_an_honest_error(serp_engines, monkeypatch):
    seed_prompts(2)
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": False, "aio": True, "ai_mode": True})
    cfg = geo_poll.ensure_config(BRAND)
    cfg["aio_monthly_cap"] = 1
    cfg["counters_aio"] = {geo_poll._month(): 1}
    state.save(geo_poll.config_doc_id(BRAND["id"]), cfg)

    with pytest.raises(ValueError, match="monthly"):
        geo_poll.poll_step(BRAND, runs=1, batch_size=50)


# ------------------------- persona rides along on the record -------------------------


def test_persona_is_copied_onto_the_stored_record(fake_engine):
    # a persona is a key the universe document knows; the prompts module
    # untags anything else on write, so it is registered the real way first
    geo_prompts.set_personas(BRAND["id"], [
        {"key": "solo", "label": "Solo practitioner", "description": "one-lawyer firm"},
    ])
    geo_prompts.save_universe(BRAND["id"], [
        {"id": "p1", "text": "q1", "intent": "category", "stage": "consideration",
         "enabled": True, "persona": "solo"},
        {"id": "p2", "text": "q2", "intent": "category", "stage": "consideration",
         "enabled": True},                                          # older prompt, no persona
    ])

    geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    by_id = {a["prompt_id"]: a for a in geo_poll.recent_answers(BRAND["id"], days=1)}
    assert by_id["p1"]["persona"] == "solo"
    assert by_id["p2"]["persona"] == ""
    assert by_id["p1"]["intent"] == "category"


# ------------------------- a console-driven sweep completes too -------------------------
# Only ``poll_until_done`` stamped the schedule, so a sweep the panel drove to
# the end stayed "due" forever and the next cron fire re-polled a day that was
# already fully measured.


def test_a_sweep_finished_from_the_console_stamps_the_schedule(fake_engine):
    seed_prompts(3)

    first = geo_poll.poll_step(BRAND, runs=1, batch_size=2)
    assert first["done"] < first["total"]
    assert "last_poll_completed_at" not in geo_poll.ensure_config(BRAND)   # still due

    geo_poll.poll_step(BRAND, runs=1, batch_size=2)

    cfg = geo_poll.ensure_config(BRAND)
    assert cfg["last_poll_completed_at"]
    assert geo_poll.poll_due(cfg)[0] is False
    assert geo_poll.poll_status(BRAND, runs=1)["due_now"] is False


def test_a_terminal_step_never_stamps_the_schedule(monkeypatch):
    seed_prompts(2)
    _engines(monkeypatch, perplexity=True)
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="fake", error="HTTP 401"))

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    assert res["terminal"] is True
    assert "last_poll_completed_at" not in geo_poll.ensure_config(BRAND)


# ------------------------- the run log -------------------------
# Written from exactly one place: the step that ends the sweep.


def test_the_run_log_gets_one_entry_when_the_sweep_ends(fake_engine):
    seed_prompts(3)

    geo_poll.poll_step(BRAND, runs=1, batch_size=2)
    assert geo_runlog.recent_runs(BRAND["id"]) == []              # not over yet

    geo_poll.poll_step(BRAND, runs=1, batch_size=2)

    runs = geo_runlog.recent_runs(BRAND["id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["id"]
    assert run["day"] == geo_poll._today()
    assert run["trigger"] == "manual"
    assert run["completed"] is True and run["stopped_because"] == "completed"
    assert run["terminal_reason"] is None
    assert run["steps"] == 2
    assert (run["done"], run["total"]) == (3, 3)
    assert run["calls"] == 3 and run["errors"] == {} and run["no_aio"] == 0
    assert run["engines"] == ["perplexity"]
    assert run["started_at"] <= run["finished_at"] and run["duration_s"] >= 0
    assert run["score"] is None            # 3 answers is below the chart's minimum sample
    assert run["plan_progress"] is None    # no Action Plan yet

    geo_poll.poll_step(BRAND, runs=1, batch_size=2)                # idle step: nothing to log
    assert len(geo_runlog.recent_runs(BRAND["id"])) == 1


def test_a_cron_sweep_is_logged_as_cron_with_the_banked_score(fake_engine):
    seed_prompts(12)                                              # enough for a chart point

    result = geo_poll.poll_until_done(BRAND, runs=1, batch_size=5)

    runs = geo_runlog.recent_runs(BRAND["id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["trigger"] == "cron"
    assert run["steps"] == result["steps"] == 3
    assert run["completed"] is True and run["calls"] == 12
    banked = geo_history.load_points(BRAND["id"])[-1]
    assert banked["date"] == geo_poll._today()
    assert run["score"] == banked["score"] is not None


def test_a_terminal_sweep_is_logged_with_its_reason_and_errors(monkeypatch):
    seed_prompts(2)
    _engines(monkeypatch, perplexity=True)
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="fake", error="HTTP 401: bad key"))

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    run = geo_runlog.recent_runs(BRAND["id"])[0]
    assert run["completed"] is False
    assert run["terminal_reason"] == res["terminal_reason"]
    assert run["stopped_because"] == res["terminal_reason"]
    assert run["errors"] == {"perplexity": 2}
    assert run["engines"] == []                                   # nothing was measured
    assert run["calls"] == 2 and (run["done"], run["total"]) == (2, 2)


def test_no_aio_observations_are_counted_on_the_run_log(monkeypatch):
    seed_prompts(2)
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": False, "aio": True, "ai_mode": False})
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="google-ai-overview",
                                                  via="dataforseo", no_aio=True))

    geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    run = geo_runlog.recent_runs(BRAND["id"])[0]
    assert run["no_aio"] == 2 and run["errors"] == {}
    assert run["engines"] == ["aio"]                              # the slot was checked


def test_the_run_log_records_where_the_plan_stood(fake_engine):
    from final_geo_agent import geo_strategy

    seed_prompts(2)
    state.save(geo_strategy.strategy_doc_id(BRAND["id"]), {
        "brand_id": BRAND["id"], "history": [],
        "current": {"generated_at": "2026-08-20T00:00:00+00:00", "summary": "s", "waves": [
            {"weeks": "1-2", "title": "w", "objective": "", "why_evidence": "",
             "actions": [{"id": "a1", "title": "A", "status": "done"},
                         {"id": "a2", "title": "B", "status": "todo"}]},
        ]},
    })

    geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    assert geo_runlog.recent_runs(BRAND["id"])[0]["plan_progress"] == {"done": 1, "total": 2}


def test_a_run_log_failure_never_breaks_a_paid_sweep(fake_engine, monkeypatch):
    seed_prompts(2)

    def broken(*a, **k):
        raise RuntimeError("run log datastore down")
    monkeypatch.setattr(geo_runlog, "record_run", broken)

    res = geo_poll.poll_step(BRAND, runs=1, batch_size=10)

    assert (res["done"], res["total"]) == (2, 2) and res["terminal"] is False
    assert geo_poll.ensure_config(BRAND)["last_poll_completed_at"]     # the sweep still completed
    assert len(geo_poll.recent_answers(BRAND["id"], days=1)) == 2      # and the answers are stored


def test_truncated_sweep_samples_every_intent_not_just_the_brand_questions():
    """Universe order opens with brand-intent questions, and a budget-starved
    sweep used to measure only those — five days of mention_rate 1.0 on
    n_prompts=3 while the full-window truth was 0.23. The first few tasks of a
    sweep must span intents, and brand questions go last."""
    prompts = (
        [{"id": f"b{i}", "text": f"brand q{i}", "intent": "brand"} for i in range(6)]
        + [{"id": f"c{i}", "text": f"cat q{i}", "intent": "category"} for i in range(6)]
        + [{"id": f"p{i}", "text": f"prob q{i}", "intent": "problem"} for i in range(6)]
    )
    ordered = geo_poll.representative_order(prompts)
    first_three = {p["intent"] for p in ordered[:3]}
    assert first_three == {"category", "problem", "brand"}
    # category leads, brand trails within each round
    assert ordered[0]["intent"] == "category"
    assert ordered[2]["intent"] == "brand"
    # nothing lost, nothing duplicated
    assert sorted(p["id"] for p in ordered) == sorted(p["id"] for p in prompts)


def test_representative_order_handles_missing_and_unknown_intents():
    prompts = [
        {"id": "a", "text": "x", "intent": "category"},
        {"id": "b", "text": "y"},                       # missing -> category bucket
        {"id": "c", "text": "z", "intent": "weird"},    # unknown keeps its own bucket
    ]
    ordered = geo_poll.representative_order(prompts)
    assert sorted(p["id"] for p in ordered) == ["a", "b", "c"]
