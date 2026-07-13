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
