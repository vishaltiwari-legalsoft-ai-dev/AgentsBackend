"""Stage-3 placement brain — vision judgment parsing, validation and the
never-raise contract. The model call is mocked (`_call_model`); no network."""

import json

from graphics_designer_agent.stage3_text import placement_brain

PNG_STUB = b"\x89PNG\r\n\x1a\nfake"


def _force_available(monkeypatch):
    monkeypatch.setattr(placement_brain, "_vision_available", lambda: True)


def test_decide_returns_validated_judgment(monkeypatch):
    _force_available(monkeypatch)
    monkeypatch.setattr(placement_brain, "_call_model", lambda p, i: json.dumps({
        "zone": "right", "secondary_zone": "bottom", "text_color": "white",
        "density": "busy", "reason": "clean sky on the right",
    }))
    out = placement_brain.decide(PNG_STUB, headline="Hi", cta="Go")
    assert out == {"zone": "right", "text_color": "white", "density": "busy",
                   "reason": "clean sky on the right"}


def test_decide_none_when_vision_unavailable(monkeypatch):
    # conftest sets GD_IMAGE_PROVIDER=mock, so the real guard also refuses —
    # assert the guard is respected without any model call.
    called = []
    monkeypatch.setattr(placement_brain, "_call_model",
                        lambda p, i: called.append(1) or "{}")
    assert placement_brain.decide(PNG_STUB) is None
    assert not called


def test_decide_none_on_garbage_and_retries_once(monkeypatch):
    _force_available(monkeypatch)
    calls = []
    monkeypatch.setattr(placement_brain, "_call_model",
                        lambda p, i: calls.append(1) or "not json at all")
    assert placement_brain.decide(PNG_STUB) is None
    assert len(calls) == 2  # one retry on malformed output, then give up


def test_decide_none_when_model_raises(monkeypatch):
    _force_available(monkeypatch)

    def boom(p, i):
        raise RuntimeError("timeout")

    monkeypatch.setattr(placement_brain, "_call_model", boom)
    assert placement_brain.decide(PNG_STUB) is None


def test_parse_falls_back_to_secondary_zone():
    out = placement_brain._parse('{"zone":"diagonal","secondary_zone":"left",'
                                 '"text_color":"dark","density":"clean","reason":"r"}')
    assert out is not None and out["zone"] == "left"


def test_parse_rejects_invalid_zones():
    assert placement_brain._parse('{"zone":"nowhere","secondary_zone":"also-bad"}') is None


def test_parse_normalises_color_and_density():
    # "light" is a common synonym; unknown density degrades to "moderate".
    out = placement_brain._parse('{"zone":"top","text_color":"light","density":"chaotic"}')
    assert out == {"zone": "top", "text_color": "white", "density": "moderate", "reason": ""}


def test_parse_extracts_json_from_prose():
    raw = 'Sure! Here you go:\n{"zone":"bottom","text_color":"dark","density":"clean","reason":"open floor"}'
    out = placement_brain._parse(raw)
    assert out is not None and out["zone"] == "bottom"
