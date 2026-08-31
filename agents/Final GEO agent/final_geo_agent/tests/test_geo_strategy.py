"""Action Plan strategy — offline, LLM and venue search faked at their seams.

The plan the panel shows is model output, so the tests that matter here are the
ones that pin what we refuse to show: an action naming a venue nobody verified,
and a plan whose venues came from the model's memory rather than a real search.
"""
import pytest

from final_geo_agent import geo_engines, geo_poll, geo_prompts, geo_strategy, geo_venues
from final_geo_agent.geo_engines import EngineAnswer

BRAND = {"id": "legalsoft", "name": "Legal Soft", "domain": "legalsoft.com",
         "seeds": ["legal virtual assistant"], "enabled": True}

# Venues the discovery step "found". Only these names may appear in a plan.
FAKE_VENUES = {
    "category": "legal virtual assistant",
    "venues": [
        {"kind": "community", "name": "r/LawFirm", "url": "https://reddit.com/r/LawFirm",
         "cited_where_absent": 14, "found_via": ["live-search"],
         "examples": [{"title": "Best intake service?", "url": "https://reddit.com/r/LawFirm/1"}]},
        {"kind": "listicle", "name": "clio.com", "url": "https://clio.com",
         "cited_where_absent": 9, "found_via": ["engine-citations"], "examples": []},
        {"kind": "review", "name": "g2.com", "url": "https://g2.com",
         "cited_where_absent": 0, "found_via": ["known-platform"],
         "examples": [], "brand_present": False},
    ],
    "counts": {"community": 1, "listicle": 1, "review": 1},
    "searched": 6, "errors": [], "complete": True,
}


def _action(title, venue, **over):
    base = {
        "title": title, "venue": venue,
        "deliverable": "a 600-word expert answer post",
        "steps": ["Open the three linked threads", "Draft a 600-word answer with two numbers",
                  "Post it from the founder account", "", 42,
                  "Reply to every comment within a day"],
        "detail": "Self-promo is banned there.",
        "owner_role": "content", "effort": "medium", "impact": "high",
        "kpi": "mention_rate", "target": "30%",
        "why_evidence": "cited 14x where the brand was absent",
        "expected_impact": "mention_rate from 21% to ~30% on the three threads by week 6",
    }
    base.update(over)
    return base


FAKE_PLAN = {
    "summary": "Strong citation base, weak naming on category questions.",
    "waves": [
        {
            "weeks": "3-4", "title": "Community presence",
            "objective": "Be named where buyers already ask",
            "why_evidence": "r/LawFirm cited 14x in answers that skipped us",
            "actions": [
                _action("Answer the three open intake threads", "r/LawFirm"),
                _action("", "r/LawFirm"),                       # junk row, dropped
            ],
        },
        {
            "weeks": "1-2", "title": "Quick wins",
            "objective": "Close the profile gaps engines read",
            "why_evidence": "no G2 profile while g2.com is cited on our prompts",
            "actions": [
                _action("Complete the G2 profile", "g2.com", owner_role="ops", effort="low"),
                _action("Add answer blocks to /intake-services", ""),   # on-site: no venue
            ],
        },
        {"weeks": "5-8", "title": "Empty wave dropped", "actions": []},
    ],
    "monitoring": {"cadence": "weekly poll", "review_ritual": "open Insights, read 3 numbers",
                   "leading_indicators": ["gap domain replies", "AIO citations up"]},
    "expectations": "4-12 weeks for retrieval, quarters for memory.",
}


@pytest.fixture(autouse=True)
def _fake_discovery(monkeypatch):
    """Venue discovery is proven in test_geo_venues; here it is a fixed input."""
    monkeypatch.setattr(geo_venues, "discover", lambda *a, **k: FAKE_VENUES)


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
    assert len(current["waves"]) == 2                  # the empty wave is dropped
    action = current["waves"][0]["actions"][0]
    assert action["id"] and action["status"] == "todo"
    assert current["baseline"]["n_answers"] == 21      # 7 prompts x 3 runs
    assert current["baseline"]["mention_rate"] == 1.0
    # the strategist saw the measured numbers and the real venue list, not vibes
    assert "source_gaps" in captured["prompt"] and "missing_questions" in captured["prompt"]
    assert "r/LawFirm" in captured["prompt"] and "VENUES" in captured["prompt"]
    assert "FORBIDDEN" in captured["system"]           # debunked-tactics guardrail in the brief
    assert geo_strategy.load_strategy(BRAND["id"])["current"]["summary"] == current["summary"]


