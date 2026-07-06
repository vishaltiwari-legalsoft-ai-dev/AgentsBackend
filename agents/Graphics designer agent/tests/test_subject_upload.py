"""Stage-2 upload-as-subject: the deterministic composite path (variant UPLOAD).

Also pins the byte-identical law: the composite branch is ONLY reachable via
variant "UPLOAD" — every other variant keeps the AI-generation path, and the
mere presence of ``subject_asset_ref`` changes nothing until asked for.
"""

from io import BytesIO

import pytest
from PIL import Image

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run, read_artifact, save_artifact
from graphics_designer_agent.stage2_element.composite import paste_subject


def _png(w: int, h: int, rgba: tuple[int, int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (w, h), rgba).save(buf, format="PNG")
    return buf.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGBA")


def test_paste_subject_bottom_center_default():
    base = _png(400, 400, (10, 20, 60, 255))
    subj = _png(100, 50, (250, 10, 10, 255))
    out = _open(paste_subject(base, subj, None))
    # 55% contain-fit of a 100x50 subject on 400px canvas -> 220x110, pasted
    # bottom-center with a 4% (16px) margin: x 90..310, y 274..384.
    assert out.getpixel((200, 329)) == (250, 10, 10, 255)
    assert out.getpixel((5, 5)) == (10, 20, 60, 255)
    assert out.size == (400, 400)


def test_paste_subject_honors_placement_cell():
    base = _png(300, 300, (10, 20, 60, 255))
    subj = _png(80, 80, (10, 250, 10, 255))
    out = _open(paste_subject(base, subj, "top-left"))
    assert out.getpixel((20, 20)) == (10, 250, 10, 255)
    assert out.getpixel((295, 295)) == (10, 20, 60, 255)


def test_paste_subject_unknown_placement_falls_back():
    base = _png(200, 200, (10, 20, 60, 255))
    subj = _png(50, 50, (250, 10, 10, 255))
    # Unknown key must not raise — falls back to bottom-center.
    out = _open(paste_subject(base, subj, "??nonsense??"))
    assert out.size == (200, 200)


def test_generate_stage2_upload_is_deterministic_composite():
    run = create_run("upload-user")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    ref = save_artifact(run["id"], 2, "subject", "cafe1234", _png(60, 60, (255, 0, 0, 255)))
    run["config"]["subject_asset_ref"] = ref

    attempt = pipeline.generate(run, 2, "UPLOAD")

    assert attempt["variant"] == "UPLOAD"
    assert attempt["provider"] == "upload-composite"
    assert attempt["method"] == "deterministic"
    assert run["state"] == "STAGE2_REVIEW"
    # The artifact exists, is a readable PNG, and keeps the Stage-1 canvas size.
    base = _open(read_artifact(run["id"], run["stages"]["1"]["approved"]["artifact"]))
    out = _open(read_artifact(run["id"], attempt["artifact"]))
    assert out.size == base.size
    # Approving it advances the pipeline exactly like an AI attempt would.
    pipeline.approve(run, 2)
    assert run["state"].startswith("STAGE3")


def test_upload_variant_without_ref_is_a_clear_error():
    run = create_run("upload-user-2")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    with pytest.raises(pipeline.PipelineError):
        pipeline.generate(run, 2, "UPLOAD")


def test_ref_presence_alone_does_not_hijack_ai_variants():
    run = create_run("upload-user-3")
    pipeline.generate(run, 1, "A")
    pipeline.approve(run, 1)
    ref = save_artifact(run["id"], 2, "subject", "beef5678", _png(40, 40, (255, 0, 0, 255)))
    run["config"]["subject_asset_ref"] = ref
    # A normal variant still goes through the provider path, not the compositor.
    attempt = pipeline.generate(run, 2, "A")
    assert attempt["variant"] == "A"
    assert attempt["provider"] != "upload-composite"
