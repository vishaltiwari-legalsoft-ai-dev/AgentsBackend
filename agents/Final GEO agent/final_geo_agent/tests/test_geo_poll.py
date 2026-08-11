"""GEO polling + prompt universe — offline, engines faked at the adapter seam."""
import pytest

from final_geo_agent import geo_engines, geo_poll, geo_prompts
from final_geo_agent.geo_engines import EngineAnswer
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
