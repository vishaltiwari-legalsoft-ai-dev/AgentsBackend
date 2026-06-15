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
