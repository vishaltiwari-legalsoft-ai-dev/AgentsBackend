"""Action Plan strategy — offline, LLM faked at the module seam."""
import pytest

from final_geo_agent import geo_engines, geo_poll, geo_prompts, geo_strategy
from final_geo_agent.geo_engines import EngineAnswer

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}

FAKE_PLAN = {
    "summary": "Strong citation base, weak naming on category questions.",
    "pillars": [
        {
            "title": "Win the category questions",
            "objective": "Get named where rivals are named today",
            "why_evidence": "brand absent on category prompts while cited 36x as a source",
            "actions": [
                {"title": "Pitch listicle inclusion on top gap domain",
                 "detail": "Outreach to the roundup pages engines already cite.",
                 "owner_role": "outreach", "effort": "medium", "impact": "high",
                 "timeframe_weeks": 6, "kpi": "mention_rate", "target": "30%"},
                {"title": "", "detail": "junk row dropped"},
            ],
        },
        {"title": "Empty pillar dropped", "objective": "", "why_evidence": "", "actions": []},
    ],
    "monitoring": {"cadence": "weekly poll", "review_ritual": "open Insights, read 3 numbers",
                   "leading_indicators": ["gap domain replies", "AIO citations up"]},
    "expectations": "4-12 weeks for retrieval, quarters for memory.",
}


def seed_measured_data(monkeypatch, n_prompts=7):
    prompts = [
        {"id": f"p{i}", "text": f"buyer question {i}", "intent": "category",
         "stage": "consideration", "enabled": True}
        for i in range(n_prompts)
    ]
    geo_prompts.save_universe(BRAND["id"], prompts)
    monkeypatch.setattr(geo_engines, "available_engines",
                        lambda: {"perplexity": True, "gemini": False, "chatgpt": False, "aio": False})
    monkeypatch.setattr(geo_engines, "poll_engine",
                        lambda e, p: EngineAnswer(engine=e, model="fake",
                                                  text=f"Legal Soft answers {p}"))
    geo_poll.poll_step(BRAND, runs=3, batch_size=50)


def test_generate_requires_real_data(monkeypatch):
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    with pytest.raises(ValueError, match="run a full poll"):
        geo_strategy.generate_strategy(BRAND)          # no answers yet


def test_generate_persists_plan_with_baseline_and_ids(monkeypatch):
    seed_measured_data(monkeypatch)
    captured = {}

    def fake_llm(system, prompt):
        captured["system"] = system
        captured["prompt"] = prompt
        return FAKE_PLAN

    monkeypatch.setattr(geo_strategy, "_llm_strategy", fake_llm)
    doc = geo_strategy.generate_strategy(BRAND)

    current = doc["current"]
    assert current["summary"].startswith("Strong citation")
    assert len(current["pillars"]) == 1                # empty pillar + junk action dropped
    action = current["pillars"][0]["actions"][0]
    assert action["id"] and action["status"] == "todo"
    assert current["baseline"]["n_answers"] == 21      # 7 prompts x 3 runs
    assert current["baseline"]["mention_rate"] == 1.0
    # the strategist saw the measured numbers, not vibes
    assert "source_gaps" in captured["prompt"] and "missing_questions" in captured["prompt"]
    assert "FORBIDDEN" in captured["system"]           # debunked-tactics guardrail in the brief
    assert geo_strategy.load_strategy(BRAND["id"])["current"]["summary"] == current["summary"]


def test_regenerate_archives_previous_to_history(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    geo_strategy.generate_strategy(BRAND)
    doc = geo_strategy.generate_strategy(BRAND)
    assert len(doc["history"]) == 1
    assert doc["history"][0]["summary"].startswith("Strong citation")


def test_action_status_updates_and_validates(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    doc = geo_strategy.generate_strategy(BRAND)
    action_id = doc["current"]["pillars"][0]["actions"][0]["id"]

    updated = geo_strategy.set_action_status(BRAND["id"], action_id, "done")
    saved_action = updated["current"]["pillars"][0]["actions"][0]
    assert saved_action["status"] == "done" and saved_action["status_at"]

    with pytest.raises(ValueError):
        geo_strategy.set_action_status(BRAND["id"], action_id, "banana")
    with pytest.raises(KeyError):
        geo_strategy.set_action_status(BRAND["id"], "nope1234", "done")
