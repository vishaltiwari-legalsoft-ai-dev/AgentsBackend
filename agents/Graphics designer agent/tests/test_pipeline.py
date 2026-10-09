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


# ── 2026-10-09 (quality pass 2): aligned stacks, real logo aspect, readable size ──
# The 2026-10-08 finals showed a ragged left edge on 1:1 / 9:16: the guard
# planned the stack at the 1080 preset while Stage 3 renders at the Stage-2
# image's native size, where a sub-heading wraps differently and its "mc"
# anchored box shifts. Copy is now planned at the render size; the logo reserve
# uses the real logo; and the copy keeps >= 80% before anything shrinks.

from graphics_designer_agent.stage3_text import text_geometry  # noqa: E402


def _upscaled(png: bytes, k: float) -> bytes:
    img = Image.open(BytesIO(png)).convert("RGB")
    img = img.resize((round(img.width * k), round(img.height * k)))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _hires_run(ar: str, cx: float, scale: float = 1.0, cy: float = 0.45, k: float = 1.9) -> dict:
    """A run whose Stage-1/2 images are ~2K (what Gemini/Sunburst return), so
    Stage 3 renders well above the 1080 preset."""
    run = _run_with_subject(ar, cx, scale)
    s1 = _upscaled(read_artifact(run["id"], run["stages"]["1"]["approved"]["artifact"]), k)
    save_artifact(run["id"], 1, "A", 1, s1)
    img = Image.open(BytesIO(s1)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    r = 0.11 * min(w, h) * scale
    hx, hy = cx * w, cy * h
    d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(60, 40, 30))
    d.rectangle([hx - 2.4 * r, hy + 1.1 * r, hx + 2.4 * r, h], fill=(20, 28, 60))
    buf = BytesIO()
    img.save(buf, format="PNG")
    save_artifact(run["id"], 2, "A", 1, buf.getvalue())
    return run


def _render_lines(run: dict) -> tuple[list[dict], int]:
    pack = registry.get_pack(run.get("brand_id"))
    base = pipeline._approved_png(run, 2)
    cw, ch = pipeline._stage_dims(run, 3)
    w, h, px = pipeline._hires_canvas(base, cw, ch)
    return text_geometry.spec_lines(gd_layout.resolve_layers(run), w, h, pack=pack,
                                    px_scale=px), w


@pytest.mark.parametrize("ar", _ARS)
def test_reflowed_copy_shares_one_left_edge_at_the_render_size(ar, monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _hires_run(ar, cx=0.30)
    guard = pipeline.generate(run, 3)["placement_guard"]
    assert guard["moved"]
    lines, w = _render_lines(run)
    # One left edge per column: a split arrangement ("top+left"/"top+right")
    # sets the headline across the band and the rest in a column beside.
    split = guard["side"].startswith("top+")
    groups: dict[bool, list[float]] = {}
    for ln in lines:
        if ln["layer"] != "cta":
            groups.setdefault(split and ln["layer"] == "headline", []).append(ln["box"][0])
    assert sum(len(g) for g in groups.values()) >= 3
    for edges in groups.values():
        # Glyph side-bearings differ by a pixel or two; a ragged stack was 20-35 px.
        assert max(edges) - min(edges) <= 0.003 * w


@pytest.mark.parametrize("ar", _ARS)
def test_copy_keeps_a_readable_size_before_anything_shrinks(ar, monkeypatch):
    """A big subject right of centre: the old single-column search shrank the
    copy to 60%. Following the subject's contour (and freeing the logo's corner
    when the logo was never placed by the user) keeps it >= 80%."""
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: None)
    run = _hires_run(ar, cx=0.62, scale=1.3, cy=0.40)
    guard = pipeline.generate(run, 3)["placement_guard"]
    assert min(guard["scale"], guard.get("body_scale", guard["scale"])) >= 0.8
    assert set(_overlap_after(run).values()) == {0}


