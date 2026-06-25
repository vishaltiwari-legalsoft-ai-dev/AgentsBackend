"""Pre-generation discovery (the "micro-conversation", Steps 1–2).

The agent gathers intent (feeling/audience/tone/style) and event/campaign context
BEFORE proposing anything, stores it on the run as ``creative_brief``, and folds
it into every suggestion + a synthesized creative direction. Curated + offline."""

from graphics_designer_agent import registry, suggestions
from graphics_designer_agent.runs import create_run


# ── discovery question script ─────────────────────────────────────────────────
def test_discovery_questions_shape():
    qs = suggestions.DISCOVERY_QUESTIONS
    ids = {q["id"] for q in qs}
    # Both steps from the spec are represented.
    assert {"feeling", "audience", "tone", "style"} <= ids  # Step 1 · intent
    assert {"event", "theme"} <= ids                        # Step 2 · context
    groups = {q["group"] for q in qs}
    assert groups == {"intent", "context"}
    for q in qs:
        assert q["kind"] in {"choice", "text", "choice_text"}
        assert q["prompt"]
        if q["kind"] in {"choice", "choice_text"}:
            assert q["options"] and all({"id", "label"} <= set(o) for o in q["options"])
        else:
            assert "options" not in q or not q.get("options")


def test_every_pack_exposes_discovery_questions():
    for entry in registry.list_packs():
        pack = registry.get_pack(entry["id"])
        assert pack.discovery_questions, f"{pack.id} has no discovery script"
        assert {q["id"] for q in pack.discovery_questions} >= {"feeling", "event", "theme"}


# ── legacy back-fill (discovery → curated concept keys) ───────────────────────
def test_derive_legacy_maps_feeling_and_event():
    out = suggestions._derive_legacy({"feeling": "urgency", "event": "hiring"})
    assert out["angle"] == "pain"      # urgency/relief → pain-point
    assert out["goal"] == "lead_gen"   # hiring/launch → lead-gen


def test_derive_legacy_does_not_override_explicit_keys():
    out = suggestions._derive_legacy({"feeling": "urgency", "angle": "aspiration"})
    assert out["angle"] == "aspiration"  # explicit wins


def test_recommend_concept_accepts_a_pure_discovery_brief():
    # No legacy goal/angle keys at all — only the new discovery dimensions.
    rec = suggestions.recommend_concept({"feeling": "relief", "tone": "bold"})
    assert rec["recommended"] in {v["id"] for v in suggestions.STAGE2_VARIANTS}


# ── synthesized creative direction ────────────────────────────────────────────
def test_synthesize_direction_offline_shape():
    d = suggestions.synthesize_direction({"feeling": "trust", "audience": "partners", "tone": "premium"})
    assert d["type"] == "direction" and d["state"] == "proposed"
    assert d["source"] == "agent"  # offline → curated, never agent+llm
    assert d["concept"] in {v["id"] for v in suggestions.STAGE2_VARIANTS}
    assert d["summary"].strip()
    assert d["palette_hint"] and d["copy_angle"]


def test_direction_is_deterministic_offline():
    brief = {"feeling": "aspiration", "audience": "growing", "event": "launch", "theme": "Q1 expansion"}
    assert suggestions.synthesize_direction(brief) == suggestions.synthesize_direction(brief)


def test_direction_concept_responds_to_the_brief():
    pain = suggestions.synthesize_direction({"feeling": "urgency"})["concept"]
    partners = suggestions.synthesize_direction({"feeling": "trust", "audience": "partners"})["concept"]
    assert pain == "D"        # urgency → pain-point storytelling
    assert partners == "C"    # partner authority
    assert pain != partners


def test_direction_weaves_event_and_theme_into_summary():
    d = suggestions.synthesize_direction({"feeling": "warmth", "event": "webinar", "theme": "Scaling Smart"})
    assert "Scaling Smart" in d["summary"]


# ── conversational strategist (the agent talks WITH the user) ─────────────────
def test_converse_opens_with_a_greeting_and_first_question():
    turn = suggestions.converse(history=[], brief={})
    assert turn["type"] == "chat" and turn["done"] is False
    assert turn["reply"]  # the agent speaks first
    assert "?" in turn["reply"]  # it asks something


def test_converse_extracts_intent_from_free_text_offline():
    qs = suggestions.DISCOVERY_QUESTIONS
    # Agent asked Q0 (feeling); user answers in their own words.
    history = [
        {"role": "agent", "text": qs[0]["prompt"]},
        {"role": "user", "text": "I want it to feel really urgent, like act now"},
    ]
    turn = suggestions.converse(history=history, brief={})
    assert turn["brief"].get("feeling") == "urgency"  # mapped from free text
    assert turn["reply"]  # acknowledges + asks the next thing
    assert turn["done"] is False


def test_converse_reaches_a_direction_after_the_dimensions():
    qs = suggestions.DISCOVERY_QUESTIONS
    history = []
    brief = {}
    replies = ["trust", "firm partners", "premium & polished", "minimal",
               "a launch", "scaling smart in Q1"]
    # Replay the whole conversation turn by turn, as the UI would.
    for i, q in enumerate(qs):
        history.append({"role": "agent", "text": q["prompt"]})
        history.append({"role": "user", "text": replies[i]})
        turn = suggestions.converse(history=history, brief=brief)
        brief = turn["brief"]
    assert turn["done"] is True
    assert turn["direction"] and turn["direction"]["concept"] in {v["id"] for v in suggestions.STAGE2_VARIANTS}


# ── run config carries an (initially empty) brief ─────────────────────────────
def test_new_run_starts_with_an_empty_creative_brief():
    run = create_run(user_id="u-test")
    assert run["config"]["creative_brief"] == {}