def test_waves_are_ordered_by_calendar_not_by_model_output_order(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)

    waves = geo_strategy.generate_strategy(BRAND)["current"]["waves"]

    # the model listed 3-4 before 1-2; a team reads the plan top to bottom
    assert [w["weeks"] for w in waves] == ["1-2", "3-4"]


def test_actions_carry_the_resolved_venue_with_its_real_url(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)

    waves = geo_strategy.generate_strategy(BRAND)["current"]["waves"]
    reddit = next(a for w in waves for a in w["actions"] if a["title"].startswith("Answer the three"))

    # resolved from the discovered list, not from whatever the model typed
    assert reddit["venue"]["url"] == "https://reddit.com/r/LawFirm"
    assert reddit["venue"]["kind"] == "community"
    assert reddit["venue"]["cited_where_absent"] == 14
    assert reddit["deliverable"] == "a 600-word expert answer post"
    # a list a team ticks off — strings only, blanks out, never a paragraph
    assert reddit["steps"] == ["Open the three linked threads",
                               "Draft a 600-word answer with two numbers",
                               "Post it from the founder account",
                               "Reply to every comment within a day"]
    assert reddit["expected_impact"].startswith("mention_rate from 21%")
    assert reddit["kpi"] == "mention_rate" and reddit["kpi_coerced"] is False
    assert reddit["assignee"] == "" and reddit["assigned_at"] is None


def test_on_site_actions_have_no_venue_and_are_kept(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)

    waves = geo_strategy.generate_strategy(BRAND)["current"]["waves"]
    on_site = next(a for w in waves for a in w["actions"] if "intake-services" in a["title"])

    assert on_site["venue"] is None


# ------------------------------------------------- the anti-hallucination guard

def test_an_action_naming_an_unverified_venue_is_dropped_and_recorded(monkeypatch):
    seed_measured_data(monkeypatch)
    plan = {
        **FAKE_PLAN,
        "waves": [{
            "weeks": "1-2", "title": "Community", "objective": "", "why_evidence": "",
            "actions": [
                _action("Post in the paralegal community", "r/ParalegalLife"),  # invented
                _action("Complete the G2 profile", "g2.com"),                   # verified
            ],
        }],
    }
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: plan)

    current = geo_strategy.generate_strategy(BRAND)["current"]

    titles = [a["title"] for w in current["waves"] for a in w["actions"]]
    assert titles == ["Complete the G2 profile"]
    # dropped, not silently rewritten to something plausible
    assert current["dropped_actions"][0]["venue"] == "r/ParalegalLife"
    assert "not in discovered list" in current["dropped_actions"][0]["reason"]


def test_a_plan_of_nothing_but_invented_venues_fails_loudly(monkeypatch):
    seed_measured_data(monkeypatch)
    plan = {
        **FAKE_PLAN,
        "waves": [{"weeks": "1-2", "title": "x", "actions": [
            _action("Post here", "r/DoesNotExist"), _action("And here", "madeupforum.com"),
        ]}],
    }
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: plan)

    # a fabricated plan must not degrade into a half-empty real one
    with pytest.raises(ValueError, match="could not verify"):
        geo_strategy.generate_strategy(BRAND)


def test_venue_names_match_case_insensitively(monkeypatch):
    # the model reproducing "r/lawfirm" is reproducing our own list, not inventing
    seed_measured_data(monkeypatch)
    plan = {**FAKE_PLAN, "waves": [{"weeks": "1-2", "title": "x", "actions": [
        _action("Answer threads", "r/lawfirm"),
    ]}]}
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: plan)

    current = geo_strategy.generate_strategy(BRAND)["current"]

    assert current["waves"][0]["actions"][0]["venue"]["name"] == "r/LawFirm"
    assert current["dropped_actions"] == []


def test_plan_records_whether_venue_discovery_was_complete(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_venues, "discover", lambda *a, **k: {
        **FAKE_VENUES, "complete": False, "errors": ["community: search unavailable"],
    })
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)

    current = geo_strategy.generate_strategy(BRAND)["current"]

    # the panel has to be able to say the venue list was partial
    assert current["venues"]["complete"] is False
    assert current["venues"]["errors"]


# ------------------------------------------------- the executability guard
# A plan that reads well but cannot be started Monday morning is refused.