def test_a_user_placed_logo_is_never_moved_to_make_room(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = _hires_run("1:1", cx=0.62, scale=1.3, cy=0.40)
    run["config"]["logo_layout"] = {**run["config"]["logo_layout"], "position": "bottom-right"}
    guard = pipeline.generate(run, 3).get("placement_guard") or {}
    assert "logo_position" not in guard
    assert run["config"]["logo_layout"]["position"] == "bottom-right"


def _square_logo_png() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (400, 400), (3, 4, 94, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("ar", _ARS)
def test_a_square_logo_finds_a_clear_corner_on_every_ratio(ar, monkeypatch):
    """The reserve used to assume a wide wordmark (h = 0.45 w): a square mark
    then landed on the copy in every corner and Stage 4 failed. The reserve now
    reads the brand's real logo."""
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    logo = _square_logo_png()
    monkeypatch.setattr(pipeline, "brand_logo_png", lambda brand_id: logo)
    run = _hires_run(ar, cx=0.30)
    guard = pipeline.generate(run, 3)["placement_guard"]
    assert guard["logo_reserve"] == {"source": "brand_logo", "aspect": 1.0}
    pipeline.approve(run, 3)
    attempt = pipeline.generate_stage4(run, logo, use_ai=False)  # no PipelineError
    base = pipeline._approved_png(run, 3)
    bw, bh = Image.open(BytesIO(base)).size
    lay = run["config"]["logo_layout"]
    box = pipeline.logo_placement(bw, bh, 400, 400, position=lay["position"],
                                  size_pct=lay.get("size_pct"), margin_pct=lay.get("margin_pct"))
    pack = registry.get_pack(run.get("brand_id"))
    cw, ch = pipeline._stage_dims(run, 3)
    iw, ih, px = pipeline._hires_canvas(pipeline._approved_png(run, 2), cw, ch)
    ink = subject_guard.text_ink_mask(gd_layout.resolve_layers(run), iw, ih, pack, px_scale=px)
    sx, sy = iw / bw, ih / bh
    region = ink[int(box["y"] * sy):int((box["y"] + box["h"]) * sy),
                 int(box["x"] * sx):int((box["x"] + box["w"]) * sx)]
    assert region.sum() == 0
    assert attempt["method"] == "deterministic"
    # Stage 3 kept the REAL logo's corner free, so Stage 4 had nothing to fix.
    assert "logo_guard" not in attempt


def test_logo_padding_is_not_counted_as_logo(monkeypatch):
    """A wordmark on a transparent square canvas (Legal Soft's bundled logo is
    one) reserves only its ink, not the empty padding."""
    buf = BytesIO()
    im = Image.new("RGBA", (2000, 2000), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([50, 550, 1950, 1450], fill=(3, 4, 94, 255))
    im.save(buf, format="PNG")
    w, h, ink = pipeline._logo_geometry(buf.getvalue())
    assert (w, h) == (2000, 2000)
    assert ink[1] == pytest.approx(0.275) and ink[3] == pytest.approx(0.725, abs=1e-3)


@pytest.mark.parametrize("ar", _ARS)
def test_logo_reserve_has_the_real_logos_shape(ar):
    run = create_run(f"reserve-{ar}")
    run["config"]["aspect_ratio"] = ar
    w, h = pipeline._stage_dims(run, 3)

    def px(box):
        return ((box[2] - box[0]) * w, (box[3] - box[1]) * h)

    sq_w, sq_h = px(pipeline._logo_reserve(run, w, h, logo_png=_square_logo_png()))
    assert sq_h == pytest.approx(sq_w, rel=0.02)            # a square mark is as tall as wide
    wm_w, wm_h = px(pipeline._logo_reserve(run, w, h, logo_png=_wordmark_png()))
    assert wm_h == pytest.approx(wm_w * 100 / 360, rel=0.02)  # the wordmark keeps its 3.6:1
    none_w, none_h = px(pipeline._logo_reserve(run, w, h, logo_png=None))
    assert none_h == pytest.approx(none_w * 0.45, rel=0.02)  # unknown → wide-wordmark default
