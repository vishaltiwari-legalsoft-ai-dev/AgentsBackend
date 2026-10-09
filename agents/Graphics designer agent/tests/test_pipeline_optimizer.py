"""Stage-3 Text Optimizer pipeline integration — flag, fan-out, staleness guard."""

import pytest

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.stage3_text import qa_brain, text_optimizer


class _FakeProvider:
    name = "fake"
    supports_negative = False

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350,
                 negative_prompt=None, label="", aspect_ratio=None, image_size=None):
        # Return a valid PNG so save/read round-trips: reuse the composite bytes.
        return reference_images[0][0], "image/png"


def _seed(run):
    pipeline.generate(run, 1, variant="A")
    pipeline.approve(run, 1)
    pipeline.generate(run, 2, variant="A")
    pipeline.approve(run, 2)


def test_mock_provider_keeps_todays_single_deterministic_attempt(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    run = create_run("u-opt-mock")
    _seed(run)
    attempt = pipeline.generate(run, 3)  # conftest forces GD_IMAGE_PROVIDER=mock
    assert attempt["provider"] == "deterministic"
    assert "style" not in attempt
    assert len(run["stages"]["3"]["attempts"]) == 1


def test_flag_off_is_deterministic_even_with_real_provider(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "0")
    run = create_run("u-opt-off")
    _seed(run)
    attempt = pipeline._generate_stage3(run, provider=_FakeProvider())
    assert attempt["provider"] == "deterministic" and "style" not in attempt


def test_optimizer_stores_three_styled_attempts(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    run = create_run("u-opt-3")
    _seed(run)
    attempt = pipeline._generate_stage3(run, provider=_FakeProvider())
    attempts = run["stages"]["3"]["attempts"]
    assert len(attempts) == 3
    assert [a["style"] for a in attempts] == ["brand_strict", "highlighted", "sharp_minimal"]
    assert attempt["style"] == "brand_strict"  # returned attempt = auto-pilot's pick
    assert len({a["set_id"] for a in attempts}) == 1
    assert all(a["ai"] and a["qa"] == "passed" and a["provider"] == "fake" for a in attempts)
    assert all(a["config_hash"] == pipeline.stage3_config_hash(run) for a in attempts)


def test_fallback_attempt_is_badged_honestly(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check",
                        lambda *a, **k: {"passed": False, "violations": ["gradient shifted"]})
    run = create_run("u-opt-fb")
    _seed(run)
    pipeline._generate_stage3(run, provider=_FakeProvider())
    a = run["stages"]["3"]["attempts"][0]
    assert a["ai"] is False and a["provider"] == "deterministic"
    assert "gradient shifted" in a["fallback_reason"] and a["qa"] == "failed"


def test_approve_rejects_stale_styled_attempt(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    run = create_run("u-opt-stale")
    _seed(run)
    pipeline._generate_stage3(run, provider=_FakeProvider())
    run["config"]["tokens"]["headline"] = "Edited afterwards"
    with pytest.raises(pipeline.PipelineError):
        pipeline.approve(run, 3)
    # a fresh generate re-hashes and approve succeeds
    pipeline._generate_stage3(run, provider=_FakeProvider())
    pipeline.approve(run, 3)
    assert run["stages"]["3"]["approved"] is not None


def test_auto_fonts_are_resolved_and_recorded(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: None)
    run = create_run("u-opt-fonts")
    _seed(run)
    run["config"]["element_styles"]["headline"]["font"] = text_optimizer.AUTO_FONT
    attempt = pipeline._generate_stage3(run, provider=_FakeProvider())
    assert attempt["fonts"]["headline"] == "Causten ExtraBold"
    # stored config still carries the sentinel — resolution never mutates it
    assert run["config"]["element_styles"]["headline"]["font"] == text_optimizer.AUTO_FONT


# ── dark-base legibility + QA-aware auto pick ────────────────────────────────
def _dark_png(size=(220, 275)):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, (14, 42, 94)).save(buf, format="PNG")
    return buf.getvalue()


def test_dark_base_records_contrast_guard_and_flips_ink(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    run = create_run("u-opt-dark")
    _seed(run)
    from graphics_designer_agent.runs import save_artifact

    # A dark user-photo background end to end (Stage 1 and the Stage-2 base),
    # so the subject guard sees an unchanged background and leaves layout alone.
    save_artifact(run["id"], 1, "A", 1, _dark_png())
    save_artifact(run["id"], 2, "A", 1, _dark_png())  # overwrite approved base: dark field
    attempt = pipeline._generate_stage3(run, provider=_FakeProvider())
    guard = attempt.get("contrast_guard") or []
    assert any(r["from"] == "dark" and r["to"] == "white" for r in guard)


def test_returned_attempt_prefers_qa_passed_style(monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    fake = [
        {"style": "brand_strict", "label": "Brand strict", "png": _dark_png(), "ai": True,
         "fallback_reason": None, "qa": "skipped", "prompt": "p1"},
        {"style": "highlighted", "label": "Highlighted", "png": _dark_png(), "ai": True,
         "fallback_reason": None, "qa": "passed", "prompt": "p2"},
        {"style": "sharp_minimal", "label": "Sharp minimal", "png": _dark_png(), "ai": True,
         "fallback_reason": None, "qa": "failed", "prompt": "p3"},
    ]
    monkeypatch.setattr(text_optimizer, "optimize", lambda **kw: fake)
    run = create_run("u-opt-pick")
    _seed(run)
    attempt = pipeline._generate_stage3(run, provider=_FakeProvider())
    assert attempt["style"] == "highlighted" and attempt["qa"] == "passed"


# ── Text geometry gate (2026-10-09) ──────────────────────────────────────────
# The polish model re-renders the text and sometimes indents a line (1:1 final,
# 2026-10-08: "Build your team…" started right of the headline's edge). Vision
# QA reads words, not pixels, so a pixel check against the deterministic line
# geometry now gates every polish before the vision call.

from io import BytesIO  # noqa: E402

from PIL import Image  # noqa: E402

from graphics_designer_agent import registry  # noqa: E402
from graphics_designer_agent.stage3_text import layout as gd_layout  # noqa: E402
from graphics_designer_agent.stage3_text import text_geometry  # noqa: E402

_ARS = ["1:1", "4:5", "9:16", "16:9"]


def _pinned_run(ar: str, uid: str) -> dict:
    run = create_run(uid)
    run["config"]["aspect_ratio"] = ar
    _seed(run)
    # A left-aligned stack the way the subject guard sets one (shared "ml" edge).
    run["config"]["layout"] = {
        "headline": {"x": 0.06, "y": 0.22, "w": 0.5, "anchor": "ml"},
        "subheading-0": {"x": 0.06, "y": 0.40, "w": 0.5, "anchor": "ml"},
        "subheading-1": {"x": 0.06, "y": 0.50, "w": 0.5, "anchor": "ml"},
        "cta": {"x": 0.06, "y": 0.64, "w": 0.5, "anchor": "ml"},
    }
    return run


class _IndentingProvider:
    """Returns the composite with one line of sub-heading 0 pushed right — the
    live failure. ``fix_after`` faithful answers come after that many drifts."""

    name = "indent"
    supports_negative = False

    def __init__(self, run: dict, shift: float = 0.025, drifts: int = 99):
        self.run, self.shift, self.drifts, self.prompts = run, shift, drifts, []

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350, **_kw):
        self.prompts.append(prompt)
        composite = reference_images[0][0]
        if len(self.prompts) > self.drifts:
            return composite, "image/png"
        pack = registry.get_pack(self.run.get("brand_id"))
        lines = text_geometry.spec_lines(gd_layout.resolve_layers(self.run), width, height,
                                         pack=pack, px_scale=width / 1080)
        x0, y0, x1, y1 = next(ln["box"] for ln in lines if ln["layer"] == "subheading-0")
        img = Image.open(BytesIO(composite)).convert("RGB")
        base = Image.open(BytesIO(pipeline._approved_png(self.run, 2))).convert("RGB").resize(img.size)
        dx = round(self.shift * width)
        strip = img.crop((x0, y0, x1, y1))
        img.paste(base.crop((x0, y0, x1, y1)), (x0, y0))
        img.paste(strip, (x0 + dx, y0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"


@pytest.mark.parametrize("ar", _ARS)
def test_a_polish_that_indents_a_line_ships_the_deterministic_render(ar, monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    qa_calls = []
    monkeypatch.setattr(qa_brain, "check",
                        lambda *a, **k: qa_calls.append(1) or {"passed": True, "violations": []})
    run = _pinned_run(ar, f"u-geo-drift-{ar}")
    provider = _IndentingProvider(run)
    pipeline._generate_stage3(run, provider=provider)
    attempts = run["stages"]["3"]["attempts"]
    assert len(attempts) == 3
    for a in attempts:
        assert a["ai"] is False and a["provider"] == "deterministic"
        assert a["fallback_reason"].startswith("polish moved the text off the verified layout")
        assert "subheading-0 line 1 drifted" in a["fallback_reason"]
        assert a["geometry"]["passed"] is False and a["geometry"]["max_align_dev"] > 0.02
    # Retried once per style with the violation fed back; the vision QA was
    # never paid for a drifted image.
    assert len(provider.prompts) == 6
    assert sum("the text layout moved" in p for p in provider.prompts) == 3
    assert not qa_calls


@pytest.mark.parametrize("ar", _ARS)
def test_a_corrected_retry_ships_as_ai_with_its_geometry(ar, monkeypatch):
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    run = _pinned_run(ar, f"u-geo-fix-{ar}")
    pipeline._generate_stage3(run, provider=_IndentingProvider(run, drifts=3))
    for a in run["stages"]["3"]["attempts"]:
        assert a["ai"] is True and a["qa"] == "passed"
        assert a["geometry"]["passed"] is True and a["geometry"]["max_align_dev"] <= 0.002


def test_sub_tolerance_drift_is_not_a_violation(monkeypatch):
    """Re-rendered glyphs land a pixel or two off; that is not drift."""
    monkeypatch.setenv("GD_TEXT_OPTIMIZER", "1")
    monkeypatch.setattr(qa_brain, "check", lambda *a, **k: {"passed": True, "violations": []})
    run = _pinned_run("1:1", "u-geo-tol")
    pipeline._generate_stage3(run, provider=_IndentingProvider(run, shift=0.002))
    assert all(a["ai"] and a["geometry"]["passed"] for a in run["stages"]["3"]["attempts"])
