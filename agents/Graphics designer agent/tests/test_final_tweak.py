"""Step-5 final tweak — guardrailed retouch with honest rejection."""

from graphics_designer_agent import final_tweak
from graphics_designer_agent.stage3_text import qa_brain


class _FakeProvider:
    name = "fake"
    supports_negative = False

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def generate(self, prompt, *, reference_images=None, width=1080, height=1350,
                 negative_prompt=None, label="", aspect_ratio=None, image_size=None):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("model down")
        return b"TWEAKED-" + str(len(self.calls)).encode(), "image/png"


def test_prompt_carries_guardrails_and_instruction():
    p = final_tweak.build_tweak_prompt("soften the shadow under the mug")
    assert final_tweak.TWEAK_GUARDRAILS in p
    assert "soften the shadow under the mug" in p
    assert "LOGO" in final_tweak.TWEAK_GUARDRAILS
    assert "gradient" in final_tweak.TWEAK_GUARDRAILS.lower()
    assert "font" in final_tweak.TWEAK_GUARDRAILS.lower()


def test_apply_tweak_pass(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak",
                        lambda *a, **k: {"passed": True, "violations": []})
    out = final_tweak.apply_tweak(final_png=b"FINAL", instruction="warmer light",
                                  provider=_FakeProvider(), width=480, height=600)
    assert out["ok"] and out["qa"] == "passed" and out["png"].startswith(b"TWEAKED")


def test_apply_tweak_retry_then_reject(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak",
                        lambda *a, **k: {"passed": False, "violations": ["logo moved"]})
    prov = _FakeProvider()
    out = final_tweak.apply_tweak(final_png=b"FINAL", instruction="x",
                                  provider=prov, width=480, height=600)
    assert len(prov.calls) == 2                      # first try + one retry
    assert "logo moved" in prov.calls[1]             # violations fed back
    assert out["ok"] is False and out["qa"] == "failed"
    assert out["violations"] == ["logo moved"] and out["png"] is None


def test_apply_tweak_qa_skipped_ships_honestly(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak", lambda *a, **k: None)
    out = final_tweak.apply_tweak(final_png=b"FINAL", instruction="x",
                                  provider=_FakeProvider(), width=480, height=600)
    assert out["ok"] and out["qa"] == "skipped"


def test_apply_tweak_provider_error(monkeypatch):
    monkeypatch.setattr(qa_brain, "check_tweak", lambda *a, **k: None)
    out = final_tweak.apply_tweak(final_png=b"FINAL", instruction="x",
                                  provider=_FakeProvider(fail=True), width=480, height=600)
    assert out["ok"] is False and out["qa"] == "not_run"
    assert out["violations"] == ["image model call failed"]
