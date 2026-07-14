"""Vision QA gate — verdict parsing + honest None when unavailable."""

from graphics_designer_agent.stage3_text import qa_brain


PNG = b"\x89PNG-fake"


def test_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: False)
    assert qa_brain.check(PNG, PNG, "desc") is None


def test_pass_verdict(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)
    monkeypatch.setattr(
        qa_brain, "_call_model",
        lambda *_: '{"text_ok":true,"elements_ok":true,"gradient_ok":true,'
                   '"photo_ok":true,"placement_ok":true,"violations":[]}')
    assert qa_brain.check(PNG, PNG, "desc") == {"passed": True, "violations": []}


def test_fail_verdict_carries_violations(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)
    monkeypatch.setattr(
        qa_brain, "_call_model",
        lambda *_: '{"text_ok":false,"elements_ok":true,"gradient_ok":true,'
                   '"photo_ok":true,"placement_ok":true,'
                   '"violations":["headline was reworded"]}')
    out = qa_brain.check(PNG, PNG, "desc")
    assert out["passed"] is False and out["violations"] == ["headline was reworded"]


def test_text_overlapping_subject_fails_placement(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)
    monkeypatch.setattr(
        qa_brain, "_call_model",
        lambda *_: '{"text_ok":true,"elements_ok":true,"gradient_ok":true,'
                   '"photo_ok":true,"placement_ok":false,'
                   '"violations":["subheading still covers the laptop screen"]}')
    out = qa_brain.check(PNG, PNG, "desc")
    assert out["passed"] is False
    assert out["violations"] == ["subheading still covers the laptop screen"]


def test_fail_without_reasons_names_the_failed_checks(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)
    monkeypatch.setattr(
        qa_brain, "_call_model",
        lambda *_: '{"text_ok":true,"elements_ok":true,"gradient_ok":false,'
                   '"photo_ok":true,"placement_ok":true,"violations":[]}')
    out = qa_brain.check(PNG, PNG, "desc")
    assert out["passed"] is False and out["violations"] == ["gradient_ok check failed"]


def test_malformed_twice_returns_none(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)
    monkeypatch.setattr(qa_brain, "_call_model", lambda *_: "not json")
    assert qa_brain.check(PNG, PNG, "desc") is None


def test_model_error_returns_none(monkeypatch):
    monkeypatch.setattr(qa_brain, "_vision_available", lambda: True)

    def boom(*_):
        raise RuntimeError("timeout")

    monkeypatch.setattr(qa_brain, "_call_model", boom)
    assert qa_brain.check(PNG, PNG, "desc") is None


def test_qa_prompt_counts_added_text_as_violation():
    prompt = qa_brain._build_prompt("HEADLINE — top left")
    assert "no new text of any kind was added" in prompt
