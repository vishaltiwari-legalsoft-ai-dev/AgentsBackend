"""The carousel must be produced by the shared 4-step backbone, not a parallel
text-drawing engine. These tests pin that contract:

- ``document_builder.build("carousel", ...)`` establishes a DISTINCT backbone base
  per frame (a real carousel — different image per slide, not one photo reused),
  then applies the per-frame text overlay. Every slide still rides the same spine.
- Reference creatives are passed to the image model as actual image inputs at
  Stage 1/2, not just described in text.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from graphics_designer_agent import pipeline, registry
from graphics_designer_agent.creative import document_builder as db


def _carousel_plan() -> dict:
    return {
        "creative_type": "carousel",
        "rationale": "hiring recruiting team carousel",
        "frames": [
            {"index": 1, "role": "hook", "headline": "We're hiring", "body": "Swipe"},
            {"index": 2, "role": "body", "headline": "Remote roles", "body": "Work anywhere"},
            {"index": 3, "role": "cta", "headline": "Apply now"},
        ],
    }


def test_carousel_generates_distinct_base_per_frame(monkeypatch):
    # Slides render concurrently, so collect call data thread-safely and assert on
    # sets/counts rather than call order.
    import threading
    lock = threading.Lock()
    base_calls: list[str | None] = []
    frame_calls: list[int] = []
    real_base, real_frame = pipeline.establish_base, pipeline.render_frame_on_base

    def spy_base(*a, **k):
        with lock:
            base_calls.append(k.get("subject"))
        return real_base(*a, **k)

    def spy_frame(*a, **k):
        with lock:
            frame_calls.append(1)
        return real_frame(*a, **k)

    monkeypatch.setattr(pipeline, "establish_base", spy_base)
    monkeypatch.setattr(pipeline, "render_frame_on_base", spy_frame)

    plan = _carousel_plan()
    # Give each frame a distinct subject so the per-slide images actually differ.
    plan["frames"][0]["subject"] = "a recruiter at a desk"
    plan["frames"][1]["subject"] = "a remote team on a video call"
    out = db.build("carousel", plan, registry.get_pack("legalsoft"))

    # A DISTINCT base per frame (distinct image per slide), one overlay per frame.
    assert len(base_calls) == 3
    assert len(frame_calls) == 3
    # The per-frame subjects are threaded into base generation (order-independent).
    assert "a recruiter at a desk" in base_calls
    assert "a remote team on a video call" in base_calls
    assert len(out) == 3
    # Output is ordered by frame index regardless of which thread finished first.
    assert [name for name, _d, _m in out] == ["frame-01.png", "frame-02.png", "frame-03.png"]
    # Frames are real square PNGs at the carousel's dimensions.
    for name, data, mime in out:
        assert mime == "image/png"
        img = Image.open(BytesIO(data))
        assert img.width == img.height  # 1:1


def test_reference_images_reach_stage1_and_stage2():
    """Stage 1 sees the reference image; Stage 2 sees upstream base + reference."""
    seen: list[int] = []

    class RecordingProvider:
        name = "rec"
        supports_negative = False

        def generate(self, prompt, *, reference_images=None, width=1080, height=1080, **_kw):
            seen.append(len(reference_images or []))
            buf = BytesIO()
            Image.new("RGB", (width, height), (20, 60, 160)).save(buf, "PNG")
            return buf.getvalue(), "image/png"

    ref_buf = BytesIO()
    Image.new("RGB", (256, 256), (200, 40, 40)).save(ref_buf, "PNG")
    references = [(ref_buf.getvalue(), "image/png")]

    pipeline.establish_base(
        "legalsoft", "1:1", reference_images=references, provider=RecordingProvider()
    )

    # Stage 1: no upstream, but the reference image is present (>=1).
    assert seen[0] >= 1
    # Stage 2: the chained Stage-1 base PLUS the reference (>=2).
    assert seen[1] >= 2
