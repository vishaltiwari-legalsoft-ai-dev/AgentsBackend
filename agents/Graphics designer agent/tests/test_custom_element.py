"""Temporary AI element (Stage 2) — proposed foreground-only, run-scoped, and
NON-canonical: it must never be added to STAGE2_VARIANTS or the frozen prompts."""

from graphics_designer_agent import pipeline, suggestions
from graphics_designer_agent.prompts import CANONICAL_SHA256, verify_integrity
from graphics_designer_agent.runs import create_run, save_run
from graphics_designer_agent.variants import STAGE2_CATEGORIES, STAGE2_VARIANTS


def _seed_stage1(run):
    """Advance a fresh run to an approved Stage 1 so Stage 2 can generate."""
    pipeline.generate(run, 1, variant="A")
    pipeline.approve(run, 1)


def _propose(run, steer="a single warm professional", exclude=None):
    sugg = suggestions.suggest_element(answers={}, steer=steer, exclude=exclude)
    e = sugg["element"]
    assert e["id"] == "AI"
    run["config"]["custom_element"] = {
        "id": "AI", "cid": e["cid"], "title": e["title"], "desc": e["desc"],
        "category": e["category"], "subject": e["subject"], "source": sugg["source"],
    }
    save_run(run)
    return sugg


# ── suggestion shape + offline behaviour ──────────────────────────────────────
def test_suggest_element_offline_shape_is_valid():
    sugg = suggestions.suggest_element(answers={}, steer="confident professional")
    assert sugg["type"] == "element" and sugg["state"] == "proposed"
    e = sugg["element"]
    assert {"id", "cid", "title", "desc", "category", "subject"} <= set(e)
    assert e["category"] in STAGE2_CATEGORIES
    assert suggestions._validate_element_subject(e["subject"]) == []


def test_regenerate_rotates_past_excluded_picks():
    first = suggestions.suggest_element(answers={}, steer="")["element"]["cid"]
    second = suggestions.suggest_element(answers={}, steer="", exclude=[first])["element"]["cid"]
    assert second != first


# ── validation: foreground-only, mirrors the catalogue invariants ─────────────
def test_validate_rejects_background_words():
    bad = "A warm gradient background behind a smiling assistant at her desk, lots of space."
    errors = suggestions._validate_element_subject(bad)
    assert any("background" in e for e in errors)


def test_validate_rejects_colour_codes():
    bad = "A professional in a blazer toned to #1746A2 seated at a tidy desk, ample space."
    errors = suggestions._validate_element_subject(bad)
    assert any("colour" in e for e in errors)


# ── build_prompt uses the run's subject through the shared blend prompt ────────
def test_build_prompt_ai_variant_uses_custom_subject():
    run = create_run("user-elem")
    _propose(run)
    built = pipeline.build_prompt(run, 2, "AI")
    assert run["config"]["custom_element"]["subject"] in built["text"]
    assert "[SUBJECT]" not in built["text"]
    assert "background" in built["text"].lower()  # the shared blend prompt is intact


def test_build_prompt_ai_variant_without_element_raises():
    run = create_run("user-elem-2")
    try:
        pipeline.build_prompt(run, 2, "AI")
    except pipeline.PipelineError:
        return
    raise AssertionError("AI variant built without a stored custom element")


def test_generate_ai_element_end_to_end_on_mock():
    run = create_run("user-elem-3")
    _seed_stage1(run)
    _propose(run)
    attempt = pipeline.generate(run, 2, variant="AI")
    assert attempt["variant"] == "AI"


# ── the catalogue + canonical prompts are never polluted ──────────────────────
def test_custom_element_does_not_pollute_catalogue():
    before_ids = {v["id"] for v in STAGE2_VARIANTS}
    before_hashes = dict(CANONICAL_SHA256)

    run = create_run("user-elem-4")
    _seed_stage1(run)
    _propose(run)
    pipeline.generate(run, 2, variant="AI")

    assert {v["id"] for v in STAGE2_VARIANTS} == before_ids
    assert CANONICAL_SHA256 == before_hashes
    assert verify_integrity() == []