def _one_wave(*actions, weeks="1-2"):
    return {**FAKE_PLAN, "waves": [{"weeks": weeks, "title": "x", "objective": "",
                                    "why_evidence": "", "actions": list(actions)}]}


def test_an_action_with_fewer_than_two_steps_is_dropped_and_recorded(monkeypatch):
    seed_measured_data(monkeypatch)
    # two waves: a wave holds at most ACTIONS_PER_WAVE items before cleaning
    first = _one_wave(
        _action("Two real steps", "", steps=["Draft the /pricing answer block", "Publish it"]),
        _action("One step only", "", steps=["Write it"]),
        _action("No steps at all", "", steps=[]),
    )["waves"][0]
    second = _one_wave(
        _action("Steps as prose", "", steps="First do this, then that"),
        _action("Blank steps", "", steps=["", "   ", 7]),
        weeks="3-4",
    )["waves"][0]
    monkeypatch.setattr(geo_strategy, "_llm_strategy",
                        lambda s, p: {**FAKE_PLAN, "waves": [first, second]})

    current = geo_strategy.generate_strategy(BRAND)["current"]

    assert [a["title"] for w in current["waves"] for a in w["actions"]] == ["Two real steps"]
    dropped = {d["title"]: d["reason"] for d in current["dropped_actions"]}
    assert set(dropped) == {"One step only", "No steps at all", "Steps as prose", "Blank steps"}
    assert all("no concrete steps" in reason for reason in dropped.values())


def test_a_plan_of_nothing_but_unexecutable_actions_fails_loudly(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: _one_wave(
        _action("Vibes", "", steps=["Be more visible"]),
    ))
    with pytest.raises(ValueError, match="no concrete steps"):
        geo_strategy.generate_strategy(BRAND)


def test_steps_are_capped_at_six(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: _one_wave(
        _action("Long list", "", steps=[f"Step {i}" for i in range(1, 10)]),
    ))

    action = geo_strategy.generate_strategy(BRAND)["current"]["waves"][0]["actions"][0]

    assert action["steps"] == [f"Step {i}" for i in range(1, 7)]
    assert len(action["steps"]) == geo_strategy.STEP_CAP == 6


def test_a_kpi_off_the_allowed_list_is_coerced_and_flagged(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: _one_wave(
        _action("Guessed kpi", "", kpi="brand_awareness"),
        _action("Real kpi", "", kpi="citation_rate"),
    ))

    actions = {a["title"]: a for a in geo_strategy.generate_strategy(BRAND)["current"]["waves"][0]["actions"]}

    assert actions["Guessed kpi"]["kpi"] == "mention_rate"
    assert actions["Guessed kpi"]["kpi_coerced"] is True     # flagged, never silent
    assert actions["Real kpi"]["kpi"] == "citation_rate"
    assert actions["Real kpi"]["kpi_coerced"] is False


def test_expected_impact_is_stored_and_defaults_to_empty(monkeypatch):
    seed_measured_data(monkeypatch)
    plan = _one_wave(
        _action("Promised", "", expected_impact="  citation_rate 8% -> 15% by week 8  "),
        _action("Silent", ""),
    )
    del plan["waves"][0]["actions"][1]["expected_impact"]
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: plan)

    actions = {a["title"]: a for a in geo_strategy.generate_strategy(BRAND)["current"]["waves"][0]["actions"]}

    assert actions["Promised"]["expected_impact"] == "citation_rate 8% -> 15% by week 8"
    assert actions["Silent"]["expected_impact"] == ""


def test_the_brief_lists_every_allowed_kpi_and_demands_steps_and_impact(monkeypatch):
    seed_measured_data(monkeypatch)
    captured = {}

    def fake_llm(system, prompt):
        captured["system"] = system
        return FAKE_PLAN

    monkeypatch.setattr(geo_strategy, "_llm_strategy", fake_llm)
    geo_strategy.generate_strategy(BRAND)

    system = captured["system"]
    assert len(geo_strategy.KPI_KEYS) == 7
    for key in geo_strategy.KPI_KEYS:
        assert key in system
    # the model is told the contract the adapter enforces, not surprised by it
    assert '"steps"' in system and '"expected_impact"' in system
    assert "fewer than 2 concrete steps is REFUSED" in system


