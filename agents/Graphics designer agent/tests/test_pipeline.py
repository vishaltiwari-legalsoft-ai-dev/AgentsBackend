"""§9.3 — every stage chains the approved upstream image; full run reaches DONE
on the offline mock provider."""

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run


def _logo_png() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (120, 120), (3, 4, 94, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_full_pipeline_reaches_done_with_chaining():
    run = create_run("user-1")
    assert run["state"] == "STAGE1_CONFIG"

    pipeline.generate(run, 1, variant="A")
    assert run["state"] == "STAGE1_REVIEW"
    pipeline.approve(run, 1)
    assert run["state"] == "STAGE2_CONFIG"

    # Stage 2 must have an upstream reference available.
    assert pipeline.reference_for(run, 2) is not None
    pipeline.generate(run, 2, variant="D")
    pipeline.approve(run, 2)

    # Approve all content tokens (router enforces this gate; here we set it).
    for t in run["config"]["tokens_approved"]:
        run["config"]["tokens_approved"][t] = True
    pipeline.generate(run, 3)
    assert run["stages"]["3"]["attempts"][0]["variant"] == "T"
    pipeline.approve(run, 3)

    pipeline.generate_stage4(run, _logo_png(), use_ai=False)
    assert run["stages"]["4"]["attempts"][0]["method"] == "deterministic"
    pipeline.approve(run, 4)
    assert run["state"] == "DONE"


def test_cannot_generate_stage2_without_stage1_approval():
    run = create_run("user-2")
    try:
        pipeline.generate(run, 2, variant="A")
    except pipeline.PipelineError:
        return
    raise AssertionError("Stage 2 generated without an approved Stage 1 image")


# ── Subject guard: copy never ships on the subject (2026-10-09) ────────────────
# The 2026-10-08 E2E put the default left text column across the subject's face
# on 1:1, 4:5 and 9:16. Stage 3 now re-flows colliding copy into clean space
# (and persists the move), Stage 4 keeps the logo off the copy, and when no clean
# space exists the run fails with a reason instead of shipping the overlap.

import pytest  # noqa: E402
from PIL import ImageDraw  # noqa: E402

from graphics_designer_agent import registry  # noqa: E402
from graphics_designer_agent.runs import read_artifact, save_artifact  # noqa: E402
from graphics_designer_agent.stage3_text import layout as gd_layout  # noqa: E402
from graphics_designer_agent.stage3_text import subject_guard  # noqa: E402

_ARS = ["1:1", "4:5", "9:16", "16:9"]


def _wordmark_png() -> bytes:
    """A wide wordmark logo (most brand logos), 3.6:1."""
    buf = BytesIO()
    Image.new("RGBA", (360, 100), (3, 4, 94, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _with_person(stage1_png: bytes, cx: float, scale: float = 1.0) -> bytes:
    """Stage 1 plus a dark 'person' (head + shoulders) centred at ``cx`` — what a
    background-preserving Stage 2 produces."""
    img = Image.open(BytesIO(stage1_png)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    r = 0.11 * min(w, h) * scale
    hx, hy = cx * w, 0.45 * h
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(60, 40, 30))          # head + hair
    d.rectangle([hx - 2.4 * r, hy + 1.1 * r, hx + 2.4 * r, h], fill=(20, 28, 60))  # torso
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run_with_subject(ar: str, cx: float, scale: float = 1.0) -> dict:
    run = create_run(f"guard-{ar}-{cx}")
    run["config"]["aspect_ratio"] = ar
    pipeline.generate(run, 1, variant="A")
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, variant="A")
    pipeline.approve(run, 2)
    s1 = read_artifact(run["id"], run["stages"]["1"]["approved"]["artifact"])
    save_artifact(run["id"], 2, "A", 1, _with_person(s1, cx, scale))
    return run


def _overlap_after(run: dict) -> dict:
    pack = registry.get_pack(run.get("brand_id"))
    w, h = pipeline._stage_dims(run, 3)
    mask = subject_guard.subject_mask(pipeline._approved_png(run, 1), pipeline._approved_png(run, 2))
    return subject_guard.overlap_report(gd_layout.resolve_layers(run), mask, w, h, pack)


@pytest.mark.parametrize("ar", _ARS)
def test_copy_on_the_subject_is_moved_off_it_on_every_ratio(ar, monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _run_with_subject(ar, cx=0.30)  # subject under the default left column
    attempt = pipeline.generate(run, 3)
    guard = attempt["placement_guard"]
    assert guard["overlap_before"] and guard["moved"]
    assert set(_overlap_after(run).values()) == {0}  # text box ∩ subject = 0 px
    # The move is persisted, so the editor shows where the text really is and
    # approve's config hash matches the render.
    assert set(guard["moved"]) <= set(run["config"]["layout"])
    pipeline.approve(run, 3)


@pytest.mark.parametrize("ar", _ARS)
def test_logo_lands_clear_of_the_copy_on_every_ratio(ar, monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _run_with_subject(ar, cx=0.30)
    pipeline.generate(run, 3)
    pipeline.approve(run, 3)
    pipeline.generate_stage4(run, _wordmark_png(), use_ai=False)
    pack = registry.get_pack(run.get("brand_id"))
    w, h = pipeline._stage_dims(run, 3)
    ink = subject_guard.text_ink_mask(gd_layout.resolve_layers(run), w, h, pack)
    lay = run["config"].get("logo_layout") or {}
    box = pipeline.logo_placement(w, h, 360, 100, position=lay.get("position", "top-left"),
                                  size_pct=lay.get("size_pct"), margin_pct=lay.get("margin_pct"))
    assert ink[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]].sum() == 0


def test_clear_copy_is_left_exactly_where_it_was(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _run_with_subject("4:5", cx=0.86, scale=0.45)  # small subject, far right
    # A pinned arrangement already clear of the subject and of the logo's spot
    # is the user's (or the vision arranger's) choice - the guard leaves it.
    pinned = {
        "headline": {"x": 0.3, "y": 0.42, "w": 0.42, "anchor": "mc"},
        "subheading-0": {"x": 0.3, "y": 0.62, "w": 0.42, "anchor": "mc"},
        "subheading-1": {"x": 0.3, "y": 0.70, "w": 0.42, "anchor": "mc"},
        "cta": {"x": 0.3, "y": 0.84, "w": 0.42, "anchor": "mc"},
    }
    run["config"]["layout"] = {k: dict(v) for k, v in pinned.items()}
    attempt = pipeline.generate(run, 3)
    assert "placement_guard" not in attempt
    assert run["config"]["layout"] == pinned


def test_no_clean_space_is_an_honest_failure_not_an_overlap(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _run_with_subject("1:1", cx=0.5, scale=2.6)  # subject fills the frame
    with pytest.raises(pipeline.PipelineError, match="cover the subject|told apart"):
        pipeline.generate(run, 3)
    assert not run["stages"]["3"]["attempts"]


def test_repainted_background_is_refused_with_a_reason(monkeypatch):
    """Stage 2 that did not keep the Stage-1 background: the subject cannot be
    located, so the guard says so rather than guessing."""
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _run_with_subject("4:5", cx=0.30)
    buf = BytesIO()
    Image.new("RGB", (1080, 1350), (200, 60, 40)).save(buf, format="PNG")
    save_artifact(run["id"], 2, "A", 1, buf.getvalue())
    with pytest.raises(pipeline.PipelineError, match="changed most of the background"):
        pipeline.generate(run, 3)
