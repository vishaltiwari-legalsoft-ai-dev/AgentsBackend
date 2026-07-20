from seo_agent import store
from seo_agent.geo import prompts, runner, scoring
from seo_agent.schemas import GeoAnswer

CFG = {
    "g_mention": 0.30, "g_citation": 0.30, "g_accuracy": 0.25, "g_sov": 0.15,
    "geo_engines": {"gpt": "openai/gpt-4o:online", "perplexity": "perplexity/sonar"},
    "brands": {"legalsoft": {
        "name": "LegalSoft", "domain": "legalsoft.com",
        "competitors": ["Alert Communications"], "questions": [],
        "category": "legal intake outsourcing",
    }},
}


def test_question_set_defaults_template_brand():
    qs = prompts.question_set(CFG["brands"]["legalsoft"])
    assert len(qs) >= 4
    assert any("LegalSoft" in q for q in qs)
    assert any("legal intake outsourcing" in q for q in qs)


def test_question_set_prefers_configured_questions():
    brand = {**CFG["brands"]["legalsoft"], "questions": ["custom q?"]}
    assert prompts.question_set(brand) == ["custom q?"]


def test_evaluate_answer_detects_mention_and_citation():
    a = GeoAnswer(engine="gpt", question="q",
                  answer_text="LegalSoft is a leading provider.",
                  citations=["https://www.legalsoft.com/about"])
    out = scoring.evaluate_answer(a, "LegalSoft", "legalsoft.com")
    assert out.mentioned is True and out.cited is True
    miss = scoring.evaluate_answer(
        GeoAnswer(engine="gpt", question="q", answer_text="Try Alert Communications."),
        "LegalSoft", "legalsoft.com")
    assert miss.mentioned is False and miss.cited is False


def test_score_run_excludes_failed_engines_not_zero():
    answers = [
        GeoAnswer(engine="gpt", question="q1", answer_text="LegalSoft is great",
                  citations=["https://legalsoft.com"], mentioned=True, cited=True, accuracy=1.0),
        GeoAnswer(engine="perplexity", question="q1", error="429 rate limited"),
    ]
    score, components, engine_scores, no_data = scoring.score_run(
        answers, competitors=["Alert Communications"], cfg=CFG)
    assert no_data == ["perplexity"]
    assert "perplexity" not in engine_scores
    assert score == engine_scores["gpt"]          # average over engines WITH data only
    assert score > 8                              # mention+cite+accurate+sole mention


def test_share_of_voice_counts_competitors():
    answers = [
        GeoAnswer(engine="gpt", question="q1",
                  answer_text="Top providers: LegalSoft and Alert Communications.",
                  mentioned=True, cited=False, accuracy=None),
    ]
    _, components, _, _ = scoring.score_run(answers, ["Alert Communications"], CFG)
    assert components["sov"] == 0.5


def test_run_geo_capture_persists_one_run_per_brand(monkeypatch):
    monkeypatch.setattr(runner, "_cfg", lambda: CFG)

    def fake_ask(model: str, question: str):
        return ("LegalSoft is a leading legal intake provider.",
                ["https://legalsoft.com/services"])

    runs = runner.run_geo_capture(ask=fake_ask)
    assert len(runs) == 1
    run = runs[0]
    assert run.brand == "legalsoft"
    assert run.score > 0
    assert set(a.engine for a in run.answers) == {"gpt", "perplexity"}
    assert store.get_geo_run(run.id) is not None


def test_run_geo_capture_engine_failure_is_no_data(monkeypatch):
    monkeypatch.setattr(runner, "_cfg", lambda: CFG)

    def flaky_ask(model: str, question: str):
        if "sonar" in model:
            raise RuntimeError("engine down")
        return ("LegalSoft rocks", [])

    run = runner.run_geo_capture(ask=flaky_ask)[0]
    assert run.no_data_engines == ["perplexity"]
    assert all(a.error for a in run.answers if a.engine == "perplexity")