def test_with_no_venues_the_brief_forbids_naming_any(monkeypatch):
    seed_measured_data(monkeypatch)
    empty = {"category": "x", "venues": [], "counts": {}, "searched": 0,
             "errors": ["all searches failed"], "complete": False}
    monkeypatch.setattr(geo_venues, "discover", lambda *a, **k: empty)
    captured = {}

    def fake_llm(system, prompt):
        captured["prompt"] = prompt
        return {**FAKE_PLAN, "waves": [{"weeks": "1-2", "title": "x", "actions": [
            _action("Add answer blocks to /intake-services", ""),
        ]}]}

    monkeypatch.setattr(geo_strategy, "_llm_strategy", fake_llm)
    geo_strategy.generate_strategy(BRAND)

    assert "Do NOT name any" in captured["prompt"]


# ------------------------------------------------------------------ lifecycle

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
    action_id = doc["current"]["waves"][0]["actions"][0]["id"]

    updated = geo_strategy.set_action_status(BRAND["id"], action_id, "done")
    saved_action = updated["current"]["waves"][0]["actions"][0]
    assert saved_action["status"] == "done" and saved_action["status_at"]

    with pytest.raises(ValueError):
        geo_strategy.set_action_status(BRAND["id"], action_id, "banana")
    with pytest.raises(KeyError):
        geo_strategy.set_action_status(BRAND["id"], "nope1234", "done")


