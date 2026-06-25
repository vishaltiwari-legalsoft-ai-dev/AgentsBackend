"""Stage-2 subject placement (prompt-steered) — §5.2b.

The Stage-2 subject is baked into the AI image (not a movable layer), so its
position is steered by an explicit override clause appended to the subject
prompt. ``auto`` (the default) is a STRICT no-op so the existing engine is
untouched unless the user opts in.
"""

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.tokens import place_subject
from graphics_designer_agent.variants import STAGE2_PLACEMENTS, STAGE2_VARIANTS

_KEYS = {p["key"] for p in STAGE2_PLACEMENTS}
_CELLS = _KEYS - {"auto"}
_SUBJECT = STAGE2_VARIANTS[0]["subject"]  # variant A — has built-in position text


def test_placement_catalog_has_auto_plus_nine_cells():
    assert _KEYS == {
        "auto",
        "top-left", "top-center", "top-right",
        "middle-left", "middle-center", "middle-right",
        "bottom-left", "bottom-center", "bottom-right",
    }
    # auto is first so the picker shows it as the default chip.
    assert STAGE2_PLACEMENTS[0]["key"] == "auto"
    for p in STAGE2_PLACEMENTS:
        assert p["label"].strip(), p


def test_auto_and_unknown_keys_are_a_strict_no_op():
    # The core safety guarantee: auto / None / "" / garbage never touch the subject.
    assert place_subject(_SUBJECT, "auto") == _SUBJECT
    assert place_subject(_SUBJECT, None) == _SUBJECT
    assert place_subject(_SUBJECT, "") == _SUBJECT
    assert place_subject(_SUBJECT, "nonsense-key") == _SUBJECT


def test_each_cell_appends_an_override_clause():
    for key in _CELLS:
        out = place_subject(_SUBJECT, key)
        assert out != _SUBJECT, key
        assert "position the subject" in out.lower(), key


def test_clause_names_the_chosen_region():
    assert "bottom-right" in place_subject(_SUBJECT, "bottom-right").lower()
    assert "top-left" in place_subject(_SUBJECT, "top-left").lower()
    assert "center" in place_subject(_SUBJECT, "middle-center").lower()


def test_softens_conflicting_built_in_position_phrase():
    # Variant A ends with "She occupies the lower portion of the frame; keep the
    # upper area open." Forcing top-center must drop that conflicting framing...
    out = place_subject(_SUBJECT, "top-center")
    assert "lower portion" not in out.lower()
    # ...while the descriptive (non-position) part of the subject survives.
    assert "virtual assistant" in out.lower()


def test_never_raises_for_any_variant_or_cell():
    for v in STAGE2_VARIANTS:
        for key in _CELLS:
            out = place_subject(v["subject"], key)
            assert out.strip()


# ── pipeline wiring (the "don't break the engine" guarantee) ───────────────────
def test_pipeline_auto_is_a_no_op():
    run = create_run("place-auto")
    base = pipeline.build_prompt(run, 2, "A")["text"]
    run["config"]["element_placement"] = "auto"
    with_auto = pipeline.build_prompt(run, 2, "A")["text"]
    assert with_auto == base  # absent and "auto" produce identical prompts
    assert "position the subject" not in with_auto.lower()
    assert STAGE2_VARIANTS[0]["subject"] in base  # curated subject untouched


def test_pipeline_cell_injects_override_clause():
    run = create_run("place-cell")
    run["config"]["element_placement"] = "bottom-right"
    built = pipeline.build_prompt(run, 2, "A")["text"]
    assert "position the subject in the bottom-right" in built.lower()
    assert "[SUBJECT]" not in built  # token substituted
    assert "background" in built.lower()  # shared blend prompt intact
