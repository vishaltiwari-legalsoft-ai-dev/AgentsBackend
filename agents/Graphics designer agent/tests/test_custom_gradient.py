"""Temporary AI gradient (Stage 1) — proposed off-brand-safe, run-scoped, and
NON-canonical: it must never touch prompts/ or the frozen hash baseline."""

from graphics_designer_agent import pipeline, suggestions
from graphics_designer_agent.prompts import CANONICAL_SHA256, PROMPT_DIR, verify_integrity
from graphics_designer_agent.runs import create_run, save_run
from graphics_designer_agent.tokens import STAGE1_AR_ANCHOR


def _propose(run, steer="more minimal", exclude=None):
    """Run the suggestion + persist it the way the router's config endpoint does."""
    sugg = suggestions.suggest_gradient(answers={}, steer=steer, exclude=exclude)
    g = sugg["gradient"]
    assert g["id"] == "AI"
    run["config"]["custom_gradient"] = {
        "id": "AI", "cid": g["cid"], "title": g["title"], "desc": g["desc"],
        "prompt": g["prompt"], "css_gradient": g["css_gradient"], "source": sugg["source"],
    }
    save_run(run)
    return sugg


# ── suggestion shape + offline behaviour ──────────────────────────────────────
def test_suggest_gradient_offline_shape_is_valid():
    sugg = suggestions.suggest_gradient(answers={}, steer="warmer")
    assert sugg["type"] == "gradient" and sugg["state"] == "proposed"
    g = sugg["gradient"]
    assert {"id", "cid", "title", "desc", "prompt", "css_gradient"} <= set(g)
    # The proposed prompt must be valid (anchor + brand-only) so it can be stored.
    assert suggestions._validate_gradient_prompt(g["prompt"]) == []
    assert STAGE1_AR_ANCHOR in g["prompt"]


def test_regenerate_rotates_past_excluded_picks():
    first = suggestions.suggest_gradient(answers={}, steer="")["gradient"]["cid"]
    second = suggestions.suggest_gradient(answers={}, steer="", exclude=[first])["gradient"]["cid"]
    assert second != first


# ── validation ────────────────────────────────────────────────────────────────
def test_validate_rejects_missing_anchor():
    bad = "A smooth gradient from #FFFFFF to #1746A2, no noise, no text."
    errors = suggestions._validate_gradient_prompt(bad)
    assert any("anchor" in e for e in errors)


def test_validate_rejects_off_brand_colours():
    bad = (
        "Create a 16:9 aspect ratio immersive abstract background gradient from "
        "#FFFFFF to #FF0000, no noise, no text."
    )
    errors = suggestions._validate_gradient_prompt(bad)
    assert any("Off-brand" in e for e in errors)
    assert "#FF0000" in " ".join(errors)


# ── build_prompt uses the run's prompt + still applies AR ──────────────────────
def test_build_prompt_ai_variant_uses_custom_prompt_and_swaps_ar():
    run = create_run("user-grad")
    run["config"]["aspect_ratio"] = "9:16"
    _propose(run)
    built = pipeline.build_prompt(run, 1, "AI")
    assert built["text"] == run["config"]["custom_gradient"]["prompt"].replace(
        STAGE1_AR_ANCHOR, "9:16 aspect ratio"
    )
    assert STAGE1_AR_ANCHOR not in built["text"]
    assert any(d["token"] == "ASPECT_RATIO" for d in built["diffs"])


def test_build_prompt_ai_variant_without_gradient_raises():
    run = create_run("user-grad-2")
    try:
        pipeline.build_prompt(run, 1, "AI")
    except pipeline.PipelineError:
        return
    raise AssertionError("AI variant built without a stored custom gradient")


def test_generate_ai_gradient_end_to_end_on_mock():
    run = create_run("user-grad-3")
    _propose(run)
    attempt = pipeline.generate(run, 1, variant="AI")
    assert attempt["variant"] == "AI"
    assert run["stages"]["1"]["attempts"][0]["prompt"] == built_text(run)


def built_text(run):
    return pipeline.build_prompt(run, 1, "AI")["text"]


# ── the canonical prompt library is never polluted ─────────────────────────────
def test_custom_gradient_does_not_pollute_canonical_library():
    before_files = sorted(p.name for p in PROMPT_DIR.glob("*.txt"))
    before_hashes = dict(CANONICAL_SHA256)

    run = create_run("user-grad-4")
    _propose(run)
    pipeline.generate(run, 1, variant="AI")

    assert sorted(p.name for p in PROMPT_DIR.glob("*.txt")) == before_files
    assert CANONICAL_SHA256 == before_hashes
    assert verify_integrity() == []