def test_update_action_assigns_and_moves_in_one_write(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    doc = geo_strategy.generate_strategy(BRAND)
    action_id = doc["current"]["waves"][0]["actions"][0]["id"]

    def saved():
        return geo_strategy.load_strategy(BRAND["id"])["current"]["waves"][0]["actions"][0]

    geo_strategy.update_action(BRAND["id"], action_id, assignee="  Priya  ")
    assert saved()["assignee"] == "Priya" and saved()["assigned_at"]
    assert saved()["status"] == "todo"                     # untouched by an assignment

    geo_strategy.update_action(BRAND["id"], action_id, status="in_progress")
    assert saved()["status"] == "in_progress" and saved()["assignee"] == "Priya"

    geo_strategy.update_action(BRAND["id"], action_id, status="done", assignee="")
    assert saved()["status"] == "done" and saved()["assignee"] == ""

    for status in geo_strategy.ACTION_STATUSES:            # the router imports this tuple
        assert geo_strategy.update_action(BRAND["id"], action_id, status=status)


def test_update_action_refuses_bad_input_without_writing(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    doc = geo_strategy.generate_strategy(BRAND)
    action_id = doc["current"]["waves"][0]["actions"][0]["id"]

    with pytest.raises(ValueError, match="status must be"):
        geo_strategy.update_action(BRAND["id"], action_id, status="banana")
    with pytest.raises(ValueError, match="80 characters"):
        geo_strategy.update_action(BRAND["id"], action_id, assignee="x" * 81)
    with pytest.raises(ValueError, match="nothing to update"):
        geo_strategy.update_action(BRAND["id"], action_id)
    with pytest.raises(KeyError):
        geo_strategy.update_action(BRAND["id"], "nope1234", status="done")
    with pytest.raises(KeyError, match="No strategy"):
        geo_strategy.update_action("never-planned", action_id, status="done")
    action = geo_strategy.load_strategy(BRAND["id"])["current"]["waves"][0]["actions"][0]
    assert action["status"] == "todo" and action["assignee"] == ""


# ---------------------------------------------- plans saved before waves existed
# A stored plan with `pillars` and no `waves` made the console throw a
# client-side exception on the Action Plan tab, which blanked the whole panel --
# Insights and Answers included -- for a plan the team had already worked through.

LEGACY_PLAN = {
    "summary": "Older plan, pillar shaped.",
    "pillars": [
        {"title": "Long arc", "objective": "o1", "why_evidence": "e1", "actions": [
            {"id": "a1", "title": "Editorial placements", "detail": "d",
             "owner_role": "outreach", "effort": "high", "impact": "high",
             "timeframe_weeks": 8, "kpi": "mention_rate", "target": "30%", "status": "todo"},
        ]},
        {"title": "Quick wins", "objective": "o2", "why_evidence": "e2", "actions": [
            {"id": "a2", "title": "Complete G2 profile", "detail": "d",
             "owner_role": "ops", "effort": "low", "impact": "medium",
             "timeframe_weeks": 2, "kpi": "citation_rate", "target": "12%", "status": "done"},
        ]},
    ],
    "monitoring": {"cadence": "weekly", "review_ritual": "r", "leading_indicators": []},
    "expectations": "e", "baseline": {"n_answers": 40}, "generated_at": "2026-08-11T00:00:00+00:00",
}


def _store_legacy():
    from seo_geo_agent import state
    state.save(geo_strategy.strategy_doc_id(BRAND["id"]),
               {"brand_id": BRAND["id"], "current": LEGACY_PLAN, "history": []})


def test_a_legacy_pillar_plan_loads_as_waves():
    _store_legacy()

    current = geo_strategy.load_strategy(BRAND["id"])["current"]

    assert [w["title"] for w in current["waves"]] == ["Quick wins", "Long arc"]
    assert current["shape"] == "migrated-from-pillars"


def test_migrated_weeks_come_from_the_only_calendar_data_those_plans_had():
    _store_legacy()

    waves = geo_strategy.load_strategy(BRAND["id"])["current"]["waves"]

    # timeframe_weeks was all those plans stored; a made-up fortnight would be worse
    assert [w["weeks"] for w in waves] == ["1-2", "1-8"]


def test_migrated_actions_gain_the_fields_the_panel_reads():
    _store_legacy()

    action = geo_strategy.load_strategy(BRAND["id"])["current"]["waves"][0]["actions"][0]

    assert action["venue"] is None          # no venue was ever recorded — not invented
    assert action["deliverable"] == ""
    assert action["steps"] == []            # the panel finds the key, never undefined
    assert action["expected_impact"] == "" and action["kpi_coerced"] is False
    assert action["assignee"] == "" and action["assigned_at"] is None
    assert action["status"] == "done"       # the team's own progress survives
    assert action["id"] == "a2"


def test_a_wave_plan_stored_before_steps_and_impact_existed_still_loads():
    """Wave-shaped plans written last month have no steps, expected_impact,
    kpi_coerced or assignee. They load with defaults; nothing is invented."""
    from seo_geo_agent import state
    state.save(geo_strategy.strategy_doc_id(BRAND["id"]), {
        "brand_id": BRAND["id"], "history": [],
        "current": {
            "summary": "wave plan, early shape",
            "waves": [{"weeks": "1-2", "title": "Quick wins", "objective": "", "why_evidence": "",
                       "actions": [{"id": "w1", "title": "Complete G2 profile", "venue": None,
                                    "deliverable": "the profile", "detail": "d",
                                    "owner_role": "ops", "effort": "low", "impact": "medium",
                                    "kpi": "citation_rate", "target": "12%",
                                    "why_evidence": "e", "status": "in_progress"}]}],
            "monitoring": {"cadence": "", "review_ritual": "", "leading_indicators": []},
            "expectations": "", "baseline": {"n_answers": 40},
            "generated_at": "2026-08-20T00:00:00+00:00",
        },
    })

    current = geo_strategy.load_strategy(BRAND["id"])["current"]
    action = current["waves"][0]["actions"][0]

    assert "shape" not in current                     # nothing was migrated
    assert action["steps"] == [] and action["expected_impact"] == ""
    assert action["kpi_coerced"] is False and action["assignee"] == ""
    assert action["status"] == "in_progress" and action["kpi"] == "citation_rate"
    # and it can still be worked: the write fills the defaults in for good
    geo_strategy.update_action(BRAND["id"], "w1", assignee="Sam")
    reread = state.load(geo_strategy.strategy_doc_id(BRAND["id"]))["current"]["waves"][0]["actions"][0]
    assert reread["assignee"] == "Sam" and reread["steps"] == []


def test_status_edits_work_on_a_migrated_plan_and_persist_the_new_shape():
    _store_legacy()

    updated = geo_strategy.set_action_status(BRAND["id"], "a1", "in_progress")

    assert updated["current"]["waves"][1]["actions"][0]["status"] == "in_progress"
    # the write settles the migration, so the next read has nothing left to convert
    reread = geo_strategy.load_strategy(BRAND["id"])["current"]
    assert "pillars" not in reread
    assert reread["waves"][1]["actions"][0]["status"] == "in_progress"


def test_a_wave_shaped_plan_is_left_untouched(monkeypatch):
    seed_measured_data(monkeypatch)
    monkeypatch.setattr(geo_strategy, "_llm_strategy", lambda s, p: FAKE_PLAN)
    geo_strategy.generate_strategy(BRAND)

    current = geo_strategy.load_strategy(BRAND["id"])["current"]

    assert "shape" not in current           # nothing was migrated
    assert current["waves"][0]["weeks"] == "1-2"
