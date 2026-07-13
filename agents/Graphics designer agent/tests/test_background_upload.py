"""Stage-1 upload-as-background (variant UPLOAD): deterministic cover-fit.

Pins the byte-identical law: the background branch is ONLY reachable via
variant "UPLOAD" — the mere presence of ``background_asset_ref`` changes
nothing until asked for.
"""
from io import BytesIO

import pytest
from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run, read_artifact, save_artifact
from graphics_designer_agent.stage1_gradient.background import cover_fit


def _png(w: int, h: int, rgb=(30, 60, 120)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(png)).size


def test_cover_fit_center_crops_to_canvas_shape():
    # 500x500 source onto a 400x225 canvas: upscaled to source width (scale
    # 1.25 -> 500x281 target), cover-resized, center-cropped.
    out = cover_fit(_png(500, 500), 400, 225)
    assert _size(out) == (500, 281)


def test_cover_fit_never_upscales_small_sources_past_canvas():
    out = cover_fit(_png(100, 100), 400, 225)
    assert _size(out) == (400, 225)


def test_cover_fit_respects_max_width():
    out = cover_fit(_png(9000, 9000), 400, 225, max_width=800)
    assert _size(out) == (800, 450)


def test_generate_stage1_upload_is_deterministic():
    run = create_run("bg-user")
    ref = save_artifact(run["id"], 1, "background", "cafe1234", _png(800, 800))
    run["config"]["background_asset_ref"] = ref

    attempt = pipeline.generate(run, 1, "UPLOAD")

    assert attempt["variant"] == "UPLOAD"
    assert attempt["provider"] == "upload-background"
    assert attempt["method"] == "deterministic"
    assert run["state"] == "STAGE1_REVIEW"
    w, h = _size(read_artifact(run["id"], attempt["artifact"]))
    cw, ch = pipeline._stage_dims(run, 1)
    assert abs(w / h - cw / ch) < 0.01  # canvas shape preserved
    # The rest of the pipeline treats it like any approved Stage-1 image.
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, "A")
    assert run["state"] == "STAGE2_REVIEW"


def test_stage1_upload_without_ref_is_a_clear_error():
    run = create_run("bg-user-2")
    with pytest.raises(pipeline.PipelineError):
        pipeline.generate(run, 1, "UPLOAD")


def test_background_ref_presence_changes_nothing_for_preset_variants():
    run = create_run("bg-user-3")
    run["config"]["background_asset_ref"] = save_artifact(
        run["id"], 1, "background", "beef5678", _png(300, 300))
    attempt = pipeline.generate(run, 1, "A")
    assert attempt["provider"] == "mock"
    assert attempt["prompt"] == pipeline.build_prompt(run, 1, "A")["text"]
