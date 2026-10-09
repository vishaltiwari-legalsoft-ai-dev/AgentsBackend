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


# ── Subject guard on carousel slides (2026-10-09) ─────────────────────────────
# Slides render through ``render_frame_on_base`` and never passed the studio's
# subject guard, but a campaign ships carousels too. Every slide now gets the
# same guarantee: copy off the subject and off the logo, logo off both.

import pytest  # noqa: E402
from PIL import ImageDraw  # noqa: E402

from graphics_designer_agent.runs import read_artifact, save_artifact  # noqa: E402
from graphics_designer_agent.stage3_text import render as gd_render  # noqa: E402
from graphics_designer_agent.stage3_text import subject_guard  # noqa: E402


def _person_on(stage1_png: bytes, cx: float, scale: float = 1.0) -> bytes:
    img = Image.open(BytesIO(stage1_png)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    r = 0.11 * min(w, h) * scale
    hx, hy = cx * w, 0.45 * h
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(60, 40, 30))
    d.rectangle([hx - 2.4 * r, hy + 1.1 * r, hx + 2.4 * r, h], fill=(20, 28, 60))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _logo() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (300, 300), (3, 4, 94, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _put_person(run: dict, cx: float, scale: float = 1.0) -> None:
    """Overwrite the approved Stage-2 base with Stage 1 + a person at ``cx``."""
    s1 = read_artifact(run["id"], run["stages"]["1"]["approved"]["artifact"])
    last = run["stages"]["2"]["attempts"][-1]
    save_artifact(run["id"], 2, last["variant"], last["attempt"], _person_on(s1, cx, scale))


def _base_with_person(cx: float, scale: float = 1.0):
    real = pipeline.establish_base

    def base(*a, **k):
        run = real(*a, **k)
        _put_person(run, cx, scale)
        return run
    return base


def test_slide_copy_is_moved_off_the_subject_and_recorded(monkeypatch):
    monkeypatch.setattr(pipeline, "establish_base", _base_with_person(0.30))
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: _logo())
    # The layout brain puts the copy in the left column — right on the subject.
    from graphics_designer_agent.creative import layout_brain
    monkeypatch.setattr(layout_brain, "decide_placement",
                        lambda *a, **k: {"placement": "left", "color": "dark", "source": "test"})
    out = db.build("carousel", _carousel_plan(), registry.get_pack("legalsoft"))
    assert [name for name, _d, _m in out] == ["frame-01.png", "frame-02.png", "frame-03.png"]
    for art in out:
        prov = db.artifact_provenance(art)
        assert prov["placement_guard"]["moved"]           # the move is on the record
        assert "fallback_reason" in prov                   # the honesty pair is kept


@pytest.mark.parametrize("cx", [0.30, 0.70])
def test_render_frame_leaves_zero_copy_on_the_subject(cx, monkeypatch):
    """Direct contract on the frame renderer the carousel uses."""
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: None)
    run = pipeline.establish_base("legalsoft", "1:1")
    _put_person(run, cx)
    captured = {}
    real_render = gd_render.render_layers

    def spy(base, layers, w, h, **kw):
        captured.update(layers=[dict(l) for l in layers], w=w, h=h)
        return real_render(base, layers, w, h, **kw)

    monkeypatch.setattr(pipeline.render, "render_layers", spy)
    report: dict = {}
    pipeline.render_frame_on_base(
        run, headline="We're hiring remote legal staff", highlight="remote",
        subheadings=["Work anywhere, start fast"], logo_png=_logo(),
        layout={"placement": "left" if cx < 0.5 else "right", "color": "dark"}, report=report)
    assert report["placement_guard"]["moved"]
    mask = subject_guard.subject_mask(pipeline._approved_png(run, 1), pipeline._approved_png(run, 2))
    pack = registry.get_pack("legalsoft")
    hits = subject_guard.overlap_report(captured["layers"], mask, captured["w"], captured["h"], pack)
    assert hits and set(hits.values()) == {0}


def test_a_slide_with_no_clean_space_is_an_honest_stand_in(monkeypatch):
    monkeypatch.setattr(pipeline, "establish_base", _base_with_person(0.5, 2.6))
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: None)
    out = db.build("carousel", _carousel_plan(), registry.get_pack("legalsoft"))
    for art in out:
        prov = db.artifact_provenance(art)
        assert prov["ai"] is False
        assert "no clean space" in prov["fallback_reason"]
        assert "not an AI image" in prov["fallback_reason"]


def test_a_slide_shows_only_the_plans_copy_and_legible_ink(monkeypatch):
    """Live 2026-10-09: the CTA slide (no body copy) showed the brand's default
    sub-texts, and white ink chosen for the brain's zone stayed white after the
    guard moved the copy onto a light area."""
    monkeypatch.setattr(pipeline, "establish_base", _base_with_person(0.70))
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: None)
    from graphics_designer_agent.creative import layout_brain
    monkeypatch.setattr(layout_brain, "decide_placement",
                        lambda *a, **k: {"placement": "right", "color": "white", "source": "test"})
    rendered = []
    real_render = gd_render.render_layers

    def spy(base, layers, w, h, **kw):
        rendered.append([dict(l) for l in layers])
        return real_render(base, layers, w, h, **kw)

    monkeypatch.setattr(pipeline.render, "render_layers", spy)
    plan = {"creative_type": "carousel", "rationale": "cta",
            "frames": [{"index": 1, "role": "cta", "headline": "Apply now"}]}
    out = db.build("carousel", plan, registry.get_pack("legalsoft"))
    (layers,) = rendered
    assert not [l for l in layers if str(l.get("id", "")).startswith("subheading-")]
    prov = db.artifact_provenance(out[0])
    head = next(l for l in layers if l["id"] == "headline")
    # The mock base is a light brand gradient: white ink is flipped and recorded.
    assert head["color"] == "dark"
    assert any(r["layer"] == "headline" and r["to"] == "dark" for r in prov["contrast_guard"])
