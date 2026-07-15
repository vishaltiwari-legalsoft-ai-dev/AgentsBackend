"""Step-5 pipeline integration — DONE-gated, honest offline error, tweak attempts."""

import pytest

from graphics_designer_agent import pipeline
from graphics_designer_agent.runs import create_run
from graphics_designer_agent.stage3_text import qa_brain


class _FakeProvider:
    name = "fake"
    supports_negative = False

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350,
                 negative_prompt=None, label="", aspect_ratio=None, image_size=None):
        return reference_images[0][0], "image/png"


def _seed_to_done(run):
    from io import BytesIO

    from PIL import Image

    for stage in (1, 2):
        pipeline.generate(run, stage, variant="A")
        pipeline.approve(run, stage)
    pipeline.generate(run, 3)
    pipeline.approve(run, 3)
    buf = BytesIO()
    Image.new("RGBA", (40, 20), (10, 10, 10, 255)).save(buf, format="PNG")
    pipeline.generate_stage4(run, buf.getvalue(), use_ai=False)
    pipeline.approve(run, 4)
    assert run["state"] == "DONE"


def test_tweak_requires_done_run():
    run = create_run("u-tw-gate")
    with pytest.raises(pipeline.PipelineError):
        pipeline.generate_tweak(run, "warmer light", provider=_FakeProvider())


def test_tweak_rejects_mock_provider_honestly():
    run = create_run("u-tw-mock")
    _seed_to_done(run)
    with pytest.raises(pipeline.PipelineError, match="offline"):
        pipeline.generate_tweak(run, "warmer light")  # conftest forces mock


def test_tweak_appends_attempt_and_keeps_done(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak",
                        lambda *a, **k: {"passed": True, "violations": []})
    run = create_run("u-tw-ok")
    _seed_to_done(run)
    before = len(run["stages"]["4"]["attempts"])
    approved_before = dict(run["stages"]["4"]["approved"])
    attempt = pipeline.generate_tweak(run, "slightly warmer light", provider=_FakeProvider())
    assert attempt["variant"] == "tweak" and attempt["qa"] == "passed"
    assert attempt["tweak_instruction"] == "slightly warmer light"
    assert len(run["stages"]["4"]["attempts"]) == before + 1
    assert run["state"] == "DONE"                             # done screen never flips
    assert run["stages"]["4"]["approved"] == approved_before  # final untouched until Keep


def test_rejected_tweak_stores_nothing(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak",
                        lambda *a, **k: {"passed": False, "violations": ["logo moved"]})
    run = create_run("u-tw-rej")
    _seed_to_done(run)
    before = len(run["stages"]["4"]["attempts"])
    with pytest.raises(pipeline.PipelineError, match="logo moved"):
        pipeline.generate_tweak(run, "x", provider=_FakeProvider())
    assert len(run["stages"]["4"]["attempts"]) == before
